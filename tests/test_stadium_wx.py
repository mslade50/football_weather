"""pipeline/stadium_wx.py: per-stadium under records keyed on the ERA5 actuals
(docs/HISTORICAL_BACKTEST_SPEC.md §8).

Offline throughout: the parsers run on inline payloads, and the ERA5 filler on the hourly slices
in tests/fixtures/git_archive/era5. The two statistical guards — that the absolute wind bands are
computed and that ``venue_noise_check`` can tell a real venue spread from coin flips — are the
point of the module, so they are pinned here rather than left to the report.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import backtest as bt
from pipeline import stadium_wx as sw

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "git_archive"
UTC = timezone.utc

NFLVERSE = (
    "season,game_type,week,gameday,gametime,away_team,home_team,location,roof,stadium_id,stadium,"
    "home_score,away_score,total_line,under_odds,total\n"
    "2019,REG,5,2019-10-06,13:00,GB,DAL,Home,dome,DAL00,AT&T Stadium,24,34,47.5,-110,58\n"
    "2019,REG,6,2019-10-13,13:00,NYJ,NE,Home,outdoors,BOS00,Gillette Stadium,33,0,43.5,-105,33\n"
    "2019,REG,7,2019-10-20,13:00,BAL,SEA,Home,outdoors,SEA00,CenturyLink Field,16,30,45.0,,46\n"
    "2019,REG,8,2019-10-27,13:00,CHI,LAC,Home,outdoors,SDG00,Dignity Health Park,17,16,40.5,-110,33\n"
    "2019,REG,9,2019-11-03,13:00,MIN,KC,Home,,KAN00,Arrowhead Stadium,23,26,,-110,49\n"
)


def _cfbd_pair():
    games = [{"id": 401, "venueId": "3504", "venue": "Kidd Brewer Stadium", "season": 2019},
             {"id": 402, "venueId": None, "venue": "Nowhere Field", "season": 2019}]
    lines = [
        {"id": 401, "season": 2019, "week": 7, "seasonType": "regular", "startDate": "2019-10-19T19:30:00.000Z",
         "homeTeam": "Appalachian State", "awayTeam": "Louisiana Monroe", "homeScore": 52, "awayScore": 7,
         "lines": [{"provider": "Bovada", "overUnder": 55.5, "overUnderOpen": 54.0},
                   {"provider": "DraftKings", "overUnder": 56.5},
                   {"provider": "William Hill", "overUnder": 55.5, "overUnderOpen": 55.0}]},
        {"id": 402, "season": 2019, "week": 7, "seasonType": "regular", "startDate": "2019-10-19T19:30:00.000Z",
         "homeTeam": "Ohio", "awayTeam": "Buffalo", "homeScore": 21, "awayScore": 24,
         "lines": [{"provider": "Bovada", "overUnder": 60.0}]},
    ]
    return games, lines


# ---- parsers ------------------------------------------------------------------------------------

def test_consensus_total_is_the_median_across_providers():
    assert sw.consensus_total([{"overUnder": 45, "overUnderOpen": 44}, {"overUnder": 44.5},
                               {"overUnder": 48.5, "overUnderOpen": 47}]) == (45.0, 45.5, 3)
    assert sw.consensus_total([]) == (None, None, 0)
    assert sw.consensus_total([{"provider": "x"}]) == (None, None, 0)
    assert sw.consensus_total([{"overUnder": 41.0}])[0] == 41.0


def test_nfl_rows_keep_outdoor_priced_games_only():
    rows = {r.game_id: r for r in sw.nfl_rows(NFLVERSE, [2019])}
    # the dome game and the game with no total_line are dropped
    assert set(rows) == {"nfl:2019:6:nyj@ne", "nfl:2019:7:bal@sea", "nfl:2019:8:chi@lac"}
    ne = rows["nfl:2019:6:nyj@ne"]
    assert ne.total_close == 43.5 and ne.close_under_odds == -105.0
    assert ne.home_score == 33 and ne.away_score == 0 and ne.roof_state == "outdoors"
    assert ne.hist is True and ne.src_result == "nflverse" and ne.kickoff_utc.endswith("Z")
    assert rows["nfl:2019:7:bal@sea"].close_under_odds is None       # blank price -> the -110 default at grading
    assert sw.nfl_rows(NFLVERSE, [2018]) == []


def test_cfb_rows_join_lines_to_the_venue_from_games():
    games, lines = _cfbd_pair()
    rows = sw.cfb_rows(games, lines, 2019)
    assert len(rows) == 2
    app = next(r for r in rows if r.home_id == "appalachian-state")
    assert app.total_close == 55.5 and app.total_open == 54.5     # median of 55.5/56.5/55.5 and 54/55
    assert app.home_score == 52 and app.away_score == 7 and app.week == 7
    assert app.ref_book == "cfbd:3" and app.season == 2019
    # the venue is looked up from /games by id; a game with no line at all is dropped
    assert next(r for r in rows if r.home_id == "ohio").stadium_id is None
    assert sw.cfb_rows(games, [dict(lines[1], lines=[])], 2019) == []


def test_grading_uses_the_closing_total_and_its_price():
    rows = sw.nfl_rows(NFLVERSE, [2019])
    for r in rows:
        bt.finalize_row(r)          # hist rows are a no-op here; stadium_wx grades via finalize_hist_row
    from pipeline import backtest_git as bg

    for r in rows:
        bg.finalize_hist_row(r)
    ne = next(r for r in rows if r.game_id == "nfl:2019:6:nyj@ne")
    assert ne.actual_total == 33.0 and ne.close_result == "W"      # 33 < 43.5
    assert ne.roi_close == pytest.approx(100 / 105)                # graded at the -105 the file carried
    sea = next(r for r in rows if r.game_id == "nfl:2019:7:bal@sea")
    assert sea.actual_total == 46.0 and sea.close_result == "L" and sea.roi_close == -1.0


# ---- ERA5, indexed ------------------------------------------------------------------------------

def test_indexed_filler_matches_the_scanning_one(tmp_path: Path):
    """The fast path exists because window_stats rescans the whole hourly file per game; it must
    still produce the same window means."""
    from pipeline import backtest_git as bg

    era5 = tmp_path / "era5"
    shutil.copytree(FIXTURES / "era5", era5)
    index = bg.era5_index(era5)
    sid, entries = next(iter(sorted(index.items())))
    hourly = (json.loads(entries[0][2].read_text(encoding="utf-8")) or {}).get("hourly") or {}
    kick = bt._dt(str(hourly["time"][1]) + ":00Z")
    start, end = bt._window(kick)
    slow = bt.window_stats(hourly, start, end)
    fast = sw._window_from_index(hourly, sw._hour_index(hourly), start)
    for key in ("temp", "wind", "gust", "rain"):
        assert fast[key] == pytest.approx(slow[key], abs=1e-9), key
    assert sw._window_from_index(hourly, sw._hour_index(hourly), datetime(1990, 1, 1, tzinfo=UTC)) == {}


def test_fill_actuals_uses_and_writes_the_shared_window_cache(tmp_path: Path):
    from pipeline import backtest_git as bg

    era5 = tmp_path / "era5"
    shutil.copytree(FIXTURES / "era5", era5)
    index = bg.era5_index(era5)
    sid, entries = next(iter(sorted(index.items())))
    hourly = (json.loads(entries[0][2].read_text(encoding="utf-8")) or {}).get("hourly") or {}
    kick = bt._dt(str(hourly["time"][1]) + ":00Z")
    row = bt.GameRow(game_id="nfl:2019:6:a@b", sport="nfl", stadium_id=sid,
                     kickoff_utc=bt.utc_iso(kick), total_close=44.0, home_score=20, away_score=17)
    assert sw.fill_actuals([row], cache_dir=era5, log=lambda _m: None) == 1
    assert row.wind_act is not None and row.src_actual.startswith("era5:")
    assert bg.window_cache_path(era5).is_file()
    # second pass is a pure cache hit (the hourly file is not reopened)
    again = bt.GameRow(game_id="nfl:2019:6:a@b", sport="nfl", stadium_id=sid, kickoff_utc=bt.utc_iso(kick))
    assert sw.fill_actuals([again], cache_dir=era5, log=lambda _m: None) == 1
    assert again.wind_act == row.wind_act


# ---- aggregation --------------------------------------------------------------------------------

def _game(sport, sid, season, wind, actual, total=45.0, odds=-110.0, i=0):
    from pipeline import backtest_git as bg

    r = bt.GameRow(game_id=f"{sport}:{season}:1:a{i}@b", sport=sport, season=season, week=1,
                   kickoff_utc=bt.utc_iso(datetime(season, 10, 1, tzinfo=UTC) + timedelta(days=i)),
                   stadium_id=sid, stadium_name=sid.replace("-", " ").title(), home_id="b",
                   total_close=total, close_under_odds=odds, home_score=actual, away_score=0, hist=True)
    r.wind_act = wind
    return bg.finalize_hist_row(r)


def test_stadium_records_carry_every_band_and_the_venue_quartile():
    rows = [_game("nfl", "windy-field", 2015 + (i % 10), wind=5.0 + i, actual=30 if i % 2 else 60, i=i)
            for i in range(20)]
    rec = next(r for r in sw.stadium_records(rows) if r["stadium_id"] == "windy-field")
    assert rec["sport"] == "nfl" and rec["all_n"] == 20 and rec["seasons"] == "2015-2024"
    assert rec["all_record"] == "10-10-0" and rec["all_win_pct"] == 0.5
    for band, lo in sw.WIND_BANDS:
        assert rec[f"{band}_n"] == sum(1 for r in rows if r.wind_act >= lo), band
    assert rec["wind_p75"] is not None and rec["top25_n"] >= 5      # the venue's own windiest quarter
    assert rec["early_n"] + rec["late_n"] == rec["all_n"]
    assert rec["thin"] is False
    thin = sw.stadium_records([_game("cfb", "tiny", 2019, 8.0, 30, i=i) for i in range(3)])
    assert thin[0]["thin"] is True and thin[0]["wind_p75"] is None  # under QUARTILE_MIN_N


def test_wind_band_table_pools_by_sport():
    # windy games go under, calm games go over
    rows = [_game("nfl", "s1", 2019, wind=14.0, actual=30, i=i) for i in range(10)]
    rows += [_game("nfl", "s1", 2019, wind=4.0, actual=60, i=50 + i) for i in range(10)]
    bands = {(b["sport"], b["band"]): b for b in sw.wind_band_table(rows)}
    assert bands[("nfl", "wind12")]["record"] == "10-0-0" and bands[("nfl", "wind12")]["win_pct"] == 1.0
    assert bands[("nfl", "under10")]["record"] == "0-10-0" and bands[("nfl", "under10")]["win_pct"] == 0.0
    assert bands[("all", "wind10")]["n"] == 10
    assert {b["sport"] for b in sw.wind_band_table(rows)} == {"nfl", "all"}
    assert sw.wind_band_table([]) == []


def test_venue_noise_check_separates_a_real_spread_from_coin_flips():
    """The guard that stops the venue table being presented as an edge."""
    noise = [{"top25_n": 16, "top25_roi": v} for v in
             (0.25, -0.20, 0.18, -0.15, 0.12, -0.10, 0.24, -0.19, 0.17, -0.14, 0.11, -0.09)]
    got = sw.venue_noise_check(noise)
    assert got["n_venues"] == 12 and got["median_n"] == 16
    assert got["signal_ratio"] < 1.15 and got["verdict"] == "indistinguishable from sampling noise"
    # a genuinely wide spread at the same sample size trips the other branch
    wide = [{"top25_n": 16, "top25_roi": v} for v in
            (1.6, -1.5, 1.4, -1.3, 1.2, -1.1, 1.0, -0.9, 1.5, -1.4, 1.1, -1.0)]
    assert sw.venue_noise_check(wide)["verdict"] == "venue spread exceeds sampling noise"
    assert sw.venue_noise_check([]) == {}          # too few venues to say anything


def test_parse_seasons_and_cli_defaults():
    assert sw.parse_seasons("2015-2024") == list(range(2015, 2025))
    assert sw.parse_seasons("2019,2021") == [2019, 2021]
    args = sw.parse_args([])
    assert args.seasons == "2015-2024" and args.sport is None and args.no_network is False
    assert sw.load_records(Path("does-not-exist.parquet")) == []
