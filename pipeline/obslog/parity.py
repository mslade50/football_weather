"""Advisory state/log comparison. Findings and internal errors always exit zero."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import state as pstate

from . import queries as q
from .store import QuoteKey, instant, utc

CLASSIFICATIONS = ("match", "mismatch", "log_started_late", "known_i1_failed_scrape",
                   "known_i2_history_clock", "known_i3_history_cap", "known_i4_weekly_backfill")
AREAS = ("baseline", "latest", "history", "closings", "wx_history", "wx_last", "weekly_totals")


def _same_to_the_second(store, logged):
    """State files keep quote clocks to the whole second; the log keeps microseconds."""
    if not (isinstance(store, list) and isinstance(logged, list) and len(store) == len(logged) == 3):
        return False
    a, b = store[2], logged[2]
    if store[:2] != logged[:2] or not (isinstance(a, str) and isinstance(b, str)):
        return False
    return a.endswith(".000000Z") and a[:19] == b[:19]


def classify(store, logged, *, late=False, known=None):
    """Known differences require positive evidence supplied by the area adapter."""
    if late:
        return "log_started_late"
    if store == logged or _same_to_the_second(store, logged):
        return "match"
    if known in CLASSIFICATIONS and known.startswith("known_"):
        return known
    return "mismatch"


def load(path):
    # Missing state is not silently treated as a matching empty store.
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stamp(value):
    return utc(instant(value)) if value else None


def quote_value(value, clock="ts"):
    return [value.get("line"), value.get("odds"), stamp(value.get(clock))] if value else None


def observed_value(value):
    return [value.line, value.odds, utc(value.observed_at)] if value else None


def parse_key(value):
    gid, market, side, book = value.split("|")
    return QuoteKey(gid, book, market, side)


def legacy_key(key):
    return pstate.odds_key(key.game_id, key.market, key.side, key.book)


def collapse_values(points):
    out = []
    for point in points:
        if not out or point[1:] != out[-1][1:]:
            out.append(point)
    return out


def clock_difference(store, logged, rows):
    return (len(store) == len(logged) and bool(rows)
            and [p[1:] for p in store] == [p[1:] for p in logged]
            and all(instant(p[0]) >= r.recorded_at for p, r in zip(store, rows, strict=True)))


class Report:
    def __init__(self, as_of, sample_limit):
        self.data = {"status": "ok", "as_of": utc(as_of), "counts": {a: dict.fromkeys(CLASSIFICATIONS, 0) for a in AREAS},
                     "sample_mismatches": [], "sample_limit": sample_limit,
                     "scope": {"excluded": ["consensus/model-fair series", "derived gs_fg/gs_fg_v2 weather impacts"]}}
        self.limit = sample_limit

    def add(self, area, key, store, logged, *, rows=(), late=False, known=None):
        result = classify(store, logged, late=late, known=known)
        self.data["counts"][area][result] += 1
        if result == "mismatch" and len(self.data["sample_mismatches"]) < self.limit:
            evidence = rows if len(rows) <= 6 else [*rows[:2], *rows[-4:]]
            self.data["sample_mismatches"].append({"area": area, "key": key, "store": store, "log": logged,
                                                    "observations": [r.data if isinstance(r, q.Observation) else r for r in evidence]})


def compare(conn, state_dir, cards, as_of, *, sample_limit=20):
    state_dir = Path(state_dir)
    report = Report(as_of, sample_limit)
    games = {c["game_id"]: c for c in cards}
    report.data["scope"]["game_count"] = len(games)
    report.data["recent_runs"] = [dict(r) for r in conn.execute(
        "SELECT run_id, stage, started_at, finished_at, attempted, successful FROM runs "
        "WHERE finished_at<=? ORDER BY finished_at DESC, id DESC LIMIT 8", (utc(as_of),))]
    report.data["run_count"] = conn.execute("SELECT count(DISTINCT run_id) FROM runs WHERE finished_at<=?", (utc(as_of),)).fetchone()[0]
    # Temporary views restrict every existing query to this board, without copying
    # observations or modifying the cached database. Main DB can be opened read-only.
    conn.execute("CREATE TEMP TABLE parity_games (game_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO parity_games VALUES (?)", [(g,) for g in games])
    for table in ("quote_observations", "game_observations", "forecast_observations"):
        # first_seen orders a capture by insertion (scrape line) order; a view has no rowid of its own.
        conn.execute(f"CREATE TEMP VIEW {table} AS SELECT {'rowid AS rowid, ' if table == 'quote_observations' else ''}* FROM main.{table} "
                     "WHERE game_id IN (SELECT game_id FROM parity_games)")
    points = defaultdict(list)
    for row in q._rows(conn, as_of):
        if row.key.book != "consensus":
            points[row.key].append(row)
    game_first = {gid: instant(row[0]) for gid in games if (row := conn.execute(
        "SELECT min(observed_at) FROM game_observations WHERE game_id=? AND observed_at<=?", (gid, utc(as_of))).fetchone())[0]}

    def late(key, fact_time):
        rows = [r for r in points.get(key, []) if r.kind == "quote"]
        first = min((r.observed_at for r in rows), default=None)
        return bool(fact_time and (first is None or instant(fact_time) < first))

    def selected(mapping):
        return {parse_key(k): v for k, v in mapping.items() if parse_key(k).game_id in games and parse_key(k).book != "consensus"}

    openers = pstate.load_openers(state_dir)
    baseline = {k: b for k, b in q.baseline(conn, as_of=as_of, cfb_t6=True, opener_policy="anchored_first_seen").items()
                if k.book != "consensus"}
    keys = set(baseline)
    for mapping in (openers.get("openers", {}), openers.get("true_openers", {}),
                    openers.get("references", {}).get("t_minus_6d", {})):
        keys.update(selected(mapping))
    for key in sorted(keys):
        store = pstate.get_baseline(openers, legacy_key(key))
        found = baseline.get(key)
        value = observed_value(found.observation) if found else None
        old = quote_value(store)
        # A valid attestation can itself predate collection; it is evidence, not a cold-cache gap.
        attested = found and found.basis == "true_opener" and old == value
        known = "known_i2_history_clock" if (old and value and found.basis == "t_minus_6d"
                 and old[:2] == value[:2] and instant(old[2]) >= found.observation.recorded_at) else None
        fact_ts = store.get("ts") if store else None
        mates = {"home": "away", "away": "home", "over": "under", "under": "over"}
        if not store and key.market in ("spread", "total") and key.side in mates:
            # The store adds a missing side only at its mate's recorded number. When that
            # anchor predates the log, the log cannot know this side is being withheld.
            mate = pstate.get_baseline(openers, legacy_key(QuoteKey(key.game_id, key.book, key.market, mates[key.side])))
            fact_ts = mate.get("ts") if mate else None
        report.add("baseline", legacy_key(key), old, value, rows=[*points[key], found.observation] if found else points[key],
                   late=late(key, fact_ts) and not attested, known=known)

    archive = selected(load(state_dir / "archive_last.json").get("last", {}))
    latest = {k: v for k, v in q.latest(conn, as_of=as_of, max_age=timedelta(hours=1), honor_expiry=True).items()
              if k.book != "consensus"}
    failed = []
    for row in conn.execute("SELECT * FROM runs WHERE finished_at<=? AND stage='odds'", (utc(as_of),)):
        failed.append((instant(row["finished_at"]), set(map(tuple, json.loads(row["attempted"]))) -
                       set(map(tuple, json.loads(row["successful"])))))
    for key in sorted(set(archive) | set(latest)):
        store, found = archive.get(key), latest.get(key)
        eligible = bool(store and store.get("available") is True and store.get("odds") is not None and store.get("ts")
                        and timedelta(0) <= as_of - instant(store["ts"]) <= timedelta(hours=1)
                        and ("expires_at" not in store or (store["expires_at"] and instant(store["expires_at"]) >= as_of)))
        old = quote_value(store) if eligible else None
        value = observed_value(found)
        failed_after_quote = found and any(t > found.recorded_at and (key.book, found.sport) in pairs for t, pairs in failed)
        known = "known_i1_failed_scrape" if store and store.get("available") is False and failed_after_quote else None
        report.add("latest", legacy_key(key), old, value, rows=points[key],
                   late=late(key, store.get("ts") if store else None) and (old is not None or value is not None), known=known)

    history = selected(load(state_dir / "history.json").get("series", {}))
    for key in sorted(set(history) | set(points)):
        rows = q.collapse(points[key], cap=120)
        old = [[stamp(p[0]), *p[1:]] for p in history.get(key, [])]
        value = [[utc(r.observed_at), r.line, r.odds] for r in rows]
        report.add("history", legacy_key(key), old, value, rows=rows,
                   late=late(key, old[0][0] if old else None),
                   known="known_i2_history_clock" if clock_difference(old, value, rows) else None)

    closings = selected(load(state_dir / "closings.json").get("closings", {}))
    frozen = {k: v for k, v in q.closing(conn, as_of=as_of, change_points=True).items() if k.book != "consensus"}
    for key in sorted(set(closings) | set(frozen)):
        kick = instant(games[key.game_id]["kickoff_utc"])
        if kick > as_of:
            continue
        store, found = closings.get(key), frozen.get(key)
        old, value = quote_value(store, "scraped_at"), observed_value(found)
        seq = history.get(key, [])
        capped = not store and found and len(seq) == 120 and all(instant(p[0]) >= kick for p in seq)
        known = "known_i3_history_cap" if capped else (
            "known_i2_history_clock" if (old and value and old[:2] == value[:2]
             and instant(old[2]) >= found.recorded_at and any(stamp(p[0]) == old[2] and p[1:] == old[:2] for p in seq)) else None)
        fact_time = store.get("scraped_at") if store else utc(kick)
        report.add("closings", legacy_key(key), old, value, rows=[*points[key], found] if found else points[key],
                   late=late(key, fact_time), known=known)

    compare_weather(conn, state_dir, games, as_of, report)
    for gid, store in (openers.get("weekly_totals") or {}).items():
        if gid not in games:
            continue
        found = q.weekly_total(conn, gid, as_of=as_of)
        old = [store.get(f) for f in ("line", "under", "over")]
        value = [found.get(f) for f in ("line", "under", "over")] if found else [None, None, None]
        first = min((r.observed_at for k, rows in points.items() if k.game_id == gid and k.book == "betonline" for r in rows),
                    default=game_first.get(gid))
        # Prove the history-only backfill accident: no stored opener but an actual
        # in-window receipt equal to a preceding out-of-window receipt.
        known = None
        if store.get("line") is None and found:
            for key, rows in points.items():
                if key.game_id == gid and key.book == "betonline" and key.market == "total":
                    if any(r.kind == "quote" and r.observed_at < found["observed_at"] and r.line == found["line"]
                           and r.odds == found.get(key.side) for r in rows):
                        known = "known_i4_weekly_backfill"
        report.add("weekly_totals", gid, old, value,
                   late=bool(store.get("ts") and (first is None or instant(store["ts"]) < first)), known=known)
    report.data["findings"] = sum(v["mismatch"] for v in report.data["counts"].values())
    return report.data


def compare_weather(conn, state_dir, games, as_of, report):
    rows = q.forecasts(conn, as_of=as_of, latest_only=False)
    by_game, by_source = defaultdict(list), defaultdict(list)
    for row in rows:
        by_game[row["game_id"]].append(row)
        by_source[f"{row['game_id']}|{row['source']}"].append(row)
    # lead_hours is time-to-kickoff: a clock that moves every run, so change-only
    # forecast storage does not key on it and neither does this comparison.
    fields = ("wind_fg", "gust_fg", "temp_fg", "rain_fg_mm", "precip_prob")
    history = load(state_dir / "wx_history.json").get("series", {})
    for gid in sorted((set(history) | set(by_game)) & set(games)):
        observations = by_game[gid]
        old = collapse_values([[stamp(p[0]), *p[2:7]] for p in history.get(gid, [])])
        value = collapse_values([[r["fetched_at"], *[json.loads(r["payload"]).get(f) for f in fields]] for r in observations])[-120:]
        is_late = bool(old and (not observations or instant(old[0][0]) < instant(observations[0]["fetched_at"])))
        clock_only = (bool(old) and len(old) == len(value) and [p[1:] for p in old] == [p[1:] for p in value]
                      and all(a[0] >= b[0] for a, b in zip(old, value, strict=True)))
        report.add("wx_history", gid, old, value, rows=observations, late=is_late,
                   known="known_i2_history_clock" if clock_only else None)
    last = load(state_dir / "wx_last.json").get("last", {})
    for key in sorted(set(last) | set(by_source)):
        gid = key.split("|", 1)[0]
        if gid not in games:
            continue
        observations = by_source[key]
        payload = json.loads(observations[-1]["payload"]) if observations else None
        old = last[key][:5] if key in last else None
        value = [payload.get(f) for f in fields] if payload else None
        report.add("wx_last", key, old, value, rows=observations, late=bool(old is not None and not observations))


def summary(data):
    lines = ["\n### Observation log parity (advisory)\n", f"Status: {data['status']}; findings: {data.get('findings', 0)}.\n",
             "| Area | Match | Mismatch | Log started late | Known differences |", "| --- | ---: | ---: | ---: | ---: |"]
    for area, counts in data.get("counts", {}).items():
        lines.append(f"| {area} | {counts['match']} | {counts['mismatch']} | {counts['log_started_late']} | "
                     f"{sum(v for k, v in counts.items() if k.startswith('known_'))} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    out = None
    data = {"status": "error", "findings": 0, "counts": {}}
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--log", required=True)
        parser.add_argument("--state-dir", default="data/state")
        parser.add_argument("--board-dir")
        parser.add_argument("--out", required=True)
        parser.add_argument("--as-of")
        parser.add_argument("--samples", type=int, default=20)
        args = parser.parse_args(argv)
        out = Path(args.out)
        board = Path(args.board_dir) if args.board_dir else Path(args.state_dir).parent / "board"
        files = sorted(board.glob("games_*.json"))
        if not files:
            raise ValueError("current board is unavailable")
        cards = [c for f in files for c in load(f)["games"]]
        as_of = instant(args.as_of) if args.as_of else datetime.now(timezone.utc)
        utc(as_of)
        with closing(sqlite3.connect(Path(args.log).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if [r[0] for r in conn.execute("SELECT version FROM schema_version")] not in ([3],):
                raise ValueError("unsupported observation-log schema")
            data = compare(conn, args.state_dir, cards, as_of, sample_limit=max(0, args.samples))
    except (Exception, SystemExit) as exc:
        data["error"] = type(exc).__name__
        print(f"::warning::Observation log parity unavailable ({type(exc).__name__}); production continues")
    try:
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(summary(data))
        print(f"Observation log parity: {data['status']}, {data.get('findings', 0)} finding(s)")
    except Exception as exc:  # Output failures must not affect the pipeline either.
        print(f"::warning::Observation log parity output unavailable ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
