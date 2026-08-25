"""Weather parsers + merge (ARCH §6) from real captured fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.contracts import WeatherForecast
from pipeline.weather import merge as M
from pipeline.weather.openmeteo import BATCH_SIZE, build_params, window_for
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.nws import KMH_TO_MPH, expand_field, parse_duration, parse_gridpoints
from pipeline.weather.parsers.openmeteo import ParsedLocation, match_location, parse_forecast

UTC = timezone.utc
MW = M.CB.DEFAULT_MEDIUM_WEIGHTS


@pytest.fixture(autouse=True)
def _data_free_merge(monkeypatch):
    """No data/climatology.csv shrinkage and default blend config (see test_forecast_blend.py)."""
    monkeypatch.setattr(M, "_default_climo", lambda: None)
    monkeypatch.setattr(M, "_default_blend_cfg", lambda: M.CB.DEFAULT_CONFIG)


@pytest.fixture(scope="module")
def om_payload(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "raw" / "openmeteo" / "forecast_multi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nws_payload(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "raw" / "nws" / "gridpoints.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def om(om_payload: dict) -> ParsedLocation:
    return parse_forecast(om_payload)[0]


@pytest.fixture(scope="module")
def nws_rows(nws_payload: dict):
    return parse_gridpoints(nws_payload)


def _hrrr_window(om: ParsedLocation):
    """First kickoff hour where HRRR has 3 consecutive non-null wind samples."""
    rows = om.models[M.HRRR]
    for i in range(len(rows) - 2):
        if all(rows[i + k].wind is not None for k in range(3)):
            return rows[i].t, rows[i : i + 3]
    raise AssertionError("fixture has no HRRR-covered window")


# ---------------------------------------------------------------- parsers


def test_openmeteo_parse_models_and_units(om: ParsedLocation, om_payload: dict):
    assert set(om.models) == {M.NBM, M.HRRR, M.GFS, M.ECMWF}
    assert len(om.models[M.NBM]) == len(om_payload["hourly"]["time"]) == 72
    assert om.units["wind_speed_10m_ncep_nbm_conus"] == "mp/h"
    assert om.units["precipitation_ncep_nbm_conus"] == "mm"
    first = om.models[M.NBM][0]
    assert first.t == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    assert first.wind == om_payload["hourly"]["wind_speed_10m_ncep_nbm_conus"][0]
    assert first.pop == om_payload["hourly"]["precipitation_probability_ncep_nbm_conus"][0]
    assert abs(om.latitude - 42.0909) < 0.1 and abs(om.longitude + 71.2643) < 0.1


def test_openmeteo_parse_batched_list_and_single_model(om_payload: dict):
    single = {"latitude": 1.0, "longitude": 2.0, "hourly": {"time": ["2026-01-01T00:00"], "wind_speed_10m": [3.5], "precipitation_probability": [40]}}
    locs = parse_forecast([om_payload, single])
    assert len(locs) == 2
    assert locs[1].models[M.BEST][0].wind == 3.5
    assert locs[1].models[M.BEST][0].pop == 40
    assert match_location(locs, 42.0909, -71.2643) is locs[0]
    assert match_location(locs, 10.0, 10.0) is None


def test_openmeteo_error_payload_raises():
    with pytest.raises(ValueError):
        parse_forecast({"error": True, "reason": "bad"})


def test_nws_duration_and_expansion():
    assert parse_duration("PT1H") == timedelta(hours=1)
    assert parse_duration("P1DT2H") == timedelta(hours=26)
    assert parse_duration("PT30M") == timedelta(minutes=30)
    fld = {"uom": "wmoUnit:km_h-1", "values": [{"validTime": "2026-08-23T13:00:00+00:00/PT2H", "value": 10.0}]}
    got = expand_field(fld)
    assert got == {
        datetime(2026, 8, 23, 13, tzinfo=UTC): 10.0 * KMH_TO_MPH,
        datetime(2026, 8, 23, 14, tzinfo=UTC): 10.0 * KMH_TO_MPH,
    }
    qpf = {"uom": "wmoUnit:mm", "values": [{"validTime": "2026-08-23T13:00:00+00:00/PT6H", "value": 3.0}]}
    spread = expand_field(qpf, accumulated=True)
    assert len(spread) == 6 and all(abs(v - 0.5) < 1e-12 for v in spread.values())
    temp = {"uom": "wmoUnit:degC", "values": [{"validTime": "2026-08-23T13:00:00+00:00/PT1H", "value": 20}]}
    assert expand_field(temp)[datetime(2026, 8, 23, 13, tzinfo=UTC)] == 68.0


def test_nws_gridpoints_fixture(nws_rows, nws_payload: dict):
    assert nws_rows, "no rows parsed"
    props = nws_payload["properties"]
    first_t = datetime.fromisoformat(props["temperature"]["values"][0]["validTime"].split("/")[0])
    r0 = next(r for r in nws_rows if r.t == first_t.astimezone(UTC))
    assert r0.temp == pytest.approx(props["temperature"]["values"][0]["value"] * 9 / 5 + 32)
    assert r0.wind == pytest.approx(props["windSpeed"]["values"][0]["value"] * KMH_TO_MPH)
    assert r0.dir == props["windDirection"]["values"][0]["value"]
    assert r0.pop == props["probabilityOfPrecipitation"]["values"][0]["value"]
    # contiguous hourly coverage inside 72h
    ts = [r.t for r in nws_rows]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert (ts[-1] - ts[0]) <= timedelta(hours=73)


def test_three_hour_mean_reproduces_legacy_kmh_multiples():
    """Old wind_fg = mean over 3 hourly points of km/h*0.621371 -> exact multiples of 0.0207124 (1-dp km/h)."""
    kmh = [12.3, 15.0, 9.7]
    mph = [k * KMH_TO_MPH for k in kmh]
    fg = M.mean3(mph)
    step = 0.1 * KMH_TO_MPH / 3  # 0.0207123...
    assert abs(step - 0.0207124) < 1e-6
    assert abs(fg / step - round(fg / step)) < 1e-6
    assert fg == sum(mph) / 3


# ---------------------------------------------------------------- merge helpers


def test_compass_and_vector_mean():
    assert M.compass16(0) == "N" and M.compass16(359) == "N"
    assert M.compass16(45) == "NE" and M.compass16(22.5) == "NNE" and M.compass16(90) == "E" and M.compass16(202) == "SSW"
    assert M.compass16(None) is None
    assert M.vector_mean_deg([350, 10]) == pytest.approx(0.0, abs=1e-9)
    assert M.vector_mean_deg([90, 180]) == pytest.approx(135.0)
    assert M.vector_mean_deg([]) is None


def test_wind_components():
    cross, head = M.wind_components(10.0, 90.0, 0.0)
    assert cross == pytest.approx(10.0) and head == pytest.approx(0.0)
    cross, head = M.wind_components(10.0, 45.0, 45.0)
    assert cross == pytest.approx(0.0) and head == pytest.approx(10.0)
    assert M.wind_components(None, 45.0, 45.0) == (None, None)


def test_choose_regime():
    assert M.choose_regime(6, False).label == "hrrr"
    assert M.choose_regime(30, True).label == "hrrr"
    assert M.choose_regime(30, False).label == "nbm"
    assert M.choose_regime(100, False).label == "nbm" and M.choose_regime(168, False).label == "nbm"
    assert M.choose_regime(200, False).label == "medium"
    assert M.choose_regime(300, False).label == "medium"
    # inside 7 d the medium-range blend is only a fallback; beyond it is the primary source
    assert M.choose_regime(100, False).prefs["wind"][:2] == [M.NBM, M.MEDIUM]
    assert M.choose_regime(100, False).prefs["gust"][0] == M.MEDIUM
    assert M.choose_regime(30, False).prefs["gust"][0] == M.GFS
    assert M.choose_regime(200, False).prefs["wind"][0] == M.MEDIUM
    assert M.choose_regime(200, False).weights == {M.AIFS: 0.4, M.ECMWF: 0.35, M.GFS: 0.25}


# ---------------------------------------------------------------- merge from fixtures


def test_short_lead_uses_hrrr_and_nbm_pop(om: ParsedLocation, nws_rows):
    kickoff, hrrr = _hrrr_window(om)
    kickoff = kickoff + timedelta(minutes=25)  # 1:25 -> floors to the hour
    now = kickoff - timedelta(hours=6)
    res = M.build_forecast("nfl:2026:1:mia@ne", kickoff, now, om, nws_rows, orientation_deg=45.0, roof_state="outdoors")
    fc = res.forecast
    assert isinstance(fc, WeatherForecast)
    assert res.regime == "hrrr" and fc.source == "hrrr"
    assert fc.lead_hours == pytest.approx(6.0)
    assert fc.wind_fg == (hrrr[0].wind + hrrr[1].wind + hrrr[2].wind) / 3
    assert fc.temp_fg == (hrrr[0].temp + hrrr[1].temp + hrrr[2].temp) / 3
    assert fc.rain_fg_mm == hrrr[0].precip + hrrr[1].precip + hrrr[2].precip
    nbm = {r.t: r for r in om.models[M.NBM]}
    exp_pop = sum(nbm[r.t].pop for r in hrrr) / 3 / 100.0
    assert fc.precip_prob == pytest.approx(exp_pop)
    assert fc.wind_dir_1h == M.compass16(hrrr[1].dir)
    assert fc.wind_dir_2h == M.compass16(hrrr[2].dir)
    assert fc.wind_dir_fg == M.compass16(fc.wind_dir_deg)
    assert fc.cross_mph is not None and fc.head_mph is not None
    assert fc.cross_mph ** 2 + fc.head_mph ** 2 == pytest.approx(fc.wind_fg ** 2)
    assert fc.model_disagreement is not None and fc.model_disagreement >= 0
    assert fc.wind_vol_fc is None and fc.wind_p10 is None and fc.wind_p90 is None
    assert len(fc.hourly) == 6 and fc.hourly[0].t == M.hour_floor(kickoff) - timedelta(hours=1)
    assert all(p.p10 is None for p in fc.hourly)
    assert not res.degradations


def test_mid_lead_uses_nbm_and_gfs_gust(om: ParsedLocation):
    kickoff = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    now = kickoff - timedelta(hours=60)
    res = M.build_forecast("cfb:2026:1:a@b", kickoff, now, om, None)
    fc = res.forecast
    assert fc.source == "nbm"
    nbm = {r.t: r for r in om.models[M.NBM]}
    gfs = {r.t: r for r in om.models[M.GFS]}
    hrs = [kickoff + timedelta(hours=i) for i in range(3)]
    assert fc.wind_fg == sum(nbm[h].wind for h in hrs) / 3
    # 48 h < lead: gusts (absent from NBM) come from the medium-range blend of the members present (IFS + GFS; no AIFS in the fixture)
    ec = {r.t: r for r in om.models[M.ECMWF]}
    exp_gust = sum((MW["ifs"] * ec[h].gust + MW["gfs"] * gfs[h].gust) / (MW["ifs"] + MW["gfs"]) for h in hrs) / 3
    assert fc.gust_fg == pytest.approx(exp_gust)
    assert fc.cross_mph is None  # no orientation given
    assert not res.degradations

    day1 = M.build_forecast("cfb:2026:1:a@b", kickoff, kickoff - timedelta(hours=40), om, None).forecast
    assert day1.source == "nbm" and day1.gust_fg == sum(gfs[h].gust for h in hrs) / 3  # <= 48 h: GFS gusts as before


def test_long_lead_blends_gfs_ecmwf_with_info_degradation(om: ParsedLocation):
    kickoff = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    now = kickoff - timedelta(days=14)
    res = M.build_forecast("cfb:2026:1:a@b", kickoff, now, om, None)
    fc = res.forecast
    assert fc.source == "medium:ifs+gfs"  # fixture predates AIFS; the blend lists the members it found
    gfs = {r.t: r for r in om.models[M.GFS]}
    ec = {r.t: r for r in om.models[M.ECMWF]}
    hrs = [kickoff + timedelta(hours=i) for i in range(3)]
    exp = sum((MW["ifs"] * ec[h].wind + MW["gfs"] * gfs[h].wind) / (MW["ifs"] + MW["gfs"]) for h in hrs) / 3
    assert fc.wind_fg == pytest.approx(exp)
    assert [d.severity for d in res.degradations] == ["info"]
    assert "low_confidence" in res.degradations[0].reason


def test_nws_only_when_openmeteo_missing(nws_rows):
    base = nws_rows[5].t
    kickoff = base
    now = base - timedelta(hours=12)
    res = M.build_forecast("nfl:2026:1:mia@ne", kickoff, now, None, nws_rows)
    fc = res.forecast
    assert fc.source == "nws"
    idx = {r.t: r for r in nws_rows}
    exp = M.mean3([idx[base + timedelta(hours=i)].wind for i in range(3)])
    assert fc.wind_fg == exp
    assert [d.severity for d in res.degradations] == ["warn"]
    assert "NWS-only" in res.degradations[0].reason


def test_no_source_at_all_is_error():
    kickoff = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    res = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=3), None, None)
    assert res.forecast.wind_fg is None
    assert [d.severity for d in res.degradations] == ["error"]


def test_nws_fills_null_fields_within_horizon():
    t0 = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    hrrr = [HourlyRow(t=t, temp=70.0, wind=(None if t == t0 + timedelta(hours=1) else 8.0), gust=12.0, dir=90.0, precip=0.0, pop=None) for t in hrs]
    nbm = [HourlyRow(t=t, temp=71.0, wind=9.0, gust=None, dir=95.0, precip=0.1, pop=None) for t in hrs]
    om = ParsedLocation(latitude=42.0, longitude=-71.0, models={M.HRRR: hrrr, M.NBM: nbm})
    nws = [HourlyRow(t=t, temp=65.0, wind=20.0, gust=25.0, dir=180.0, precip=1.0, pop=50.0) for t in hrs]
    res = M.build_forecast("nfl:2026:1:a@b", t0, t0 - timedelta(hours=5), om, nws)
    fc = res.forecast
    # wind: hrrr 8, nbm fallback 9 (NWS only fills when every model is null), hrrr 8
    assert fc.wind_fg == (8.0 + 9.0 + 8.0) / 3
    # pop null in every model -> NWS 50% for all three hours
    assert fc.precip_prob == pytest.approx(0.5)
    assert fc.source == "hrrr"  # wind never needed NWS


def test_nws_not_used_beyond_seven_days():
    t0 = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    gfs = [HourlyRow(t=t, temp=70.0, wind=8.0, gust=None, dir=90.0, precip=0.0, pop=None) for t in hrs]
    om = ParsedLocation(latitude=42.0, longitude=-71.0, models={M.GFS: gfs})
    nws = [HourlyRow(t=t, temp=65.0, wind=20.0, gust=25.0, dir=180.0, precip=1.0, pop=50.0) for t in hrs]
    res = M.build_forecast("nfl:2026:1:a@b", t0, t0 - timedelta(days=9), om, nws)
    assert res.forecast.precip_prob is None
    assert res.forecast.gust_fg is None


def test_dome_zeroes_components_but_keeps_weather(om: ParsedLocation):
    kickoff, hrrr = _hrrr_window(om)
    res = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=3), om, None, orientation_deg=0.0, roof_state="dome")
    fc = res.forecast
    assert fc.roof_state == "dome"
    assert fc.cross_mph == 0.0 and fc.head_mph == 0.0
    assert fc.wind_fg is not None


def test_forecast_round_trips_to_dict(om: ParsedLocation):
    kickoff, _ = _hrrr_window(om)
    fc = M.build_forecast("nfl:2026:1:a@b", kickoff, kickoff - timedelta(hours=3), om, None).forecast
    d = fc.to_dict()
    assert d["game_id"] == "nfl:2026:1:a@b"
    assert len(d["hourly"]) == 6 and set(d["hourly"][0]) == {"t", "temp", "wind", "gust", "dir", "precip", "pop", "p10", "p90"}


# ---------------------------------------------------------------- client param building


def test_build_params_and_window():
    pts = [(42.0909, -71.2643), (40.8135, -74.0745)]
    k1 = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    k2 = datetime(2026, 9, 14, 0, 20, tzinfo=UTC)
    start, end = window_for([k1, k2])
    assert start == k1 - timedelta(hours=1) and end == k2 + timedelta(hours=4)
    p = build_params(pts, start, end)
    assert p["latitude"] == "42.0909,40.8135" and p["longitude"] == "-71.2643,-74.0745"
    assert p["start_hour"] == "2026-09-13T16:00" and p["end_hour"] == "2026-09-14T04:00"
    assert p["wind_speed_unit"] == "mph" and p["temperature_unit"] == "fahrenheit"
    assert p["precipitation_unit"] == "mm" and p["timezone"] == "UTC"
    assert "ncep_hrrr_conus" in p["models"]
    assert BATCH_SIZE == 50


def test_fetch_forecast_batches_and_captures(monkeypatch, om_payload: dict):
    from pipeline.weather import openmeteo as OM

    calls = []

    def fake_get_json(client, url, params):
        n = len(params["latitude"].split(","))
        calls.append(n)
        return [om_payload] * n, url

    monkeypatch.setattr(OM, "_get_json", fake_get_json)
    captured = []
    pts = [(42.0 + i * 0.01, -71.0) for i in range(120)]
    locs = OM.fetch_forecast(pts, forecast_days=2, capture=lambda name, payload, url: captured.append(name))
    assert calls == [50, 50, 20]
    assert len(locs) == 120
    assert captured == ["openmeteo_forecast_00", "openmeteo_forecast_01", "openmeteo_forecast_02"]
