"""Append-only, standard-library storage. All public instants must be aware.

IDs are SHA256 of canonical, complete row content (excluding the ID itself).
An identical append is a no-op; a corrected payload at the same clock is a new
fact. Different runs/receipt clocks retain unchanged observations. Provider
timestamps never replace our observation or knowledge clocks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import Game, GameLine, WeatherForecast


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
        if existing and [r[0] for r in conn.execute("SELECT version FROM schema_version")] not in ([1], [2]):
            raise UnsupportedSchema("unsupported observation-log schema version")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        conn.executescript("BEGIN IMMEDIATE;\n" + schema + "\nDELETE FROM schema_version;\n"
                           "INSERT INTO schema_version VALUES (2);\nCOMMIT;")
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
        insert(conn, "game_observations", dict(
            game_id=game.game_id, observed_at=utc(now), run_id=run_id,
            kickoff=utc(game.kickoff_utc), sport=game.sport, season=game.season,
            week=game.week, payload=encode(asdict(game)),
        ))


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


def _quote(conn, line, run_id, now, capture_id):
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
    insert(conn, "quote_observations", row)


def _withdrawals(conn, lines, successful, run_id, now, capture_id):
    """Withdraw main quotes missing from a successful (book, sport) scrape.

    Reads only the newest main row per key for the books/sports that actually
    succeeded, in SQL, instead of materializing the whole log.
    """
    seen = {QuoteKey(x.game_id, x.book, x.market, x.side) for x in lines if x.is_main}
    stamp = utc(now)
    for book, sport in sorted(successful):
        rows = conn.execute(
            "SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY game_id, book, market, side "
            "ORDER BY observed_at DESC, recorded_at DESC, id DESC) AS n FROM quote_observations "
            "WHERE is_main=1 AND book=? AND sport=? AND observed_at<=? AND recorded_at<=?) "
            "WHERE n=1 AND kind='quote'", (book, sport, stamp, stamp)).fetchall()
        for previous in rows:
            if key_of(previous) in seen:
                continue
            row = dict(previous)
            row.pop("id"), row.pop("n")
            row.update(capture_id=capture_id, run_id=run_id, observed_at=stamp, recorded_at=stamp, kind="withdrawn")
            insert(conn, "quote_observations", row)


def append_odds(conn: sqlite3.Connection, lines: list[GameLine], *, run_id: str, observed_at: datetime,
                games: list[Game] = (), successful: set[tuple[str, str]] = frozenset(),
                attempted: set[tuple[str, str]] = frozenset(), started_at: datetime | None = None):
    """One atomic receipt: facts, quotes, successful empty coverage, withdrawals.

    capture_id groups the batch, including alternate lines; pairs require a
    common capture AND canonical number. successful is explicit, never inferred
    from absence of lines. Unsuccessful/unscraped pairs cannot withdraw anything.
    """
    capture = identity((run_id, utc(observed_at), "odds"))
    with conn:
        _run(conn, run_id, "odds", observed_at, started_at, attempted | successful, successful, {g.sport for g in games} | {ln.sport for ln in lines})
        _games(conn, games, run_id, observed_at)
        for line in lines:
            _quote(conn, line, run_id, observed_at, capture)
        _withdrawals(conn, lines, successful, run_id, observed_at, capture)


def record_withdrawals(conn, lines, successful, *, run_id: str, observed_at: datetime):
    """Standalone successful-coverage receipt; prefer append_odds for atomicity."""
    with conn:
        _run(conn, run_id, "coverage", observed_at, None, successful, successful)
        _withdrawals(conn, lines, successful, run_id, observed_at, identity((run_id, utc(observed_at), "coverage")))


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
            insert(conn, "forecast_observations", dict(
                game_id=fc.game_id, source=fc.source, model="merged", run_id=run_id,
                fetched_at=utc(fetched_at), run_time=utc(fc.run_time) if fc.run_time else None,
                lead_hours=fc.lead_hours, valid_start=utc(min(hours)) if hours else None,
                valid_end=utc(max(hours)) if hours else None, payload=encode(asdict(fc)),
            ))
