"""Cheap physical guards for values that cannot be a real game-window signal."""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# A football stadium hour above this is a decode/unit error, not weather.
# The US hourly record is well below a multi-metre total; 2,172 mm in one
# 3 h window showed up as a long-range data error. Tunable via
# ``WEATHER_MAX_HOURLY_PRECIP_MM``.
DEFAULT_MAX_HOURLY_PRECIP_MM = 150.0
_flagged: set[int] = set()


def max_hourly_precip_mm() -> float:
    raw = os.environ.get("WEATHER_MAX_HOURLY_PRECIP_MM", "").strip()
    if not raw:
        return DEFAULT_MAX_HOURLY_PRECIP_MM
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_HOURLY_PRECIP_MM


def plausible_precip_mm(value: Optional[float]) -> Optional[float]:
    """Keep a non-negative hourly millimetre amount. Drop anything past the cap.

    The dropped value is a data error: callers see ``None`` and do not treat
    it as rain. The first few distinct magnitudes are logged.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < 0:
        return 0.0
    limit = max_hourly_precip_mm()
    if number > limit:
        bucket = int(number)
        if bucket not in _flagged and len(_flagged) < 12:
            log.warning(
                "dropping implausible precipitation %.1f mm (hourly limit %.1f mm); treating it as a data error",
                number, limit,
            )
        _flagged.add(bucket)
        return None
    return number
