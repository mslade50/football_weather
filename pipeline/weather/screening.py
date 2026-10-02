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

VERSION = "joint-wind-rain-v2"


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
) -> Decision:
    if active:
        return Decision(True, ("existing_active_signal",), 0)
    if roof_state in C.CLOSED_ROOF_STATES:
        return Decision(False, ("confirmed_closed_roof",), 3)
    lead = forecast.lead_hours
    if not finite(lead) or lead < 0:
        return Decision(True, ("unknown_horizon",), 1)
    wind_margin, temp_margin, rain_margin = (
        (2.0, 2.0, 0.5) if lead <= 48 else
        (3.0, 3.0, 0.75) if lead <= 120 else
        (4.0, 5.0, 1.0)
    )
    wind_threshold = (8.0 if sport == "nfl" else min(C.CFB_DOW_LOW_WIND.values())) - wind_margin
    temp_threshold = (60.0 if sport == "nfl" else 65.0) + temp_margin
    rain_threshold = 2.0 - rain_margin
    window = {hour_floor(kickoff) + timedelta(hours=i) for i in range(3)}
    reasons = []
    pairs = []
    rain_totals = []
    complete = 0
    def pair(wind: float, temp: float) -> tuple[float, float]:
        return (CB.blend(wind, forecast.climo_wind, lead, "wind", blend_cfg),
                CB.blend(temp, forecast.climo_temp, lead, "temp", blend_cfg))
    for rows in (getattr(location, "models", {}) or {}).values():
        rows = [row for row in rows if row.t in window]
        good = [row for row in rows if finite(row.wind) and finite(row.temp) and finite(row.precip)]
        if len({row.t for row in good}) == 3:
            complete += 1
            pairs.append(pair(sum(row.wind for row in good) / 3, sum(row.temp for row in good) / 3))
            rain_totals.append(sum(row.precip for row in good))
        pairs.extend(pair(row.wind, row.temp) for row in rows if finite(row.wind) and finite(row.temp))
    if finite(forecast.wind_fg) and finite(forecast.temp_fg):
        pairs.append((forecast.wind_fg, forecast.temp_fg))
    if finite(forecast.wind_fg_raw) and finite(forecast.temp_fg_raw):
        pairs.append(pair(forecast.wind_fg_raw, forecast.temp_fg_raw))
    if finite(forecast.rain_fg_mm):
        rain_totals.append(forecast.rain_fg_mm)
    if complete == 0 or any(not finite(getattr(forecast, k)) for k in ("wind_fg", "temp_fg", "rain_fg_mm")):
        reasons.append("unreliable_point_coverage")
    if now is not None and (forecast.run_time is None or not 0 <= (now - forecast.run_time).total_seconds() <= 1800):
        reasons.append("stale_or_unknown_point_time")
    if lead > 15 * 24:
        reasons.append("outside_documented_horizon")
    if any(wind >= wind_threshold and temp <= temp_threshold for wind, temp in pairs):
        reasons.append("joint_wind_temperature")
    if any(rain >= rain_threshold for rain in rain_totals):
        reasons.append("game_window_rain")
    if previous and reasons:
        for key, old_key, amount in (("wind_fg", "wind_fg", 2), ("temp_fg", "temp_fg", 5), ("rain_fg_mm", "rain_fg", 0.5)):
            old, current = previous.get(old_key), getattr(forecast, key)
            if finite(old) and finite(current) and abs(old - current) >= amount:
                reasons.append("material_point_change")
                break
    return Decision(bool(reasons), tuple(reasons) or ("below_joint_wind_rain_buffers",), 1 if reasons else 3)
