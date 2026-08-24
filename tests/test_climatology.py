"""climatology: ERA5 archive fixture -> month-specific wind means + annual temp; csv cache round trip."""

from __future__ import annotations

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


def test_build_climatology_skips_cached_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: list[dict]) -> None:
    d = tmp_path / "data"
    (d / "aliases").mkdir(parents=True)
    (d / "aliases" / "nfl.json").write_text("{}", encoding="utf-8")
    (d / "aliases" / "cfb.json").write_text("{}", encoding="utf-8")
    (d / "stadiums.csv").write_text("stadium_id,name,lat,lon,nflverse_stadium_id\nx,X,37.4,-121.9,\ny,Y,35.1,-90.0,\n", encoding="utf-8")
    (d / "teams.csv").write_text("team_id,sport,name,short,home_stadium_id,avg_temp_f,conference,classification,aliases\nt1,nfl,T1,T1,x,,,nfl,\nt2,nfl,T2,T2,y,,,nfl,\n", encoding="utf-8")
    (d / "stadiums_overrides.csv").write_text("stadium_id,field,value,note\n", encoding="utf-8")
    fetched: list[dict] = []

    def fake_fetch(points, start, end, fetcher=None, log=print, **kw):  # noqa: ANN001
        fetched.append(dict(points))
        return C.parse_archive(payload[: len(points)], sorted(points))

    monkeypatch.setattr(C, "fetch_archive", fake_fetch)
    rows = C.build_climatology(d, log=lambda s: None)
    assert set(rows) == {"x", "y"} and fetched == [{"x": (37.4, -121.9), "y": (35.1, -90.0)}]
    assert (d / "climatology.csv").exists()
    rows2 = C.build_climatology(d, log=lambda s: None)
    assert len(fetched) == 1 and set(rows2) == {"x", "y"}  # nothing re-fetched
    C.build_climatology(d, ids={"x"}, refresh=True, log=lambda s: None)
    assert fetched[-1] == {"x": (37.4, -121.9)}
