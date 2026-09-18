"""Shadow-mode identity harness: re-adjudicate two identity decisions with Jev.

Production behaviour is untouched. The harness replays decisions the existing
code already made — which stadium a CFBD venue string names, and which program a
sportsbook's team string names — records Jev's verdict beside the matcher's, and
reports where they disagree.

    python -m tools.identity_shadow harvest venues [--backtest-dir data/backtest/git]
    python -m tools.identity_shadow harvest teams  [--raw-runs data/raw_runs]
    python -m tools.identity_shadow judge --target {venues,teams}
                                          [--limit N] [--dry-run]
                                          [--only-unresolved] [--max-hits N]
    python -m tools.identity_shadow report --target {venues,teams}

Outputs land under output/identity_shadow/ (gitignored). Nothing here writes to
data/, site/web/data/ or any production path.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline.odds import merge as merge_mod
from pipeline.odds import teams as teams_mod
from pipeline.stadiums.loader import StadiumBook, load_stadium_book
from tools.questions import (
    NONE_OF_THESE,
    TEAM_CHOICE_KEY,
    VENUE_CHOICE_KEY,
    candidate_key,
    compat_key,
    team_choice_key,
    team_questions,
    team_state,
    venue_questions,
    venue_state,
)
from tools.shadow_log import (
    append_jsonl,
    read_jsonl,
    shadow_dir,
    stable_key,
    team_scores,
    team_top_candidates,
    venue_scores,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
BACKTEST_DIR = DATA_DIR / "backtest" / "git"
RAW_RUNS_DIR = DATA_DIR / "raw_runs"
LEGACY_DB_DIR = Path("tests") / "fixtures" / "raw" / "legacy_db"

MAX_VENUE_CANDIDATES = 10
MAX_TEAM_CANDIDATES = 10
MAX_ALIASES = 3

# --- acceptance ---------------------------------------------------------------
# A promotion needs BOTH: the Choice put enough mass on the winning candidate,
# and the companion Noul for that candidate agrees the two strings can name one
# thing. The Noul is a code-side veto, not advice: a confident-but-wrong pick is
# exactly what a Choice alone keeps producing when every candidate is plausible.
ACCEPT_MIN_P = 0.6
ACCEPT_MIN_COMPAT = 0.5

# Labeled team strings the corpus cannot label by itself. The negatives are the
# rejections pinned by tests/test_merge_aliases.py; the positives are the four
# strings the matcher measurably fails on today plus the Miami pair the test
# suite guards. No labeled string appears in the Choice instructions verbatim as
# a worked example with its answer.
LABELED_TEAMS: list[tuple[str, str, str, str]] = [
    ("cfb", "MER", NONE_OF_THESE,
     "test_merge_aliases.py::test_shared_abbreviation_resolves_to_highest_level — "
     "Mercer / Merrimack / Mercyhurst are all FCS"),
    ("cfb", "Miami", NONE_OF_THESE,
     "test_merge_aliases.py::test_miami_never_crosses — Miami (FL) and Miami (OH) both "
     "answer to the bare city"),
    ("nfl", "Los Angeles", NONE_OF_THESE,
     "test_merge_aliases.py::test_ambiguous_city_is_unresolved — Rams and Chargers"),
    ("nfl", "New York", NONE_OF_THESE,
     "test_merge_aliases.py::test_ambiguous_city_is_unresolved — Giants and Jets"),
    ("cfb", "Zzzz Polytechnic Univ", NONE_OF_THESE,
     "test_merge_aliases.py::test_unresolved_logged_once_and_registered — not a school"),
    ("cfb", "Miami (FL)", "miami-fl", "test_merge_aliases.py::test_miami_never_crosses"),
    ("cfb", "Miami (OH)", "miami-oh", "test_merge_aliases.py::test_miami_never_crosses"),
    ("cfb", "Southern Mississippi Golden Eagles", "southern-miss",
     "legacy bol_ncaaf.db string the matcher fails on"),
    ("cfb", "UConn Huskies", "connecticut",
     "legacy bol_ncaaf.db string the matcher fails on"),
    ("cfb", "UL Monroe Warhawks", "louisiana-monroe",
     "legacy bol_ncaaf.db string the matcher fails on"),
    ("cfb", "Saint Francis", "st-francis-pa",
     "CFBD school string the matcher fails on"),
]

_STATE_SUFFIX = re.compile(r"\(([A-Z]{2})\)\s*$")
_GAME_SPLIT = re.compile(r"\s+(?:vs\.?|@|at)\s+", re.I)

_TARGET_FILES = {
    "venues": ("venues_candidates.jsonl", "venues_verdicts.jsonl", "venues_agreement.md"),
    "teams": ("teams_candidates.jsonl", "teams_verdicts.jsonl", "teams_agreement.md"),
}
VENUE_HITS = ("id_hit", "name_hit")


# ---- shared helpers --------------------------------------------------------------
def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def _pct(prob: Optional[float]) -> str:
    return "-" if prob is None else f"{prob * 100:.0f}%"


def _num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _probability(answer: Optional[dict], choice: Any) -> Optional[float]:
    if not answer:
        return None
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        # A JSON round trip stringifies non-string keys, so try both forms.
        for key in (choice, str(choice)):
            if key in probs:
                try:
                    return float(probs[key])
                except (TypeError, ValueError):
                    return None
    try:
        return float(answer.get("confidence"))
    except (TypeError, ValueError):
        return None


def _answer(verdict_row: dict, key: str) -> Optional[dict]:
    return ((verdict_row.get("verdict") or {}).get("answers") or {}).get(key)


def _noul(verdict_row: dict, key: str) -> Optional[float]:
    answer = _answer(verdict_row, key) or {}
    try:
        return float(answer.get("noul"))
    except (TypeError, ValueError):
        return None


def accepted(pick: Optional[str], prob: Optional[float], compat: Optional[float]) -> bool:
    """The promotion rule: a real candidate, enough probability mass on it, and a
    companion Noul that does not veto it."""
    if not pick or pick == NONE_OF_THESE:
        return False
    if prob is None or prob < ACCEPT_MIN_P:
        return False
    return (compat if compat is not None else 0.0) >= ACCEPT_MIN_COMPAT


def _pick(verdict_row: dict, record: dict, target: str) -> tuple[Optional[str],
                                                                 Optional[str],
                                                                 Optional[float],
                                                                 Optional[float]]:
    """``(picked id, human label, p(pick), companion Noul for the pick)``."""
    choice_key = VENUE_CHOICE_KEY if target == "venues" else TEAM_CHOICE_KEY
    answer = _answer(verdict_row, choice_key)
    choice = (answer or {}).get("choice")
    if choice is None:
        return None, None, None, None
    prob = _probability(answer, choice)
    if str(choice) == NONE_OF_THESE:
        return NONE_OF_THESE, NONE_OF_THESE, prob, None
    candidates = record.get("candidates") or []
    for index, cand in enumerate(candidates):
        key = candidate_key(index) if target == "venues" else team_choice_key(cand)
        if str(choice) in (key, f"{key}#{index}"):
            pick = cand.get("stadium_id") if target == "venues" else cand.get("team_id")
            label = cand.get("name") or pick
            return str(pick), str(label), prob, _noul(verdict_row, compat_key(index))
    return str(choice), str(choice), prob, None


def _labels(out_dir: Path, target: str) -> dict:
    path = Path(out_dir) / f"{target}_labels.json"
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning(f"[shadow] ignoring malformed {path}: {exc}")
        return {}
    return blob if isinstance(blob, dict) else {}


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    for record in records:
        append_jsonl(path, record)


# ---- harvest: venues -------------------------------------------------------------
def _home_teams_by_stadium(book: StadiumBook) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for team in book.teams.values():
        if team.home_stadium_id:
            out[team.home_stadium_id].append(team.name)
    return out


def _venue_candidate(book: StadiumBook, stadium_id: str, score: float, source: str,
                     homes: dict[str, list[str]]) -> Optional[dict]:
    st = book.stadiums.get(stadium_id)
    if st is None:
        return None
    return {
        "stadium_id": stadium_id,
        "name": st.name,
        "city": st.city,
        "state": st.state,
        "aliases": list(st.aliases)[:MAX_ALIASES],
        "home_teams": sorted(homes.get(stadium_id, []))[:3],
        "score": round(float(score), 3),
        "source": source,
    }


def venue_candidates(book: StadiumBook, entry: dict, resolved_id: Optional[str],
                     homes: dict[str, list[str]], data_dir: Path = DATA_DIR) -> list[dict]:
    """<= MAX_VENUE_CANDIDATES rows, filled in this order:

    1. the stadium the matcher resolved (so every positive is checkable at all);
    2. the home stadium of the two programs the feed listed most often at this
       venue — that is what catches a rename, where string similarity is worthless
       ("High Point Solutions Stadium" scores 0.68 against its own successor);
    3. same-city then same-state rows, when the feed carried a location;
    4. the best string neighbours over the slug index ``find_stadium`` searches.

    The reserved slots come first so a flood of "... Stadium" near-matches can never
    push the real answer out. The returned list is ordered by score, so a
    candidate's position never leaks which source put it there."""
    scores = venue_scores(book, entry["venue_name"])
    by_id = {sid: sc for sid, sc, _ in scores}
    picked: dict[str, dict] = {}

    def add(stadium_id: Optional[str], source: str) -> None:
        if not stadium_id or stadium_id in picked or len(picked) >= MAX_VENUE_CANDIDATES:
            return
        cand = _venue_candidate(book, stadium_id, by_id.get(stadium_id, 0.0), source, homes)
        if cand is not None:
            picked[stadium_id] = cand

    add(resolved_id, "matcher")
    for team_name in entry["homes"][:2]:
        team_id = teams_mod.normalize_team("cfb", team_name, "cfbd", data_dir=data_dir)
        st = book.stadium_for_team("cfb", team_id) if team_id else None
        add(st.stadium_id if st is not None else None, "home_team")

    city, state = entry.get("city"), entry.get("state")
    if city or state:
        near = []
        for st in book.stadiums.values():
            if city and (st.city or "").lower() == str(city).lower():
                near.append((0, -by_id.get(st.stadium_id, 0.0), st.stadium_id, "same_city"))
            elif state and (st.state or "").lower() == str(state).lower():
                near.append((1, -by_id.get(st.stadium_id, 0.0), st.stadium_id, "same_state"))
        for _, _, stadium_id, source in sorted(near)[:4]:
            add(stadium_id, source)

    for stadium_id, _, _ in scores:
        if len(picked) >= MAX_VENUE_CANDIDATES:
            break
        add(stadium_id, "fuzzy")
    return sorted(picked.values(), key=lambda c: (-c["score"], c["stadium_id"]))


