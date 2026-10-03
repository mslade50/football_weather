"""Point-only joint predicates; buffers are not calibrated probabilities."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.contracts import WeatherForecast
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.openmeteo import ParsedLocation
from pipeline.weather.screening import screen

KO = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)


def case(lead=24, **values):
    fields = dict(wind=1, temp=70, gust=2, precip=0, pop=0)
    fields.update(values)
    rows = [HourlyRow(t=KO + timedelta(hours=i), **fields) for i in range(3)]
    loc = ParsedLocation(32, -96, models={"gfs": rows, "ifs": rows})
    fc = WeatherForecast("game", "nbm", lead_hours=lead, wind_fg=fields["wind"],
                         temp_fg=fields["temp"], gust_fg=fields["gust"], rain_fg_mm=3 * fields["precip"],
                         precip_prob=fields["pop"] / 100)
    return fc, loc


def decide(fc, loc, sport="nfl", **context):
    return screen(sport, fc, loc, KO, **context)


def test_benign_complete_weather_is_not_sampled():
    assert not decide(*case()).eligible


def test_active_signal_always_retained_even_closed_or_missing():
    assert decide(*case(), active=True, roof_state="closed").priority == 0
    assert decide(case()[0], None, active=True).eligible


@pytest.mark.parametrize("fields", [{"gust": 35}, {"temp": 90}, {"temp": 20}, {"pop": 95}])
def test_no_heat_cold_gust_or_probability_only_selection(fields):
    assert not decide(*case(**fields)).eligible


def test_rain_uses_three_hour_total_without_widening_long_lead_buffer():
    assert decide(*case(precip=0.5)).reasons == ("game_window_rain",)
    assert decide(*case(precip=0.4)).reasons == ("rain_tail_risk",)
    assert not decide(*case(precip=0.05)).eligible
    assert decide(*case(lead=144, precip=0.4)).eligible


def test_rain_tail_guard_covers_wet_model_when_primary_point_model_is_dry():
    fc, loc = case()
    loc.models["aifs"] = [replace(row, precip=0.1, pop=None) for row in loc.models["gfs"]]
    assert decide(fc, loc).reasons == ("rain_tail_risk",)
    loc.models["aifs"][0] = replace(loc.models["aifs"][0], precip=0)
    assert not decide(fc, loc).eligible


def test_joint_conditions_same_model_and_hour():
    fc, loc = case()
    loc.models["gfs"] = [replace(row, wind=25, temp=80) for row in loc.models["gfs"]]
    loc.models["ifs"] = [replace(row, wind=1, temp=30) for row in loc.models["ifs"]]
    assert not decide(fc, loc).eligible
    loc.models["gfs"][0] = replace(loc.models["gfs"][0], temp=60)
    assert "joint_wind_temperature" in decide(fc, loc).reasons


def test_irrelevant_hours_cannot_trigger():
    fc, loc = case()
    loc.models["gfs"].append(HourlyRow(t=KO - timedelta(hours=2), wind=30, temp=30, precip=10))
    assert not decide(fc, loc).eligible


def test_climatology_weight_applied_to_raw_pair():
    fc, loc = case(lead=240, wind=30, temp=90)
    fc = replace(fc, wind_fg=10, temp_fg=45, climo_wind=10, climo_temp=40)
    assert decide(fc, loc).eligible


def test_long_lead_does_not_widen_buffer_and_cfb_uses_actual_run_day():
    assert not decide(*case(wind=4.9, temp=60)).eligible
    assert not decide(*case(lead=144, wind=4.9, temp=60)).eligible
    assert not decide(*case(wind=6.30, temp=65), sport="cfb").eligible
    assert decide(*case(wind=9, temp=65), sport="cfb").eligible


def test_no_percentile_or_confidence_in_screen():
    fc, loc = case()
    assert decide(replace(fc, wind_p90=99, wind_vol_fc=99, precip_prob_ens=1), loc) == decide(fc, loc)


@pytest.mark.parametrize("mode", ["missing", "missing_hour", "null_point", "nonfinite", "horizon"])
def test_unreliable_point_requests_detail_without_claiming_member_eligibility(mode):
    fc, loc = case()
    if mode == "missing":
        loc = None
    elif mode == "missing_hour":
        loc.models = {key: rows[:2] for key, rows in loc.models.items()}
    elif mode == "null_point":
        fc = replace(fc, wind_fg=None)
    elif mode == "nonfinite":
        fc = replace(fc, wind_fg=float("nan"))
    else:
        fc = replace(fc, lead_hours=400)
    decision = decide(fc, loc)
    assert not decision.eligible and decision.priority == 2


def test_optional_gust_missing_does_not_make_complete_point_unreliable():
    fc, loc = case()
    loc.models = {key: [replace(row, gust=None, pop=None) for row in rows] for key, rows in loc.models.items()}
    assert not decide(replace(fc, gust_fg=None), loc).eligible


def test_stale_point_and_material_change():
    fc, loc = case()
    now = KO - timedelta(hours=24)
    assert decide(replace(fc, run_time=now - timedelta(minutes=31)), loc, now=now).priority == 2
    assert not decide(fc, loc, previous={"wind_fg": 20}).eligible
    fc, loc = case(wind=10, temp=60)
    assert "material_point_change" in decide(fc, loc, previous={"wind_fg": 5}).reasons


@pytest.mark.parametrize("spread", [-10.01, 10.01, -30, 30])
def test_cfb_known_disqualified_opener_does_not_request_members(spread):
    decision = decide(*case(wind=30, temp=30, precip=3), sport="cfb", open_spread=spread)
    assert decision.reasons == ("cfb_opening_spread_ineligible",) and decision.priority == 3
    assert decide(*case(), sport="cfb", open_spread=spread, active=True).priority == 0


@pytest.mark.parametrize("spread", [-10, 10, 0, None, float("nan")])
def test_cfb_boundary_or_unknown_opener_retains_near_signal_coverage(spread):
    assert decide(*case(wind=12, temp=50), sport="cfb", open_spread=spread).eligible


def test_nws_purdue_tail_is_sent_to_detail_without_broadening_final_member_buffer():
    fc, loc = case(wind=5.75, temp=70)
    assert decide(fc, loc, sport="cfb", for_refinement=True).eligible
    assert not decide(fc, loc, sport="cfb").eligible
    assert decide(*case(wind=6.7, temp=66.5), sport="cfb").eligible


def test_cfb_combined_wind_flag_uses_its_joint_temperature_boundary():
    assert "joint_cfb_wind_flag" in decide(*case(wind=14, temp=69), sport="cfb").reasons
    assert not decide(*case(wind=14, temp=75), sport="cfb").eligible
