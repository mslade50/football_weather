"""First observed BetOnline totals in the weekly Eastern-time opening window."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from pipeline import state
from pipeline.contracts import Game, GameLine
from utils.timeutil import to_et, utc_iso


def opening_window(game: Game) -> tuple[datetime, datetime]:
    """Football weeks run Tuesday through Monday; open on the preceding Sunday."""
    kickoff = to_et(game.kickoff_utc)
    tuesday = kickoff - timedelta(days=(kickoff.weekday() - 1) % 7)
    sunday = (tuesday - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    if game.sport == "cfb":
        return sunday + timedelta(hours=12), sunday + timedelta(days=1)
    return sunday + timedelta(hours=18), sunday + timedelta(days=1, hours=12)


def record_weekly_totals(openers: dict, history: dict, lines: Sequence[GameLine],
                         games: Sequence[Game], now: datetime) -> None:
    """Preserve first qualifying observations, including unchanged live scrapes.

    Earlier/later weeks and other books cannot supply a fallback. Historical
    change points can backfill a known observation; absent observations stay unknown.
    """
    store = openers.setdefault("weekly_totals", {})
    for game in games:
        start, end = opening_window(game)
        points = []

        def add(ts, line, side, odds, *, start=start, end=end, points=points):
            stamp = state.parse_utc(ts)
            if (stamp is not None and start <= stamp < end and stamp <= now
                    and isinstance(line, (int, float)) and not isinstance(line, bool) and math.isfinite(line)
                    and isinstance(odds, (int, float)) and not isinstance(odds, bool)
                    and math.isfinite(odds) and odds != 0):
                points.append((stamp, line, side, odds))

        prior = store.get(game.game_id) or {}
        for side in ("under", "over"):
            add(prior.get("ts"), prior.get("line"), side, prior.get(side))
            key = state.odds_key(game.game_id, "total", side, "betonline")
            first = state.get_opener(openers, key) or {}
            add(first.get("ts"), first.get("line"), side, first.get("odds"))
            for point in (history.get("series") or {}).get(key, []):
                if isinstance(point, (list, tuple)) and len(point) >= 3:
                    add(point[0], point[1], side, point[2])
        for quote in lines:
            if (quote.game_id == game.game_id and quote.book == "betonline"
                    and quote.market == "total" and quote.is_main and quote.side in ("under", "over")):
                add(quote.scraped_at or now, quote.line, quote.side, quote.odds)

        chosen = min(points, key=lambda p: (p[0], p[2] != "under")) if points else None
        value = {"book": "betonline", "line": chosen[1] if chosen else None,
                 "under": None, "over": None, "ts": utc_iso(chosen[0]) if chosen else None,
                 "window_start": utc_iso(start), "window_end": utc_iso(end)}
        if chosen:
            for stamp, line, side, odds in points:
                if stamp == chosen[0] and line == chosen[1]:
                    value[side] = odds
        store[game.game_id] = value
