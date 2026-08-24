"""Team-name canonicalization for sportsbook rows (ARCH §8, PLAN Phase 2).

``normalize_team(sport, raw, book=None) -> team_id | None`` resolves any book
spelling ("Sacramento St", "Miami Florida", "N.Y. Giants", Kalshi "NYG") to the
canonical ``team_id`` used by the schedule (``data/aliases/{nfl,cfb}.json``
keys: nflverse abbr lowercased for NFL, CFBD school slug for CFB).

Resolution order (first hit wins):

1. exact alias lookup on a lowercase-alphanumeric key ('N.Y. Giants' ->
   'nygiants'), also trying the raw string as a team_id / slug;
2. the same lookup on spelling variants: trailing ``St``/``St.`` -> ``State``,
   ``Univ``/``University`` stripped, ``(FL)``-style qualifiers unwrapped,
   ``Intl`` -> ``International``;
3. fuzzy: rapidfuzz ``fuzz.ratio`` on the normalized keys, accepted at
   ``FUZZY_MIN`` (92) and only when the runner-up *team* is at least
   ``FUZZY_MARGIN`` points behind (so 'Miami' never fuzzes to the wrong Miami);
   ``difflib.SequenceMatcher`` is the fallback when rapidfuzz is missing or
   stubbed (tests stub it with MagicMock).

Unresolved strings are logged once per (sport, book, raw) and collected in a
module-level register that the build reads into ``RunMeta.unresolved_names``.
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import re
import threading
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
FUZZY_MIN = 92.0      # rapidfuzz ratio (0-100) required to accept a fuzzy match
FUZZY_MARGIN = 3.0    # best team must beat the runner-up team by this much
_TOP_LEVEL = ("nfl", "fbs")
_LEVEL_RANK = {"nfl": 0, "fbs": 0, "fcs": 1, "ii": 2, "iii": 3}

_WORD_ST = re.compile(r"\bst\.?$", re.I)
_WORD_ST_MID = re.compile(r"\bst\.?\s+(?=(univ|university)\b)", re.I)
_PAREN = re.compile(r"\(([^)]*)\)")
_UNIV = re.compile(r"\b(university|univ\.?|univ)\b", re.I)
_INTL = re.compile(r"\bintl\.?\b", re.I)
_MULTI_WS = re.compile(r"\s+")


def slug(text: str) -> str:
    t = str(text).lower().replace("&", "").replace("'", "").replace("’", "").replace(".", "")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def normalize_alias(text: str) -> str:
    """Lookup key: lowercase alphanumerics only ('N.Y. Giants' -> 'nygiants')."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower().replace("&", "and"))


def variants(raw: str) -> list[str]:
    """Spelling variants of ``raw`` tried (in order) before fuzzy matching."""
    base = _MULTI_WS.sub(" ", str(raw).strip())
    out: list[str] = [base]

    def add(s: str) -> None:
        s = _MULTI_WS.sub(" ", s).strip(" -")
        if s and s not in out:
            out.append(s)

    add(_WORD_ST.sub("State", base))
    add(_WORD_ST_MID.sub("State ", base))
    add(_INTL.sub("International", base))
    add(_UNIV.sub("", base))
    add(_PAREN.sub(lambda m: f" {m.group(1)} ", base))       # 'Miami (FL)' -> 'Miami FL'
    add(_PAREN.sub("", base))                                 # 'Miami (FL)' -> 'Miami'
    if "-" in base and " " not in base:                       # 'sacramento-st' slug form
        add(base.replace("-", " "))
        add(_WORD_ST.sub("State", base.replace("-", " ")))
    # combined: slug + St->State + Intl
    combo = _INTL.sub("International", _WORD_ST.sub("State", base.replace("-", " ")))
    add(combo)
    add(_UNIV.sub("", combo))
    return out


_QUAL_RE = re.compile(
    r"\b(east|west|north|south|eastern|western|northern|southern|central|state|st|tech|am)\b", re.I
)


def qualifiers(text: str) -> frozenset:
    """Direction / type words whose presence must agree for a fuzzy match."""
    t = str(text).lower().replace("a&m", " am ").replace(".", "")
    return frozenset("state" if w == "st" else w for w in _QUAL_RE.findall(t))


