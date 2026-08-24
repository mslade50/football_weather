"""Load data/stadiums.csv + data/stadiums_overrides.csv + data/teams.csv + data/aliases/*.json.

`load_stadium_book()` returns a StadiumBook with Stadium/Team maps and
`resolve(game)` which maps a schedule Game onto a stadium plus the legacy
per-game inputs (travel_alt, home_temp, away_temp, wind_avg, roof_state).

Neutral sites (ARCH §7 judge note): both teams travel, so travel_alt / away_temp
are evaluated for each side against the venue and the side with the larger
altitude penalty becomes the "away" side for the impact model. Legacy output
columns keep the schedule's home/away.
"""

from __future__ import annotations

import csv
import difflib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from pipeline.contracts import Degradation, Game, Stadium, Team

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MONTH_KEYS = {9: "sep", 10: "oct", 11: "nov", 12: "dec", 1: "jan"}
FUZZY_MIN = 0.92
_FLOAT_FIELDS = {"lat", "lon", "elevation_m", "orientation_deg", "avg_wind_static", "avg_temp_f"}
_INT_FIELDS = {"capacity", "year_built"}
_BOOL_FIELDS = {"needs_review"}
_STADIUM_FIELDS = {f.name for f in fields(Stadium)}


def slug(text: str) -> str:
    t = str(text).lower().replace("&", "").replace("'", "").replace("’", "").replace(".", "")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def normalize_alias(text: str) -> str:
    """Lookup key: lowercase alphanumerics only ('N.Y. Giants' -> 'nygiants')."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower().replace("&", "and"))


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _as_float(v: Any) -> float | None:
    if _blank(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    f = _as_float(v)
    return None if f is None else int(f)


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _coerce(name: str, v: Any) -> Any:
    if name in _FLOAT_FIELDS:
        return _as_float(v)
    if name in _INT_FIELDS:
        return _as_int(v)
    if name in _BOOL_FIELDS:
        return _as_bool(v)
    if name == "aliases":
        return [a for a in str(v or "").split("|") if a]
    return None if _blank(v) else str(v)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _fuzzy_score(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz  # type: ignore

        score = fuzz.ratio(a, b)
        if isinstance(score, (int, float)):
            return float(score) / 100.0
    except Exception:  # noqa: BLE001 - not installed or stubbed
        pass
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_aliases(sport: str, data_dir: Path = DATA_DIR) -> dict[str, list[str]]:
    path = data_dir / "aliases" / f"{sport}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class ResolvedGame:
    game: Game
    stadium: Stadium | None
    home_team: Team | None
    away_team: Team | None
    stadium_source: str
    roof_state: str | None = None
    home_temp: float | None = None
    away_temp: float | None = None
    travel_alt: float | None = None
    travel_alt_home: float | None = None
    travel_alt_away: float | None = None
    penalized_side: str = "away"
    wind_avg: float | None = None

    @property
    def game_loc(self) -> str | None:
        if self.stadium is None:
            return None
        return f"{self.stadium.lat}, {self.stadium.lon}"


@dataclass
class StadiumBook:
    stadiums: dict[str, Stadium] = field(default_factory=dict)
    teams: dict[tuple[str, str], Team] = field(default_factory=dict)
    classification: dict[tuple[str, str], str | None] = field(default_factory=dict)
    alias_index: dict[str, dict[str, str]] = field(default_factory=dict)
    nflverse_index: dict[str, str] = field(default_factory=dict)
    cfbd_venue_index: dict[str, str] = field(default_factory=dict)
    espn_venue_index: dict[str, str] = field(default_factory=dict)
    name_index: dict[str, str] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    # ---- lookups -----------------------------------------------------------------
    def team(self, sport: str, team_id: str) -> Team | None:
        return self.teams.get((sport, team_id))

    def teams_for(self, sport: str) -> list[Team]:
        return [t for (s, _), t in self.teams.items() if s == sport]

    def stadium_for_team(self, sport: str, team_id: str) -> Stadium | None:
        t = self.team(sport, team_id)
        if t is None or not t.home_stadium_id:
            return None
        return self.stadiums.get(t.home_stadium_id)

    def find_stadium(self, key: str | None) -> Stadium | None:
        """stadium_id, nflverse id, CFBD/ESPN venue id or a name/alias."""
        if _blank(key):
            return None
        k = str(key).strip()
        for idx in (self.stadiums, ):
            if k in idx:
                return idx[k]
        for idx in (self.nflverse_index, self.cfbd_venue_index, self.espn_venue_index):
            if k in idx:
                return self.stadiums.get(idx[k])
        sid = self.name_index.get(slug(k))
        return self.stadiums.get(sid) if sid else None

    def resolve_team(self, sport: str, raw: str, fuzzy: bool = True) -> str | None:
        if _blank(raw):
            return None
        idx = self.alias_index.get(sport, {})
        key = normalize_alias(raw)
        if key in idx:
            return idx[key]
        if (sport, str(raw)) in self.teams:
            return str(raw)
        s = slug(raw)
        if (sport, s) in self.teams:
            return s
        if not fuzzy or not idx:
            return None
        best_id, best = None, 0.0
        for alias_key, team_id in idx.items():
            sc = _fuzzy_score(key, alias_key)
            if sc > best:
                best_id, best = team_id, sc
        return best_id if best >= FUZZY_MIN else None

    # ---- per-game resolution --------------------------------------------------
    def resolve(self, game: Game, ctx: Any = None) -> ResolvedGame:
        home = self.team(game.sport, game.home_id)
        away = self.team(game.sport, game.away_id)
        stadium = self.find_stadium(game.stadium_id)
        source = "game.stadium_id" if stadium is not None else "none"
        if stadium is None and home is not None and not game.neutral:
            stadium = self.stadium_for_team(game.sport, game.home_id)
            source = "home_team" if stadium is not None else "none"
        if stadium is None and home is not None and game.neutral:
            stadium = self.stadium_for_team(game.sport, game.home_id)
            source = "home_team_neutral_fallback" if stadium is not None else "none"
            if stadium is not None:
                _degrade(ctx, "stadiums", f"{game.game_id}: neutral site {game.stadium_id!r} unknown; using home stadium", "warn")
        if stadium is None:
            # warn (not error): the row is still written with NaN static columns and
            # one unmapped venue must not fail the whole run / page Telegram.
            _degrade(ctx, "stadiums", f"{game.game_id}: no stadium for {game.stadium_id!r} / home {game.home_id!r}", "warn")
            self.unresolved.append(game.game_id)
        for side, t, tid in (("home", home, game.home_id), ("away", away, game.away_id)):
            if t is None:
                _degrade(ctx, "teams", f"{game.game_id}: unknown {side} team {tid!r}", "warn")
                self.unresolved.append(f"{game.sport}:{tid}")

        rg = ResolvedGame(game=game, stadium=stadium, home_team=home, away_team=away, stadium_source=source)
        rg.roof_state = game.roof_state or _roof_state(stadium)
        if stadium is not None and game.kickoff_local is not None:
            mk = MONTH_KEYS.get(game.kickoff_local.month)
            rg.wind_avg = stadium.avg_wind_by_month.get(mk) if mk else None
            if rg.wind_avg is None:
                rg.wind_avg = stadium.avg_wind_static
        if stadium is not None and stadium.roof_type == "dome":
            rg.wind_avg = 0.0 if rg.wind_avg is None else rg.wind_avg

        venue_elev = stadium.elevation_m if stadium is not None else None
        home_elev = _home_elev(self, game.sport, home)
        away_elev = _home_elev(self, game.sport, away)
        rg.travel_alt_home = _diff(venue_elev, home_elev)
        rg.travel_alt_away = _diff(venue_elev, away_elev)
        home_t = home.avg_temp_f if home is not None else None
        away_t = away.avg_temp_f if away is not None else None

        if game.neutral:
            # larger altitude penalty picks the side treated as "away" by the model; ties -> schedule away
            if (rg.travel_alt_home or 0.0) > (rg.travel_alt_away or 0.0):
                rg.penalized_side = "home"
                rg.travel_alt, rg.away_temp, rg.home_temp = rg.travel_alt_home, home_t, away_t
            else:
                rg.penalized_side = "away"
                rg.travel_alt, rg.away_temp, rg.home_temp = rg.travel_alt_away, away_t, home_t
        else:
            rg.penalized_side = "away"
            rg.travel_alt = rg.travel_alt_away
            rg.home_temp, rg.away_temp = home_t, away_t
        return rg


def _degrade(ctx: Any, component: str, reason: str, severity: str) -> Degradation | None:
    if ctx is None:
        return None
    if hasattr(ctx, "degrade"):
        return ctx.degrade(component, reason, severity)
    if isinstance(ctx, list):
        d = Degradation(component=component, reason=reason, severity=severity)
        ctx.append(d)
        return d
    return None


def _roof_state(stadium: Stadium | None) -> str | None:
    if stadium is None or stadium.roof_type is None:
        return None
    return {"dome": "dome", "open": "outdoors"}.get(stadium.roof_type)  # retractable -> None (weather heuristic)


def _home_elev(book: StadiumBook, sport: str, team: Team | None) -> float | None:
    if team is None:
        return None
    st = book.stadium_for_team(sport, team.team_id)
    return st.elevation_m if st is not None else None


def _diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return float(a) - float(b)


# ---- loading ---------------------------------------------------------------------

def _stadium_from_row(row: dict[str, str]) -> Stadium | None:
    kwargs: dict[str, Any] = {}
    for name in _STADIUM_FIELDS:
        if name == "avg_wind_by_month":
            continue
        if name in row:
            kwargs[name] = _coerce(name, row[name])
    by_month = {}
    for m in ("sep", "oct", "nov", "dec", "jan"):
        v = _as_float(row.get(f"avg_wind_{m}"))
        if v is not None:
            by_month[m] = v
    kwargs["avg_wind_by_month"] = by_month
    kwargs.setdefault("aliases", [])
    if kwargs.get("lat") is None or kwargs.get("lon") is None:
        return None
    return Stadium(**kwargs)


def apply_overrides(rows: list[dict[str, str]], overrides: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    by_id = {r["stadium_id"]: r for r in rows}
    for o in overrides:
        sid, fld = o.get("stadium_id", "").strip(), o.get("field", "").strip()
        if sid in by_id and fld and (fld in _STADIUM_FIELDS or fld.startswith("avg_wind_")):
            by_id[sid][fld] = o.get("value", "")
    return rows


def load_stadium_book(data_dir: Path = DATA_DIR, sports: Iterable[str] = ("nfl", "cfb")) -> StadiumBook:
    book = StadiumBook()
    rows = apply_overrides(_read_csv(data_dir / "stadiums.csv"), _read_csv(data_dir / "stadiums_overrides.csv"))
    for row in rows:
        st = _stadium_from_row(row)
        if st is None:
            book.skipped.append(row)
            continue
        book.stadiums[st.stadium_id] = st
        if st.nflverse_stadium_id:
            book.nflverse_index[st.nflverse_stadium_id] = st.stadium_id
        if st.cfbd_venue_id:
            book.cfbd_venue_index[st.cfbd_venue_id] = st.stadium_id
        if st.espn_venue_id:
            book.espn_venue_index[st.espn_venue_id] = st.stadium_id
        for nm in [st.name] + list(st.aliases):
            book.name_index.setdefault(slug(nm), st.stadium_id)

    for row in _read_csv(data_dir / "teams.csv"):
        sport = row.get("sport", "")
        if sport not in sports:
            continue
        aliases = [a for a in (row.get("aliases") or "").split("|") if a]
        t = Team(
            team_id=row["team_id"],
            sport=sport,
            name=row.get("name") or row["team_id"],
            short=row.get("short") or None,
            home_stadium_id=row.get("home_stadium_id") or None,
            avg_temp_f=_as_float(row.get("avg_temp_f")),
            conference=row.get("conference") or None,
            aliases=aliases,
        )
        book.teams[(sport, t.team_id)] = t
        book.classification[(sport, t.team_id)] = row.get("classification") or None

    for sport in sports:
        idx: dict[str, str] = {}
        for t in book.teams_for(sport):
            for a in [t.team_id, t.name] + list(t.aliases):
                idx.setdefault(normalize_alias(a), t.team_id)
        for team_id, al in load_aliases(sport, data_dir).items():
            for a in al:
                idx.setdefault(normalize_alias(a), team_id)
        book.alias_index[sport] = idx
    return book


__all__ = [
    "DATA_DIR",
    "MONTH_KEYS",
    "ResolvedGame",
    "StadiumBook",
    "apply_overrides",
    "load_aliases",
    "load_stadium_book",
    "normalize_alias",
    "slug",
]
