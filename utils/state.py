"""Run-to-run state tracking — detects new/changed markets between runs.

Adapted from golf_scraping/utils/state.py. The unit of identity is
``game_key(game_id, market, book)`` instead of the golf matchup key; the
"tournament" grouping becomes the per-sport ``season:week`` scope embedded in
``game_id`` (``{sport}:{season}:{week}:{away}@{home}``).
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/state.json")
ET = ZoneInfo("America/New_York")

_KEY_SEP = "|"


def game_key(game_id: str, market: str, book: str) -> str:
    """Identity for a single (game, market, book) line: ``game_id|market|book``.

    ``game_id`` already encodes sport/season/week/away/home, so a key the previous
    run didn't have means a fresh market was posted (a book coming online, a new
    week's slate opening, totals appearing after spreads).
    """
    return _KEY_SEP.join([game_id or "", market or "", book or ""])


def _key_scope(key: str) -> str:
    """``sport:season:week`` scope of a key (the football analogue of tournament)."""
    game_id = key.split(_KEY_SEP)[0]
    parts = game_id.split(":")
    return ":".join(parts[:3]) if len(parts) >= 3 else game_id


def _line_fields(line: Any) -> tuple[str, str, str]:
    """Pull (game_id, market, book) from a GameLine or a plain dict."""
    if isinstance(line, dict):
        return str(line.get("game_id") or ""), str(line.get("market") or ""), str(line.get("book") or "")
    return (
        str(getattr(line, "game_id", "") or ""),
        str(getattr(line, "market", "") or ""),
        str(getattr(line, "book", "") or ""),
    )


@dataclass
class RunDelta:
    """What changed since the last run."""
    new_scopes: list[str]
    removed_scopes: list[str]
    prev_total: int
    curr_total: int
    counts_by_book: dict[str, int]
    counts_by_scope: dict[str, int]
    is_first_run: bool = False
    new_keys: list[str] = field(default_factory=list)

    @property
    def has_new_markets(self) -> bool:
        return bool(self.new_scopes) or bool(self.new_keys)

    @property
    def significant_change(self) -> bool:
        return abs(self.curr_total - self.prev_total) > 5

    @property
    def new_key_counts(self) -> dict[str, int]:
        """New keys grouped as ``{'book market': count}`` for alerts."""
        counts: dict[str, int] = {}
        for key in self.new_keys:
            parts = key.split(_KEY_SEP)
            market = parts[1] if len(parts) > 1 and parts[1] else "?"
            book = parts[2] if len(parts) > 2 and parts[2] else "?"
            label = f"{book} {market}"
            counts[label] = counts.get(label, 0) + 1
        return counts


def load_state(path: Path = STATE_FILE) -> Optional[dict]:
    """Load previous run state from disk."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read state file: {e}")
        return None


def today_et() -> str:
    """Return today's date in ET timezone as YYYY-MM-DD."""
    return datetime.now(ET).strftime("%Y-%m-%d")


def already_succeeded_today(path: Path = STATE_FILE) -> bool:
    state = load_state(path)
    if state is None:
        return False
    return state.get("last_success_date") == today_et()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_state(lines: Iterable[Any], path: Path = STATE_FILE) -> None:
    """Persist current run state to disk.

    The ``keys`` set is *accumulated* across runs rather than overwritten: prior
    keys for any scope (sport:season:week) still active this run are carried and
    unioned with the current run's keys. Required because the light (httpx) job
    and the Playwright job each call save_state with only their own books.
    """
    lines = list(lines)
    scopes: set[str] = set()
    counts_by_book: dict[str, int] = {}
    counts_by_scope: dict[str, int] = {}
    counts_by_market: dict[str, int] = {}
    curr_keys: set[str] = set()

    for ln in lines:
        game_id, market, book = _line_fields(ln)
        key = game_key(game_id, market, book)
        curr_keys.add(key)
        scope = _key_scope(key)
        scopes.add(scope)
        counts_by_book[book] = counts_by_book.get(book, 0) + 1
        counts_by_scope[scope] = counts_by_scope.get(scope, 0) + 1
        counts_by_market[market] = counts_by_market.get(market, 0) + 1

    prev = load_state(path) or {}

    if not lines:
        prev["last_run"] = datetime.now(ET).isoformat()
        _write(path, prev)
        return

    prev_keys = set(prev.get("keys", []))
    carried = {k for k in prev_keys if _key_scope(k) in scopes}
    keys = sorted(carried | curr_keys)

    state = {
        "last_run": datetime.now(ET).isoformat(),
        "total": len(lines),
        "scopes": sorted(scopes),
        "counts_by_book": counts_by_book,
        "counts_by_scope": counts_by_scope,
        "counts_by_market": counts_by_market,
        "keys": keys,
    }
    _write(path, state)


def compute_delta(lines: Iterable[Any], path: Path = STATE_FILE) -> RunDelta:
    """Compare current lines against previous state to find changes."""
    lines = list(lines)
    prev = load_state(path)

    counts_by_book: dict[str, int] = {}
    counts_by_scope: dict[str, int] = {}
    curr_scopes: set[str] = set()
    curr_keys: set[str] = set()

    for ln in lines:
        game_id, market, book = _line_fields(ln)
        key = game_key(game_id, market, book)
        curr_keys.add(key)
        scope = _key_scope(key)
        curr_scopes.add(scope)
        counts_by_book[book] = counts_by_book.get(book, 0) + 1
        counts_by_scope[scope] = counts_by_scope.get(scope, 0) + 1

    if prev is None:
        return RunDelta(
            new_scopes=sorted(curr_scopes),
            removed_scopes=[],
            prev_total=0,
            curr_total=len(lines),
            counts_by_book=counts_by_book,
            counts_by_scope=counts_by_scope,
            is_first_run=True,
            new_keys=sorted(curr_keys),
        )

    prev_scopes = set(prev.get("scopes", []))
    prev_keys = set(prev.get("keys", []))

    return RunDelta(
        new_scopes=sorted(curr_scopes - prev_scopes),
        removed_scopes=sorted(prev_scopes - curr_scopes),
        prev_total=prev.get("total", 0),
        curr_total=len(lines),
        counts_by_book=counts_by_book,
        counts_by_scope=counts_by_scope,
        new_keys=sorted(curr_keys - prev_keys),
    )