def load_aliases(sport: str, data_dir: Path = DATA_DIR) -> dict[str, list[str]]:
    path = Path(data_dir) / "aliases" / f"{sport}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_classification(sport: str, data_dir: Path = DATA_DIR) -> dict[str, str]:
    """``team_id -> classification`` ('nfl' / 'fbs' / 'fcs' / 'ii' / 'iii') from teams.csv."""
    path = Path(data_dir) / "teams.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {
            str(r.get("team_id")): str(r.get("classification") or "").lower()
            for r in csv.DictReader(fh)
            if r.get("team_id") and str(r.get("sport")) == sport
        }


def _ratio(a: str, b: str) -> float:
    """0-100 similarity; rapidfuzz when real, difflib otherwise."""
    try:
        from rapidfuzz import fuzz  # type: ignore

        score = fuzz.ratio(a, b)
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return float(score)
    except Exception:  # noqa: BLE001 - not installed / stubbed
        pass
    return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


class TeamResolver:
    """Alias index for one sport. Build once per run (``get_resolver``).

    Three lookup tiers, tried in order for every spelling variant: explicit
    aliases from the json, generated ``St``/``State`` spellings, and the
    ``team_id`` itself. A key shared by several teams inside a tier is ambiguous
    and only resolves when exactly one candidate is top level (nfl / fbs).
    """

    def __init__(
        self,
        sport: str,
        aliases: dict[str, Iterable[str]],
        classification: dict[str, str] | None = None,
    ):
        self.sport = sport
        self.index: dict[str, str] = {}
        self.gen: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.ambiguous: dict[str, set] = {}
        self.quals: dict[str, frozenset] = {}
        self.team_ids: set = set()
        self.classification: dict[str, str] = dict(classification or {})
        for team_id, names in aliases.items():
            tid = str(team_id)
            self.team_ids.add(tid)
            for name in names:
                key = normalize_alias(name)
                self._add(self.index, key, tid)
                self.quals.setdefault(key, qualifiers(str(name)))
            for name in (tid, tid.replace("-", " ")):
                self._add(self.ids, normalize_alias(name), tid)
        # 'St' spellings of every 'State' alias, and vice versa, are free
        for key, team_id in list(self.index.items()):
            if key.endswith("state"):
                self._add(self.gen, key[: -len("state")] + "st", team_id)
            elif key.endswith("st") and len(key) > 4:
                self._add(self.gen, key[: -len("st")] + "state", team_id)
            else:
                continue
            self.quals.setdefault(list(self.gen)[-1], self.quals.get(key, frozenset()))
        self._tiers = (self.index, self.gen, self.ids)
        self._keys: list[str] = sorted(set(self.index) | set(self.gen))

    def _add(self, tier: dict[str, str], key: str, team_id: str) -> None:
        if not key:
            return
        cur = tier.get(key)
        if cur is None:
            tier[key] = team_id
        elif cur != team_id:
            self.ambiguous.setdefault((id(tier), key), {cur}).add(team_id)

    def _lookup(self, tier: dict[str, str], key: str) -> str | None:
        tid = tier.get(key)
        if tid is None:
            return None
        amb = self.ambiguous.get((id(tier), key))
        return self._disambiguate(amb) if amb else tid

    # ---- lookups -----------------------------------------------------------
    def exact(self, raw: str) -> str | None:
        keys = [normalize_alias(v) for v in variants(raw)]
        for tier in self._tiers:
            for key in keys:
                if key in tier:
                    return self._lookup(tier, key)
        if raw in self.team_ids:
            return raw
        s = slug(raw)
        return s if s in self.team_ids else None

    def _disambiguate(self, candidates: set) -> str | None:
        """An alias shared by several teams ('ARK', 'Los Angeles'): accept it only
        when exactly one candidate plays at the top level (nfl / fbs), or -- when no
        candidate is top level -- exactly one sits at the highest level present
        ('DEL' = Delaware over Delta State)."""
        top = [t for t in candidates if self.classification.get(t) in _TOP_LEVEL]
        if len(top) == 1:
            return top[0]
        if top:
            return None
        # no top-level candidate: accept the single highest-classified one (fcs > ii > iii)
        ranked = sorted(candidates, key=lambda t: _LEVEL_RANK.get(self.classification.get(t, ""), 9))
        best = _LEVEL_RANK.get(self.classification.get(ranked[0], ""), 9)
        tied = [t for t in ranked if _LEVEL_RANK.get(self.classification.get(t, ""), 9) == best]
        return tied[0] if len(tied) == 1 and best < 9 else None

    def _teams_for(self, cand: str) -> tuple:
        for tier in (self.index, self.gen):
            if cand in tier:
                return tuple(self.ambiguous.get((id(tier), cand)) or (tier[cand],))
        return ()

    def fuzzy(self, raw: str) -> tuple[str | None, float]:
        best: dict[str, float] = {}
        raw_q = qualifiers(raw)
        for v in variants(raw):
            key = normalize_alias(v)
            if not key:
                continue
            for cand in self._keys:
                cand_q = self.quals.get(cand, frozenset())
                if raw_q and cand_q and cand_q != raw_q:
                    continue      # 'East Texas A&M' must never fuzz to 'West Texas A&M'
                sc = _ratio(key, cand)
                for tid in self._teams_for(cand):
                    if sc > best.get(tid, 0.0):
                        best[tid] = sc
        if not best:
            return None, 0.0
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if top >= FUZZY_MIN and top - second >= FUZZY_MARGIN:
            return top_id, top
        return None, top

    def resolve(self, raw: str | None, fuzzy: bool = True) -> str | None:
        if raw is None or not str(raw).strip():
            return None
        raw = str(raw).strip()
        hit = self.exact(raw)
        if hit is not None or not fuzzy:
            return hit
        tid, _ = self.fuzzy(raw)
        return tid


