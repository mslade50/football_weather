"""Single source of truth for "which quote is the main line".

Parsers flag ``is_main`` per row, but several books can flag more than one rung
(ProphetX marks every line main when the feed omits ``favourite``).  Every
downstream consumer (consensus, fair edges, the board's odds block, openers)
must agree on one pair per (game, book, market), so ``select_main_lines`` runs
once right after matching and leaves exactly one ``is_main`` pair behind.

Only rows the parser flagged ``is_main`` are eligible: a book with no main
contributes no main line (alternates are never promoted).  Pure and
deterministic -- the result does not depend on input order.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timezone

from pipeline.contracts import GameLine
from pipeline.model import fair

_PRIMARY = {"spread": "home", "total": "over", "ml": "home"}
_OPPOSITE = {"home": "away", "away": "home", "over": "under", "under": "over"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def pair_key(ln: GameLine) -> float | None:
    """Home-relative spread number / total number; ``None`` for ml or no line."""
    if ln.market == "ml" or ln.line is None:
        return None
    return round(-ln.line if ln.market == "spread" and ln.side == "away" else ln.line, 2)


def _ts(ln: GameLine) -> float:
    dt = ln.scraped_at
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH).total_seconds()


def _recency(ln: GameLine) -> tuple[float, str, int]:
    return (_ts(ln), ln.source_id or "", ln.odds if ln.odds is not None else 0)


def _balance(sides: dict[str, GameLine]) -> float:
    """|p_primary - 0.5| of the devigged pair; ``inf`` when it cannot be priced."""
    if len(sides) < 2:
        return float("inf")
    primary = next((s for s in ("home", "over") if s in sides), None)
    if primary is None:
        return float("inf")
    a, b = sides[primary], sides.get(_OPPOSITE[primary])
    try:
        if a.odds is None and a.prob_raw is None:
            return float("inf")
        return abs(fair.vigfree_prob(a, b) - 0.5)
    except (TypeError, ValueError):
        return float("inf")


def _pair_rank(key: float | None, sides: dict[str, GameLine]) -> tuple:
    oldest = min((ln.source_id or "", _ts(ln)) for ln in sides.values())
    return (0 if len(sides) >= 2 else 1, _balance(sides), abs(key) if key is not None else 0.0,
            oldest, key is not None, key or 0.0)


def select_main_lines(lines: Sequence[GameLine]) -> list[GameLine]:
    """All input rows in original order; ``is_main`` stays True only on the one
    chosen pair per (game, book, market)."""
    rows = list(lines)
    groups: dict[tuple[str, str, str], dict[float | None, dict[str, list[int]]]] = {}
    for i, ln in enumerate(rows):
        if ln.is_main:
            groups.setdefault((ln.game_id, ln.book, ln.market), {}).setdefault(pair_key(ln), {}).setdefault(ln.side, []).append(i)
    keep: set[int] = set()
    for pairs in groups.values():
        best: tuple | None = None
        best_sides: dict[str, int] = {}
        for key, by_side in pairs.items():
            sides = {s: rows[max(idx, key=lambda i: _recency(rows[i]))] for s, idx in by_side.items()}
            rank = _pair_rank(key, sides)
            if best is None or rank < best:
                best = rank
                best_sides = {s: max(idx, key=lambda i: _recency(rows[i])) for s, idx in by_side.items()}
        keep.update(best_sides.values())
    return [ln if (not ln.is_main or i in keep) else dataclasses.replace(ln, is_main=False)
            for i, ln in enumerate(rows)]


__all__ = ["pair_key", "select_main_lines"]
