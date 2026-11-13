"""Schedule status vocabulary and the pre-kickoff eligibility policy.

Raw status remains published for display/history. Old snapshots without a code
are normalized only here; consumers never interpret provider wording themselves.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum

from utils.timeutil import ensure_utc, parse_iso


class GameStatus(str, Enum):
    SCHEDULED = "scheduled"
    TBD = "tbd"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"
    UNSPECIFIED = "unspecified"


def normalize_status(value) -> GameStatus:
    """Normalize source status at ingestion (also the single legacy-card fallback)."""
    text = str(value or '').lower()
    for pattern, code in (
        ('final', GameStatus.FINAL),
        ('live|progress|in_play|half', GameStatus.LIVE),
        ('cancel', GameStatus.CANCELLED),
        ('postpon', GameStatus.POSTPONED),
        ('suspend', GameStatus.SUSPENDED),
        ('tbd', GameStatus.TBD),
        ('unknown', GameStatus.UNKNOWN),
    ):
        if re.search(pattern, text):
            return code
    return GameStatus.SCHEDULED if text == 'scheduled' else GameStatus.UNSPECIFIED


BETTABLE = (GameStatus.SCHEDULED, GameStatus.TBD, GameStatus.UNKNOWN, GameStatus.UNSPECIFIED)


def _recognized(code) -> bool:
    try:
        GameStatus(code)
    except ValueError:
        return False
    return True


def status_code(game) -> GameStatus:
    get = game.get if isinstance(game, Mapping) else lambda key: getattr(game, key, None)
    code = get('status_code')
    if code is not None:
        # Display only. bettable_before_kickoff rejects an unrecognized code outright.
        return GameStatus(code) if _recognized(code) else GameStatus.UNKNOWN
    return normalize_status(get('status'))


def bettable_before_kickoff(game, now: datetime) -> bool:
    """Preserve TBD/unknown eligibility; require a future kickoff and no stopped/live state.

    A published ``status_code`` outside the vocabulary (including an empty
    string) is not bettable. ``site/web/game-status.js`` and
    ``site/worker/game-status.js`` make the same choice, pinned by
    ``tests/fixtures/game_status_table.json``. Only a missing code falls back
    to the raw ``status`` text.
    """
    get = game.get if isinstance(game, Mapping) else lambda key: getattr(game, key, None)
    kickoff = get('kickoff_utc')
    try:
        kickoff = ensure_utc(kickoff) if isinstance(kickoff, datetime) else ensure_utc(parse_iso(kickoff))
    except (TypeError, ValueError, AttributeError):
        return False
    published = get('status_code')
    if published is not None and not _recognized(published):
        return False
    return kickoff > ensure_utc(now) and status_code(game) in BETTABLE
