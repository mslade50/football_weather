"""Append-only, standard-library storage. All public instants must be aware.

Schema v4 is change-only and slim. A quote, forecast or game row is appended
only when its *semantic value* differs from the latest logged one for that key;
scrape clocks and provider metadata never force a row. Every successful
(book, sport) scrape writes small per-market ``coverage`` rows (keys NULL); a
failed or partial scrape writes coverage that lists only the (game, side) pairs
it actually listed. "This quote was still live and unchanged at T" is the last
change row plus a later coverage row that confirms it with no logged row for the
key in between (withdrawals are rows). Only a successful (book, sport) withdraws.

Only main lines are logged: every production store compared here (openers,
latest, history, closings) is main-lines-only, and raw captures keep alternates
and provider payloads verbatim, so rows carry a capture reference instead.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import Game, GameLine, WeatherForecast

SCHEMA_VERSION = 4
MARKETS = ("ml", "spread", "total")
# Clocks and provider metadata, not values: they move without the forecast moving.
# lead_hours is time-to-kickoff; run_time is the fetch's model-run stamp (it moved
# on 140 of 152 consecutive-run pairs in production while values did not).
FORECAST_CLOCKS = ("lead_hours", "run_time", "point_source_updated_at", "ensemble_fetched_at")


class UnsupportedSchema(ValueError):
    """The cached log was written by an unknown (e.g. older or newer) schema version."""


@dataclass(frozen=True, order=True)
class MarketKey:
    game_id: str
    book: str
    market: str


@dataclass(frozen=True, order=True)
class QuoteKey:
    game_id: str
    book: str
    market: str
    side: str

    @property
    def pair(self) -> MarketKey:
        return MarketKey(self.game_id, self.book, self.market)


def utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_default(value):
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(type(value).__name__)


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)


def identity(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Create schema, reject unknown versions, and return a caller-owned connection.

    Use contextlib.closing(connect(path)) to close it; sqlite's context manager
    commits/rolls back but does not close a connection. Parent must already exist.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone()
        if existing and [r[0] for r in conn.execute("SELECT version FROM schema_version")] not in ([SCHEMA_VERSION],):
            raise UnsupportedSchema("unsupported observation-log schema version")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        conn.executescript("BEGIN IMMEDIATE;\n" + schema + "\nDELETE FROM schema_version;\n"
                           f"INSERT INTO schema_version VALUES ({SCHEMA_VERSION});\nCOMMIT;")
    except Exception:
        conn.close()
        raise
    return conn


open = connect


def insert(conn, table, row):
    """Idempotent by content hash (small tables: runs, games, forecasts)."""
    row = {"id": identity(row), **row}
    conn.execute(f"INSERT OR IGNORE INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def _games(conn, games, run_id, now):
    for game in games:
        row = dict(game_id=game.game_id, observed_at=utc(now), run_id=run_id,
                   kickoff=utc(game.kickoff_utc), sport=game.sport, season=game.season,
                   week=game.week, payload=encode(asdict(game)))
        head = conn.execute("SELECT kickoff, sport, season, week, payload FROM game_observations WHERE game_id=? "
                            "AND observed_at<=? ORDER BY observed_at DESC, id DESC LIMIT 1", (game.game_id, row["observed_at"])).fetchone()
        if head is None or tuple(head) != (row["kickoff"], row["sport"], row["season"], row["week"], row["payload"]):
            insert(conn, "game_observations", row)


def append_games(conn: sqlite3.Connection, games: list[Game], *, run_id: str, observed_at: datetime):
    with conn:
        _games(conn, games, run_id, observed_at)


def _run(conn, run_id, stage, now, started_at, attempted, successful, sports=()):
    insert(conn, "runs", dict(run_id=run_id, stage=stage, started_at=utc(started_at or now),
                             finished_at=utc(now), attempted=encode(sorted(attempted)), successful=encode(sorted(successful)),
                             books=encode(sorted({b for b, _ in attempted | successful})),
                             sports=encode(sorted(set(sports) | {s for _, s in attempted | successful}))))


def key_of(row) -> QuoteKey:
    return QuoteKey(row["game_id"], row["book"], row["market"], row["side"])


def _capture(conn, capture_id, run_id) -> int:
    conn.execute("INSERT OR IGNORE INTO captures (capture_id, run_id) VALUES (?, ?)", (capture_id, run_id))
    return conn.execute("SELECT cap FROM captures WHERE capture_id=?", (capture_id,)).fetchone()[0]


def _quote_row(line, now, cap):
    expires, actionable, opened = (utc(x) if x is not None else None
                                   for x in (line.expires_at, line.actionable_until, line.opened_at))
    clock = utc(line.scraped_at or now)
    return dict(game_id=line.game_id, sport=line.sport, book=line.book, market=line.market, side=line.side,
                line=line.line, odds=line.odds, prob_raw=line.prob_raw, observed_at=clock, recorded_at=utc(now),
                quote_at=None, kind="quote", cap=cap, expires_at=expires, actionable_until=actionable,
                opening_line=line.opening_line, opening_odds=line.opening_odds, opened_at=opened,
                opening_source=line.opening_source)


def _insert_quote(conn, row):
    # Natural-key idempotence (no hash column): re-appending a receipt is a no-op.
    cols = ",".join(row)
    conn.execute(
        f"INSERT INTO quote_observations ({cols}) SELECT {','.join('?' for _ in row)} WHERE NOT EXISTS ("
        "SELECT 1 FROM quote_observations WHERE game_id=? AND book=? AND market=? AND side=? "
        "AND observed_at=? AND recorded_at=? AND kind=?)",
        (*row.values(), row["game_id"], row["book"], row["market"], row["side"],
         row["observed_at"], row["recorded_at"], row["kind"]))


def _ttl(row):
    """Expiry as an offset from the observation clock: rolling-expiry quotes (BetCris) keep it fixed."""
    at = instant(row["observed_at"])
    return tuple((instant(row[f]) - at) if row[f] else None for f in ("expires_at", "actionable_until"))


def semantic(row):
    """What a consumer can act on. Scrape clocks, run ids and provider metadata are excluded.

    prob_raw is a provider value (exchange fair price), so it counts. Expiry counts
    as an offset; queries shift it with the confirmation clock.
    """
    return (row["line"], row["odds"], row["prob_raw"], row["opening_line"], row["opening_odds"],
            row["opened_at"], row["opening_source"], _ttl(row))


def _changed(head, row):
    return head is None or head["kind"] != "quote" or semantic(head) != semantic(row)


def _heads(conn, book, sport, stamp):
    """Newest logged row per (game, market, side) of one book/sport, via the quote_head index."""
    rows = conn.execute(
        "SELECT * FROM quote_observations WHERE rowid IN (SELECT rid FROM (SELECT rowid AS rid, "
        "row_number() OVER (PARTITION BY game_id, market, side ORDER BY recorded_at DESC, rowid DESC) AS n "
        "FROM quote_observations WHERE book=? AND sport=? AND recorded_at<=?) WHERE n=1)",
        (book, sport, stamp)).fetchall()
    return {(r["game_id"], r["market"], r["side"]): dict(r) for r in rows}


def _append_lines(conn, lines, successful, now, cap):
    """Change-only rows for main lines; returns heads and per-scope partial confirmations."""
    stamp = utc(now)
    main = [ln for ln in lines if ln.is_main]
    heads = {group: _heads(conn, *group, stamp) for group in {(ln.book, ln.sport) for ln in main} | set(successful)}
    unchanged = {}
    for line in main:
        row = _quote_row(line, now, cap)
        group = (line.book, line.sport)
        slot, k = heads[group], (line.game_id, line.market, line.side)
        if _changed(slot.get(k), row):
            _insert_quote(conn, row)
            slot[k] = row
        elif group not in successful:
            unchanged.setdefault((*group, line.market), []).append((line.game_id, line.side, line.scraped_at or now))
    return heads, unchanged


def _coverage(conn, lines, successful, unchanged, cap, now):
    stamp = utc(now)
    for book, sport in sorted(successful):
        group = [ln for ln in lines if ln.book == book and ln.sport == sport and ln.is_main]
        every = min((ln.scraped_at or now for ln in group), default=now)
        for market in MARKETS:
            clock = min((ln.scraped_at or now for ln in group if ln.market == market), default=every)
            _insert_cover(conn, cap, book, sport, market, utc(clock), stamp, None)
    for (book, sport, market), seen in sorted(unchanged.items()):
        keys = zlib.compress(encode(sorted((g, s) for g, s, _ in seen)).encode("utf-8"), 6)
        _insert_cover(conn, cap, book, sport, market, utc(min(c for _, _, c in seen)), stamp, keys)


def _insert_cover(conn, cap, book, sport, market, observed, recorded, keys):
    conn.execute(
        "INSERT INTO coverage (cap, book, sport, market, observed_at, recorded_at, keys) SELECT ?,?,?,?,?,?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM coverage WHERE cap=? AND book=? AND sport=? AND market=?)",
        (cap, book, sport, market, observed, recorded, keys, cap, book, sport, market))


def _withdrawals(conn, seen, heads, successful, now, cap):
    """Withdraw main quotes missing from a successful (book, sport) scrape.

    ``heads`` are the newest rows per key, read in SQL through an index, not
    the whole log. Partial scrapes never reach here.
    """
    stamp = utc(now)
    for group in sorted(successful):
        for previous in heads.get(group, {}).values():
            if previous["kind"] != "quote" or key_of(previous) in seen:
                continue
            row = dict(previous)
            row.update(cap=cap, observed_at=stamp, recorded_at=stamp, kind="withdrawn",
                       quote_at=previous["quote_at"] or previous["observed_at"])
            _insert_quote(conn, row)


def append_odds(conn: sqlite3.Connection, lines: list[GameLine], *, run_id: str, observed_at: datetime,
                games: list[Game] = (), successful: set[tuple[str, str]] = frozenset(),
                attempted: set[tuple[str, str]] = frozenset(), started_at: datetime | None = None):
    """One atomic receipt: facts, changed main quotes, coverage, withdrawals.

    capture_id groups the batch; pairs require a common capture AND canonical
    number. successful is explicit, never inferred from absence of lines.
    Unsuccessful/unscraped pairs cannot withdraw anything and, though the lines
    they did list are logged when changed and confirmed individually, they write
    no full coverage. Alternate lines are not logged (see module docstring).
    """
    capture = identity((run_id, utc(observed_at), "odds"))
    successful = set(successful)
    with conn:
        _run(conn, run_id, "odds", observed_at, started_at, attempted | successful, successful, {g.sport for g in games} | {ln.sport for ln in lines})
        _games(conn, games, run_id, observed_at)
        cap = _capture(conn, capture, run_id)
        heads, unchanged = _append_lines(conn, lines, successful, observed_at, cap)
        _coverage(conn, lines, successful, unchanged, cap, observed_at)
        _withdrawals(conn, {QuoteKey(x.game_id, x.book, x.market, x.side) for x in lines if x.is_main},
                     heads, successful, observed_at, cap)


def record_withdrawals(conn, lines, successful, *, run_id: str, observed_at: datetime):
    """Standalone successful-coverage receipt; prefer append_odds for atomicity."""
    capture = identity((run_id, utc(observed_at), "coverage"))
    successful = set(successful)
    with conn:
        _run(conn, run_id, "coverage", observed_at, None, successful, successful)
        cap = _capture(conn, capture, run_id)
        heads = {group: _heads(conn, *group, utc(observed_at)) for group in successful}
        _coverage(conn, lines, successful, {}, cap, observed_at)
        _withdrawals(conn, {QuoteKey(x.game_id, x.book, x.market, x.side) for x in lines if x.is_main},
                     heads, successful, observed_at, cap)


def forecast_content(payload, clocks=FORECAST_CLOCKS) -> dict:
    text = zlib.decompress(payload).decode("utf-8") if isinstance(payload, bytes) else payload
    return {k: v for k, v in json.loads(text).items() if k not in clocks}


def append_forecasts(conn: sqlite3.Connection, forecasts: list[WeatherForecast], *, run_id: str,
                     fetched_at: datetime, games: list[Game] = (), started_at: datetime | None = None):
    """Preserve the entire forecast contract (zlib JSON), change-only on its values.

    model='merged' identifies this contract. Lead hours and fetch clocks do not
    make a new row; lead and hourly valid window are indexed on the rows written.
    """
    with conn:
        _run(conn, run_id, "weather", fetched_at, started_at, set(), set(), {g.sport for g in games})
        _games(conn, games, run_id, fetched_at)
        for fc in forecasts:
            if fc.retained:
                continue  # An old observation must not acquire a new receipt/run.
            hours = [p.t for p in fc.hourly]
            text = encode(asdict(fc))
            row = dict(
                game_id=fc.game_id, source=fc.source, model="merged", run_id=run_id,
                fetched_at=utc(fetched_at), run_time=utc(fc.run_time) if fc.run_time else None,
                lead_hours=fc.lead_hours, valid_start=utc(min(hours)) if hours else None,
                valid_end=utc(max(hours)) if hours else None, payload=zlib.compress(text.encode("utf-8"), 6),
            )
            head = conn.execute("SELECT payload FROM forecast_observations WHERE game_id=? AND source=? AND model=? "
                                "AND fetched_at<=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                                (fc.game_id, fc.source, "merged", row["fetched_at"])).fetchone()
            if head is None or forecast_content(head["payload"]) != forecast_content(text):
                insert(conn, "forecast_observations", row)
