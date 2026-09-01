"""Map dot rules ported from pages/nfl_weather.py, pages/cfb_weather.py,
pages/combined_signals.py (ARCH §7.4). Pure functions on plain floats; None /
NaN inputs never match a condition (mirrors pandas NaN comparisons).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from pipeline.model import config as C

NO = "No Impact"
LOW = "Low Impact"
MID = "Mid Impact"
HIGH = "High Impact"
VERY_HIGH = "Very High Impact"


@dataclass(frozen=True)
class Signal:
    level: str
    color: str
    size: int
    label: str = ""
    drivers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.level)


def _f(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _gt(a: Optional[float], b: float) -> bool:
    return a is not None and a > b


def _lt(a: Optional[float], b: float) -> bool:
    return a is not None and a < b


def _between(a: Optional[float], lo: float, hi: float) -> bool:
    return a is not None and lo <= a <= hi


# ---- NFL ---------------------------------------------------------------------

def nfl_signal(wind_fg: Optional[float], temp_fg: Optional[float], rain_fg: Optional[float]) -> Signal:
    """Evaluated purple-first so High is reachable (legacy ordering made it dead code)."""
    w, t, r = _f(wind_fg), _f(temp_fg), _f(rain_fg)
    if _gt(w, 15) and _between(t, 32, 45):
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH], drivers=("wind",))
    if _gt(r, 2) or (w is not None and 8 < w < 15 and _lt(t, 60)):
        driver = "rain" if _gt(r, 2) else "wind"
        return Signal(LOW, "blue", C.SIGNAL_SIZES[LOW], drivers=(driver,))
    if _gt(w, 15) and _lt(t, 60):
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID], drivers=("wind",))
    return Signal(NO, "green", C.SIGNAL_SIZES[NO])


def nfl_wind_vol(wind_vol_static: Optional[str], wind_fg: Optional[float]) -> Optional[str]:
    w = _f(wind_fg)
    if w is not None and w < C.NFL_WIND_VOL_LOW_BELOW:
        return "Low"
    return wind_vol_static


def wind_diff(wind_fg: Optional[float], avg_wind: Optional[float]) -> Optional[float]:
    w, a = _f(wind_fg), _f(avg_wind)
    if w is None or a is None:
        return None
    return w - a


# ---- CFB ---------------------------------------------------------------------

def cfb_low_wind_threshold(weekday: int) -> float:
    """weekday: Monday=0 .. Sunday=6 (ET day of the run)."""
    return C.CFB_DOW_LOW_WIND.get(weekday, C.CFB_DOW_DEFAULT)


def cfb_altitude_mid_trigger(
    temp_fg: Optional[float],
    open_spread: Optional[float],
    travel_alt: Optional[float],
) -> bool:
    """Whether the legacy CFB altitude-plus-warmth rule contributes a Mid signal."""
    t, sp, alt = _f(temp_fg), _f(open_spread), _f(travel_alt)
    return _gt(alt, 800) and _gt(t, 75) and _between(sp, -20.5, 20.5)


def cfb_signal(
    wind_fg: Optional[float],
    temp_fg: Optional[float],
    rain_fg: Optional[float],
    open_spread: Optional[float],
    travel_alt: Optional[float],
    home_temp: Optional[float],
    away_temp: Optional[float],
    weekday: int,
) -> Signal:
    w, t, r = _f(wind_fg), _f(temp_fg), _f(rain_fg)
    sp, alt = _f(open_spread), _f(travel_alt)
    ht, at = _f(home_temp), _f(away_temp)
    base = cfb_low_wind_threshold(weekday)
    hi = base + C.CFB_HIGH_OFFSET
    tight = _between(sp, -10.5, 10.5)
    wide = _between(sp, -20.5, 20.5)

    rain_cond = _gt(r, 2)
    heat_cond = _gt(t, 80) and _lt(ht, 57) and _lt(at, 57)
    altitude_mid = cfb_altitude_mid_trigger(t, sp, alt)

    if _gt(w, hi) and _lt(t, 50) and tight:
        return Signal(VERY_HIGH, "darkred", C.SIGNAL_SIZES[VERY_HIGH], drivers=("wind",))
    if _gt(w, hi) and _lt(t, 65) and tight:
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH], drivers=("wind",))
    if (_gt(w, hi) and _lt(t, 65) and wide) or altitude_mid:
        driver = "altitude_warmth" if altitude_mid else "wind"
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID], drivers=(driver,))
    if ((_gt(w, base) and _lt(t, 65)) or rain_cond or heat_cond) and wide:
        if rain_cond:
            return Signal(LOW, "black", C.SIGNAL_SIZES[LOW], label="Low (Rain)", drivers=("rain",))
        if heat_cond:
            return Signal(LOW, "red", C.SIGNAL_SIZES[LOW], label="Low (Temp)", drivers=("temperature",))
        return Signal(LOW, "blue", C.SIGNAL_SIZES[LOW], label="Low (Wind)", drivers=("wind",))
    return Signal(NO, "green", C.SIGNAL_SIZES[NO])


# ---- Combined ---------------------------------------------------------------

def combined_flags(
    sport: str,
    wind_fg: Optional[float],
    temp_fg: Optional[float],
    open_spread: Optional[float],
    travel_alt: Optional[float],
    home_temp: Optional[float],
    away_temp: Optional[float],
) -> list[str]:
    """Flags in the order the combined page concatenated them."""
    w, t, sp, alt = _f(wind_fg), _f(temp_fg), _f(open_spread), _f(travel_alt)
    ht, at = _f(home_temp), _f(away_temp)
    flags: list[str] = []
    if sport == "cfb":
        if sp is not None and abs(sp) < 10.5 and _lt(t, 70) and _gt(w, 14):
            flags.append("CFB Wind")
    elif sport == "nfl":
        if _gt(w, 15) and _lt(t, 60):
            flags.append("NFL Wind")
    if _lt(ht, 57) and _lt(at, 57) and _gt(t, 80):
        flags.append("Heat")
    if sport == "cfb" and _gt(alt, 800) and _between(sp, -10, 10) and _gt(t, 75):
        flags.append("Alt+Heat")
    return flags


def combined_color(flag: str) -> str:
    return C.COMBINED_COLORS[flag]


def dot_size(gs_fg_pct: Optional[float]) -> float:
    g = _f(gs_fg_pct)
    if g is None:
        return C.DOT_SIZE_BASE
    return abs(g) * C.DOT_SIZE_SLOPE + C.DOT_SIZE_BASE


__all__ = [
    "Signal",
    "NO",
    "LOW",
    "MID",
    "HIGH",
    "VERY_HIGH",
    "nfl_signal",
    "nfl_wind_vol",
    "wind_diff",
    "cfb_low_wind_threshold",
    "cfb_altitude_mid_trigger",
    "cfb_signal",
    "combined_flags",
    "combined_color",
    "dot_size",
]
