"""Legacy writer contract: column lists + cell formats pinned against the frozen legacy files
under tests/fixtures/legacy/ (the last Streamlit-era nfl_weather.csv / cfb_weather.xlsx)."""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pipeline.outputs import legacy as L

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "legacy"
NFL_SPEC = FIXTURES / "nfl_weather.csv"
CFB_SPEC = FIXTURES / "cfb_weather.xlsx"

DATE_RE = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT|SUN) \d{2}/\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2} (AM|PM)$")
LOC_RE = re.compile(r"^-?\d+(\.\d+)?, -?\d+(\.\d+)?$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")
NFL_GAME_RE = re.compile(r"^[^A-Z]+ vs [^A-Z]+$")
CFB_GAME_RE = re.compile(r"^.+ @ .+$")

ET = ZoneInfo("America/New_York")


def _rec(sport: str, **kw) -> L.LegacyRecord:
    base = dict(
        sport=sport,
        away_name="Seattle" if sport == "nfl" else "Georgia Southern",
        home_name="New England" if sport == "nfl" else "Appalachian State",
        kickoff_local=datetime(2026, 2, 8, 18, 30, tzinfo=ET),
        stadium_name="Gillette Stadium",
        lat=42.0909,
        lon=-71.2643,
        avg_wind=6.23,
        avg_wind_month=5.6,
        wind_vol="high",
        orient="N-S",
        wind_impact="med",
        weakest_wind_effect="x S",
        travel_alt=69.4,
        home_temp=51.74,
        away_temp=52.12,
        year_built=2002,
        wind_dir_1h="SE",
        wind_dir_2h="SE",
        temp_fg=10.280000000000001,
        wind_fg=9.890155083333333,
        wind_dir_fg="SE",
        rain_fg=0.0,
        gs_fg_pct=-2.465,
        away_fg_pct=0.0,
    )
    base.update(kw)
    return L.LegacyRecord(**base)


# ---- column lists vs repo files ---------------------------------------------------

def test_nfl_columns_match_repo_file():
    assert list(pd.read_csv(NFL_SPEC, nrows=0).columns) == L.NFL_COLUMNS
    assert len(L.NFL_COLUMNS) == 31


def test_cfb_columns_match_repo_file():
    sheets = pd.read_excel(CFB_SPEC, sheet_name=None, nrows=0)
    assert list(sheets) == [L.CFB_SHEET_FBS, L.CFB_SHEET_OTHER]
    assert list(sheets[L.CFB_SHEET_FBS].columns) == L.CFB_FBS_COLUMNS
    assert list(sheets[L.CFB_SHEET_OTHER].columns) == L.CFB_OTHER_COLUMNS
    assert len(L.CFB_FBS_COLUMNS) == 37
    assert len(L.CFB_OTHER_COLUMNS) == 24


def _check_formats(df: pd.DataFrame, game_re: re.Pattern, has_ts: bool = True) -> None:
    for v in df["Date"].dropna():
        assert DATE_RE.match(v), v
    for v in df["Time"].dropna():
        assert TIME_RE.match(v), v
    for v in df["game_loc"].dropna():
        assert LOC_RE.match(v), v
    for v in df["Game"].dropna():
        assert game_re.match(v), v
    if has_ts:
        for v in df["Timestamp"].dropna():
            assert TS_RE.match(str(v)), v


def test_repo_file_formats():
    nfl = pd.read_csv(NFL_SPEC)
    _check_formats(nfl, NFL_GAME_RE)
    assert nfl["gs_fg"].abs().max() < 1.0  # NFL fraction
    assert nfl["travel_alt"].dtype.kind == "i"
    cfb = pd.read_excel(CFB_SPEC, sheet_name=L.CFB_SHEET_FBS)
    _check_formats(cfb, CFB_GAME_RE)
    assert cfb["gs_fg"].abs().max() > 1.0  # CFB percent
    other = pd.read_excel(CFB_SPEC, sheet_name=L.CFB_SHEET_OTHER)
    _check_formats(other, re.compile(r"^.+ vs .+$"), has_ts=False)


# ---- writers reproduce the formats ---------------------------------------------

def test_nfl_writer_roundtrip(tmp_path: Path):
    ts = "2026-02-07T10:00:55"
    rec = _rec("nfl", odds={"spread_now": -3.0, "odds_now": -108, "total_now": 38.0, "under_now": -110,
                            "spread_open": -3.0, "odds_open": -108, "total_open": 38.0, "under_open": -110})
    out = L.write_nfl_csv([rec], tmp_path / "nfl_weather.csv", ts)
    df = pd.read_csv(out)
    assert list(df.columns) == L.NFL_COLUMNS
    row = df.iloc[0]
    assert row["Game"] == "seattle vs new england"
    assert row["Date"] == "SUN 02/08"
    assert row["Time"] == "06:30 PM"
    assert row["game_loc"] == "42.0909, -71.2643"
    assert row["travel_alt"] == 69 and df["travel_alt"].dtype.kind == "i"
    assert row["year_built"] == 2002
    assert row["gs_fg"] == pytest.approx(-0.02465)
    assert row["wind_fg"] == pytest.approx(9.890155083333333)  # unrounded
    assert row["Odds_now"] == -108 and df["Odds_now"].dtype.kind == "i"
    assert row["Timestamp"] == ts
    _check_formats(df, NFL_GAME_RE)


