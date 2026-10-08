"""Point-only eligibility for the unchanged full-member wind/rain estimator.

Buffers are conservative engineering margins, not fitted probability bounds.
No independent wind/temperature extrema, confidence or ensemble outputs are
used. Heat/altitude/gust alone remain evaluated by the fresh point stack.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pipeline.contracts import WeatherForecast
from pipeline.model import config as C
from pipeline.weather import climatology_blend as CB
from pipeline.weather.merge import hour_floor

VERSION = "nws-first-joint-v7"
RAIN_TAIL_MM = 0.25  # Small model-window amounts can coexist with >2 mm member tails.


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reasons: tuple[str, ...]
    priority: int


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def screen(
    sport: str, forecast: WeatherForecast, location: Any, kickoff: datetime,
    *, roof_state: str | None = None, active: bool = False,
    elevation_m: float | None = None, previous: dict[str, Any] | None = None,
    home_temp: float | None = None, away_temp: float | None = None,
    travel_alt: float | None = None, now: datetime | None = None,
    blend_cfg: Any = None,
    nws_rows: Any = None, for_refinement: bool = False, open_spread: float | None = None,
) -> Decision:
    if active:
        return Decision(True, ("existing_active_signal",), 0)
    if roof_state in C.CLOSED_ROOF_STATES:
        return Decision(False, ("confirmed_closed_roof",), 3)
    # Match the existing CFB signal gate. Unknown openers remain conservative;
    # active notifications above always retain their member coverage.
    if sport == "cfb" and finite(open_spread) and abs(open_spread) > C.CFB_OPEN_SPREAD_MAX:
        return Decision(False, ("cfb_opening_spread_ineligible",), 3)
    lead = forecast.lead_hours
    if not finite(lead) or lead < 0:
        return Decision(False, ("unknown_horizon",), 2)
    base_wind = 8.0 if sport == "nfl" else C.CFB_WIND_MIN_MPH
    base_temp = 60.0 if sport == "nfl" else C.CFB_WIND_MAX_TEMP_F
    wind_threshold = base_wind - (4.0 if for_refinement else 3.0)
    temp_threshold = base_temp + (5.0 if for_refinement else 2.0)
    rain_threshold = 1.5
    window = {hour_floor(kickoff) + timedelta(hours=i) for i in range(3)}
    reasons = []
    pairs = []
    rain_totals = []
    complete = 0
    def pair(wind: float, temp: float) -> tuple[float, float]:
        return (CB.blend(wind, forecast.climo_wind, lead, "wind", blend_cfg),
                CB.blend(temp, forecast.climo_temp, lead, "temp", blend_cfg))
    model_rows = list((getattr(location, "models", {}) or {}).values())
    if nws_rows:
        model_rows.append(nws_rows)
    for rows in model_rows:
        rows = [row for row in rows if row.t in window]
        good = [row for row in rows if finite(row.wind) and finite(row.temp) and finite(row.precip)]
        if len({row.t for row in good}) == 3:
            complete += 1
            pairs.append(pair(sum(row.wind for row in good) / 3, sum(row.temp for row in good) / 3))
            rain_totals.append(sum(row.precip for row in good))
        # A transient hour must reach the actual joint signal boundary; the
        # modest near-signal margin applies to game-window means only.
        for row in rows:
            if finite(row.wind) and finite(row.temp):
                wind, temp = pair(row.wind, row.temp)
                if sport == "cfb":
                    reaches_signal = wind > C.CFB_WIND_MIN_MPH and temp < C.CFB_WIND_MAX_TEMP_F
                else:
                    reaches_signal = wind >= base_wind and temp <= base_temp
                if reaches_signal:
                    pairs.append((wind, temp))
    if finite(forecast.wind_fg) and finite(forecast.temp_fg):
        pairs.append((forecast.wind_fg, forecast.temp_fg))
    if finite(forecast.wind_fg_raw) and finite(forecast.temp_fg_raw):
        pairs.append(pair(forecast.wind_fg_raw, forecast.temp_fg_raw))
    if finite(forecast.rain_fg_mm):
        rain_totals.append(forecast.rain_fg_mm)
    unreliable = complete == 0 or any(not finite(getattr(forecast, k)) for k in ("wind_fg", "temp_fg", "rain_fg_mm"))
    stale = now is not None and (forecast.run_time is None or not 0 <= (now - forecast.run_time).total_seconds() <= 1800)
    if unreliable or stale or lead > 15 * 24:
        return Decision(False, ("unreliable_point_coverage" if unreliable else "stale_or_unknown_point_time" if stale else
                                "outside_documented_horizon",), 2)
    if any(wind >= wind_threshold and temp <= temp_threshold for wind, temp in pairs):
        reasons.append("joint_wind_temperature")
    if sport == "cfb" and any(wind >= C.CFB_WIND_MIN_MPH - (4.0 if for_refinement else 3.0)
                               and temp <= C.CFB_WIND_MAX_TEMP_F + (5.0 if for_refinement else 2.0)
                               for wind, temp in pairs):
        reasons.append("joint_cfb_wind_flag")
    if any(rain >= rain_threshold for rain in rain_totals):
        reasons.append("game_window_rain")
    elif any(rain >= RAIN_TAIL_MM for rain in rain_totals):
        reasons.append("rain_tail_risk")
    if previous and reasons:
        for key, old_key, amount in (("wind_fg", "wind_fg", 2), ("temp_fg", "temp_fg", 5), ("rain_fg_mm", "rain_fg", 0.5)):
            old, current = previous.get(old_key), getattr(forecast, key)
            if finite(old) and finite(current) and abs(old - current) >= amount:
                reasons.append("material_point_change")
                break
    return Decision(bool(reasons), tuple(reasons) or ("below_joint_wind_rain_buffers",), 1 if reasons else 3)
