"""pipeline/backtest.py: the 118 legacy buckets load with ids preserved; first-match
lookup reproduces pages/cfb_weather.py (pandas oracle re-implemented verbatim);
aggregation formulas reproduce the sheet's Wins/Losses/Push/Sample/Margin/ROI/+CLV/CLV%;
snapshot → row extraction (closing + lead-N); actuals window stats; result parsers;
CLI end-to-end on a tiny snapshot dir (--no-network) → backtest.json + parquet."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pipeline import backtest as bt
from pipeline.outputs import json_out

XLSX = Path(__file__).resolve().parent / "fixtures" / "legacy" / "cfb_weather_backtest.xlsx"
KICK = datetime(2026, 10, 3, 19, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def defs() -> list[bt.Bucket]:
    return bt.load_grid_defs(XLSX)


@pytest.fixture(scope="module")
def sheet() -> pd.DataFrame:
    return pd.read_excel(XLSX, sheet_name="Backtesting")


# ---- definitions ----------------------------------------------------------------------------

def test_grid_defs_preserve_118_ids(defs, sheet):
    assert len(defs) == 118
    assert [b.id for b in defs] == list(range(1, 119)) == sheet["Signal"].tolist()
    assert defs[102].is_separator and defs[102].id == 103
    assert sum(1 for b in defs if b.sport == "cfb") == 72 + 15
    assert sum(1 for b in defs if b.sport == "nfl") == 30
    assert {b.clv for b in defs} == {None, "Positive", "Negative"}
    nfl_32_45 = [b for b in defs if b.sport == "nfl" and b.temp_lo == 32 and b.temp_hi == 45]
    assert [b.id for b in nfl_32_45] == [101]
    assert defs[0].legacy["Wins"] == 165 and defs[0].legacy_columns()["Sport"] == "NCAAF"


LEGACY_KEYS = ("Wins", "Losses", "Push", "Sample", "Margin", "ROI", "+ CLV", "CLV %")


def test_grid_rows_carry_the_sheet_numbers_under_legacy(defs, sheet):
    grid = bt.grid_stats([], defs)
    assert len(grid) == 118 and all(g["Sample"] == 0 and g["ROI"] is None for g in grid)
    for g, rec in zip(grid, sheet.to_dict(orient="records"), strict=True):
        assert set(g["legacy"]) == set(LEGACY_KEYS)
        for k in LEGACY_KEYS:
            want = rec[k]
            if isinstance(want, float) and want != want:
                assert g["legacy"][k] is None, (g["id"], k)
            else:
                assert g["legacy"][k] == pytest.approx(float(want)), (g["id"], k)
    by = {g["id"]: g for g in grid}
    assert (by[1]["legacy"]["Wins"], by[1]["legacy"]["Losses"], by[1]["legacy"]["Sample"]) == (165, 162, 333)
    assert by[103]["legacy"] == {k: None for k in LEGACY_KEYS}   # separator row


def test_stadium_sheet_and_legacy_meta(defs, tmp_path: Path):
    sheet_st = pd.read_excel(XLSX, sheet_name="Stadiums")
    rows = bt.load_stadium_sheet(XLSX)
    assert len(rows) == len(sheet_st) == 129
    assert all(set(r) == {"Team", "Stadium", "Record", "Percentage", "sport"} for r in rows)
    assert all(r["sport"] == "cfb" for r in rows)
    assert rows[0]["Stadium"] == "AT&T Stadium" and rows[0]["Team"] is None and rows[0]["Record"] == "3-0-0"
    assert rows[1] == {"Team": "Pittsburgh", "Stadium": "Acrisure Stadium", "Record": "15-17-1", "Percentage": pytest.approx(-0.102), "sport": "cfb"}
    assert bt.legacy_meta(defs, XLSX) == {"source": "cfb_weather_backtest.xlsx", "seasons": "pre-2026", "n_buckets": 118}
    # a workbook without a Stadiums sheet -> [] (the grid still loads)
    only_bt = tmp_path / "grid_only.xlsx"
    pd.read_excel(XLSX, sheet_name="Backtesting").to_excel(only_bt, sheet_name="Backtesting", index=False)
    assert bt.load_stadium_sheet(only_bt) == [] and len(bt.load_grid_defs(only_bt)) == 118


# ---- legacy oracle (pages/cfb_weather.py get_backtesting_data, verbatim semantics) ---------------

def _legacy_lookup(df_bt: pd.DataFrame, temp_fg, wind_fg, spread_abs, clv_status):
    df_bt = df_bt.copy()
    df_bt["Wind Below"] = df_bt["Wind Below"].fillna(100)
    df_bt["Spread_l"] = df_bt["Spread_l"].fillna(0)
    df_bt["Temp Above"] = df_bt["Temp Above"].fillna(0)
    match = df_bt[
        (df_bt["Temp Above"] <= temp_fg) & (df_bt["Temp Below"] >= temp_fg)
        & (df_bt["Wind Above"] <= wind_fg) & (df_bt["Wind Below"] >= wind_fg)
        & (df_bt["CLV from Open"] == clv_status)
    ]
    match = match[(match["Spread_h"] >= spread_abs) & (match["Spread_l"] <= spread_abs)]
    return int(match.iloc[0]["Signal"]) if not match.empty else None


def test_first_match_reproduces_legacy_lookup(defs, sheet):
    temps = [20, 32, 45, 49.9, 50, 55, 60, 65, 74.9, 75, 80, 95, 100, 101]
    winds = [3, 7.9, 8, 9, 10, 10.1, 12, 14.9, 15, 15.1, 20, 30, 101]
    spreads = [0, 3.5, 9.5, 10, 10.5, 15, 20, 20.5, 28]
    n_matched = 0
    for t, w, s, c in itertools.product(temps, winds, spreads, ("Positive", "Negative")):
        b = bt.first_match(defs, "cfb", t, w, s, c)
        got = b.id if b else None
        assert got == _legacy_lookup(sheet, t, w, s, c), (t, w, s, c)
        n_matched += got is not None
    assert n_matched > 0
    # NFL rows never match in the legacy UI (Spread_h NaN); sheet rows with NaN CLV never match either
    assert bt.first_match(defs, "nfl", 40, 20, 3, "Positive") is None
    assert all(bt.first_match(defs, "cfb", 55, 10, 5, c) is not None for c in ("Positive", "Negative"))
    assert bt.first_match(defs, "cfb", 55, 10, 5, None) is None


def test_first_match_is_first_row_not_best_row(defs):
    # temp 55 / wind 10 / |spread| 5 / Positive satisfies ids 20 (spread ≤10), 44 (spread ≤20), 105 (wind 8-10, temp ≤75) → first = 20
    ids = [b.id for b in defs if bt.bucket_matches(b, "cfb", 55, 10, 5, "Positive", legacy_lookup=True)]
    assert ids == [20, 44, 105]
    assert bt.first_match(defs, "cfb", 55, 10, 5, "Positive").id == 20


# ---- aggregation semantics -------------------------------------------------------------------

def _row(**kw) -> bt.GameRow:
    base = {"game_id": "cfb:2026:5:a@b", "sport": "cfb", "season": 2026, "week": 5, "kickoff_utc": "2026-10-03T19:30:00Z",
            "stadium_id": "s1", "stadium_name": "S One", "home_name": "B", "temp_fc": 55.0, "wind_fc": 10.0,
            "total_open": 50.0, "total_close": 48.0, "spread_open": -5.0, "home_score": 20, "away_score": 21}
    base.update(kw)
    return bt.finalize_row(bt.GameRow(**base))


def test_aggregation_bounds_nan_means_unbounded_and_nfl_rows_fill(defs):
    r = _row()
    assert r.clv_status == "Positive" and r.under_result == "W" and r.margin == 7.0 and r.spread_abs == 5.0
    ids = {g["id"] for g in bt.grid_stats([r], defs) if g["Sample"]}
    # all-CLV rows (19, 43, 104) count too in aggregation; Positive rows (20, 44, 105) as well (107/108 need temp ≤50)
    assert ids == {19, 20, 43, 44, 104, 105}
    nfl = _row(game_id="nfl:2026:5:a@b", sport="nfl", temp_fc=40.0, wind_fc=18.0, spread_open=-7.0)
    ids = {g["id"] for g in bt.grid_stats([nfl], defs) if g["Sample"]}
    # NFL wind≥8/temp≤60 rows (82, 83, 100), wind≥15 [32,50] rows (94, 95) and the [32,45] row 101
    assert ids == {82, 83, 94, 95, 100, 101}
    cold = _row(game_id="nfl:2026:5:c@d", sport="nfl", temp_fc=20.0, wind_fc=5.0)
    assert {g["id"] for g in bt.grid_stats([cold], defs) if g["Sample"]} == {102}   # wind/temp-above unbounded
    dome = _row(game_id="cfb:2026:5:e@f", temp_fc=None)
    assert not any(g["Sample"] for g in bt.grid_stats([dome], defs))


def test_stats_formulas_reproduce_sheet_numbers(defs):
    # sheet row 1: W165 L162 P6 Sample 333 ROI -0.036 +CLV 163 CLV% 0.4895 ; Margin -2.08
    rows = []
    for i in range(333):
        res = "W" if i < 165 else "L" if i < 327 else "P"
        score = (10, 11) if res == "W" else (30, 31) if res == "L" else (24, 24)
        rows.append(_row(game_id=f"cfb:2026:5:t{i}@u", temp_fc=80.0, wind_fc=10.0, spread_open=-15.0,
                         total_open=(50.0 if i < 163 else 47.0), total_close=48.0, home_score=score[0], away_score=score[1]))
    g = {x["id"]: x for x in bt.grid_stats(rows, defs)}[1]
    assert (g["Wins"], g["Losses"], g["Push"], g["Sample"]) == (165, 162, 6, 333)
    assert g["ROI"] == pytest.approx(-0.036, abs=0.0015)
    assert g["+ CLV"] == 163 and g["CLV %"] == pytest.approx(0.4895, abs=1e-3)
    assert g["Margin"] == pytest.approx(sum(r.margin for r in rows) / 333, abs=0.01)
    assert g["legacy"]["ROI"] == pytest.approx(-0.036)
    # Positive sub-row 2 only counts the 163 positives; the separator row stays empty
    g2 = {x["id"]: x for x in bt.grid_stats(rows, defs)}
    assert g2[2]["Sample"] == 163 and g2[3]["Sample"] == 170 and g2[103]["Sample"] == 0 and g2[103]["Sport"] is None


def test_stadium_results_record_and_percentage():
    rows = [_row(game_id=f"cfb:2026:5:t{i}@b", home_score=(10 if i < 3 else 40), away_score=(0 if i < 3 else 20)) for i in range(4)]
    rows.append(_row(game_id="cfb:2026:5:x@y", stadium_id="s2", stadium_name="Two", home_name="Y", home_score=24, away_score=24))
    out = bt.stadium_results(rows, now="2026-10-05T00:00:00Z")
    by = {o["stadium_id"]: o for o in out}
    assert by["s1"]["Record"] == "3-1-0" and by["s1"]["Team"] == "B" and by["s1"]["Stadium"] == "S One"
    assert by["s1"]["Percentage"] == pytest.approx((3 * 100 / 110 - 1) / 4, abs=1e-3)
    assert by["s2"]["Record"] == "0-0-1" and by["s2"]["roi"] == 0.0 and by["s2"]["n"] == 1
    assert set(by["s1"]) >= {"under_w", "under_l", "under_p", "roi", "n", "updated_at", "sport", "season"}


def test_alerts_clv_groups_v1_vs_v2():
    recs = [
        {"alert_key": "a", "family": "edge", "tier": "edge", "sport": "nfl", "book": "betonline", "model_version": "v1", "market": "total", "clv_pts": 1.5},
        {"alert_key": "b", "family": "edge", "tier": "strong", "sport": "cfb", "book": "pinnacle", "model_version": "v2", "market": "total", "clv_pts": -0.5},
        {"alert_key": "c", "family": "edge", "tier": "edge", "sport": "nfl", "book": "betonline", "model_version": "v1", "market": "spread", "clv_pts": None},
        {"alert_key": "d", "family": "move", "clv_pts": 9.0},
    ]
    out = bt.alerts_clv(recs)
    assert out["n"] == 2
    assert [(g["key"], g["n"], g["avg_clv"], g["pos"]) for g in out["by_model"]] == [("v1", 1, 1.5, 1), ("v2", 1, -0.5, 0)]
    assert out["by_league"][0]["key"] == "NFL" and out["alerts"][0]["alert_key"] == "a"


# ---- snapshots → rows ---------------------------------------------------------------------------

def _card(lead_h: float, wind: float, total_now: float, total_open: float = 50.0) -> dict:
    return {
        "game_id": "cfb:2026:5:a@b", "sport": "cfb", "season": 2026, "week": 5, "kickoff_utc": "2026-10-03T19:30:00Z",
        "home": {"team_id": "b", "name": "B"}, "away": {"team_id": "a", "name": "A"}, "neutral": False,
        "stadium": {"stadium_id": "s1", "name": "S One", "lat": 40.0, "lon": -83.0, "roof_state": "outdoors"},
        "weather": {"temp_fg": 55.0, "wind_fg": wind, "gust_fg": wind + 5, "rain_fg": 0.2, "wind_dir_fg": "N", "lead_hours": lead_h},
        "impact": {"v1": {"gs_fg_pct": -2.0, "away_fg_pct": 0.0}, "v2": {"gs_fg_pct": -1.5, "away_fg_pct": 0.0}, "model_version": "v1"},
        "consensus": {"total_open": total_open, "total_now": total_now, "spread_open": -6.5, "spread_now": -7.0, "ref_book": "pinnacle"},
        "fair": {"fair_total": total_now * 0.98, "fair_total_v2": total_now * 0.985},
    }


def _snaps():
    return [
        (KICK - timedelta(hours=130), _card(130, 9.0, 50.0)),
        (KICK - timedelta(hours=70), _card(70, 12.0, 49.0)),
        (KICK - timedelta(hours=20), _card(20, 14.0, 48.5)),
        (KICK - timedelta(hours=1), _card(1, 16.0, 48.0)),
        (KICK + timedelta(hours=1), _card(-1, 30.0, 40.0)),   # post-kickoff snapshot: ignored for the close
    ]


def test_row_from_snapshots_close_and_leads():
    r = bt.row_from_snapshots("cfb:2026:5:a@b", _snaps())
    assert (r.wind_fc, r.total_close, r.lead_fc) == (16.0, 48.0, 1.0)
    assert r.total_open == 50.0 and r.spread_open == -6.5 and r.spread_close == -7.0
    assert (r.wind_lead1, r.wind_lead3, r.wind_lead5) == (14.0, 12.0, 9.0)   # 20h→lead1, 70h→lead3, 130h→lead5 (±12h)
    assert r.gs_fg_v1 == -2.0 and r.gs_fg_v2 == -1.5 and r.fair_total_v2 == pytest.approx(48 * 0.985)
    assert r.src_forecast.startswith("snapshot:2026-10-03T18:30:00Z")
    assert bt.finalize_row(r).clv_status == "Positive" and r.under_result is None


def test_build_rows_merges_d1_and_snapshots_and_filters_by_kickoff():
    d1 = bt.D1Data(games=[{"game_id": "cfb:2026:5:a@b", "sport": "cfb", "season": 2026, "week": 5, "kickoff_utc": "2026-10-03T19:30:00Z",
                           "home_id": "b", "away_id": "a", "stadium_id": "s1", "gs_fg": -2.5},
                          {"game_id": "cfb:2026:6:c@d", "sport": "cfb", "season": 2026, "week": 6, "kickoff_utc": "2026-10-10T19:30:00Z",
                           "home_id": "d", "away_id": "c", "stadium_id": "s1"}],
                   stadiums=[{"stadium_id": "s1", "name": "S One", "lat": 40.0, "lon": -83.0}],
                   closings=[{"game_id": "cfb:2026:5:a@b", "book": "betonline", "market": "total", "side": "over", "line": 47.5},
                             {"game_id": "cfb:2026:5:a@b", "book": "betonline", "market": "spread", "side": "home", "line": -7.5}],
                   odds_history=[{"scraped_at": "2026-09-30T00:00:00Z", "game_id": "cfb:2026:5:a@b", "book": "betonline", "market": "total", "side": "over", "line": 51.0},
                                 {"scraped_at": "2026-09-29T00:00:00Z", "game_id": "cfb:2026:5:a@b", "book": "betonline", "market": "total", "side": "over", "line": 52.0}])
    rows = bt.build_rows(snapshots={"cfb:2026:5:a@b": _snaps()}, d1=d1, now=KICK + timedelta(hours=5))
    assert [r.game_id for r in rows] == ["cfb:2026:5:a@b"]
    r = rows[0]
    assert r.gs_fg_v1 == -2.5                       # D1 value kept
    assert r.wind_fc == 16.0 and r.stadium_name == "S One"
    assert (r.total_close, r.spread_close, r.ref_book) == (47.5, -7.5, "betonline")   # D1 closing overrides consensus
    assert r.total_open == 52.0                      # earliest odds_history row = opener
    assert r.clv_status == "Positive"


# ---- weather window + results parsers ---------------------------------------------------------------

def test_window_stats_legacy_three_hour_aggregation():
    times = [(KICK.replace(minute=0) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(-1, 5)]
    ser = {"time": times, "temperature_2m": [50, 52, 54, 56, 58, 60], "wind_speed_10m": [5, 10, 12, 14, 30, 40],
           "wind_gusts_10m": [8, 15, 17, 19, 40, 50], "wind_direction_10m": [0, 350, 10, 0, 90, 180], "precipitation": [1, 0.5, 0.2, 0.3, 9, 9]}
    s, e = bt._window(KICK)
    st = bt.window_stats(ser, s, e)
    assert st["temp"] == pytest.approx(54) and st["wind"] == pytest.approx(12) and st["gust"] == pytest.approx(17)
    assert st["rain"] == pytest.approx(1.0) and st["dir"] == "N"
    ser2 = {"time": times, "wind_speed_10m_previous_day3": [1, 2, 3, 4, 5, 6]}
    assert bt.window_stats(ser2, s, e, suffix="_previous_day3")["wind"] == pytest.approx(3)
    assert bt.hourly_series([{"hourly": ser}, {"hourly": {}}])[1] == {}


def test_fetch_actuals_and_previous_runs_with_fake_client():
    r = _row(lat=40.0, lon=-83.0)
    r.wind_lead1 = 9.0
    calls = []
    times = [(KICK.replace(minute=0) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(0, 3)]

    def fake_get(url, params):
        calls.append((url, params))
        if "historical" in url:
            if params["models"] == bt.ACTUAL_MODELS:
                raise RuntimeError("no hrrr here")
            return {"hourly": {"time": times, "temperature_2m": [40, 40, 40], "wind_speed_10m": [20, 22, 24], "precipitation": [0, 0, 0]}}
        return {"hourly": {"time": times, "wind_speed_10m_previous_day3": [11, 11, 11], "temperature_2m_previous_day3": [45, 45, 45],
                           "precipitation_previous_day3": [0, 0.1, 0], "wind_speed_10m_previous_day5": [None, None, None]}}

    assert bt.fetch_actuals([r], get=fake_get, sleep=lambda s: None) == 1
    assert (r.temp_act, r.wind_act, r.src_actual) == (40, 22, "openmeteo_hist:best_match")
    assert bt.fetch_previous_runs([r], get=fake_get, sleep=lambda s: None) == 1
    assert (r.wind_lead1, r.wind_lead3, r.wind_lead5, r.rain_lead3) == (9.0, 11.0, None, pytest.approx(0.1))
    assert calls[0][1]["start_hour"] == "2026-10-03T19:00" and calls[0][1]["end_hour"] == "2026-10-03T21:00"
    assert "wind_speed_10m_previous_day1" in calls[-1][1]["hourly"]


def test_result_parsers_and_apply_scores():
    cfbd = [{"homeTeam": "Michigan", "awayTeam": "Ohio State", "homePoints": 24, "awayPoints": 27},
            {"home_team": "TCU", "away_team": "North Carolina", "home_points": None, "away_points": None}]
    assert bt.parse_cfbd_scores(cfbd, lambda n: n.lower().replace(" ", "-")) == {("michigan", "ohio-state"): (24, 27)}
    espn = {"events": [{"competitions": [{"status": {"type": {"completed": True}}, "competitors": [
        {"homeAway": "home", "score": "31", "team": {"displayName": "Michigan Wolverines"}},
        {"homeAway": "away", "score": "10", "team": {"displayName": "Ohio State Buckeyes"}}]}]},
        {"competitions": [{"status": {"type": {"completed": False}}, "competitors": []}]}]}
    assert bt.parse_espn_scores(espn, lambda t: t["displayName"].split()[0].lower()) == {("michigan", "ohio"): (31, 10)}
    csv_text = "game_id,season,game_type,week,home_team,away_team,home_score,away_score,result,total\n" \
               "2026_03_SEA_NE,2026,REG,3,NE,SEA,20,17,3,37\n2026_04_SEA_NE,2026,REG,4,NE,SEA,,,,\n"
    scores = bt.parse_nflverse_scores(csv_text)
    assert scores == {("ne", "sea", 2026, 3): (20, 17)}
    r = _row(game_id="nfl:2026:3:sea@ne", sport="nfl", home_id="ne", away_id="sea", week=3, home_score=None, away_score=None)
    assert bt.apply_scores([r], scores, "nflverse", keyed_by_week=True) == 1
    assert (bt.finalize_row(r).actual_total, r.result, r.under_result, r.src_result) == (37.0, 3.0, "W", "nflverse")


def test_grade_under():
    assert bt.grade_under(48.0, 47.0) == "W" and bt.grade_under(48.0, 49.0) == "L" and bt.grade_under(48.0, 48.0) == "P"
    assert bt.grade_under(None, 48.0) is None


# ---- CLI end to end ----------------------------------------------------------------------------------

def test_main_writes_backtest_json_and_parquet(tmp_path: Path):
    snap_dir = tmp_path / "snapshots" / "cfb" / "2026" / "5"
    snap_dir.mkdir(parents=True)
    for ts, card in _snaps():
        run_id = ts.strftime("%Y%m%dT%H%M%SZ") + "-cfb"
        (snap_dir / f"{run_id}.json").write_text(json.dumps({"meta": {"run_id": run_id, "last_updated": ts.isoformat()}, "games": [card]}))
    board = tmp_path / "board"
    pq = tmp_path / "pq"
    state = tmp_path / "state"
    state.mkdir()
    sql = tmp_path / "bt.sql"
    rc = bt.main(["--snapshot-dir", str(tmp_path / "snapshots"), "--state-dir", str(state), "--board-dir", str(board),
                  "--parquet-dir", str(pq), "--no-network", "--now", "2026-10-04T00:00:00Z", "--d1-sql", str(sql), "--freeze"])
    assert rc == 0
    payload = json.loads((board / json_out.BACKTEST_FILE).read_text())
    assert len(payload["grid"]) == 118 and [g["id"] for g in payload["grid"]] == list(range(1, 119))
    assert payload["meta"]["n_games"] == 1 and payload["meta"]["n_graded"] == 0
    game = payload["games"][0]
    # temp 55 / wind 16 / |spread| 6.5 / Positive → wind≥15, temp [50,60], spread ≤20 (Spread_l NaN→0), Positive = id 56
    assert game["game_id"] == "cfb:2026:5:a@b" and game["Signal"] == 56
    assert game["Sample"] == payload["grid"][55]["Sample"]
    assert set(payload) == {"meta", "grid", "stadium_results", "stadium_results_legacy", "alerts_clv", "games",
                            "tier_scorecard", "hist_games", "stadium_wx", "stadium_wx_bands"}
    # the historical (--from-git) blocks are always present and empty without a replay
    assert payload["tier_scorecard"] == [] and payload["hist_games"] == [] and "hist" not in payload["meta"]
    # stadium_wx is built by its own CLI; absent parquet -> empty, never a failure
    assert isinstance(payload["stadium_wx"], list) and isinstance(payload["stadium_wx_bands"], list)
    assert all(g["by_season"] == {} and g["by_season_close"] == {} for g in payload["grid"])
    # nothing graded yet: this season's columns are empty, the sheet's numbers ride along on every row
    assert payload["meta"]["legacy"] == {"source": "cfb_weather_backtest.xlsx", "seasons": "pre-2026", "n_buckets": 118}
    assert payload["grid"][0]["legacy"] == {"Wins": 165, "Losses": 162, "Push": 6, "Sample": 333, "Margin": -2.08, "ROI": -0.036,
                                            "+ CLV": 163, "CLV %": pytest.approx(0.4895, abs=1e-3)}
    assert payload["stadium_results"] == []
    assert len(payload["stadium_results_legacy"]) == 129
    assert payload["stadium_results_legacy"][1] == {"Team": "Pittsburgh", "Stadium": "Acrisure Stadium", "Record": "15-17-1",
                                                    "Percentage": pytest.approx(-0.102), "sport": "cfb"}
    for name in bt.PARQUET_TABLES:
        assert (pq / f"{name}.parquet").is_file()
    assert len(pd.read_parquet(pq / "grid.parquet")) == 118
    assert "legacy" not in pd.read_parquet(pq / "grid.parquet").columns
    assert (state / "closings.json").is_file()   # --freeze wrote the (empty) closings state
    assert not sql.exists()                       # nothing to execute: no closings, no graded stadiums
    # a fresh run with no inputs at all still emits the full grid
    rc = bt.main(["--snapshot-dir", str(tmp_path / "nope"), "--state-dir", str(state), "--board-dir", str(board),
                  "--parquet-dir", str(pq), "--no-network"])
    assert rc == 0 and len(json.loads((board / json_out.BACKTEST_FILE).read_text())["grid"]) == 118


def test_load_sqlite_replays_d1_inserts_on_migrations(tmp_path: Path):
    sql = tmp_path / "d1_inserts.sql"
    sql.write_text(
        "INSERT INTO games (game_id, sport, season, week, kickoff_utc, home_id, away_id, updated_at) VALUES "
        "('cfb:2026:5:a@b','cfb',5,5,'2026-10-03T19:30:00Z','b','a','x');\n"
        "INSERT OR IGNORE INTO closings (game_id, book, market, side, line, odds, scraped_at, kickoff_utc, frozen_at) VALUES "
        "('cfb:2026:5:a@b','pinnacle','total','over',47.0,-110,'t','k','f');\n"
    )
    d1 = bt.load_sqlite(sql)
    assert [g["game_id"] for g in d1.games] == ["cfb:2026:5:a@b"] and d1.closings[0]["line"] == 47.0
    assert bt.load_sqlite(tmp_path / "missing.db").empty
    export = tmp_path / "export"
    export.mkdir()
    (export / "alerts.json").write_text(json.dumps([{"results": [{"alert_key": "a", "clv_pts": 1.0}]}]))
    assert bt.load_export_dir(export).alerts == [{"alert_key": "a", "clv_pts": 1.0}]
