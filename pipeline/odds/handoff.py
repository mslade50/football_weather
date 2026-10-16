"""Scraped-line handoff between the parallel Playwright scrape job and the publish job.

The ``playwright_scrape`` CI job runs ``pipeline.build --scrape-only DIR``: it scrapes
the Playwright books (raw captures land in ``data/raw_runs/`` as usual, raw-first)
and writes one handoff file per sport/book::

    DIR/{sport}/{book}.json
    {"schema_version": 1, "sport", "book", "run_id", "written_at",
     "fetch_errors": {...}, "games": [RawGame...], "lines": [GameLine...]}

``games`` keeps the scraper's raw team strings / kickoff / neutral flag that
``pipeline.odds.merge.raw_games_from_scraper`` would read off a live scraper, so the
publish job resolves provisional ids exactly as an in-process scrape would.

The publish job (``pipeline.build --ingest-lines DIR``) loads these instead of
scraping. Every line keeps its original ``scraped_at``: it is what ``archive_last``
``ts``, ``ODDS_CARRY_MAX_AGE`` and ``pipeline.current_quotes`` judge, so a line is
never presented fresher than when it was seen. Lines older than ``max_age`` (or
expired, undated, or implausibly in the future) are rejected at ingest.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline.contracts import GameLine
from pipeline.state import parse_utc
from utils.timeutil import utc_iso

SCHEMA_VERSION = 1
# Runner clocks are NTP-synced; allow a little skew before calling a line "future".
FUTURE_SKEW = timedelta(minutes=2)
_DATETIME_FIELDS = frozenset(f.name for f in fields(GameLine) if f.name in {
    "scraped_at", "source_updated_at", "expires_at", "opened_at"})


def handoff_path(base: Path, sport: str, book: str) -> Path:
    return Path(base) / sport / f"{book}.json"


def _line_dict(line: GameLine) -> dict[str, Any]:
    out = line.to_dict()
    for name in _DATETIME_FIELDS:
        if out.get(name) is not None:
            out[name] = utc_iso(out[name])
    return out


def _raw_game_dict(g: Any) -> dict[str, Any]:
    kick = getattr(g, "kickoff_utc", None)
    return {"game_id": str(g.game_id), "away": str(g.away), "home": str(g.home),
            "kickoff_utc": utc_iso(kick) if isinstance(kick, datetime) else None,
            "neutral": bool(getattr(g, "neutral", False))}


def dump_lines(base: Path, sport: str, book: str, lines: list[GameLine], *, run_id: str | None,
               raw_games: Sequence[Any] = (), fetch_errors: dict[str, str] | None = None,
               now: datetime | None = None) -> Path:
    """Write one sport/book handoff file (atomic rename); returns its path."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sport": sport,
        "book": book,
        "run_id": run_id,
        "written_at": utc_iso(now) if now is not None else utc_iso(),
        "fetch_errors": dict(fetch_errors or {}),
        "games": [_raw_game_dict(g) for g in raw_games],
        "lines": [_line_dict(ln) for ln in lines if ln.sport == sport and ln.book == book],
    }
    path = handoff_path(base, sport, book)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, allow_nan=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


@dataclass
class Ingested:
    """One sport/book handoff after freshness filtering."""

    book: str
    lines: list[GameLine] = field(default_factory=list)
    scraper: Any = None              # stub exposing last_games / fetch_errors like a live scraper
    run_id: str | None = None
    missing: bool = False            # no / unreadable handoff file
    rejected_stale: int = 0          # older than max_age, or future-dated
    rejected_expired: int = 0        # provider expiry already passed
    rejected_invalid: int = 0        # undated or failed GameLine validation
    problems: list[str] = field(default_factory=list)


def _line_from_dict(row: dict[str, Any]) -> GameLine:
    kw = {f.name: row[f.name] for f in fields(GameLine) if f.name in row}
    for name in _DATETIME_FIELDS:
        if kw.get(name) is not None:
            kw[name] = parse_utc(kw[name])
    kw["outcome_ids"] = dict(kw.get("outcome_ids") or {})
    return GameLine(**kw)


def load_lines(base: Path, sport: str, book: str, *, now: datetime, max_age: timedelta) -> Ingested:
    """Read a handoff file and keep only lines that are still current at ``now``.

    Never raises: a missing/corrupt/mismatched file comes back as ``missing=True`` with
    the reason in ``problems`` so the caller can degrade (0 lines, like a dark scrape).
    """
    out = Ingested(book=book)
    path = handoff_path(base, sport, book)

    def missing(reason: str) -> Ingested:
        # Book health records this like a failed scrape (0 lines + fetch error).
        out.missing = True
        out.problems.append(reason)
        out.scraper = SimpleNamespace(last_games=[], fetch_errors={sport: reason})
        return out

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing(f"no handoff file {path.as_posix()}")
    except (OSError, ValueError) as exc:
        return missing(f"unreadable handoff {path.name}: {type(exc).__name__}")
    if (not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("sport") != sport or payload.get("book") != book):
        return missing(f"handoff {path.name} has an unexpected schema/sport/book")
    out.run_id = payload.get("run_id")
    games = []
    for g in payload.get("games") or []:
        if isinstance(g, dict) and g.get("game_id") and g.get("away") and g.get("home"):
            games.append(SimpleNamespace(game_id=str(g["game_id"]), away=str(g["away"]), home=str(g["home"]),
                                         kickoff_utc=parse_utc(g.get("kickoff_utc")),
                                         neutral=bool(g.get("neutral", False))))
    errors = payload.get("fetch_errors") or {}
    out.scraper = SimpleNamespace(last_games=games, fetch_errors=dict(errors) if isinstance(errors, dict) else {})
    for row in payload.get("lines") or []:
        try:
            line = _line_from_dict(row)
        except (TypeError, ValueError, KeyError):
            out.rejected_invalid += 1
            continue
        if line.sport != sport or line.book != book or line.scraped_at is None:
            out.rejected_invalid += 1
            continue
        age = now - line.scraped_at
        if age > max_age or age < -FUTURE_SKEW:
            out.rejected_stale += 1
            continue
        if line.expires_at is not None and now > line.expires_at:
            out.rejected_expired += 1
            continue
        out.lines.append(line)
    return out


__all__ = ["SCHEMA_VERSION", "FUTURE_SKEW", "Ingested", "dump_lines", "handoff_path", "load_lines"]
