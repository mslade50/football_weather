"""Small parser primitives; provider-specific acceptance rules stay explicit."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, TypeVar

from pipeline.contracts import GameLine
from utils.numbers import finite_number as finite_number
from utils.timeutil import parse_iso_utc

# Both binary exchanges reject a BBO wider than 25 cents before pricing it.
MAX_QUOTE_WIDTH = 0.25


def positive_zero(value: float | None) -> float | None:
    return 0.0 if value == 0 else value


def parse_utc(value: object, *, naive_utc: bool = True) -> datetime | None:
    """ISO with Z handling; reject naive provider clocks when requested.

    Unlike timeutil.parse_iso this does not strip whitespace, preserving the
    parsers' existing acceptance rules. UTC normalization uses timeutil.
    """
    return parse_iso_utc(value, naive_utc=naive_utc, strings_only=True)


def valid_lines(lines: Iterable[GameLine | None]) -> list[GameLine]:
    """Keep factory successes in order; game_line still owns drop accounting."""
    return [line for line in lines if line is not None]


class _GameIdentity(Protocol):
    @property
    def game_id(self) -> str: ...


_Game = TypeVar("_Game", bound=_GameIdentity)


def dedupe_games(games: Iterable[_Game]) -> list[_Game]:
    """Drop repeated provisional identities, preserving the first record."""
    seen: set[str] = set()
    out: list[_Game] = []
    for game in games:
        if game.game_id not in seen:
            seen.add(game.game_id)
            out.append(game)
    return out