# ---- module-level cache + unresolved register ---------------------------------

_LOCK = threading.Lock()
_RESOLVERS: dict[tuple[str, str], TeamResolver] = {}
_UNRESOLVED: dict[tuple[str, str, str], int] = {}


def get_resolver(sport: str, data_dir: Path = DATA_DIR) -> TeamResolver:
    key = (sport, str(data_dir))
    with _LOCK:
        res = _RESOLVERS.get(key)
        if res is None:
            res = TeamResolver(sport, load_aliases(sport, data_dir), load_classification(sport, data_dir))
            _RESOLVERS[key] = res
        return res


def clear_cache() -> None:
    with _LOCK:
        _RESOLVERS.clear()


def normalize_team(
    sport: str,
    raw: str | None,
    book: str | None = None,
    *,
    fuzzy: bool = True,
    data_dir: Path = DATA_DIR,
) -> str | None:
    """Canonical ``team_id`` for a book's team string, or None (logged + registered)."""
    res = get_resolver(sport, data_dir)
    tid = res.resolve(raw, fuzzy=fuzzy)
    if tid is None and raw is not None and str(raw).strip():
        _register_unresolved(sport, book or "", str(raw).strip())
    return tid


def _register_unresolved(sport: str, book: str, raw: str) -> None:
    key = (sport, book, raw)
    with _LOCK:
        first = key not in _UNRESOLVED
        _UNRESOLVED[key] = _UNRESOLVED.get(key, 0) + 1
    if first:
        logger.warning(f"[teams] unresolved {sport} team name {raw!r} (book={book or '-'})")


def unresolved_names(sport: str | None = None) -> list[str]:
    """``'sport|book|raw'`` strings seen since the last ``reset_unresolved``."""
    with _LOCK:
        keys = sorted(k for k in _UNRESOLVED if sport is None or k[0] == sport)
    return [f"{s}|{b}|{r}" for s, b, r in keys]


def reset_unresolved(sport: str | None = None) -> None:
    with _LOCK:
        if sport is None:
            _UNRESOLVED.clear()
        else:
            for k in [k for k in _UNRESOLVED if k[0] == sport]:
                del _UNRESOLVED[k]


__all__ = [
    "DATA_DIR",
    "FUZZY_MIN",
    "FUZZY_MARGIN",
    "TeamResolver",
    "clear_cache",
    "get_resolver",
    "load_aliases",
    "load_classification",
    "normalize_alias",
    "normalize_team",
    "reset_unresolved",
    "slug",
    "unresolved_names",
    "variants",
]