def test_nfl_writer_empty_odds_are_blank(tmp_path: Path):
    out = L.write_nfl_csv([_rec("nfl")], tmp_path / "n.csv", "2026-02-07T10:00:55")
    df = pd.read_csv(out)
    assert df["Spread_now"].isna().all()
    assert df["Under_open"].isna().all()
    assert "None" not in out.read_text(encoding="utf-8")


def test_cfb_writer_roundtrip(tmp_path: Path):
    ts = "2025-12-29T10:01:36"
    fbs = _rec("cfb", kickoff_local=datetime(2025, 12, 29, 14, 0, tzinfo=ET), travel_alt=930.0876617800001,
               temp_fg=22.704, wind_fg=25.23, gs_fg_pct=-10.9125, away_fg_pct=-1.1625,
               odds={"fd_open": 59.5, "odds_o": -115, "fd_now": 58.5, "odds_n": -115, "open": 8.5, "current": 9.0})
    other = _rec("cfb", away_name="Montana", home_name="Montana State", is_fbs=False, avg_wind_month=None,
                 kickoff_local=datetime(2025, 12, 20, 14, 0, tzinfo=ET))
    out = L.write_cfb_xlsx([fbs, other], tmp_path / "cfb_weather.xlsx", ts)
    sheets = pd.read_excel(out, sheet_name=None)
    assert list(sheets) == [L.CFB_SHEET_FBS, L.CFB_SHEET_OTHER]
    f = sheets[L.CFB_SHEET_FBS]
    assert list(f.columns) == L.CFB_FBS_COLUMNS
    row = f.iloc[0]
    assert row["Game"] == "Georgia Southern @ Appalachian State"
    assert row["Date"] == "MON 12/29" and row["Time"] == "02:00 PM"
    assert row["travel_alt"] == pytest.approx(930.0876617800001)
    assert row["temp_fg"] == 22.7 and row["wind_fg"] == 25.2
    assert row["gs_fg"] == -10.91 and row["away_fg"] == -1.16
    assert row["wind_diff"] == pytest.approx(19.6)
    assert row["Move_t"] == pytest.approx((58.5 - 59.5) / 59.5)
    assert row["Move_s"] == pytest.approx(-0.5)
    assert math.isnan(row["My_total"]) and math.isnan(row["Edge_s"])
    assert row["Timestamp"] == ts
    _check_formats(f, CFB_GAME_RE)

    o = sheets[L.CFB_SHEET_OTHER]
    assert list(o.columns) == L.CFB_OTHER_COLUMNS
    assert o.iloc[0]["Game"] == "Montana vs Montana State"
    assert o.iloc[0]["Home Team"] == "Montana State"
    assert o.iloc[0]["Away Team"] == "Montana"
    assert math.isnan(o.iloc[0]["wind_diff"])


def test_cfb_derived_columns_when_projection_present():
    rec = _rec("cfb", gs_fg_pct=-4.0, away_fg_pct=-2.0,
               odds={"fd_now": 50.0, "total_proj": 50.0, "spread": -10.0})
    row = L.cfb_fbs_row(rec, "2024-10-01T10:00:00")
    assert row["My_total"] == pytest.approx(48.0)
    assert row["Edge"] == pytest.approx((50.0 - 48.0) / 48.0)
    assert row["My_spread"] == pytest.approx(-9.8)
    assert row["Edge_s"] == pytest.approx(-0.2)


def test_write_legacy_dispatch(tmp_path: Path):
    assert L.write_legacy("nfl", [_rec("nfl")], tmp_path).name == L.NFL_FILENAME
    assert L.write_legacy("cfb", [_rec("cfb")], tmp_path).name == L.CFB_FILENAME
    with pytest.raises(ValueError):
        L.write_legacy("xfl", [], tmp_path)


def test_empty_records_still_write_headers(tmp_path: Path):
    df = pd.read_csv(L.write_nfl_csv([], tmp_path / "n.csv"))
    assert list(df.columns) == L.NFL_COLUMNS and df.empty
    sheets = pd.read_excel(L.write_cfb_xlsx([], tmp_path / "c.xlsx"), sheet_name=None)
    assert list(sheets[L.CFB_SHEET_FBS].columns) == L.CFB_FBS_COLUMNS
