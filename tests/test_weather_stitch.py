"""Phase 5 weather stitching (ARCH §6): ensemble members -> wind_vol_fc / P10-P90,
lead bands pick the right source, NWS fill, model disagreement, roof heuristic,
and the degradation path when the ensemble is missing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.weather import merge as M
from pipeline.weather.openmeteo import ENSEMBLE_URL, build_ensemble_params
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import CONTROL, EnsembleLocation, Member, parse_ensemble
from pipeline.weather.parsers.openmeteo import ParsedLocation, parse_forecast

UTC = timezone.utc
ENS_T0 = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)   # fixture spans 08:00..14:00 UTC


@pytest.fixture(scope="module")
def ens_payload(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "raw" / "openmeteo" / "ensemble.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ens(ens_payload: dict) -> EnsembleLocation:
    return parse_ensemble(ens_payload)[0]


@pytest.fixture(scope="module")
def om(fixtures_dir: Path) -> ParsedLocation:
    payload = json.loads((fixtures_dir / "raw" / "openmeteo" / "forecast_multi.json").read_text(encoding="utf-8"))
    return parse_forecast(payload)[0]


def _synthetic_ens(t0: datetime, winds: list[float], n_hours: int = 6, precip_wet: int = 0) -> EnsembleLocation:
    """Members with constant per-member wind; first ``precip_wet`` members get 1 mm/h."""
    loc = EnsembleLocation(latitude=42.0, longitude=-71.0, times=[t0 + timedelta(hours=i) for i in range(-1, n_hours - 1)])
    for i, w in enumerate(winds):
        m = Member(model="ecmwf_ifs025_ensemble", member=CONTROL if i == 0 else f"member{i:02d}")
        m.wind = [w] * n_hours
        m.gust = [w * 1.4] * n_hours
        m.precip = [1.0 if i < precip_wet else 0.0] * n_hours
        loc.members[m.key] = m
    return loc


# ---------------------------------------------------------------- parser


def test_ensemble_parse_models_members_units(ens: EnsembleLocation, ens_payload: dict):
    assert ens.models == ["ecmwf_ifs025_ensemble", "ncep_gefs_seamless"]
    assert ens.n_members("ecmwf_ifs025_ensemble") == 51 and ens.n_members("ncep_gefs_seamless") == 31
    assert ens.n_members() == 82
    assert len(ens.times) == 7 and ens.times[0] == ENS_T0
    assert ens.units["wind_speed_10m_ecmwf_ifs025_ensemble"] == "mp/h"
    ctrl = ens.members["ecmwf_ifs025_ensemble:control"]
    assert ctrl.wind == ens_payload["hourly"]["wind_speed_10m_ecmwf_ifs025_ensemble"]
    m7 = ens.members["ncep_gefs_seamless:member07"]
    assert m7.precip == ens_payload["hourly"]["precipitation_member07_ncep_gefs_seamless"]
    assert len(m7.gust) == 7


def test_ensemble_parse_error_and_list():
    with pytest.raises(ValueError):
        parse_ensemble({"error": True, "reason": "bad"})
    assert len(parse_ensemble([{"latitude": 1, "longitude": 2, "hourly": {"time": []}}] * 2)) == 2


def test_build_ensemble_params_window_and_units():
    p = build_ensemble_params([(42.09, -71.26)], ENS_T0, ENS_T0 + timedelta(hours=5))
    assert p["models"] == "ecmwf_ifs025,gfs_seamless"
    assert p["hourly"] == "wind_speed_10m,wind_gusts_10m,precipitation"
    assert p["wind_speed_unit"] == "mph" and p["precipitation_unit"] == "mm" and p["timezone"] == "UTC"
    assert p["start_hour"] == "2026-08-25T08:00" and p["end_hour"] == "2026-08-25T13:00"
    assert ENSEMBLE_URL.startswith("https://ensemble-api.open-meteo.com")


def test_fetch_ensemble_batches_and_captures(monkeypatch, ens_payload: dict):
    from pipeline.weather import openmeteo as OM

    calls = []

    def fake_get_json(client, url, params):
        assert url == ENSEMBLE_URL
        n = len(params["latitude"].split(","))
        calls.append(n)
        return [ens_payload] * n, url

    monkeypatch.setattr(OM, "_get_json", fake_get_json)
    captured = []
    pts = [(42.0 + i * 0.01, -71.0) for i in range(60)]
    locs = OM.fetch_ensemble(pts, forecast_days=2, capture=lambda name, payload, url: captured.append(name))
    assert calls == [50, 10] and len(locs) == 60
    assert captured == ["openmeteo_ensemble_00", "openmeteo_ensemble_01"]
    assert OM.fetch_ensemble([]) == []


# ---------------------------------------------------------------- statistics


def test_percentile():
    assert M.percentile([], 0.5) is None
    assert M.percentile([3.0], 0.9) == 3.0
    assert M.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert M.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.1) == pytest.approx(1.4)
    assert M.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.9) == pytest.approx(4.6)


def test_ensemble_stats_from_fixture(ens: EnsembleLocation):
    window = [ENS_T0 + timedelta(hours=i) for i in (1, 2, 3)]
    display = [ENS_T0 + timedelta(hours=i) for i in range(0, 6)]
    st = M.ensemble_stats(ens, window, display)
    assert st is not None and st.n_members == 82
    assert st.wind_p10 <= st.wind_p50 <= st.wind_p90
    assert st.wind_vol_fc == pytest.approx(st.wind_p90 - st.wind_p10)
    assert st.wind_vol_fc > 0
    assert 0.0 <= st.precip_prob_ens <= 1.0
    assert st.gust_p90 is not None and st.gust_p90 >= st.wind_p90
    assert set(st.hourly_p10) == set(display) and all(st.hourly_p10[t] <= st.hourly_p90[t] for t in display)
    # pooled member means must bracket the percentiles
    means = []
    idx = {t: i for i, t in enumerate(ens.times)}
    for m in ens.members.values():
        means.append(sum(m.wind[idx[t]] for t in window) / 3)
    assert min(means) <= st.wind_p10 and st.wind_p90 <= max(means)


def test_ensemble_stats_synthetic_values():
    t0 = ENS_T0
    winds = [float(i) for i in range(1, 22)]  # 1..21 -> p10=3, p50=11, p90=19
    loc = _synthetic_ens(t0, winds, precip_wet=7)
    window = [t0 + timedelta(hours=i) for i in range(3)]
    st = M.ensemble_stats(loc, window, window)
    assert st is not None
    assert st.wind_p10 == pytest.approx(3.0) and st.wind_p50 == 11.0 and st.wind_p90 == pytest.approx(19.0)
    assert st.wind_vol_fc == pytest.approx(16.0)
    assert st.precip_prob_ens == pytest.approx(7 / 21)


def test_ensemble_stats_rejects_missing_window_or_few_members():
    t0 = ENS_T0
    assert M.ensemble_stats(None, [t0], []) is None
    few = _synthetic_ens(t0, [5.0] * 5)
    assert M.ensemble_stats(few, [t0, t0 + timedelta(hours=1)], []) is None
    ok = _synthetic_ens(t0, [5.0] * 12)
    assert M.ensemble_stats(ok, [t0 + timedelta(hours=40)], []) is None  # outside the member times


# ---------------------------------------------------------------- merge with ensemble


def test_build_forecast_with_ensemble_fills_vol_and_band(om: ParsedLocation, ens: EnsembleLocation):
    kickoff = ENS_T0 + timedelta(hours=1, minutes=30)   # floors to 09:00; window 09..11 inside 08..14
    now = kickoff - timedelta(hours=30)
    res = M.build_forecast("nfl:2026:1:mia@ne", kickoff, now, om, None, orientation_deg=158.0,
                           roof_state="outdoors", ens=ens, expect_ensemble=True)
    fc = res.forecast
    assert fc.wind_vol_fc is not None and fc.wind_vol_fc > 0
    assert fc.wind_p10 <= fc.wind_p50 <= fc.wind_p90
    assert fc.wind_vol_fc == pytest.approx(fc.wind_p90 - fc.wind_p10)
    assert res.precip_prob_ens is not None and 0.0 <= res.precip_prob_ens <= 1.0
    assert fc.precip_prob_ens == res.precip_prob_ens     # contract field, not only the MergeResult side-output
    assert res.ensemble is not None and res.ensemble.n_members == 82
    assert len(fc.hourly) == 6
    assert all(p.p10 is not None and p.p90 is not None and p.p10 <= p.p90 for p in fc.hourly)
    assert fc.lead_hours == pytest.approx(30.0)
    assert fc.source in ("hrrr", "nbm") and fc.cross_mph is not None
    assert not [d for d in res.degradations if "ensemble" in d.reason]


def test_build_forecast_without_ensemble_degrades_to_static(om: ParsedLocation):
    kickoff = ENS_T0 + timedelta(hours=1)
    res = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=30), om, None, expect_ensemble=True)
    fc = res.forecast
    assert fc.wind_vol_fc is None and fc.wind_p10 is None and fc.wind_p90 is None
    assert all(p.p10 is None for p in fc.hourly)
    degs = [d for d in res.degradations if "ensemble missing" in d.reason]
    assert len(degs) == 1 and degs[0].severity == "info" and degs[0].component == "weather"
    # no expectation -> silent (Phase 1 behaviour)
    quiet = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=30), om, None)
    assert not [d for d in quiet.degradations if "ensemble" in d.reason]


def test_ensemble_window_outside_members_is_ignored(om: ParsedLocation, ens: EnsembleLocation):
    kickoff = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)   # ensemble fixture does not cover
    res = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=30), om, None, ens=ens, expect_ensemble=True)
    assert res.forecast.wind_vol_fc is None
    assert any("ensemble missing" in d.reason for d in res.degradations)


# ---------------------------------------------------------------- lead bands / source stamps


def _rows(hrs, wind, gust=None, temp=60.0, precip=0.0, pop=None, dir=90.0):
    return [HourlyRow(t=t, temp=temp, wind=wind, gust=gust, dir=dir, precip=precip, pop=pop) for t in hrs]


def _three_model_om(t0: datetime) -> ParsedLocation:
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    return ParsedLocation(
        latitude=42.0, longitude=-71.0,
        models={
            M.HRRR: _rows(hrs, 10.0, gust=15.0, pop=None),
            M.NBM: _rows(hrs, 12.0, gust=None, pop=40.0),
            M.GFS: _rows(hrs, 14.0, gust=20.0, pop=30.0),
            M.ECMWF: _rows(hrs, 16.0, gust=22.0, pop=20.0),
        },
    )


def test_lead_bands_pick_source_and_stamp_lead_hours():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    om = _three_model_om(t0)
    short = M.build_forecast("g", t0, t0 - timedelta(hours=6), om).forecast
    assert short.source == "hrrr" and short.wind_fg == 10.0 and short.gust_fg == 15.0
    assert short.precip_prob == pytest.approx(0.4)  # PoP from NBM
    assert short.lead_hours == pytest.approx(6.0)

    synoptic = M.build_forecast("g", t0, t0 - timedelta(hours=40), om).forecast
    assert synoptic.source == "hrrr" and synoptic.lead_hours == pytest.approx(40.0)  # HRRR covers the window

    mid = M.build_forecast("g", t0, t0 - timedelta(days=5), om).forecast
    assert mid.source == "nbm" and mid.wind_fg == 12.0 and mid.gust_fg == 20.0  # gust from GFS
    assert mid.lead_hours == pytest.approx(120.0)

    res = M.build_forecast("g", t0, t0 - timedelta(days=14), om)
    assert res.forecast.source == "gfs_ecmwf" and res.forecast.wind_fg == 15.0  # mean of GFS/ECMWF
    assert any("low_confidence" in d.reason and d.severity == "info" for d in res.degradations)


def test_hrrr_without_window_coverage_beyond_18h_falls_to_nbm():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    om = _three_model_om(t0)
    om.models[M.HRRR] = [HourlyRow(t=r.t, temp=r.temp, wind=None, gust=None, dir=None, precip=None, pop=None) for r in om.models[M.HRRR]]
    fc = M.build_forecast("g", t0, t0 - timedelta(hours=30), om).forecast
    assert fc.source == "nbm" and fc.wind_fg == 12.0


def test_model_disagreement_is_max_minus_min_of_model_means():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    om = _three_model_om(t0)
    fc = M.build_forecast("g", t0, t0 - timedelta(hours=6), om).forecast
    assert fc.model_disagreement == pytest.approx(6.0)  # 16 - 10
    only = ParsedLocation(latitude=0, longitude=0, models={M.NBM: om.models[M.NBM]})
    assert M.build_forecast("g", t0, t0 - timedelta(hours=6), only).forecast.model_disagreement is None


def test_nws_fills_null_and_stamps_source_suffix():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    om = ParsedLocation(latitude=42.0, longitude=-71.0, models={M.NBM: _rows(hrs, None, temp=None, pop=None, precip=None)})
    nws = _rows(hrs, 18.0, gust=26.0, temp=55.0, pop=70.0, precip=0.5)
    res = M.build_forecast("g", t0, t0 - timedelta(hours=30), om, nws)
    fc = res.forecast
    assert fc.source == "nbm+nws"
    assert fc.wind_fg == 18.0 and fc.temp_fg == 55.0 and fc.gust_fg == 26.0
    assert fc.precip_prob == pytest.approx(0.7) and fc.rain_fg_mm == pytest.approx(1.5)
    assert not [d for d in res.degradations if d.severity == "error"]

    beyond = M.build_forecast("g", t0, t0 - timedelta(days=8), om, nws).forecast
    assert beyond.wind_fg is None and beyond.source == "nbm"


def test_nws_only_when_openmeteo_absent_warns():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    nws = _rows(hrs, 18.0, gust=26.0, temp=55.0, pop=70.0)
    res = M.build_forecast("g", t0, t0 - timedelta(hours=30), None, nws)
    assert res.forecast.source == "nws" and res.forecast.wind_fg == 18.0
    assert any(d.severity == "warn" and "NWS-only" in d.reason for d in res.degradations)


# ---------------------------------------------------------------- roof + components


def test_roof_state_for_heuristic():
    assert M.roof_state_for("closed", "retractable", 80.0, 0.0, 5.0) == "closed"    # schedule wins
    assert M.roof_state_for(None, "dome", 80.0, 0.0, 5.0) == "dome"
    assert M.roof_state_for(None, "open", 80.0, 0.0, 5.0) == "outdoors"
    assert M.roof_state_for(None, "retractable", 39.0, 0.0, 5.0) == "closed"
    assert M.roof_state_for(None, "retractable", 70.0, 0.61, 5.0) == "closed"
    assert M.roof_state_for(None, "retractable", 70.0, 0.1, 20.1) == "closed"
    assert M.roof_state_for(None, "retractable", 70.0, 0.1, 15.0) == "open"
    assert M.roof_state_for(None, "retractable", None, None, None) == "open"
    assert M.roof_state_for(None, None, 70.0, 0.1, 15.0) is None


def test_build_forecast_retractable_heuristic_and_components():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    windy = ParsedLocation(latitude=0, longitude=0, models={M.NBM: _rows(hrs, 25.0, gust=35.0, dir=90.0, pop=10.0)})
    res = M.build_forecast("g", t0, t0 - timedelta(hours=30), windy, orientation_deg=0.0, roof_type="retractable")
    assert res.forecast.roof_state == "closed" and res.roof_heuristic
    assert res.forecast.cross_mph == 0.0 and res.forecast.head_mph == 0.0
    assert res.forecast.wind_fg == 25.0  # kept for display

    calm = ParsedLocation(latitude=0, longitude=0, models={M.NBM: _rows(hrs, 10.0, gust=14.0, dir=90.0, pop=10.0)})
    res = M.build_forecast("g", t0, t0 - timedelta(hours=30), calm, orientation_deg=0.0, roof_type="retractable")
    assert res.forecast.roof_state == "open" and res.roof_heuristic
    assert res.forecast.cross_mph == pytest.approx(10.0) and res.forecast.head_mph == pytest.approx(0.0, abs=1e-9)

    # 45 deg off a N-S field: equal cross/head components
    res = M.build_forecast("g", t0, t0 - timedelta(hours=30), calm, orientation_deg=45.0, roof_type="open")
    assert res.forecast.roof_state == "outdoors" and not res.roof_heuristic
    assert res.forecast.cross_mph == pytest.approx(res.forecast.head_mph)
    assert res.forecast.cross_mph == pytest.approx(10.0 / 2 ** 0.5)


def test_hourly_strip_covers_kickoff_minus_1_to_plus_4():
    t0 = datetime(2026, 9, 6, 17, 40, tzinfo=UTC)
    om = _three_model_om(t0.replace(minute=0))
    fc = M.build_forecast("g", t0, t0 - timedelta(hours=6), om).forecast
    ts = [p.t for p in fc.hourly]
    assert ts[0] == datetime(2026, 9, 6, 16, 0, tzinfo=UTC) and ts[-1] == datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    assert len(ts) == 6 and all(b - a == timedelta(hours=1) for a, b in zip(ts, ts[1:], strict=False))
