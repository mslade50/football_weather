"""Map dot rules ported from pages/nfl_weather.py, pages/cfb_weather.py,
pages/combined_signals.py (ARCH §7.4). Pure functions on plain floats; None /
NaN inputs never match a condition (mirrors pandas NaN comparisons).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from pipeline.model import config as C
from pipeline.model.process import temp_gate

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


def _ge(a: Optional[float], b: float) -> bool:
    return a is not None and a >= b


def _lt(a: Optional[float], b: float) -> bool:
    return a is not None and a < b


def _between(a: Optional[float], lo: float, hi: float) -> bool:
    return a is not None and lo <= a <= hi


def _temp_allows(sport: str, temp: Optional[float]) -> bool:
    """Temperature blocks only when that league's optional gate is on."""
    enabled, max_f = temp_gate(sport)
    if not enabled:
        return True
    return _lt(temp, max_f)


# ---- NFL ---------------------------------------------------------------------

def nfl_signal(wind_fg: Optional[float], temp_fg: Optional[float], rain_fg: Optional[float]) -> Signal:
    """Evaluated purple-first so High is reachable (legacy ordering made it dead code).

    The flag is sustained wind >= 15 mph. Mid and High stay stricter severity
    labels (wind above 15 mph inside a cold band). A warm flag is Low unless
    the optional temperature gate is on, in which case a warm game is No Impact.
    """
    w, t, r = _f(wind_fg), _f(temp_fg), _f(rain_fg)
    allows = _temp_allows("nfl", t)
    if allows and _gt(w, 15) and _between(t, 32, 45):
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH], drivers=("wind",))
    if allows and _gt(w, 15) and _lt(t, C.PROCESS_TEMP_MAX_F):
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID], drivers=("wind",))
    rain_cond = allows and _ge(r, C.PROCESS_RAIN_SIGNAL_MM)
    wind_flag = allows and _ge(w, C.PROCESS_WIND_FLAG_MPH)
    # Exactly 15 mph is a flag and stays Low. Mid/High still require wind above 15.
    if rain_cond or (wind_flag and not (_gt(w, 15) and _lt(t, C.PROCESS_TEMP_MAX_F))):
        driver = "rain" if rain_cond else "wind"
        return Signal(LOW, "blue", C.SIGNAL_SIZES[LOW], drivers=(driver,))
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

def _cfb_spread_eligible(open_spread: Optional[float]) -> bool:
    spread = _f(open_spread)
    return _between(spread, -C.CFB_OPEN_SPREAD_MAX, C.CFB_OPEN_SPREAD_MAX)


def cfb_wind_eligible(wind_fg: Optional[float], temp_fg: Optional[float], open_spread: Optional[float]) -> bool:
    """CFB wind flag: |opening spread|≤10 and sustained wind >= 15 mph.

    Temperature is not a hard gate. When the optional CFB temperature gate is
    on, the temperature must be strictly under that gate's max (default 65°F).
    """
    w, t = _f(wind_fg), _f(temp_fg)
    return _cfb_spread_eligible(open_spread) and _temp_allows("cfb", t) and _ge(w, C.CFB_WIND_MIN_MPH)


def cfb_altitude_mid_trigger(
    temp_fg: Optional[float],
    open_spread: Optional[float],
    travel_alt: Optional[float],
) -> bool:
    """Whether CFB altitude plus warmth contributes an eligible Mid signal."""
    t, sp, alt = _f(temp_fg), _f(open_spread), _f(travel_alt)
    return _gt(alt, 800) and _gt(t, 75) and _cfb_spread_eligible(sp)


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
    """CFB dot tier; ``weekday`` remains in the signature for caller compatibility."""
    w, t, r = _f(wind_fg), _f(temp_fg), _f(rain_fg)
    sp, alt = _f(open_spread), _f(travel_alt)
    ht, at = _f(home_temp), _f(away_temp)
    if not _cfb_spread_eligible(sp):
        return Signal(NO, "green", C.SIGNAL_SIZES[NO])

    wind_eligible = cfb_wind_eligible(w, t, sp)
    # 21.5 mph is the historical severity boundary, not the 15 mph flag.
    # Weekday no longer changes the wind rule. A warm flag is still Low:
    # High / Very High keep their temperature bands.
    hi = C.CFB_HIGH_WIND_MPH

    rain_cond = _temp_allows("cfb", t) and _ge(r, C.PROCESS_RAIN_SIGNAL_MM)
    heat_cond = _gt(t, 80) and _lt(ht, 57) and _lt(at, 57)
    altitude_mid = cfb_altitude_mid_trigger(t, sp, alt)

    if wind_eligible and _gt(w, hi) and _lt(t, 50):
        return Signal(VERY_HIGH, "darkred", C.SIGNAL_SIZES[VERY_HIGH], drivers=("wind",))
    if wind_eligible and _gt(w, hi) and _lt(t, 65):
        return Signal(HIGH, "purple", C.SIGNAL_SIZES[HIGH], drivers=("wind",))
    if altitude_mid:
        return Signal(MID, "orange", C.SIGNAL_SIZES[MID], drivers=("altitude_warmth",))
    if wind_eligible or rain_cond or heat_cond:
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
        if not _cfb_spread_eligible(sp):
            return flags
        if cfb_wind_eligible(w, t, sp):
            flags.append("CFB Wind")
    elif sport == "nfl":
        if _temp_allows("nfl", t) and _ge(w, C.PROCESS_WIND_FLAG_MPH):
            flags.append("NFL Wind")
    if _lt(ht, 57) and _lt(at, 57) and _gt(t, 80):
        flags.append("Heat")
    if sport == "cfb" and _gt(alt, 800) and _gt(t, 75):
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
    "cfb_wind_eligible",
    "cfb_altitude_mid_trigger",
    "cfb_signal",
    "combined_flags",
    "combined_color",
    "dot_size",
]