def _cfbd_games(backtest_dir: Path) -> list[tuple[Path, list[dict]]]:
    out = []
    for path in sorted(Path(backtest_dir).glob("cfbd_games_*.json")):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[shadow] skipping unreadable {path.name}: {exc}")
            continue
        games = blob.get("games") if isinstance(blob, dict) else blob
        if isinstance(games, list):
            out.append((path, games))
    return out


def harvest_venues(out_dir: Path, backtest_dir: Path, data_dir: Path) -> dict:
    book = load_stadium_book(data_dir)
    homes_by_stadium = _home_teams_by_stadium(book)

    seen: dict[tuple[str, str], dict] = {}
    for _, games in _cfbd_games(backtest_dir):
        for game in games:
            name = str(game.get("venue") or "").strip()
            if not name:
                continue
            raw_id = game.get("venueId", game.get("venue_id"))
            venue_id = "" if raw_id is None else str(raw_id)
            entry = seen.setdefault((name, venue_id), {
                "venue_name": name, "venue_id": venue_id, "count": 0,
                "home_counts": Counter(), "seasons": set(),
                "city": game.get("venueCity") or game.get("venue_city"),
                "state": game.get("venueState") or game.get("venue_state"),
            })
            entry["count"] += 1
            entry["home_counts"][str(game.get("homeTeam") or game.get("home_team") or "")] += 1
            season = game.get("season")
            if season is not None:
                entry["seasons"].add(int(season))

    records: list[dict] = []
    decisions: Counter = Counter()
    for entry in seen.values():
        if not entry.get("state"):
            match = _STATE_SUFFIX.search(entry["venue_name"])
            entry["state"] = match.group(1) if match else None
        entry["homes"] = [n for n, _ in entry["home_counts"].most_common(3) if n]

        # Replay pipeline/schedule/cfb.py:103 — venue id first, then the name.
        stadium = book.find_stadium(entry["venue_id"]) if entry["venue_id"] else None
        decision = "id_hit" if stadium is not None else ""
        if stadium is None:
            stadium = book.find_stadium(entry["venue_name"])
            decision = "name_hit" if stadium is not None else "miss"
        resolved_id = stadium.stadium_id if stadium is not None else None
        decisions[decision] += 1

        seasons = sorted(entry["seasons"])
        records.append({
            "kind": "venue",
            "key": stable_key("venue", [entry["venue_name"], entry["venue_id"]]),
            "venue_name": entry["venue_name"],
            "venue_id": entry["venue_id"],
            "city": entry.get("city"),
            "state": entry.get("state"),
            "home_team": entry["homes"][0] if entry["homes"] else None,
            "home_teams": entry["homes"],
            "seasons": [seasons[0], seasons[-1]] if seasons else [],
            "seen_count": entry["count"],
            "matcher_decision": decision,
            "resolved_stadium_id": resolved_id,
            # id_hit / name_hit rows are positives: Jev should land on the stadium
            # the matcher resolved. Misses carry no label until a human adds one.
            "expected": resolved_id if decision in VENUE_HITS else None,
            "source": "corpus",
            "candidates": venue_candidates(book, entry, resolved_id, homes_by_stadium,
                                            data_dir),
        })

    teams_mod.reset_unresolved()  # the home-team lookups above are shadow traffic
    order = {"miss": 0, "name_hit": 1, "id_hit": 2}
    records.sort(key=lambda r: (order.get(r["matcher_decision"], 3), -r["seen_count"],
                                r["venue_name"]))
    out_path = Path(out_dir) / _TARGET_FILES["venues"][0]
    _write_records(out_path, records)
    return {"distinct": len(records), "decisions": decisions, "path": out_path,
            "stadiums": len(book.stadiums)}


