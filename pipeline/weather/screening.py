"""Conservative candidate screen, pending replay validation before integration.

Never uses member percentiles, confidence, odds eligibility or ensemble rain.
Buffers are proposed engineering safeguards, not calibrated probability bounds.
Only determines eligibility; an overloaded quota must not relabel a candidate
as benign. Current signal thresholds remain in model.signals, unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pipeline.contracts import WeatherForecast
from pipeline.model import config as C
from pipeline.weather.merge import hour_floor

VERSION = "signal-screen-draft-v1"


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reasons: tuple[str, ...]
    priority: int  # active, current trigger, unreliable, borderline, benign


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def screen(
    sport: str, forecast: WeatherForecast, location: Any, kickoff: Any,
    *, roof_state: str | None = None, active: bool = False,
    elevation_m: float | None = None, previous: dict[str, Any] | None = None,
    home_temp: float | None = None, away_temp: float | None = None,
    travel_alt: float | None = None,
    now: Any = None,
) -> Decision:
    if active:
        return Decision(True, ("existing_active_signal",), 0)
    if roof_state in C.CLOSED_ROOF_STATES:
        return Decision(False, ("confirmed_closed_roof",), 4)
    lead = forecast.lead_hours
    if not finite(lead) or lead < 0:
        return Decision(True, ("unknown_horizon",), 2)
    # Min CFB threshold over every weekday: a Friday run must not screen away
    # a game that becomes eligible at Saturday's lower 8.79 mph threshold.
    margin, temp_buffer, gust, wet, pop, cold, heat = (
        (4, 8, 20, 0.5, 0.20, 40, 75) if lead <= 48 else
        (4, 10, 18, 0.25, 0.15, 45, 72) if lead <= 120 else
        (5, 15, 15, 0.1, 0.10, 50, 70)
    )
    wind_floor = (8.0 if sport == "nfl" else min(C.CFB_DOW_LOW_WIND.values())) - margin
    temp_ceiling = (60.0 if sport == "nfl" else 65.0) + temp_buffer
    window = {hour_floor(kickoff) + timedelta(hours=i) for i in range(3)}
    models = getattr(location, "models", {}) if location is not None else {}
    values: dict[str, list[float]] = {key: [] for key in ("wind", "temp", "gust", "precip", "pop")}
    complete = 0
    for rows in models.values():
        rows = [row for row in rows if row.t in window]
        if len({row.t for row in rows if all(finite(getattr(row, key)) for key in ("wind", "temp", "precip"))}) == 3:
            complete += 1
        for key in values:
            values[key].extend(getattr(row, key) for row in rows if finite(getattr(row, key)))
    # Include blended and unblended game-window forecasts. Never dismiss a
    # model's adverse hourly value simply because the blended mean is benign.
    for field, key in (("wind_fg", "wind"), ("wind_fg_raw", "wind"), ("temp_fg", "temp"),
                       ("temp_fg_raw", "temp"), ("gust_fg", "gust"), ("rain_fg_mm", "precip")):
        value = getattr(forecast, field, None)
        if finite(value):
            values[key].append(value)
    probability = forecast.precip_prob
    if finite(probability):
        values["pop"].append(probability * 100)
    reasons = []
    if now is not None and (forecast.run_time is None or not 0 <= (now - forecast.run_time).total_seconds() <= 1800):
        reasons.append("stale_or_unknown_point_time")
    if complete < 2 or not values["gust"] or any(not finite(getattr(forecast, key)) for key in ("wind_fg", "temp_fg", "rain_fg_mm")):
        reasons.append("unreliable_point_coverage")
    if lead > 15 * 24:
        reasons.append("outside_documented_horizon")
    high_wind = max(values["wind"], default=0)
    low_temp = min(values["temp"], default=100)
    high_temp = max(values["temp"], default=-100)
    rain = max(values["precip"], default=0)
    if high_wind >= wind_floor and low_temp <= temp_ceiling:
        reasons.append("borderline_wind_temperature")
    if high_wind >= 15 - margin:  # material v1 wind impact starts at 15 even when warm
        reasons.append("borderline_wind_impact")
    if (max(values["gust"], default=0) >= gust and low_temp <= temp_ceiling) or max(values["gust"], default=0) >= 30:
        reasons.append("gust_risk")
    if rain >= wet or max(values["pop"], default=0) >= pop * 100:
        reasons.append("precipitation_risk")
    if low_temp <= cold:
        reasons.append("cold_or_snow_risk")  # precipitation is liquid-equivalent
    cold_origins = (not finite(home_temp) or home_temp < 62) and (not finite(away_temp) or away_temp < 62)
    if (high_temp >= heat and cold_origins) or high_temp >= 90:
        reasons.append("heat_risk")
    altitude_risk = travel_alt >= 600 if finite(travel_alt) else finite(elevation_m) and elevation_m >= 600
    if sport == "cfb" and altitude_risk and high_temp >= 75 - temp_buffer:
        reasons.append("altitude_warmth_risk")
    if previous:
        for field, threshold in (("wind_fg", 2), ("temp_fg", 5), ("rain_fg_mm", 0.5)):
            old = previous.get(field if field != "rain_fg_mm" else "rain_fg")
            new = getattr(forecast, field)
            if finite(old) and finite(new) and abs(new - old) >= threshold:
                reasons.append("material_point_change")
                break
    current = ((finite(forecast.wind_fg) and finite(forecast.temp_fg)
                and forecast.wind_fg > (8 if sport == "nfl" else min(C.CFB_DOW_LOW_WIND.values()))
                and forecast.temp_fg < (60 if sport == "nfl" else 65))
               or (finite(forecast.rain_fg_mm) and forecast.rain_fg_mm > 2)
               or (finite(forecast.temp_fg) and ((forecast.temp_fg > 80 and cold_origins)
                    or (sport == "cfb" and altitude_risk and forecast.temp_fg > 75))))
    priority = 1 if current else 2 if "unreliable_point_coverage" in reasons else 3 if reasons else 4
    return Decision(bool(reasons), tuple(reasons) or ("below_conservative_buffers",), priority)


@dataclass(frozen=True)
class Candidate:
    game_id: str
    location_key: tuple[Any, ...]
    decision: Decision
    lead_hours: float


@dataclass(frozen=True)
class SamplePlan:
    sampled_games: tuple[str, ...]
    deferred_games: tuple[str, ...]
    high_priority_deferred: tuple[str, ...]
    estimated_calls: float
    statuses: dict[str, str]


def plan(candidates: list[Candidate], point_calls: float, call_limit: float = 550) -> SamplePlan:
    """Draft quota cap, NOT activated. Deferral is never screening rejection.

    3 requested variables * (51 IFS + 31 GEFS members) / 10 = 24.6 per
    location. 550*17=9350 leaves nominal daily headroom at current cadence;
    it is not an account-wide daily usage ledger or a retry guarantee.
    Active/current-trigger locations sort first, then unknown/borderline; ties
    use nearest kickoff. All selected games at a shared location get the same
    sampled/deferred status. Missing precision is explicit even for active games.
    """
    groups: dict[tuple[Any, ...], list[Candidate]] = {}
    statuses = {}
    for candidate in candidates:
        if candidate.decision.eligible:
            groups.setdefault(candidate.location_key, []).append(candidate)
        else:
            statuses[candidate.game_id] = "not_sampled_below_screen"
    ordered = sorted(groups.values(), key=lambda group: min((c.decision.priority, c.lead_hours, c.game_id) for c in group))
    sampled, deferred, high_priority = [], [], []
    cost = point_calls
    for group in ordered:
        allowed = cost + 24.6 <= call_limit + 1e-9
        if allowed:
            cost += 24.6
        for candidate in group:
            (sampled if allowed else deferred).append(candidate.game_id)
            statuses[candidate.game_id] = "sample_planned_full_members" if allowed else "eligible_but_quota_deferred"
            if not allowed and candidate.decision.priority <= 1:
                high_priority.append(candidate.game_id)
    return SamplePlan(tuple(sampled), tuple(deferred), tuple(high_priority), cost, statuses)
