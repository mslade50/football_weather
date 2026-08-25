"""climatology: ERA5 archive fixture -> month-specific wind means + annual temp; csv cache round trip."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from pipeline.stadiums import climatology as C

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "raw" / "era5" / "archive_daily_two_locations.json"


@pytest.fixture(scope="module")
def payload() -> list[dict]:
    return json.loads(FIX.read_text(encoding="utf-8"))


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals)


def test_monthly_means_match_hand_computation(payload: list[dict]) -> None:
    daily = payload[0]["daily"]
    got = C.monthly_means(daily)
    for key, month in (("sep", "09"), ("oct", "10"), ("nov", "11"), ("dec", "12"), ("jan", "01")):
        vals = [w for t, w in zip(daily["time"], daily["wind_speed_10m_mean"], strict=True) if t[5:7] == month and w is not None]
        assert len(vals) >= 28, key
        assert got[f"avg_wind_{key}"] == pytest.approx(_mean(vals), abs=0.006)
    temps = [x for x in daily["temperature_2m_mean"] if x is not None]
    assert got["avg_temp_f"] == pytest.approx(_mean(temps), abs=0.006)
    assert got["n_days"] == len(temps) == 153


def test_monthly_means_ignore_nulls_and_missing_months() -> None:
    daily = {"time": ["2023-09-01", "2023-09-02", "2023-07-01"], "wind_speed_10m_mean": [4.0, None, 9.0], "temperature_2m_mean": [60.0, 70.0, None]}
    got = C.monthly_means(daily)
    assert got["avg_wind_sep"] == 4.0 and got["avg_wind_oct"] is None and got["avg_wind_jan"] is None
    assert got["avg_temp_f"] == 65.0 and got["n_days"] == 2
    empty = C.monthly_means({"time": []})
    assert empty["avg_temp_f"] is None and all(empty[f"avg_wind_{m}"] is None for m in ("sep", "oct", "nov", "dec", "jan"))


def test_parse_archive_maps_locations_in_request_order(payload: list[dict]) -> None:
    got = C.parse_archive(payload, ["levis-stadium", "simmons-bank-liberty-stadium"])
    assert set(got) == {"levis-stadium", "simmons-bank-liberty-stadium"}
    assert got["levis-stadium"]["avg_wind_sep"] != got["simmons-bank-liberty-stadium"]["avg_wind_sep"]
    single = C.parse_archive(payload[0], ["only"])
    assert list(single) == ["only"]
    assert C.parse_archive({"error": True}, ["x"]) == {}


def test_fetch_archive_batches_and_caches(payload: list[dict]) -> None:
    calls: list[dict] = []

    class FakeFetcher:
        def json(self, name, method, url, params=None, **kw):  # noqa: ANN001
            calls.append({"name": name, "params": params, **kw})
            n = len(params["latitude"].split(","))
            return payload[:n]

    pts = {"a": (1.0, 2.0), "b": (3.0, 4.0), "c": (5.0, 6.0)}
    got = C.fetch_archive(pts, "2015-01-01", "2024-12-31", fetcher=FakeFetcher(), batch=2, throttle_s=0.0, log=lambda s: None)
    assert set(got) == {"a", "b", "c"}
    assert len(calls) == 2 and calls[0]["params"]["latitude"] == "1.00000,3.00000"
    assert calls[0]["params"]["daily"] == "wind_speed_10m_mean,temperature_2m_mean"
    assert calls[0]["params"]["wind_speed_unit"] == "mph" and calls[0]["params"]["temperature_unit"] == "fahrenheit"
    assert calls[0]["name"].startswith("era5_2015-01-01_2024-12-31_") and calls[0]["name"] != calls[1]["name"]


def test_climatology_csv_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "climatology.csv"
    rows = [{"stadium_id": "b", "lat": 1.5, "lon": -2.5, "start_date": "2015-01-01", "end_date": "2024-12-31", "avg_wind_sep": 7.12, "avg_wind_oct": 7.5,
             "avg_wind_nov": 8.0, "avg_wind_dec": 8.4, "avg_wind_jan": None, "avg_temp_f": 61.2, "n_days": 3653, "fetched_at": "2026-01-01T00:00:00Z"},
            {"stadium_id": "a", "lat": 0, "lon": 0, "start_date": "2015-01-01", "end_date": "2024-12-31", "avg_temp_f": 50.0}]
    C.write_climatology(p, rows)
    text = p.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(C.COLUMNS)
    back = C.read_climatology(p)
    assert list(back) == ["a", "b"]  # sorted by stadium_id
    assert back["b"]["avg_wind_sep"] == "7.12" and back["b"]["avg_wind_jan"] == "" and back["a"]["avg_wind_sep"] == ""
    assert C.read_climatology(tmp_path / "missing.csv") == {}


def _hourly(start: dt.date, n_days: int, wind_fn, temp: float = 60.0, rain_hours: tuple[int, ...] = ()) -> dict:
    """Synthetic ERA5 hourly block: wind = wind_fn(day, utc_hour), gust = 2*wind, 1 mm at ``rain_hours``."""
    times, winds, gusts, temps, precs = [], [], [], [], []
    for d in range(n_days):
        for h in range(24):
            t = dt.datetime.combine(start + dt.timedelta(days=d), dt.time(h))
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            w = float(wind_fn(d, h))
            winds.append(w)
            gusts.append(2.0 * w)
            temps.append(temp)
            precs.append(1.0 if h in rain_hours else 0.0)
    return {"time": times, "wind_speed_10m": winds, "wind_gusts_10m": gusts, "temperature_2m": temps, "precipitation": precs}


def test_reduce_hourly_cells_and_summary_at_lon_zero() -> None:
    week = _hourly(dt.date(2023, 9, 4), 7, lambda d, h: h, rain_hours=(6,))     # Mon..Sun, ISO week 36
    june = _hourly(dt.date(2023, 6, 1), 2, lambda d, h: 30.0, temp=80.0)         # off-season: summary only
    block = {k: june[k] + week[k] for k in week}
    summary, cells = C.reduce_hourly(block, lon=0.0)
    assert summary["avg_wind_sep"] == 11.5 and summary["avg_wind_oct"] is None and summary["avg_wind_jan"] is None
    assert summary["avg_temp_f"] == pytest.approx((2 * 24 * 80.0 + 7 * 24 * 60.0) / (9 * 24), abs=0.006)
    assert summary["n_days"] == 9
    assert [(c["iso_week"], c["tod_bin"]) for c in cells] == [(36, 0), (36, 1), (36, 2), (36, 3)]
    b0, b1 = cells[0], cells[1]
    assert b0["n_hours"] == 42 and b0["wind_mean"] == 2.5 and b0["wind_p10"] == 0.0 and b0["wind_p50"] == 2.5 and b0["wind_p90"] == 5.0
    assert b0["gust_mean"] == 5.0 and b0["gust_p90"] == 10.0 and b0["temp_mean"] == 60.0 and b0["temp_p10"] == 60.0 and b0["rain_freq"] == 0.0
    assert b1["wind_mean"] == 8.5 and b1["rain_freq"] == pytest.approx(1 / 6, abs=0.001)


def test_reduce_hourly_uses_solar_time_bins() -> None:
    week = _hourly(dt.date(2023, 9, 4), 7, lambda d, h: h)
    _summary, cells = C.reduce_hourly(week, lon=-75.0)   # UTC-5 solar: bin 3 (18-24 local) = UTC 23 (same day) + 0..4 (next day)
    by = {(c["iso_week"], c["tod_bin"]): c for c in cells}
    # week 36 bin 3: UTC 23 of all 7 days + UTC 0..4 of days 2..7 (day 1's 0..4 belong to Sunday of week 35)
    assert by[(36, 3)]["n_hours"] == 37 and by[(36, 3)]["wind_mean"] == pytest.approx((7 * 23 + 6 * 10) / 37, abs=0.006)
    assert by[(36, 0)]["wind_mean"] == 7.5   # local 00-06 = UTC 5..10
    assert (35, 3) not in by   # the 5 orphan hours before the week start fall under MIN_CELL_HOURS
    empty_summary, empty_cells = C.reduce_hourly({"time": []}, lon=0.0)
    assert empty_summary["avg_temp_f"] is None and empty_cells == []


def test_csv_keeps_summary_last_per_stadium_and_splits_layers(tmp_path: Path) -> None:
    from pipeline.stadiums.build_stadiums import load_climatology

    p = tmp_path / "climatology.csv"
    base = {"lat": 1.0, "lon": 2.0, "start_date": "2015-01-01", "end_date": "2024-12-31", "fetched_at": "x"}
    rows = [
        {"stadium_id": "b", **base, "avg_wind_sep": 7.1, "avg_temp_f": 61.2, "n_days": 3653},
        {"stadium_id": "b", **base, "iso_week": 40, "tod_bin": 3, "n_hours": 400, "wind_mean": 9.0, "temp_mean": 55.0, "rain_freq": 0.05},
        {"stadium_id": "b", **base, "iso_week": 1, "tod_bin": 0, "n_hours": 400, "wind_mean": 8.0},
        {"stadium_id": "a", **base, "avg_wind_sep": 5.0, "avg_temp_f": 50.0, "n_days": 3653},
    ]
    C.write_climatology(p, rows)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(C.COLUMNS) and C.COLUMNS[:13] == C.SUMMARY_COLUMNS
    ids = [ln.split(",")[0] for ln in lines[1:]]
    assert ids == ["a", "b", "b", "b"] and lines[-1].startswith("b,1.0,2.0,2015-01-01,2024-12-31,7.1,")  # summary row last
    assert C.read_climatology(p)["b"]["avg_wind_sep"] == "7.1" and set(C.read_climatology(p)) == {"a", "b"}
    cells = C.read_cells(p)
    assert [(c["iso_week"], c["tod_bin"]) for c in cells["b"]] == [("1", "0"), ("40", "3")] and "a" not in cells
    # the legacy reader (last row per stadium wins) still sees the summary columns
    assert load_climatology(p)["b"]["avg_wind_sep"] == "7.1" and load_climatology(p)["b"]["avg_temp_f"] == "61.2"


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "aliases").mkdir(parents=True)
    (d / "aliases" / "nfl.json").write_text("{}", encoding="utf-8")
    (d / "aliases" / "cfb.json").write_text("{}", encoding="utf-8")
    (d / "stadiums.csv").write_text("stadium_id,name,lat,lon,nflverse_stadium_id\nx,X,37.4,-121.9,\ny,Y,35.1,-90.0,\n", encoding="utf-8")
    (d / "teams.csv").write_text("team_id,sport,name,short,home_stadium_id,avg_temp_f,conference,classification,aliases\nt1,nfl,T1,T1,x,,,nfl,\nt2,nfl,T2,T2,y,,,nfl,\n", encoding="utf-8")
    (d / "stadiums_overrides.csv").write_text("stadium_id,field,value,note\n", encoding="utf-8")
    return d


def test_build_climatology_hourly_fetch_cells_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _data_dir(tmp_path)
    fetched: list[dict] = []
    week = _hourly(dt.date(2023, 9, 4), 7, lambda d, h: h)

    def fake_fetch(points, start, end, fetcher=None, log=print, **kw):  # noqa: ANN001
        fetched.append(dict(points))
        return {sid: week for sid in points}

    monkeypatch.setattr(C, "fetch_archive_hourly", fake_fetch)
    rows = C.build_climatology(d, log=lambda s: None)
    assert set(rows) == {"x", "y"} and fetched == [{"x": (37.4, -121.9), "y": (35.1, -90.0)}]
    assert rows["x"]["avg_wind_sep"] == 11.5 and rows["x"]["n_days"] == 7
    cells = C.read_cells(d / "climatology.csv")
    assert set(cells) == {"x", "y"} and len(cells["x"]) == 4 and cells["x"][0]["lat"] == "37.4"
    rows2 = C.build_climatology(d, log=lambda s: None)
    assert len(fetched) == 1 and set(rows2) == {"x", "y"}  # nothing re-fetched
    C.build_climatology(d, ids={"x"}, refresh=True, log=lambda s: None)
    assert fetched[-1] == {"x": (37.4, -121.9)}
    assert len(C.read_cells(d / "climatology.csv")["y"]) == 4  # untouched stadium keeps its cells
    # a legacy summary-only file (no cells) is stale -> re-fetched
    C.write_climatology(d / "climatology.csv", [rows["x"], rows["y"]])
    C.build_climatology(d, log=lambda s: None)
    assert fetched[-1] == {"x": (37.4, -121.9), "y": (35.1, -90.0)}


def test_fetch_archive_hourly_one_request_per_stadium_cached() -> None:
    calls: list[dict] = []
    week = _hourly(dt.date(2023, 9, 4), 1, lambda d, h: 1.0)

    class FakeFetcher:
        def __init__(self) -> None:
            self.store: dict[str, dict] = {"era5h_2015-01-01_2024-12-31_b": {"hourly": week}}

        def cached(self, name):  # noqa: ANN001
            return self.store.get(name)

        def json(self, name, method, url, params=None, **kw):  # noqa: ANN001
            if name in self.store:
                return self.store[name]
            calls.append({"name": name, "params": params, **kw})
            return {"hourly": week}

    got = C.fetch_archive_hourly({"a": (1.0, 2.0), "b": (3.0, 4.0)}, "2015-01-01", "2024-12-31", fetcher=FakeFetcher(), throttle_s=1.5, log=lambda s: None)
    assert set(got) == {"a", "b"} and len(calls) == 1 and calls[0]["name"] == "era5h_2015-01-01_2024-12-31_a"
    p = calls[0]["params"]
    assert p["hourly"] == "wind_speed_10m,wind_gusts_10m,temperature_2m,precipitation" and p["latitude"] == "1.00000"
    assert p["wind_speed_unit"] == "mph" and p["temperature_unit"] == "fahrenheit" and p["precipitation_unit"] == "mm"
    assert p["start_date"] == "2015-01-01" and p["end_date"] == "2024-12-31" and calls[0]["throttle"] == 1.5
