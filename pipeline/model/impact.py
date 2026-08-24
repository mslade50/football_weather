"""v1 impact model — exact reproduction of legacy gs_fg / away_fg (AUDIT §5, ARCH §7.1).

All components are in PERCENT. ``legacy_scale`` converts to the legacy file
convention: NFL gs_fg/away_fg are fractions (-0.035), CFB stay percent (-3.5).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pipeline.model import config as C


def _nan_to_none(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        return None if math.isnan(x) else float(x)
    except TypeError:
        return None


def tier(value: Optional[float], tiers: Sequence[tuple[float, float]]) -> float:
    """First-match descending tier lookup; None/NaN -> 0."""
    v = _nan_to_none(value)
    if v is None:
        return 0.0
    for threshold, component in tiers:
        if v >= threshold:
            return component
    return 0.0


def wind_component(wind_fg: Optional[float]) -> float:
    return tier(wind_fg, C.WIND_TIERS)


def cold_component(temp_fg: Optional[float]) -> float:
    t = _nan_to_none(temp_fg)
    if t is None:
        return 0.0
    return max(0.0, C.COLD_BASE_F - t) * C.COLD_PER_F


def heat_component(temp_fg: Optional[float]) -> float:
    t = _nan_to_none(temp_fg)
    if t is None:
        return 0.0
    return max(0.0, t - C.HEAT_BASE_F) * C.HEAT_PER_F


def rain_component(rain_fg_mm: Optional[float], month: Optional[int]) -> float:
    """Legacy rain tiers. ``month`` is the month of the RUN (the generator's clock),
    not the game date: September runs zeroed rain even for October kickoffs.
    Thresholds in ``RAIN_TIER_STRICT_MM`` are exclusive (>), the rest inclusive (>=)."""
    if month is not None and int(month) in C.RAIN_SUPPRESS_MONTHS:
        return 0.0
    r = _nan_to_none(rain_fg_mm)
    if r is None:
        return 0.0
    for threshold, component in C.RAIN_TIERS_MM:
        if r > threshold or (r == threshold and threshold not in C.RAIN_TIER_STRICT_MM):
            return component
    return 0.0


def heat_away_component(
    temp_fg: Optional[float],
    away_temp: Optional[float],
    sport: str,
    home_temp: Optional[float] = None,
    era_date: Optional[str] = None,
) -> float:
    """heat_away = heat when the away side comes from a meaningfully cooler city.

    NFL (every era) and CFB runs before ``CFB_HEAT_AWAY_DELTA_UNTIL`` fire on
    ``home_temp - away_temp >= HEAT_AWAY_DELTA_F``; CFB from 2024-09-27 on fires
    on ``away_temp < HEAT_AWAY_CUTOFF_F['cfb']``. ``era_date`` (ISO, golden replay
    only) selects the rule; None means the current era.
    """
    t = _nan_to_none(temp_fg)
    a = _nan_to_none(away_temp)
    if t is None or a is None or t <= C.HEAT_BASE_F:
        return 0.0
    if sport == "nfl" or (era_date is not None and era_date < C.CFB_HEAT_AWAY_DELTA_UNTIL):
        h = _nan_to_none(home_temp)
        if h is None:
            return 0.0
        fired = (h - a) >= C.HEAT_AWAY_DELTA_F
    else:
        fired = a < C.HEAT_AWAY_CUTOFF_F["cfb"]
    return heat_component(t) if fired else 0.0


def cold_away_component(
    temp_fg: Optional[float],
    away_temp: Optional[float],
    sport: str = "nfl",
    era_date: Optional[str] = None,
) -> float:
    """cold_away below the per-sport away-temp floor; NFL runs before
    ``NFL_COLD_AWAY_ERA`` (golden replay via ``era_date``) use the legacy 65 floor."""
    t = _nan_to_none(temp_fg)
    a = _nan_to_none(away_temp)
    if t is None or a is None:
        return 0.0
    floor = C.COLD_AWAY_AWAY_TEMP_MIN_F[sport]
    if sport == "nfl" and era_date is not None and era_date < C.NFL_COLD_AWAY_ERA:
        floor = C.NFL_COLD_AWAY_LEGACY_MIN_F
    if t < C.COLD_AWAY_BASE_F and a >= floor:
        return max(0.0, C.COLD_AWAY_BASE_F - t) * C.COLD_AWAY_PER_F
    return 0.0


def alt_component(travel_alt_m: Optional[float], sport: str, home_elev_m: Optional[float] = None) -> float:
    """Altitude tiers on travel_alt; CFB adds the 2.0 tier for a high-elevation home
    site (>=1100 m) hosting a visitor from >=700 m below. Skipped when home
    elevation is unknown."""
    base = tier(travel_alt_m, C.ALT_TIERS_M[sport])
    if base != 0.0 or sport != "cfb":
        return base
    ta = _nan_to_none(travel_alt_m)
    he = _nan_to_none(home_elev_m)
    if ta is not None and he is not None and ta >= C.CFB_ALT2_TRAVEL_MIN_M and he >= C.CFB_ALT2_HOME_ELEV_MIN_M:
        return C.CFB_ALT2_C
    return 0.0


@dataclass(frozen=True)
class ImpactV1:
    sport: str
    wind_c: float
    cold_c: float
    heat_c: float
    rain_c: float
    heat_away: float
    cold_away: float
    alt_c: float
    gs_fg_pct: float
    away_fg_pct: float
    roof_closed: bool = False
    model_version: str = C.MODEL_VERSION_V1

    @property
    def gs_fg_legacy(self) -> float:
        return legacy_scale(self.gs_fg_pct, self.sport)

    @property
    def away_fg_legacy(self) -> float:
        return legacy_scale(self.away_fg_pct, self.sport)

    def components(self) -> dict:
        return {
            "wind": self.wind_c,
            "cold": self.cold_c,
            "heat": self.heat_c,
            "rain": self.rain_c,
            "heat_away": self.heat_away,
            "cold_away": self.cold_away,
            "alt": self.alt_c,
        }


def legacy_scale(pct: float, sport: str) -> float:
    return pct / 100.0 if sport == "nfl" else pct


def compute_impact_v1(
    sport: str,
    month: Optional[int],
    temp_fg: Optional[float],
    wind_fg: Optional[float],
    rain_fg_mm: Optional[float],
    travel_alt_m: Optional[float],
    away_temp: Optional[float],
    home_temp: Optional[float] = None,
    roof_state: Optional[str] = None,
    home_elev_m: Optional[float] = None,
    era_date: Optional[str] = None,
) -> ImpactV1:
    """Reproduce legacy gs_fg / away_fg in percent.

    ``month`` is the month of the RUN (the legacy generator's clock), not the game
    date — it only drives September rain suppression. ``home_temp`` feeds the
    heat_away delta rule. ``home_elev_m`` (game-site elevation) enables the CFB
    2.0 altitude tier. ``era_date`` (ISO date, golden replay only) selects
    era-specific legacy rules; None means the current era. A closed roof zeroes
    the game-site components; away components (alt) are unaffected by the roof.
    """
    if sport not in C.SPORTS:
        raise ValueError(f"unknown sport {sport!r}")
    closed = roof_state in C.CLOSED_ROOF_STATES

    wind_c = 0.0 if closed else wind_component(wind_fg)
    cold_c = 0.0 if closed else cold_component(temp_fg)
    heat_c = 0.0 if closed else heat_component(temp_fg)
    rain_c = 0.0 if closed else rain_component(rain_fg_mm, month)
    gs = -(wind_c + cold_c + heat_c + rain_c)

    heat_away = 0.0 if closed else heat_away_component(temp_fg, away_temp, sport, home_temp, era_date)
    cold_away = 0.0 if closed else cold_away_component(temp_fg, away_temp, sport, era_date)
    alt_c = alt_component(travel_alt_m, sport, home_elev_m)
    # CFB legacy sums the away components; NFL takes the larger of temp vs altitude.
    away = -(alt_c + heat_away + cold_away) if sport == "cfb" else -max(heat_away + cold_away, alt_c)

    return ImpactV1(
        sport=sport,
        wind_c=wind_c,
        cold_c=cold_c,
        heat_c=heat_c,
        rain_c=rain_c,
        heat_away=heat_away,
        cold_away=cold_away,
        alt_c=alt_c,
        gs_fg_pct=gs + 0.0,  # normalise -0.0
        away_fg_pct=away + 0.0,
        roof_closed=closed,
    )


def gs_fg_pct(sport: str, month: Optional[int], temp_fg: float, wind_fg: float, rain_fg_mm: float) -> float:
    return compute_impact_v1(sport, month, temp_fg, wind_fg, rain_fg_mm, None, None).gs_fg_pct


def away_fg_pct(
    sport: str,
    temp_fg: float,
    travel_alt_m: Optional[float],
    away_temp: Optional[float],
    home_temp: Optional[float] = None,
) -> float:
    return compute_impact_v1(sport, None, temp_fg, None, None, travel_alt_m, away_temp, home_temp=home_temp).away_fg_pct


def ambiguous_buckets(
    temp_fg: Optional[float],
    rain_fg_mm: Optional[float],
    travel_alt_m: Optional[float],
    away_temp: Optional[float],
    sport: str,
    wind_fg: Optional[float] = None,
    home_temp: Optional[float] = None,
    home_elev_m: Optional[float] = None,
) -> list[str]:
    """Names of documented ambiguity bands this row falls into (for test logging)."""
    out: list[str] = []
    r = _nan_to_none(rain_fg_mm)
    lo, hi = C.AMBIGUOUS_BANDS["rain_mm"]
    if r is not None and lo <= r <= hi:
        out.append("rain_6mm_boundary")
    t = _nan_to_none(temp_fg)
    a = _nan_to_none(away_temp)
    h = _nan_to_none(home_temp)
    lo, hi = C.AMBIGUOUS_BANDS["heat_away_delta_f"]
    if t is not None and t > C.HEAT_BASE_F and a is not None and h is not None and lo < (h - a) < hi:
        out.append("heat_away_delta_8-11F")
    alt = _nan_to_none(travel_alt_m)
    he = _nan_to_none(home_elev_m)
    if alt is not None and sport == "nfl":
        lo, hi = C.AMBIGUOUS_BANDS["alt_nfl_2_0_m"]
        if lo < alt < hi:
            out.append("alt_nfl_2_0_edge")
        lo, hi = C.AMBIGUOUS_BANDS["alt_nfl_3_5_m"]
        if lo < alt < hi:
            out.append("alt_nfl_3_5_edge")
    if alt is not None and sport == "cfb":
        lo, hi = C.AMBIGUOUS_BANDS["cfb_alt2_travel_m"]
        if lo < alt < hi and he is not None and he >= C.CFB_ALT2_HOME_ELEV_MIN_M:
            out.append("cfb_alt2_travel_edge")
        lo, hi = C.AMBIGUOUS_BANDS["cfb_alt2_home_elev_m"]
        if he is not None and lo < he < hi and alt >= C.CFB_ALT2_TRAVEL_MIN_M:
            out.append("cfb_alt2_home_elev_edge")
    # CFB legacy stored wind_fg / rain_fg at 1dp, so exact-threshold values can flip tiers.
    w = _nan_to_none(wind_fg)
    if sport == "cfb" and w is not None and any(abs(w - th) <= 0.05 for th, _ in C.WIND_TIERS):
        out.append("cfb_wind_1dp_tier_edge")
    if sport == "cfb" and r is not None and any(abs(r - th) <= 0.05 for th, _ in C.RAIN_TIERS_MM):
        out.append("cfb_rain_1dp_tier_edge")
    return out


# =============================================================================
# v2 (ARCH §7.5) — additive, side by side with v1. Nothing above this line changes.
# =============================================================================

CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "data" / "calibration.json"
COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_cal_cache: dict[str, float] | None = None


def load_v2_calibration(path: Path = CALIBRATION_PATH, use_cache: bool = True) -> dict[str, float]:
    """``V2_DEFAULTS`` overlaid with the ``v2`` block of data/calibration.json (missing/invalid -> defaults)."""
    global _cal_cache
    if use_cache and _cal_cache is not None and path == CALIBRATION_PATH:
        return dict(_cal_cache)
    cal = dict(C.V2_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data.get("v2") if isinstance(data, dict) else None
        if isinstance(block, dict):
            for k, v in block.items():
                if k in cal and isinstance(v, (int, float)) and not isinstance(v, bool):
                    cal[k] = float(v)
    except (OSError, ValueError):
        pass
    if path == CALIBRATION_PATH:
        _cal_cache = dict(cal)
    return cal


def to_compass8(deg: Optional[float]) -> Optional[str]:
    d = _nan_to_none(deg)
    if d is None:
        return None
    return COMPASS_8[int(((d % 360.0) + 22.5) // 45.0) % 8]


def compass16_to_8(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    s = label.strip().upper()
    if s in COMPASS_8:
        return s
    if s in COMPASS_16:
        return to_compass8(COMPASS_16.index(s) * 22.5)  # NNE->NE, ENE->E, WSW->W (same rule as degrees)
    return None


def parse_weak_set(weakest_wind_effect: Optional[str]) -> Optional[set[str]]:
    """Legacy ``weakest_wind_effect`` -> set of 8-pt directions where wind matters LESS.

    ``'x N'`` -> every direction except N; ``'E/W'`` -> {E, W}; ``'N, S'`` -> {N, S};
    ``'all'`` / blank -> empty set (wind always counts fully). Unknown tokens are ignored."""
    if weakest_wind_effect is None:
        return set()
    s = str(weakest_wind_effect).strip().upper()
    if not s or s in ("ALL", "NONE", "NAN", "-"):
        return set()
    exclude = False
    if s.startswith("X "):
        exclude = True
        s = s[2:]
    tokens = {t.strip() for t in s.replace("/", ",").replace("&", ",").replace(" AND ", ",").split(",")}
    dirs = {compass16_to_8(t) for t in tokens} - {None}
    if not dirs:
        return set()
    return (set(COMPASS_8) - dirs) if exclude else set(dirs)  # type: ignore[arg-type]


def dir_multiplier(
    wind_dir_fg: Optional[str],
    weakest_wind_effect: Optional[str],
    cal: Optional[dict[str, float]] = None,
    wind_dir_deg: Optional[float] = None,
) -> float:
    cal = cal or load_v2_calibration()
    weak = parse_weak_set(weakest_wind_effect)
    if not weak:
        return 1.0
    d8 = compass16_to_8(wind_dir_fg) or to_compass8(wind_dir_deg)
    if d8 is None:
        return 1.0
    return cal["dir_mult_weak"] if d8 in weak else 1.0


def effective_wind(wind_fg: Optional[float], gust_fg: Optional[float], cal: Optional[dict[str, float]] = None) -> Optional[float]:
    """``w_eff = b*wind + (1-b)*gust``; a missing gust falls back to the wind alone."""
    cal = cal or load_v2_calibration()
    w = _nan_to_none(wind_fg)
    g = _nan_to_none(gust_fg)
    if w is None:
        return None
    if g is None or g < w:
        g = w
    b = cal["gust_blend"]
    return b * w + (1.0 - b) * g


def directional_wind(
    w_eff: Optional[float],
    wind_dir_deg: Optional[float],
    orientation_deg: Optional[float],
    cal: Optional[dict[str, float]] = None,
) -> Optional[float]:
    """``w_dir = sqrt(cross^2 + k*head^2)`` from ``w_eff`` components; no orientation/direction -> ``w_eff``."""
    cal = cal or load_v2_calibration()
    w = _nan_to_none(w_eff)
    if w is None:
        return None
    d = _nan_to_none(wind_dir_deg)
    o = _nan_to_none(orientation_deg)
    if d is None or o is None:
        return w
    theta = math.radians(d - o)
    cross = abs(w * math.sin(theta))
    head = abs(w * math.cos(theta))
    return math.sqrt(cross * cross + cal["head_weight"] * head * head)


def wind_curve(w_dir: Optional[float], cal: Optional[dict[str, float]] = None) -> float:
    cal = cal or load_v2_calibration()
    w = _nan_to_none(w_dir)
    if w is None:
        return 0.0
    x = max(0.0, w - cal["wind_offset_mph"])
    return min(cal["wind_cap"], cal["wind_coeff"] * x ** cal["wind_exp"])


def rain_component_v2(
    rain_fg_mm: Optional[float],
    precip_prob: Optional[float],
    precip_prob_ens: Optional[float] = None,
    cal: Optional[dict[str, float]] = None,
) -> tuple[float, float]:
    """(rain_c2, expected_mm). Probability = ensemble member fraction when present, else
    NBM PoP; neither -> deterministic (prob 1 if any mm forecast). No September suppression."""
    cal = cal or load_v2_calibration()
    mm = _nan_to_none(rain_fg_mm) or 0.0
    p = _nan_to_none(precip_prob_ens)
    if p is None:
        p = _nan_to_none(precip_prob)
    if p is None:
        p = 1.0 if mm > 0 else 0.0
    p = min(1.0, max(0.0, p))
    expected = p * mm
    if p < cal["rain_prob_min"]:
        return 0.0, expected
    return tier(expected, C.RAIN_TIERS_MM), expected


def alt_component_v2(travel_alt_m: Optional[float], cal: Optional[dict[str, float]] = None) -> float:
    cal = cal or load_v2_calibration()
    a = _nan_to_none(travel_alt_m)
    if a is None:
        return 0.0
    return min(cal["alt_cap"], cal["alt_slope_per_m"] * max(0.0, a - cal["alt_base_m"]))


def heat_away_component_v2(
    temp_fg: Optional[float], home_temp: Optional[float], away_temp: Optional[float], cal: Optional[dict[str, float]] = None
) -> float:
    cal = cal or load_v2_calibration()
    t = _nan_to_none(temp_fg)
    h = _nan_to_none(home_temp)
    a = _nan_to_none(away_temp)
    if t is None or h is None or a is None:
        return 0.0
    if t > C.HEAT_BASE_F and (h - a) >= cal["heat_away_delta_f"]:
        return heat_component(t)
    return 0.0


@dataclass(frozen=True)
class ImpactV2:
    sport: str
    wind_c: float
    cold_c: float
    heat_c: float
    rain_c: float
    heat_away: float
    cold_away: float
    alt_c: float
    gs_fg_pct: float
    away_fg_pct: float
    w_eff: Optional[float]
    w_dir: Optional[float]
    dir_mult: float
    expected_mm: float
    roof_closed: bool = False
    conf: Optional[float] = None
    ensemble: bool = False
    model_version: str = C.MODEL_VERSION_V2

    @property
    def gs_fg_legacy(self) -> float:
        return legacy_scale(self.gs_fg_pct, self.sport)

    @property
    def away_fg_legacy(self) -> float:
        return legacy_scale(self.away_fg_pct, self.sport)

    def components(self) -> dict:
        return {
            "wind": self.wind_c,
            "cold": self.cold_c,
            "heat": self.heat_c,
            "rain": self.rain_c,
            "heat_away": self.heat_away,
            "cold_away": self.cold_away,
            "alt": self.alt_c,
        }


def compute_impact_v2(
    sport: str,
    temp_fg: Optional[float],
    wind_fg: Optional[float],
    gust_fg: Optional[float],
    rain_fg_mm: Optional[float],
    precip_prob: Optional[float],
    travel_alt_m: Optional[float],
    home_temp: Optional[float],
    away_temp: Optional[float],
    *,
    wind_dir_deg: Optional[float] = None,
    wind_dir_fg: Optional[str] = None,
    orientation_deg: Optional[float] = None,
    weakest_wind_effect: Optional[str] = None,
    precip_prob_ens: Optional[float] = None,
    roof_state: Optional[str] = None,
    conf: Optional[float] = None,
    cal: Optional[dict[str, float]] = None,
) -> ImpactV2:
    """Orientation-aware, gust-blended, probabilistic-rain, continuous-altitude impact (percent)."""
    if sport not in C.SPORTS:
        raise ValueError(f"unknown sport {sport!r}")
    cal = cal or load_v2_calibration()
    closed = roof_state in C.CLOSED_ROOF_STATES

    w_eff = effective_wind(wind_fg, gust_fg, cal)
    w_dir = directional_wind(w_eff, wind_dir_deg, orientation_deg, cal)
    dmult = dir_multiplier(wind_dir_fg, weakest_wind_effect, cal, wind_dir_deg)
    wind_c = 0.0 if closed else wind_curve(w_dir, cal) * dmult
    cold_c = 0.0 if closed else cold_component(temp_fg)
    heat_c = 0.0 if closed else heat_component(temp_fg)
    rain_c, expected_mm = rain_component_v2(rain_fg_mm, precip_prob, precip_prob_ens, cal)
    if closed:
        rain_c = 0.0
    gs = -(wind_c + cold_c + heat_c + rain_c)

    heat_away = 0.0 if closed else heat_away_component_v2(temp_fg, home_temp, away_temp, cal)
    cold_away = 0.0 if closed else cold_away_component(temp_fg, away_temp, sport)
    alt_c = alt_component_v2(travel_alt_m, cal)
    away = -max(heat_away + cold_away, alt_c)

    return ImpactV2(
        sport=sport,
        wind_c=wind_c,
        cold_c=cold_c,
        heat_c=heat_c,
        rain_c=rain_c,
        heat_away=heat_away,
        cold_away=cold_away,
        alt_c=alt_c,
        gs_fg_pct=gs + 0.0,
        away_fg_pct=away + 0.0,
        w_eff=w_eff,
        w_dir=w_dir,
        dir_mult=dmult,
        expected_mm=expected_mm,
        roof_closed=closed,
        conf=conf,
        ensemble=precip_prob_ens is not None,
    )


__all__ = [
    "ImpactV1",
    "compute_impact_v1",
    "ImpactV2",
    "compute_impact_v2",
    "load_v2_calibration",
    "parse_weak_set",
    "dir_multiplier",
    "effective_wind",
    "directional_wind",
    "wind_curve",
    "rain_component_v2",
    "alt_component_v2",
    "heat_away_component_v2",
    "to_compass8",
    "compass16_to_8",
    "gs_fg_pct",
    "away_fg_pct",
    "legacy_scale",
    "tier",
    "wind_component",
    "cold_component",
    "heat_component",
    "rain_component",
    "heat_away_component",
    "cold_away_component",
    "alt_component",
    "ambiguous_buckets",
]
