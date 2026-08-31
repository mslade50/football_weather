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
    """Evaluated purple-first so High is reachable (legacy ordering made it dead code).

    Low carries the cause in its ``label`` the way CFB always has ("Low (Rain)" / "Low (Wind)"):
    the two conditions are unrelated bets and the bare "Low Impact" label made them
    indistinguishable on the board and in the backtest. ``level`` stays ``LOW`` and
    ``alerts.signal_slug`` still maps both to "low", so keys and tiers are unchanged."""
    w, t, r = _f(wind_fg), _f(temp_fg), _f(rain_fg)
    if _gt(w, 15) and _between(t, 32, 45):
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH])
    rain_cond = _gt(r, 2)
    wind_cond = w is not None and 8 < w < 15 and _lt(t, 60)
    if rain_cond or wind_cond:
        if rain_cond:   # rain wins the label when both fire, as in cfb_signal
            return Signal(LOW, "black", C.SIGNAL_SIZES[LOW], "Low (Rain)")
        return Signal(LOW, "blue", C.SIGNAL_SIZES[LOW], "Low (Wind)")
    if _gt(w, 15) and _lt(t, 60):
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID])
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

    if _gt(w, hi) and _lt(t, 50) and tight:
        return Signal(VERY_HIGH, "darkred", C.SIGNAL_SIZES[VERY_HIGH])
    if _gt(w, hi) and _lt(t, 65) and tight:
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH])
    if ((_gt(w, hi) and _lt(t, 65)) or (_gt(alt, 800) and _gt(t, 75))) and wide:
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID])
    if ((_gt(w, base) and _lt(t, 65)) or rain_cond or heat_cond) and wide:
        if rain_cond:
            return Signal(LOW, "black", C.SIGNAL_SIZES[LOW], "Low (Rain)")
        if heat_cond:
            return Signal(LOW, "red", C.SIGNAL_SIZES[LOW], "Low (Temp)")
        return Signal(LOW, "blue", C.SIGNAL_SIZES[LOW], "Low (Wind)")
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
    "cfb_signal",
    "combined_flags",
    "combined_color",
    "dot_size",
]