# ---- harvest: teams --------------------------------------------------------------
def _team_meta(data_dir: Path) -> dict[tuple[str, str], dict]:
    path = Path(data_dir) / "teams.csv"
    if not path.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            team_id, sport = row.get("team_id"), row.get("sport")
            if team_id and sport:
                out[(sport, team_id)] = {
                    "name": row.get("name") or team_id,
                    "classification": (row.get("classification") or "").lower() or None,
                    "conference": row.get("conference") or None,
                }
    return out


def _parse_raw_file(path: Path, sport: str) -> tuple[Optional[str], list]:
    """``(book, GameLine rows)`` for one captured raw payload, or ``(None, [])``."""
    from pipeline.odds.parsers import betcris, betonline, fanduel, kalshi, novig, pinnacle, prophetx

    name = path.name
    if name.startswith("betcris_") and name.endswith(".html"):
        page = name[len("betcris_"):-len(".html")]
        return "betcris", betcris.parse(path.read_text(encoding="utf-8"), sport, page=page)
    if not name.endswith(".json"):
        return None, []
    if name.startswith("betonline_"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        league = name[len("betonline_"):-len(".json")]
        return "betonline", betonline.parse(payload, sport, league=league)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if name.endswith("_fanduel.json"):
        return "fanduel", fanduel.parse(payload, sport)
    if name.startswith("kalshi_"):
        return "kalshi", kalshi.parse(payload, sport)
    if name.startswith("novig_"):
        return "novig", novig.parse(payload, sport)
    if name.startswith("pinnacle_"):
        return "pinnacle", pinnacle.parse(payload.get("matchups") or [],
                                          payload.get("markets") or [], sport)
    if name.startswith("prophetx_"):
        return "prophetx", prophetx.parse(payload.get("events") or [],
                                          payload.get("markets") or {}, sport)
    return None, []


def _from_raw_runs(raw_runs: Path) -> Counter:
    """``Counter[(sport, book, raw)]`` over every captured run."""
    found: Counter = Counter()
    for sport in ("cfb", "nfl"):
        root = Path(raw_runs) / sport
        if not root.is_dir():
            continue
        for run in sorted(root.iterdir()):
            if not run.is_dir():
                continue
            for path in sorted(run.iterdir()):
                if not path.is_file():
                    continue
                try:
                    book, lines = _parse_raw_file(path, sport)
                except Exception as exc:  # noqa: BLE001 - a stale capture must not stop the sweep
                    logger.warning(f"[shadow] {path.name}: {type(exc).__name__}: {exc}")
                    continue
                if not book:
                    continue
                for line in lines:
                    raw_game = merge_mod.parse_provisional(line.game_id, book)
                    if raw_game is None:
                        continue
                    for raw in (raw_game.away, raw_game.home):
                        if raw:
                            found[(sport, book, raw)] += 1
    return found


def _from_legacy_db(db_dir: Path) -> Counter:
    """``Counter[(sport, book, raw)]`` from the frozen sqlite captures."""
    found: Counter = Counter()
    specs = [("bol_ncaaf.db", "ncaaf_odds", "Game", "betonline_legacy"),
             ("fd_cfb.db", "totals", "Event Name", "fanduel_legacy")]
    for filename, table, column, book in specs:
        path = Path(db_dir) / filename
        if not path.exists():
            continue
        con = sqlite3.connect(str(path))
        try:
            rows = con.execute(f'select "{column}" from "{table}"').fetchall()
        except sqlite3.DatabaseError as exc:
            logger.warning(f"[shadow] {filename}: {exc}")
            rows = []
        finally:
            con.close()
        for (value,) in rows:
            for part in _GAME_SPLIT.split(str(value or "")):
                part = part.strip()
                if part:
                    found[("cfb", book, part)] += 1
    return found


def _from_cfbd_schools(backtest_dir: Path) -> Counter:
    found: Counter = Counter()
    for _, games in _cfbd_games(backtest_dir):
        for game in games:
            for key in ("homeTeam", "home_team", "awayTeam", "away_team"):
                value = str(game.get(key) or "").strip()
                if value:
                    found[("cfb", "cfbd", value)] += 1
    return found


def team_candidates(resolver: Any, raw: str, meta: dict, sport: str,
                    resolved_id: Optional[str], data_dir: Path) -> list[dict]:
    """<= MAX_TEAM_CANDIDATES rows. The pre-filter is the resolver's own: only teams
    whose ``qualifiers()`` agree with ``raw`` are scored at all (``team_scores``
    mirrors ``TeamResolver.fuzzy``), top level breaks ties, aliases are capped."""
    scores = team_scores(resolver, raw)
    ranked = team_top_candidates(resolver, raw, limit=MAX_TEAM_CANDIDATES,
                                 max_aliases=MAX_ALIASES, data_dir=data_dir, scores=scores)
    if resolved_id and resolved_id not in {c["team_id"] for c in ranked}:
        by_id = dict(scores)
        ranked = ranked[:MAX_TEAM_CANDIDATES - 1] + [{
            "team_id": resolved_id,
            "score": round(float(by_id.get(resolved_id, 0.0)), 3),
            "classification": None,
            "aliases": [],
        }]
    out = []
    for cand in ranked:
        info = meta.get((sport, cand["team_id"]), {})
        aliases = cand.get("aliases") or []
        if not aliases:
            aliases = teams_mod.load_aliases(sport, data_dir).get(cand["team_id"], [])[:MAX_ALIASES]
        out.append({
            "team_id": cand["team_id"],
            "name": info.get("name") or cand["team_id"],
            "classification": cand.get("classification") or info.get("classification"),
            "conference": info.get("conference"),
            "aliases": aliases[:MAX_ALIASES],
            "score": cand["score"],
        })
    return sorted(out, key=lambda c: (-c["score"], c["team_id"]))


def _team_decision(resolver: Any, raw: str) -> tuple[str, Optional[str], float]:
    """``(decision, resolved_id, top fuzzy score)`` — which branch of
    ``TeamResolver.resolve`` (teams.py:252) this string takes."""
    hit = resolver.exact(raw)
    if hit is not None:
        return "exact", hit, 100.0
    team_id, top = resolver.fuzzy(raw)
    if team_id is not None:
        return "fuzzy_accept", team_id, float(top)
    return ("fuzzy_reject" if top > 0 else "unresolved"), None, float(top)


def harvest_teams(out_dir: Path, raw_runs: Path, backtest_dir: Path, legacy_db: Path,
                  data_dir: Path) -> dict:
    sources: dict[str, Counter] = {
        "raw_runs": _from_raw_runs(raw_runs),
        "legacy_db": _from_legacy_db(legacy_db),
        "cfbd": _from_cfbd_schools(backtest_dir),
    }
    merged: dict[tuple[str, str], dict] = {}
    for source, counter in sources.items():
        for (sport, book, raw), count in counter.items():
            entry = merged.setdefault((sport, raw), {"books": set(), "sources": set(),
                                                     "count": 0})
            entry["books"].add(book)
            entry["sources"].add(source)
            entry["count"] += count

    meta = _team_meta(data_dir)
    labeled_keys = {(sport, raw) for sport, raw, _, _ in LABELED_TEAMS}
    records: list[dict] = []
    decisions: Counter = Counter()

    def build(sport: str, raw: str, entry: dict, expected: Optional[str],
              source: str, note: str = "") -> dict:
        resolver = teams_mod.get_resolver(sport, data_dir)
        decision, resolved_id, top = _team_decision(resolver, raw)
        decisions[decision] += 1
        return {
            "kind": "team",
            "key": stable_key("team", [sport, raw]),
            "sport": sport,
            "raw": raw,
            "books": sorted(entry["books"]),
            "sources": sorted(entry["sources"]),
            "seen_count": entry["count"],
            "matcher_decision": decision,
            "resolved_id": resolved_id,
            "top_score": round(top, 3),
            "expected": expected,
            "label_note": note,
            "source": source,
            "candidates": team_candidates(resolver, raw, meta, sport, resolved_id, data_dir),
        }

    for (sport, raw), entry in merged.items():
        if (sport, raw) in labeled_keys:
            continue
        resolver = teams_mod.get_resolver(sport, data_dir)
        exact_hit = resolver.exact(raw)
        # Every exact alias hit is a data/aliases/{sport}.json positive: the answer is
        # the team the alias table already names. Fuzzy and unresolved rows carry no
        # label — they are the decisions under review.
        records.append(build(sport, raw, entry, exact_hit if exact_hit else None, "corpus"))

    labeled: list[dict] = []
    for sport, raw, expected, note in LABELED_TEAMS:
        entry = merged.get((sport, raw)) or {"books": set(), "sources": set(), "count": 0}
        # "labeled" joins `sources`, never `books`: the state Jev sees must carry the
        # books that really quoted the string and nothing that hints at a label.
        entry = {"books": set(entry["books"]),
                 "sources": set(entry["sources"]) | {"labeled"},
                 "count": entry["count"]}
        labeled.append(build(sport, raw, entry, expected, "labeled", note))

    # Interleave the sports inside each decision class so a --limit or --max-hits
    # spends its budget on both books' spellings, not on 30 NFL abbreviations.
    rank = {"unresolved": 0, "fuzzy_reject": 0, "fuzzy_accept": 1, "exact": 2}
    seats: dict[tuple[int, str], int] = defaultdict(int)
    ordered_pairs: list[tuple[tuple, dict]] = []
    for record in sorted(records, key=lambda r: (rank.get(r["matcher_decision"], 3),
                                                 -r["seen_count"], r["raw"])):
        klass = rank.get(record["matcher_decision"], 3)
        seat = seats[(klass, record["sport"])]
        seats[(klass, record["sport"])] = seat + 1
        ordered_pairs.append(((klass, seat, record["sport"]), record))
    ordered = labeled + [r for _, r in sorted(ordered_pairs, key=lambda kv: kv[0])]
    teams_mod.reset_unresolved()

    out_path = Path(out_dir) / _TARGET_FILES["teams"][0]
    _write_records(out_path, ordered)
    return {
        "distinct": len(ordered),
        "labeled": len(labeled),
        "decisions": decisions,
        "per_source": {name: len(counter) for name, counter in sources.items()},
        "path": out_path,
    }


# ---- judge -----------------------------------------------------------------------
def _is_hit(target: str, record: dict) -> bool:
    """A corpus row the matcher already resolved. Labeled rows never count, so
    ``--max-hits`` can never spend the budget on them or skip one."""
    if record.get("source") == "labeled":
        return False
    if target == "venues":
        return record.get("matcher_decision") in VENUE_HITS
    return record.get("matcher_decision") == "exact"


def _digest(target: str, record: dict) -> dict:
    common = {"matcher_decision": record.get("matcher_decision"),
              "expected": record.get("expected"), "source": record.get("source")}
    if target == "venues":
        return {**common, "venue_name": record.get("venue_name"),
                "venue_id": record.get("venue_id"),
                "resolved_stadium_id": record.get("resolved_stadium_id"),
                "candidates": [c.get("stadium_id") for c in record.get("candidates") or []]}
    return {**common, "raw": record.get("raw"), "sport": record.get("sport"),
            "resolved_id": record.get("resolved_id"),
            "candidates": [c.get("team_id") for c in record.get("candidates") or []]}


def run_judge(out_dir: Path, target: str, limit: Optional[int] = None,
              dry_run: bool = False, only_unresolved: bool = False,
              max_hits: Optional[int] = None, judge_fn: Any = None) -> dict:
    if judge_fn is None:
        from tools.jev_client import judge as judge_fn  # lazy: the SDK is optional
    cand_name, verdict_name, _ = _TARGET_FILES[target]
    cand_path = Path(out_dir) / cand_name
    verdict_path = Path(out_dir) / verdict_name
    candidates = read_jsonl(cand_path)
    if not candidates:
        return {"judged": 0, "skipped": 0, "failed": 0,
                "note": f"no candidates at {cand_path}"}

    done = {v.get("key") for v in read_jsonl(verdict_path)}
    judged = failed = skipped = 0
    hits = 0
    for record in candidates:
        key = record.get("key") or stable_key(target, [json.dumps(record, sort_keys=True)])
        if key in done:
            skipped += 1
            continue
        if _is_hit(target, record):
            if only_unresolved or (max_hits is not None and hits >= max_hits):
                skipped += 1
                continue
            hits += 1
        if target == "venues":
            state, questions = venue_state(record), venue_questions(record)
        else:
            state, questions = team_state(record), team_questions(record)

        verdict = judge_fn(state, questions, dry_run=dry_run)
        if verdict is None:
            failed += 1
            continue
        append_jsonl(verdict_path, {
            "key": key, "kind": record.get("kind", target),
            "candidate": _digest(target, record),
            "verdict": verdict.to_dict(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        done.add(key)
        judged += 1
        if limit and judged >= limit:
            break
    return {"judged": judged, "skipped": skipped, "failed": failed, "path": verdict_path}


# ---- report ----------------------------------------------------------------------
def _rows_for(out_dir: Path, target: str) -> tuple[list[dict], int, int, int]:
    cand_name, verdict_name, _ = _TARGET_FILES[target]
    candidates = {c.get("key"): c for c in read_jsonl(Path(out_dir) / cand_name)}
    verdicts = {v.get("key"): v for v in read_jsonl(Path(out_dir) / verdict_name)}
    rows: list[dict] = []
    dry = 0
    for key, verdict_row in verdicts.items():
        record = candidates.get(key)
        if record is None:
            continue
        if ((verdict_row.get("verdict") or {}).get("answers")) is None:
            dry += 1  # dry-run echo: no answer to agree or disagree with
            continue
        pick, label, prob, compat = _pick(verdict_row, record, target)
        usage = verdict_row.get("verdict") or {}
        rows.append({
            "key": key, "record": record, "pick": pick, "label": label,
            "prob": prob, "compat": compat,
            "accepted": accepted(pick, prob, compat),
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
        })
    return rows, len(candidates), len(verdicts), dry


def _usage_line(rows: list[dict]) -> str:
    return (f"- Jev calls in this report: **{len(rows)}** · input tokens "
            f"{sum(r['input_tokens'] for r in rows)} · output tokens "
            f"{sum(r['output_tokens'] for r in rows)}")


def report_venues(out_dir: Path) -> tuple[str, str]:
    out_dir = Path(out_dir)
    rows, n_cand, n_verdict, dry = _rows_for(out_dir, "venues")
    labels = _labels(out_dir, "venues")

    lines = ["# Identity shadow — venue resolution agreement", "",
             f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"Candidates: {n_cand} · verdicts: {n_verdict}",
             f"Accept rule: pick != {NONE_OF_THESE} AND p(pick) >= {ACCEPT_MIN_P} "
             f"AND compat Noul >= {ACCEPT_MIN_COMPAT}", ""]
    if not n_verdict:
        lines += ["No verdicts yet. Run "
                  "`python -m tools.identity_shadow judge --target venues`."]
        text = "\n".join(lines) + "\n"
        (out_dir / _TARGET_FILES["venues"][2]).write_text(text, encoding="utf-8")
        return text, "no verdicts yet"

    agree = disagree = 0
    disagree_rows, miss_rows = [], []
    for row in sorted(rows, key=lambda r: -r["record"].get("seen_count", 0)):
        record = row["record"]
        decision = record.get("matcher_decision")
        if decision in VENUE_HITS:
            if row["accepted"] and row["pick"] == record.get("resolved_stadium_id"):
                agree += 1
            else:
                disagree += 1
                disagree_rows.append([
                    record.get("venue_name"), decision, record.get("resolved_stadium_id"),
                    row["label"] if row["accepted"] else f"{row['label']} (not accepted)",
                    _pct(row["prob"]), _num(row["compat"])])
        else:
            miss_rows.append([
                row["key"], record.get("venue_name"), record.get("venue_id"),
                record.get("home_team"), row["label"], _pct(row["prob"]),
                _num(row["compat"]), "yes" if row["accepted"] else "no",
                labels.get(row["key"], "")])

    judged_hits = agree + disagree
    rate = (agree / judged_hits * 100) if judged_hits else 0.0
    lines += ["## Resolved venues (matcher said id_hit / name_hit)", "",
              f"- judged: **{judged_hits}**",
              f"- agreement: **{rate:.1f}%** ({agree} agree, {disagree} disagree)",
              f"- dry-run rows ignored: {dry}", ""]
    if disagree_rows:
        lines += ["### Disagreements — each one is a labeled miss", "",
                  _md_table(["venue", "matcher", "matcher stadium_id", "Jev pick", "p",
                             "compat"], disagree_rows), ""]
        lines += [f"- **LABELED MISS: {r[0]} — matcher {r[2]}, Jev {r[3]}**"
                  for r in disagree_rows] + [""]
    else:
        lines += ["- no labeled miss: every resolved venue came back on the matcher's "
                  "stadium.", ""]

    lines += ["## Unresolved venues (matcher said miss)", "",
              "This is the human-review table. Add the correct `stadium_id` (or "
              "`\"none\"`) to `venues_labels.json` as `{\"<key>\": \"<stadium_id>\"}`.", ""]
    if miss_rows:
        lines += [_md_table(["key", "venue", "venue_id", "home team", "Jev pick", "p",
                             "compat", "accepted", "human_label"], miss_rows), ""]
        unlabeled = sum(1 for r in miss_rows if not r[-1])
        mismatched = [r for r in miss_rows
                      if r[-1] and r[-1] != (r[4] if r[7] == "yes" else "none")]
        lines += [f"- unlabeled: **{unlabeled}** of {len(miss_rows)}",
                  f"- labeled and contradicted by Jev: **{len(mismatched)}**", ""]
    else:
        lines += ["_none judged yet_", ""]
    lines += [_usage_line(rows), ""]

    text = "\n".join(lines) + "\n"
    (out_dir / _TARGET_FILES["venues"][2]).write_text(text, encoding="utf-8")
    summary = (f"venue hits judged: {judged_hits}; agreement {rate:.1f}% "
               f"({agree} agree / {disagree} disagree); misses judged: {len(miss_rows)}; "
               f"dry-run ignored: {dry}")
    return text, summary


def report_teams(out_dir: Path) -> tuple[str, str]:
    out_dir = Path(out_dir)
    rows, n_cand, n_verdict, dry = _rows_for(out_dir, "teams")
    labels = _labels(out_dir, "teams")

    lines = ["# Identity shadow — team canonicalization agreement", "",
             f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"Candidates: {n_cand} · verdicts: {n_verdict}",
             f"Accept rule: pick != {NONE_OF_THESE} AND p(pick) >= {ACCEPT_MIN_P} "
             f"AND compat Noul >= {ACCEPT_MIN_COMPAT}", ""]
    if not n_verdict:
        lines += ["No verdicts yet. Run "
                  "`python -m tools.identity_shadow judge --target teams`."]
        text = "\n".join(lines) + "\n"
        (out_dir / _TARGET_FILES["teams"][2]).write_text(text, encoding="utf-8")
        return text, "no verdicts yet"

    labeled_rows, misses, open_rows = [], [], []
    for row in sorted(rows, key=lambda r: (r["record"].get("source") != "labeled",
                                           -r["record"].get("seen_count", 0))):
        record = row["record"]
        expected = record.get("expected") or labels.get(row["key"])
        actual = row["pick"] if row["accepted"] else NONE_OF_THESE
        table_row = [
            f"{record.get('sport')}:{record.get('raw')}", record.get("source"),
            record.get("matcher_decision"), record.get("resolved_id") or "-",
            row["label"], _pct(row["prob"]), _num(row["compat"]),
            "yes" if row["accepted"] else "no", expected or "",
        ]
        if expected:
            ok = actual == expected
            labeled_rows.append(table_row + ["ok" if ok else "MISS"])
            if not ok:
                misses.append((record, row, expected, actual))
        else:
            open_rows.append(table_row)

    hits = len(labeled_rows) - len(misses)
    rate = (hits / len(labeled_rows) * 100) if labeled_rows else 0.0
    lines += ["## Labeled accuracy", "",
              f"- labeled records judged: **{len(labeled_rows)}** "
              "(alias-table positives + the curated rejections)",
              f"- correct: **{hits}** · misses: **{len(misses)}** "
              f"(**{rate:.1f}%**)",
              f"- dry-run rows ignored: {dry}", ""]
    if misses:
        lines += ["### Misses — every one is a promotion bug", "",
                  _md_table(["raw", "expected", "got", "p", "compat"],
                            [[f"{m[0].get('sport')}:{m[0].get('raw')}", m[2],
                              m[1]["label"] if m[1]["accepted"] else NONE_OF_THESE,
                              _pct(m[1]["prob"]), _num(m[1]["compat"])] for m in misses]), ""]
        lines += [f"- **MISS: {m[0].get('raw')} — expected {m[2]}, got {m[3]}**"
                  for m in misses] + [""]
    elif labeled_rows:
        lines += ["- none: every labeled record landed on its expected answer.", ""]

    if labeled_rows:
        lines += ["## Labeled records", "",
                  _md_table(["raw", "source", "matcher", "matcher id", "Jev pick", "p",
                             "compat", "accepted", "expected", "verdict"], labeled_rows), ""]
    lines += ["## Unlabeled records (fuzzy accepts, fuzzy rejects, unresolved)", ""]
    if open_rows:
        lines += [_md_table(["raw", "source", "matcher", "matcher id", "Jev pick", "p",
                             "compat", "accepted", "expected"], open_rows), ""]
    else:
        lines += ["_none judged yet_", ""]
    lines += [_usage_line(rows), ""]

    text = "\n".join(lines) + "\n"
    (out_dir / _TARGET_FILES["teams"][2]).write_text(text, encoding="utf-8")
    summary = (f"judged records: {len(rows)}; labeled {hits}/{len(labeled_rows)} correct "
               f"({rate:.1f}%); unlabeled judged: {len(open_rows)}; "
               f"dry-run ignored: {dry}")
    return text, summary


# ---- CLI -------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.identity_shadow",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output root (default output/identity_shadow)")
    sub = parser.add_subparsers(dest="command", required=True)
    targets = ["venues", "teams"]

    harvest = sub.add_parser("harvest", help="replay the corpus into candidate records")
    harvest.add_argument("target", choices=targets)
    harvest.add_argument("--backtest-dir", type=Path, default=BACKTEST_DIR)
    harvest.add_argument("--raw-runs", type=Path, default=RAW_RUNS_DIR)
    harvest.add_argument("--legacy-db", type=Path, default=LEGACY_DB_DIR)
    harvest.add_argument("--data-dir", type=Path, default=DATA_DIR)

    judge_cmd = sub.add_parser("judge", help="ask Jev about unjudged candidates")
    judge_cmd.add_argument("--target", choices=targets, required=True)
    judge_cmd.add_argument("--limit", type=int, default=None)
    judge_cmd.add_argument("--dry-run", action="store_true")
    judge_cmd.add_argument("--only-unresolved", action="store_true",
                           help="skip rows the matcher already resolved")
    judge_cmd.add_argument("--max-hits", type=int, default=None,
                           help="cap how many already-resolved rows are judged")

    report = sub.add_parser("report", help="write the agreement markdown")
    report.add_argument("--target", choices=targets, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    out_dir = args.out_dir or shadow_dir()
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    if args.command == "harvest" and args.target == "venues":
        stats = harvest_venues(out_dir, args.backtest_dir, args.data_dir)
        print(f"distinct (venue, venue_id): {stats['distinct']} -> {stats['path']}")
        print(f"stadiums.csv rows loaded: {stats['stadiums']}")
        print("matcher_decision breakdown:")
        for decision, count in stats["decisions"].most_common():
            print(f"  {decision:<10} {count}")
        return 0

    if args.command == "harvest":
        stats = harvest_teams(out_dir, args.raw_runs, args.backtest_dir, args.legacy_db,
                              args.data_dir)
        print(f"distinct (sport, raw): {stats['distinct']} "
              f"({stats['labeled']} labeled) -> {stats['path']}")
        print("distinct (sport, book, raw) per source:")
        for name, count in sorted(stats["per_source"].items()):
            print(f"  {name:<10} {count}")
        print("matcher_decision breakdown:")
        for decision, count in stats["decisions"].most_common():
            print(f"  {decision:<14} {count}")
        return 0

    if args.command == "judge":
        stats = run_judge(out_dir, args.target, args.limit, args.dry_run,
                          args.only_unresolved, args.max_hits)
        if stats.get("note"):
            print(stats["note"])
            return 0
        print(f"judged {stats['judged']}, skipped {stats['skipped']} (already judged or "
              f"filtered), failed {stats['failed']} -> {stats['path']}")
        return 0

    reports = {"venues": report_venues, "teams": report_teams}
    text, summary = reports[args.target](out_dir)
    print(summary)
    print(f"wrote {Path(out_dir) / _TARGET_FILES[args.target][2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
