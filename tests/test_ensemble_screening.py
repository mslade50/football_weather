"""Screen/cap proposal is pure: no requests, forecasts or alerts are sent."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.contracts import WeatherForecast
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.openmeteo import ParsedLocation
from pipeline.weather.screening import Candidate, Decision, plan, screen

KO = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)


def case(lead=24, **values):
    fields = dict(wind=1, temp=70, gust=2, precip=0, pop=0)
    fields.update(values)
    rows = [HourlyRow(t=KO + timedelta(hours=i), **fields) for i in range(3)]
    loc = ParsedLocation(32, -96, models={"gfs": rows, "ifs": rows})
    fc = WeatherForecast("game", "nbm", lead_hours=lead, wind_fg=fields["wind"],
                         temp_fg=fields["temp"], gust_fg=fields["gust"], rain_fg_mm=fields["precip"],
                         precip_prob=fields["pop"] / 100)
    return fc, loc


def decide(fc, loc, sport="nfl", **context):
    return screen(sport, fc, loc, KO, home_temp=70, away_temp=70, **context)


def test_benign_complete_weather_is_not_sampled():
    assert not decide(*case()).eligible


def test_active_signal_is_always_retained_even_with_closed_roof():
    result = decide(*case(), active=True, roof_state="closed")
    assert result.eligible and result.priority == 0


@pytest.mark.parametrize("fields,reason", [
    ({"wind": 4, "temp": 68}, "borderline_wind_temperature"),
    ({"gust": 30}, "gust_risk"),
    ({"precip": 0.5}, "precipitation_risk"),
    ({"pop": 20}, "precipitation_risk"),
    ({"temp": 32}, "cold_or_snow_risk"),
    ({"temp": 90}, "heat_risk"),
])
def test_short_range_buffers_and_extremes(fields, reason):
    assert reason in decide(*case(**fields)).reasons


def test_cfb_uses_lowest_weekday_threshold_without_spread_gate():
    result = decide(*case(wind=4.79, temp=65), sport="cfb")
    assert result.eligible


def test_long_lead_widens_buffer_and_uses_unblended_raw_model_hours():
    assert not decide(*case(wind=3.5, temp=70)).eligible
    assert decide(*case(lead=144, wind=3.5, temp=70)).eligible
    fc, loc = case()
    loc.models["gfs"][0] = replace(loc.models["gfs"][0], wind=25)
    assert decide(fc, loc).eligible


def test_no_ensemble_confidence_or_percentile_is_used():
    fc, loc = case()
    assert decide(replace(fc, wind_p90=99, wind_vol_fc=99, precip_prob_ens=1), loc) == decide(fc, loc)


@pytest.mark.parametrize("mode", ["missing", "one_model", "missing_hour", "missing_gust", "null_point", "nonfinite", "horizon"])
def test_unreliable_point_forecasts_fail_open(mode):
    fc, loc = case()
    if mode == "missing":
        loc = None
    if mode == "one_model":
        loc.models.pop("ifs")
    if mode == "missing_hour":
        loc.models = {key: rows[:2] for key, rows in loc.models.items()}
    if mode == "missing_gust":
        fc = replace(fc, gust_fg=None)
        loc.models = {key: [replace(row, gust=None) for row in rows] for key, rows in loc.models.items()}
    if mode == "null_point":
        fc = replace(fc, wind_fg=None)
    if mode == "nonfinite":
        fc = replace(fc, wind_fg=float("nan"))
    if mode == "horizon":
        fc = replace(fc, lead_hours=400)
    assert decide(fc, loc).eligible


def test_stale_weather_is_not_used_to_reject_candidate():
    fc, loc = case()
    now = KO - timedelta(hours=24)
    assert decide(replace(fc, run_time=now - timedelta(minutes=31)), loc, now=now).eligible


def test_material_change_and_refresh_re_evaluation():
    fc, loc = case()
    assert decide(fc, loc, previous={"wind_fg": 5}).eligible
    assert not decide(fc, loc).eligible
    assert decide(*case(wind=10, temp=60)).eligible


def test_altitude_and_heat_context_are_conservative_without_odds():
    fc, loc = case(temp=75)
    assert screen("cfb", fc, loc, KO, travel_alt=600, home_temp=70, away_temp=70).eligible
    assert screen("cfb", fc, loc, KO, home_temp=None, away_temp=None).eligible
    assert not screen("cfb", fc, loc, KO, home_temp=70, away_temp=70).eligible


def test_cap_prioritizes_active_and_current_triggers_and_names_every_deferred_game():
    candidates = [Candidate("border", ("c",), Decision(True, ("borderline",), 3), 1),
                  Candidate("active", ("a",), Decision(True, ("active",), 0), 48),
                  Candidate("trigger", ("b",), Decision(True, ("current",), 1), 24)]
    result = plan(candidates, point_calls=396.6, call_limit=421.2)
    assert result.sampled_games == ("active",)
    assert set(result.deferred_games) == {"trigger", "border"}
    assert result.high_priority_deferred == ("trigger",)
    assert result.statuses["trigger"] == "eligible_but_quota_deferred"
    assert result.estimated_calls == pytest.approx(421.2)


def test_shared_location_costs_once_and_unselected_game_never_claims_member_precision():
    result = plan([Candidate("a", ("venue",), Decision(True, ("active",), 0), 24),
                   Candidate("b", ("venue",), Decision(True, ("border",), 3), 48),
                   Candidate("benign", ("other",), Decision(False, (), 4), 1)], point_calls=0, call_limit=24.6)
    assert result.sampled_games == ("a", "b") and result.estimated_calls == 24.6
    assert result.statuses["benign"] == "not_sampled_below_screen"
