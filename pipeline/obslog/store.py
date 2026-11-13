"""Append-only, standard-library storage. All public instants must be aware.

IDs are SHA256 of canonical, complete row content (excluding the ID itself).
An identical append is a no-op; a corrected payload at the same clock is a new
fact. Provider timestamps never replace our observation or knowledge clocks.

Schema v3 is change-only. A quote, forecast or game row is appended only when
its value differs from the latest logged one for that key. A (book, sport)
scrape that succeeded in full also writes small per-market ``coverage`` rows;
"this quote was still live and unchanged at T" is the last change row plus a
later coverage row with no logged row for the key in between (withdrawals are
rows). Unsuccessful or partial scrapes write no coverage and append every
present line, exactly as before, so they neither confirm nor withdraw anything
they did not literally observe.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import Game, GameLine, WeatherForecast

SCHEMA_VERSION = 3
MARKETS = ("ml", "spread", "total")
# Clocks, not values: they change on every scrape without the quote changing.
QUOTE_CLOCKS = ("scraped_at", "run_id", "source_updated_at")
# lead_hours is time-to-kickoff; the other two are provider fetch clocks.
FORECAST_CLOCKS = ("lead_hours", "point_source_updated_at", "ensemble_fetched_at")


class UnsupportedSchema(ValueError):
    """The cached log was written by an unknown (e.g. newer) schema version."""


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
    # Table/columns are exclusively internal constants, never caller input.
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


def _quote_row(line, run_id, now, capture_id):
    payload = asdict(line)
    row = {name: payload[name] for name in (
        "game_id", "sport", "book", "market", "side", "line", "odds", "prob_raw", "is_main",
        "source_id", "opening_line", "opening_odds", "opening_source",
    )}
    row.update({name: utc(payload[name]) if payload[name] is not None else None for name in (
        "source_updated_at", "expires_at", "actionable_until", "opened_at",
    )})
    row.update(capture_id=capture_id, run_id=run_id, observed_at=utc(line.scraped_at or now), quote_at=utc(line.scraped_at or now),
               recorded_at=utc(now), kind="quote", payload=encode(payload))
    return row


def _content(payload: str, clocks) -> dict:
    return {k: v for k, v in json.loads(payload).items() if k not in clocks}


def _heads(conn, book, sport, stamp):
    """Newest logged main row per (game, market, side) of one book/sport, via the quote_head index."""
    rows = conn.execute(
        "SELECT * FROM quote_observations WHERE rowid IN (SELECT rid FROM (SELECT rowid AS rid, "
        "row_number() OVER (PARTITION BY game_id, market, side ORDER BY recorded_at DESC, rowid DESC) AS n "
        "FROM quote_observations WHERE book=? AND sport=? AND is_main=1 AND recorded_at<=?) WHERE n=1)",
        (book, sport, stamp)).fetchall()
    return {(r["game_id"], r["market"], r["side"]): r for r in rows}


def _alt_head(conn, row, stamp):
    return conn.execute(
        "SELECT * FROM quote_observations WHERE game_id=? AND book=? AND market=? AND side=? AND is_main=0 "
        "AND line IS ? AND recorded_at<=? ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (row["game_id"], row["book"], row["market"], row["side"], row["line"], stamp)).fetchone()


def _ttl(row):
    """Expiry as an offset from the observation clock: rolling-expiry quotes (BetCris) keep it fixed."""
    at = instant(row["observed_at"])
    return tuple((instant(row[f]) - at) if row[f] else None for f in ("expires_at", "actionable_until"))


def _changed(head, row):
    """A quote changed when its value or expiry offset did; scrape clocks alone are not a change.

    Queries shift a confirmed quote's expires_at/actionable_until by the same
    amount as its confirmation clock, so equal offsets mean equal meaning.
    """
    if head is None or head["kind"] != "quote":
        return True
    clocks = QUOTE_CLOCKS + ("expires_at", "actionable_until")
    return _content(head["payload"], clocks) != _content(row["payload"], clocks) or _ttl(head) != _ttl(row)


def _withdrawals(conn, seen, heads, run_id, now, capture_id):
    """Withdraw main quotes missing from a successful (book, sport) scrape.

    ``heads`` are the newest main rows per key for the books/sports that
    actually succeeded, read in SQL through an index, not the whole log.
    """
    stamp = utc(now)
    for _group, group in sorted(heads.items()):
        for previous in group.values():
            if previous["kind"] != "quote" or key_of(previous) in seen:
                continue
            row = dict(previous)
            row.pop("id")
            row.update(capture_id=capture_id, run_id=run_id, observed_at=stamp, recorded_at=stamp, kind="withdrawn")
            insert(conn, "quote_observations", row)


def _coverage(conn, lines, successful, run_id, now, capture_id):
    """One small row per (book, sport, market): when this full scrape observed it."""
    for book, sport in sorted(successful):
        group = [ln for ln in lines if ln.book == book and ln.sport == sport]
        every = min((ln.scraped_at or now for ln in group), default=now)
        for market in MARKETS:
            clock = min((ln.scraped_at or now for ln in group if ln.market == market), default=every)
            insert(conn, "coverage", dict(capture_id=capture_id, run_id=run_id, book=book, sport=sport,
                                          market=market, observed_at=utc(clock), recorded_at=utc(now)))


def _append_lines(conn, lines, successful, run_id, now, capture):
    stamp = utc(now)
    heads = {group: _heads(conn, *group, stamp) for group in successful}
    alts = {}
    for line in lines:
        row = _quote_row(line, run_id, now, capture)
        group = (line.book, line.sport)
        if group not in successful:
            insert(conn, "quote_observations", row)  # unsuccessful scrape: log what was seen, as always
            continue
        if line.is_main:
            slot = heads[group]
            k = (line.game_id, line.market, line.side)
            if _changed(slot.get(k), row):
                insert(conn, "quote_observations", row)
                slot[k] = dict(row)
        else:
            k = (line.game_id, line.book, line.market, line.side, line.line)
            if k not in alts:
                alts[k] = _alt_head(conn, row, stamp)
            if _changed(alts[k], row):
                insert(conn, "quote_observations", row)
                alts[k] = dict(row)
    return heads


def append_odds(conn: sqlite3.Connection, lines: list[GameLine], *, run_id: str, observed_at: datetime,
                games: list[Game] = (), successful: set[tuple[str, str]] = frozenset(),
                attempted: set[tuple[str, str]] = frozenset(), started_at: datetime | None = None):
    """One atomic receipt: facts, quotes, successful empty coverage, withdrawals.

    capture_id groups the batch, including alternate lines; pairs require a
    common capture AND canonical number. successful is explicit, never inferred
    from absence of lines. Unsuccessful/unscraped pairs cannot withdraw anything.
    Lines of a successful (book, sport) are appended only when they differ from
    the latest logged value; the run's coverage rows confirm the rest.
    """
    capture = identity((run_id, utc(observed_at), "odds"))
    successful = set(successful)
    with conn:
        _run(conn, run_id, "odds", observed_at, started_at, attempted | successful, successful, {g.sport for g in games} | {ln.sport for ln in lines})
        _games(conn, games, run_id, observed_at)
        heads = _append_lines(conn, lines, successful, run_id, observed_at, capture)
        _coverage(conn, lines, successful, run_id, observed_at, capture)
        _withdrawals(conn, {QuoteKey(x.game_id, x.book, x.market, x.side) for x in lines if x.is_main}, heads, run_id, observed_at, capture)


def record_withdrawals(conn, lines, successful, *, run_id: str, observed_at: datetime):
    """Standalone successful-coverage receipt; prefer append_odds for atomicity."""
    capture = identity((run_id, utc(observed_at), "coverage"))
    successful = set(successful)
    with conn:
        _run(conn, run_id, "coverage", observed_at, None, successful, successful)
        heads = {group: _heads(conn, *group, utc(observed_at)) for group in successful}
        _coverage(conn, lines, successful, run_id, observed_at, capture)
        _withdrawals(conn, {QuoteKey(x.game_id, x.book, x.market, x.side) for x in lines if x.is_main}, heads, run_id, observed_at, capture)


def append_forecasts(conn: sqlite3.Connection, forecasts: list[WeatherForecast], *, run_id: str,
                     fetched_at: datetime, games: list[Game] = (), started_at: datetime | None = None):
    """Preserve the entire forecast contract, including hourly/model windows.

    model='merged' identifies this contract. Component model data retains its
    own clocks in payload; lead and hourly valid window are indexed explicitly.
    """
    with conn:
        _run(conn, run_id, "weather", fetched_at, started_at, set(), set(), {g.sport for g in games})
        _games(conn, games, run_id, fetched_at)
        for fc in forecasts:
            if fc.retained:
                continue  # An old observation must not acquire a new receipt/run.
            hours = [p.t for p in fc.hourly]
            row = dict(
                game_id=fc.game_id, source=fc.source, model="merged", run_id=run_id,
                fetched_at=utc(fetched_at), run_time=utc(fc.run_time) if fc.run_time else None,
                lead_hours=fc.lead_hours, valid_start=utc(min(hours)) if hours else None,
                valid_end=utc(max(hours)) if hours else None, payload=encode(asdict(fc)),
            )
            head = conn.execute("SELECT payload FROM forecast_observations WHERE game_id=? AND source=? AND model=? "
                                "AND fetched_at<=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                                (fc.game_id, fc.source, "merged", row["fetched_at"])).fetchone()
            if head is None or _content(head["payload"], FORECAST_CLOCKS) != _content(row["payload"], FORECAST_CLOCKS):
                insert(conn, "forecast_observations", row)
