"""Lead-weighted climatology shrinkage (ARCH §6): curves + config parsing, the ERA5 cell
table, and its application inside merge.build_forecast (blended vs raw fields)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.contracts import WeatherForecast
from pipeline.outputs.json_out import _weather_block
from pipeline.weather import climatology_blend as CB
from pipeline.weather import merge as M
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import CONTROL, EnsembleLocation, Member
from pipeline.weather.parsers.openmeteo import ParsedLocation

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)   # Sunday 1 pm EDT: ISO week 40, solar bin 2 at lon -71 (12-18 local)
LON = -71.0


@pytest.fixture(autouse=True)
def _no_auto_climo(monkeypatch):
    monkeypatch.setattr(M, "_default_climo", lambda: None)
    monkeypatch.setattr(M, "_default_blend_cfg", lambda: M.CB.DEFAULT_CONFIG)


# ---------------------------------------------------------------- curves / config


def test_default_curve_is_flat_then_linear_then_held():
    c = CB.Curve(floor=0.45)
    assert c.weight(None) == 1.0 and c.weight(0) == 1.0 and c.weight(24) == 1.0 and c.weight(48) == 1.0
    assert c.weight(108) == pytest.approx(1.0 - 0.55 * 0.5)
    assert c.weight(168) == pytest.approx(0.45) and c.weight(300) == pytest.approx(0.45)
    assert CB.DEFAULT_CONFIG.weight(168, "wind") == 0.45 and CB.DEFAULT_CONFIG.weight(168, "gust") == 0.45
    assert CB.DEFAULT_CONFIG.weight(168, "temp") == 0.7 and CB.DEFAULT_CONFIG.weight(168, "rain_prob") == 0.5


def test_fitted_points_are_interpolated_piecewise():
    c = CB.Curve(full_h=48, floor_h=168, floor=0.3, points=((168, 0.5), (72, 0.9)))   # unsorted on purpose
    assert c.weight(48) == 1.0 and c.weight(60) == pytest.approx(0.95)
    assert c.weight(72) == pytest.approx(0.9) and c.weight(120) == pytest.approx(0.7)
    assert c.weight(168) == pytest.approx(0.5) and c.weight(400) == pytest.approx(0.5)   # points win over floor
    assert CB.Curve(points=((10, 0.2),)).weight(100) == pytest.approx(CB.Curve().weight(100))  # points <= full_h ignored


def test_parse_blend_block_tolerates_garbage_and_clamps():
    assert CB.parse_blend_block(None) is CB.DEFAULT_CONFIG and CB.parse_blend_block("x") is CB.DEFAULT_CONFIG
    cfg = CB.parse_blend_block({
        "weights": {"wind": {"floor": 1.7, "points": [[72, "bad"], [96, 0.8], "junk"]}, "temp": "nope"},
        "medium_range_weights": {"aifs": -1, "ifs": "x", "gfs": 0.5, "other": 0.1},
        "medium_range_start_h": "144",
    })
    assert cfg.curves["wind"].floor == 1.0 and cfg.curves["wind"].points == ((96.0, 0.8),)
    assert cfg.curves["temp"] == CB.DEFAULT_CURVES["temp"] and cfg.curves["rain_prob"] == CB.DEFAULT_CURVES["rain_prob"]
    assert cfg.medium_weights == {"gfs": 0.5, "other": 0.1} and cfg.medium_start_h == 144.0
    assert M.medium_weights(cfg) == {M.GFS: 0.5}   # unknown aliases dropped when mapping to model ids
    zero = CB.parse_blend_block({"medium_range_weights": {"aifs": 0, "ifs": 0}})
    assert zero.medium_weights == CB.DEFAULT_MEDIUM_WEIGHTS


def test_load_blend_config_from_file_and_shipped_calibration(tmp_path: Path):
    assert CB.load_blend_config(tmp_path / "missing.json") is CB.DEFAULT_CONFIG
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"forecast_blend": {"weights": {"temp": {"floor": 0.9}}}}), encoding="utf-8")
    cfg = CB.load_blend_config(p)
    assert cfg.curves["temp"].floor == 0.9 and cfg.curves["wind"].floor == 0.45 and cfg.origin == "cal.json"
    shipped = CB.load_blend_config(ROOT / "data" / "calibration.json", use_cache=False)
    assert set(shipped.curves) == set(CB.KINDS) and sum(shipped.medium_weights.values()) > 0
    assert all(0.0 < shipped.curves[k].weight(168) <= 1.0 for k in CB.KINDS)
    assert all(shipped.curves[k].weight(24) == 1.0 for k in CB.KINDS)


def test_blend_arithmetic_and_edge_cases():
    cfg = CB.DEFAULT_CONFIG
    assert CB.blend(None, 5.0, 100, "wind", cfg) is None
    assert CB.blend(12.0, None, 100, "wind", cfg) == 12.0
    assert CB.blend(12.0, 8.0, 24, "wind", cfg) == 12.0
    w = cfg.weight(168, "wind")
    assert CB.blend(12.0, 8.0, 168, "wind", cfg) == pytest.approx(w * 12.0 + (1 - w) * 8.0)
    assert CB.blend(12.0, 8.0, 168, "gust", cfg) == CB.blend(12.0, 8.0, 168, "wind", cfg)
    assert CB.blend(0.9, 0.9, 400, "rain_prob", cfg) == pytest.approx(0.9)
    assert CB.blend(1.5, 1.2, 400, "rain_prob", cfg) == 1.0 and CB.blend(-2.0, -1.0, 400, "wind", cfg) == 0.0


# ---------------------------------------------------------------- local-time keys + table


def test_local_key_uses_solar_time_and_iso_week():
    assert CB.solar_offset_h(-71.0) == -5 and CB.solar_offset_h(0.0) == 0 and CB.solar_offset_h(-118.0) == -8
    assert CB.local_key(T0, LON) == (10, 40, 2)                       # 17 UTC -> 12 solar -> bin 2
    assert CB.local_key(datetime(2026, 10, 5, 3, 0, tzinfo=UTC), LON) == (10, 40, 3)   # 03 UTC Mon -> 22 Sun (week 40)
    assert CB.local_key(datetime(2027, 1, 1, 12, 0, tzinfo=UTC), 0.0) == (1, 53, 2)     # Fri 2027-01-01 = ISO week 53 of 2026
    assert CB.in_season(8) and CB.in_season(1) and not CB.in_season(7)


def _cell(sid: str = "gillette", wk: int = 40, tb: int = 2, **kw) -> CB.ClimoCell:
    base = dict(n_hours=400, wind_mean=8.0, wind_p10=3.0, wind_p50=7.0, wind_p90=14.0, gust_mean=16.0, gust_p90=28.0,
                temp_mean=58.0, temp_p10=45.0, temp_p50=58.0, temp_p90=70.0, rain_freq=0.06)
    base.update(kw)
    return CB.ClimoCell(stadium_id=sid, iso_week=wk, tod_bin=tb, **base)


def test_climo_table_lookup_by_id_and_nearest(tmp_path: Path):
    table = CB.ClimoTable([_cell(), _cell(tb=3, wind_mean=6.0), _cell("far", wind_mean=20.0)], {"gillette": (42.09, -71.26), "far": (40.0, -80.0)})
    assert len(table) == 3 and table.stadium_ids() == {"gillette", "far"}
    assert table.lookup(T0, stadium_id="gillette").wind_mean == 8.0
    assert table.lookup(T0 + timedelta(hours=6), stadium_id="gillette").wind_mean == 6.0   # next bin
    assert table.lookup(T0 + timedelta(days=7), stadium_id="gillette") is None             # week 41 not in table
    assert table.lookup(T0, lat=42.098, lon=-71.253).stadium_id == "gillette"              # Open-Meteo snapped coords
    assert table.lookup(T0, lat=42.5, lon=-71.26) is None                                   # > 0.3 deg away
    assert table.lookup(T0, stadium_id="unknown", lat=40.05, lon=-80.1).stadium_id == "far"  # id miss -> nearest
    assert table.lookup(T0) is None
    # csv round trip through the stadiums/climatology.py writer
    from pipeline.stadiums.climatology import write_climatology

    p = tmp_path / "climatology.csv"
    rows = [{"stadium_id": "gillette", "lat": 42.09, "lon": -71.26, "avg_wind_sep": 7.0, "avg_temp_f": 60.0},
            {"stadium_id": "gillette", "lat": 42.09, "lon": -71.26, "iso_week": 40, "tod_bin": 2, "n_hours": 400, "wind_mean": 8.0, "temp_mean": 58.0, "rain_freq": 0.06}]
    write_climatology(p, rows)
    back = CB.ClimoTable.from_csv(p)
    assert len(back) == 1 and back.points["gillette"] == (42.09, -71.26)
    c = back.lookup(T0, stadium_id="gillette")
    assert c.wind_mean == 8.0 and c.temp_mean == 58.0 and c.rain_freq == 0.06 and c.wind_p10 is None and c.n_hours == 400
    assert CB.default_table(tmp_path / "none.csv") is None and CB.ClimoTable.from_csv(tmp_path / "none.csv").cells == {}


def test_shipped_climatology_cells_are_sane():
    table = CB.default_table(use_cache=False)
    if table is None:
        pytest.skip("data/climatology.csv has no weekly cells (run pipeline.stadiums.climatology)")
    assert len(table) > 1000 and len(table.stadium_ids()) > 100
    for c in table.cells.values():
        assert c.n_hours >= 24 and 0.0 <= c.rain_freq <= 1.0
        assert c.wind_p10 <= c.wind_p50 <= c.wind_p90 and c.temp_p10 <= c.temp_p50 <= c.temp_p90
        assert 0.0 <= c.wind_mean < 40.0 and -40.0 < c.temp_mean < 110.0 and c.gust_mean >= c.wind_mean
    assert {k[2] for k in table.cells} == {0, 1, 2, 3}
    assert {k[1] for k in table.cells} <= set(range(1, 7)) | set(range(30, 54))   # Aug..Jan ISO weeks only


# ---------------------------------------------------------------- merge integration


def _rows(hrs, wind, gust=None, temp=60.0, precip=0.0, pop=None, dir=90.0):
    return [HourlyRow(t=t, temp=temp, wind=wind, gust=gust, dir=dir, precip=precip, pop=pop) for t in hrs]


def _om(t0: datetime = T0) -> ParsedLocation:
    hrs = [t0 + timedelta(hours=i) for i in range(-1, 5)]
    return ParsedLocation(latitude=42.098, longitude=-71.253, models={
        M.NBM: _rows(hrs, 12.0, gust=None, pop=40.0, precip=0.2),
        M.GFS: _rows(hrs, 14.0, gust=20.0, pop=30.0),
        M.ECMWF: _rows(hrs, 16.0, gust=22.0, pop=20.0),
    })


def _ens(t0: datetime = T0) -> EnsembleLocation:
    loc = EnsembleLocation(latitude=42.098, longitude=-71.253, times=[t0 + timedelta(hours=i) for i in range(-1, 5)])
    for i, w in enumerate(float(x) for x in range(1, 22)):   # p10 3, p50 11, p90 19
        m = Member(model="ecmwf_ifs025_ensemble", member=CONTROL if i == 0 else f"member{i:02d}")
        m.wind, m.gust, m.precip = [w] * 6, [w * 1.4] * 6, [0.0] * 6
        loc.members[m.key] = m
    return loc


TABLE = CB.ClimoTable([_cell()], {"gillette": (42.09, -71.26)})


def test_blend_applies_beyond_48h_and_keeps_raw():
    cfg = CB.DEFAULT_CONFIG
    res = M.build_forecast("g", T0, T0 - timedelta(days=5), _om(), None, orientation_deg=0.0, ens=_ens(), climo=TABLE, blend_cfg=cfg, stadium_id="gillette")
    fc = res.forecast
    w = cfg.weight(120.0, "wind")
    assert 0.0 < w < 1.0 and fc.blend_w == pytest.approx(w) and res.blend_w == fc.blend_w and res.climo_cell is TABLE.cells[("gillette", 40, 2)]
    assert fc.wind_fg_raw == 12.0 and fc.wind_fg == pytest.approx(w * 12.0 + (1 - w) * 8.0)
    assert fc.climo_wind == 8.0 and fc.climo_temp == 58.0
    assert fc.temp_fg_raw == 60.0 and fc.temp_fg == pytest.approx(cfg.weight(120.0, "temp") * 60.0 + (1 - cfg.weight(120.0, "temp")) * 58.0)
    gust_raw = (0.35 * 22.0 + 0.25 * 20.0) / 0.6
    assert fc.gust_fg == pytest.approx(w * gust_raw + (1 - w) * 16.0)
    wr = cfg.weight(120.0, "rain_prob")
    assert fc.precip_prob == pytest.approx(wr * 0.4 + (1 - wr) * 0.06)
    assert fc.rain_fg_mm == pytest.approx(0.6)   # amount never blended
    # ensemble band pulled toward the climatological band the same way
    assert fc.wind_p10 == pytest.approx(w * 3.0 + (1 - w) * 3.0) and fc.wind_p90 == pytest.approx(w * 19.0 + (1 - w) * 14.0)
    assert fc.wind_p50 == pytest.approx(w * 11.0 + (1 - w) * 7.0) and fc.wind_vol_fc == pytest.approx(fc.wind_p90 - fc.wind_p10)
    assert res.ensemble.wind_p90 == 19.0   # raw ensemble stats untouched
    # components use the blended wind
    assert fc.cross_mph == pytest.approx(fc.wind_fg) and fc.head_mph == pytest.approx(0.0, abs=1e-9)
    assert fc.source == "nbm" and not [d for d in res.degradations if "climatology" in d.reason]


def test_no_blend_inside_48h_and_nearest_lookup_without_stadium_id():
    fc = M.build_forecast("g", T0, T0 - timedelta(hours=30), _om(), None, climo=TABLE).forecast
    assert fc.wind_fg == 12.0 == fc.wind_fg_raw and fc.blend_w == 1.0 and fc.climo_wind == 8.0  # cell found via Open-Meteo coords
    far = M.build_forecast("g", T0, T0 - timedelta(days=5), _om(), None, climo=TABLE, blend_cfg=CB.DEFAULT_CONFIG).forecast
    assert far.blend_w == pytest.approx(CB.DEFAULT_CONFIG.weight(120.0, "wind")) and far.wind_fg < 12.0


def test_missing_cell_degrades_info_and_uses_raw():
    res = M.build_forecast("g", T0 + timedelta(days=7), T0 + timedelta(days=2), _om(T0 + timedelta(days=7)), None, climo=TABLE, stadium_id="gillette")
    fc = res.forecast
    assert fc.wind_fg == fc.wind_fg_raw == 12.0 and fc.blend_w == 1.0 and fc.climo_wind is None
    degs = [d for d in res.degradations if "climatology" in d.reason]
    assert len(degs) == 1 and degs[0].severity == "info" and "blend_w=1" in degs[0].reason
    # inside 48 h a missing cell is not worth a degradation
    quiet = M.build_forecast("g", T0 + timedelta(days=7), T0 + timedelta(days=6), _om(T0 + timedelta(days=7)), None, climo=TABLE)
    assert not [d for d in quiet.degradations if "climatology" in d.reason]
    # no table at all (auto_climo off / file missing) -> plain forecast, blend_w = 1
    off = M.build_forecast("g", T0, T0 - timedelta(days=5), _om(), None, auto_climo=False).forecast
    assert off.wind_fg == 12.0 and off.blend_w == 1.0


def test_blend_config_is_read_from_calibration_json(tmp_path: Path, monkeypatch):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"forecast_blend": {"weights": {"wind": {"full_h": 24, "floor_h": 48, "floor": 0.0}}}}), encoding="utf-8")
    cfg = CB.load_blend_config(p)
    fc = M.build_forecast("g", T0, T0 - timedelta(hours=60), _om(), None, climo=TABLE, blend_cfg=cfg).forecast
    assert fc.wind_fg == 8.0 and fc.wind_fg_raw == 12.0 and fc.blend_w == 0.0   # pure climatology past the floor
    assert fc.temp_fg == pytest.approx(cfg.weight(60.0, "temp") * 60.0 + (1 - cfg.weight(60.0, "temp")) * 58.0)


def test_weather_block_and_contract_carry_blend_fields():
    fc = WeatherForecast(game_id="g", source="nbm", wind_fg=9.5, temp_fg=57.0, wind_fg_raw=12.0, temp_fg_raw=60.0, blend_w=0.6, climo_wind=8.0, climo_temp=55.0)
    d = fc.to_dict()
    assert d["wind_fg_raw"] == 12.0 and d["blend_w"] == 0.6 and d["climo_temp"] == 55.0
    block = _weather_block(fc, avg_wind=7.0)
    assert {"wind_fg_raw", "temp_fg_raw", "blend_w", "climo_wind", "climo_temp"} <= set(block)
    assert block["wind_fg"] == 9.5 and block["wind_fg_raw"] == 12.0 and block["wind_diff"] == 2.5   # wind_diff off the blended value
    assert WeatherForecast(game_id="g", source="nbm").blend_w is None
