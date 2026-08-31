"""pipeline/backtest_git.py: the historical replay of the git line/forecast archive
(docs/HISTORICAL_BACKTEST_SPEC.md).

Everything runs offline from ``tests/fixtures/git_archive`` (real blobs of one NFL and one CFB
week of 2025 at leads ~7 d → 4 h, trimmed to 15 games, plus the nflverse / CFBD / ERA5 slices
those games need; rebuild with ``python scripts/make_backtest_git_fixtures.py``).

Covered: legacy-column parsing, naive-ET timestamps, the year-less ``Date``, team resolution and
``game_id``, NFL January season rollover, leads across the DST switch, alert-snapshot selection
(first tier inside 240 h) and tier persistence, closing selection (≤ 6 h), grading + ROI at −110
and +100 and the CLV sign, bucket assignment against the legacy first-match ids, by_season
aggregation, the ERA5 window cache, the v1 replay match rate, and
``python -m pipeline.backtest --from-git --no-network`` end to end.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pipeline import backtest as bt
from pipeline import backtest_git as bg
from pipeline.model import signals as sig_mod
from pipeline.outputs import json_out
from utils.timeutil import ET, ensure_utc, parse_iso

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "git_archive"
SEASON = 2025
UTC = timezone.utc


# ---- fixtures ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshots(manifest) -> dict[str, pd.DataFrame]:
    out = {}
    for sport in ("nfl", "cfb"):
        paths = [(e["sha"], e["commit_date"], FIXTURES / e["file"]) for e in manifest if e["sport"] == sport]
        out[sport] = pd.DataFrame(bg.extract_from_files(paths, sport), columns=list(bg.SNAP_COLUMNS))
    return out


@pytest.fixture(scope="module")
def caches(tmp_path_factory, snapshots) -> tuple[Path, Path]:
    """(git cache, era5 cache) laid out the way a real ``--from-git`` run leaves them.

    The ERA5 tree is copied because ``fill_actuals`` memoises windows next to it."""
    root = tmp_path_factory.mktemp("git_archive")
    git = root / "git"
    git.mkdir()
    for sport, df in snapshots.items():
        df.to_parquet(bg.snapshots_path(git, sport), index=False)
    shutil.copy(FIXTURES / "nflverse_games.csv", git / "nflverse_games.csv")
    shutil.copy(FIXTURES / f"cfbd_games_{SEASON}.json", git / f"cfbd_games_{SEASON}.json")
    era5 = root / "era5"
    shutil.copytree(FIXTURES / "era5", era5)
    return git, era5


@pytest.fixture(scope="module")
def defs() -> list[bt.Bucket]:
    return bt.load_grid_defs()


@pytest.fixture(scope="module")
def hist(caches, defs) -> bg.HistResult:
    git, era5 = caches
    return bg.run(seasons=[SEASON], defs=defs, git_cache=git, era5_cache=era5, no_network=True, log=lambda _m: None)


# ---- 1. snapshot parsing ------------------------------------------------------------------------

def test_snapshot_rows_map_the_legacy_columns_per_sport(snapshots):
    nfl, cfb = snapshots["nfl"], snapshots["cfb"]
    assert len(nfl) > 40 and len(cfb) > 90
    assert set(nfl.columns) == set(bg.SNAP_COLUMNS) == set(cfb.columns)
    # NFL 'away vs home' lowercase city names, BetOnline Total_now/Under_now/Spread_open
    row = nfl[nfl["away_raw"] == "green bay"].iloc[0]
    assert row["home_raw"] == "n.y. giants" and row["sport"] == "nfl" and row["sheet"] == "csv"
    assert row["total_now"] is not None and str(row["run_ts"]).endswith("Z")
    # CFB FBS 'Away @ Home' school names, FanDuel FD_now/Odds_n/Open
    fbs = cfb[cfb["sheet"] == "FBS"]
    assert (fbs["away_raw"] != "").all() and (fbs["home_raw"] != "").all()
    assert fbs["total_now"].notna().any() and fbs["spread_open"].notna().any()
    # the Other sheet is FCS-vs-FCS: its own Home Team / Away Team columns, and never a line
    other = cfb[cfb["sheet"] == "Other"]
    assert len(other) and other["total_now"].isna().all() and other["under_now"].isna().all()
    assert (other["away_raw"] != other["home_raw"]).all()


def test_timestamp_is_naive_et_and_the_other_sheet_falls_back_to_the_commit():
    commit = "2025-11-20T18:00:00+00:00"

    def run_ts(stamp, sheet="FBS"):
        rec = {"Game": "a @ b", "Date": "SAT 11/15", "gs_fg": -3.5}
        if stamp is not None:
            rec["Timestamp"] = stamp
        if sheet == "Other":
            rec.update({"Game": "a vs b", "Home Team": "b", "Away Team": "a"})
        return bg.snapshot_rows(pd.DataFrame([rec]), sport="cfb", sheet=sheet, sha="abc1234", commit_date=commit)[0]

    row = run_ts("2025-11-15T05:16:18.090399")
    assert row["run_ts"] == "2025-11-15T10:16:18Z"      # 05:16 EST -> 10:16 UTC
    assert row["run_month"] == 11 and row["sha"] == "abc1234"
    # DST: the same wall clock in EDT is an hour earlier in UTC
    assert run_ts("2025-11-01T05:16:18.090399")["run_ts"] == "2025-11-01T09:16:18Z"
    # fractional seconds of any precision (fromisoformat on 3.10 only takes 3 or 6 digits)
    assert run_ts("2025-11-15T05:16:18.1")["run_ts"] == "2025-11-15T10:16:18Z"
    assert run_ts("2025-11-15T05:16:18")["run_ts"] == "2025-11-15T10:16:18Z"
    assert run_ts(pd.Timestamp("2025-11-15T05:16:18"))["run_ts"] == "2025-11-15T10:16:18Z"
    assert run_ts("garbage")["run_ts"] == "2025-11-20T18:00:00Z"    # unparseable -> the commit
    assert run_ts(None, sheet="Other")["run_ts"] == "2025-11-20T18:00:00Z"   # the Other sheet has no Timestamp


def test_game_date_takes_the_nearest_occurrence_of_the_year_less_date():
    dec = datetime(2025, 12, 30, 15, 0, tzinfo=UTC)
    assert bg.game_date("SUN 01/05", dec) == datetime(2026, 1, 5).date()      # rolls into the next year
    jan = datetime(2025, 1, 3, 15, 0, tzinfo=UTC)
    assert bg.game_date("SAT 12/28", jan) == datetime(2024, 12, 28).date()    # rolls back
    stale = datetime(2024, 9, 24, 13, 15, tzinfo=UTC)                          # a row kept 2 days past kickoff
    assert bg.game_date("SUN 09/22", stale) == datetime(2024, 9, 22).date()
    assert bg.game_date("SAT 02/29", datetime(2025, 3, 1, tzinfo=UTC)) == datetime(2024, 2, 29).date()
    assert bg.game_date(None, dec) is None and bg.game_date("TBD", dec) is None


# ---- 2. identity ---------------------------------------------------------------------------------

def test_team_resolution_and_game_id(caches):
    git, _ = caches
    sched = bg.load_schedule("nfl", [SEASON], cache_dir=git, no_network=True)
    index = bg.GameIndex("nfl", sched)
    assert index.team("n.y. giants") == "nyg" and index.team("l.a. chargers") == "lac"
    assert index.team("green bay") == "gb"
    cfb = bg.GameIndex("cfb", [])
    assert cfb.team("Miami (OH)") == "miami-oh" and cfb.team("Brigham Young") == "brigham-young"
    assert cfb.team("Texas A&M") == "texas-am" and cfb.team("BYU") == "brigham-young"
    run_ts = datetime(2025, 11, 13, tzinfo=UTC)
    game = index.match("green bay", "n.y. giants", datetime(2025, 11, 16).date(), run_ts)
    assert game is not None and game.game_id == f"nfl:{SEASON}:11:gb@nyg"
    assert game.season == SEASON and game.week == 11 and game.home_id == "nyg" and game.away_id == "gb"
    assert game.home_score == 20 and game.away_score == 27 and game.roof_state == "outdoors"
    # a date a week off still resolves through the next kickoff of that matchup
    assert index.match("green bay", "n.y. giants", None, run_ts).game_id == game.game_id
    assert index.match("not a team", "n.y. giants", None, run_ts) is None
    assert index.unresolved["nfl|not a team"] == 1


def test_nfl_january_playoffs_belong_to_the_previous_season():
    csv = ("season,game_type,week,gameday,gametime,away_team,home_team,location,roof,stadium_id,stadium,home_score,away_score\n"
           "2025,REG,12,2025-11-23,13:00,GB,NYG,Home,outdoors,NYC01,MetLife Stadium,20,17\n"
           "2025,WC,1,2026-01-10,16:30,BUF,NE,Home,outdoors,BOS00,Gillette Stadium,24,21\n"
           "2025,SB,1,2026-02-08,18:30,SEA,NE,Neutral,dome,,Neutral Site,30,27\n")
    games = {g.game_id: g for g in bg.nfl_schedule(csv, [SEASON])}
    assert set(games) == {f"nfl:{SEASON}:12:gb@nyg", f"nfl:{SEASON}:19:buf@ne", f"nfl:{SEASON}:22:sea@ne"}
    wc = games[f"nfl:{SEASON}:19:buf@ne"]
    assert wc.season == SEASON and wc.kickoff_utc == datetime(2026, 1, 10, 21, 30, tzinfo=UTC)   # 16:30 EST
    assert wc.home_score == 24 and wc.away_score == 21
    assert games[f"nfl:{SEASON}:22:sea@ne"].neutral is True
    assert bg.nfl_schedule(csv, [2024]) == []


def test_lead_hours_are_real_hours_across_the_dst_switch(caches):
    git, _ = caches
    sched = [g for g in bg.load_schedule("nfl", [SEASON], cache_dir=git, no_network=True)][:1]
    game = sched[0]
    rec = {"sport": "nfl", "run_ts": bt.utc_iso(game.kickoff_utc - timedelta(hours=36)), "run_month": 11,
           "commit_date": "2025-11-01T00:00:00+00:00", "temp_fg": 40.0, "wind_fg": 9.0, "rain_fg": 0.0,
           "travel_alt": 0.0, "home_temp": 50.0, "away_temp": 50.0, "gs_fg": -0.0125, "sha": "x", "sheet": "csv"}
    assert bg.replay_snapshot(rec, game).lead_h == pytest.approx(36.0)
    # 2025-11-01 10:00 ET is EDT (-4), 2025-11-03 10:00 ET is EST (-5): the wall clocks are 48 h
    # apart but the real gap is 49 h, so every lead is computed on the UTC instants
    early = ensure_utc(parse_iso("2025-11-01T10:00:00", default_tz=ET))
    late = ensure_utc(parse_iso("2025-11-03T10:00:00", default_tz=ET))
    assert (late - early) / timedelta(hours=1) == pytest.approx(49.0)
    early_rec = {**rec, "run_ts": bt.utc_iso(early)}
    game_at_late = bg.SchedGame(game_id=game.game_id, sport="nfl", season=SEASON, week=1, kickoff_utc=late,
                                home_id=game.home_id, away_id=game.away_id)
    assert bg.replay_snapshot(early_rec, game_at_late).lead_h == pytest.approx(49.0)


# ---- 3. alert / closing selection ------------------------------------------------------------------

def _replay(lead_h: float, tier: str, total: float = 45.0, odds: float = -110.0) -> bg.Replay:
    return bg.Replay(run_ts=datetime(2025, 11, 20, tzinfo=UTC) + timedelta(hours=240 - lead_h), lead_h=lead_h,
                     sha="x", sheet="csv", temp_fg=40.0, wind_fg=12.0, rain_fg=0.0, gs_fg=-2.0, away_fg=0.0,
                     gs_fg_archived=-2.0, matched=True, tier=tier, total_now=total, under_now=odds)


def test_alert_snapshot_is_the_first_tier_inside_the_horizon():
    series = [_replay(300, sig_mod.HIGH), _replay(240, sig_mod.NO), _replay(200, sig_mod.LOW),
              _replay(100, sig_mod.VERY_HIGH), _replay(3, sig_mod.MID)]
    alert = bg.alert_snapshot(series)
    assert alert is not None and alert.lead_h == 200 and alert.tier == sig_mod.LOW   # 300 h is past the horizon
    assert bg.alert_snapshot([_replay(300, sig_mod.HIGH)]) is None
    assert bg.alert_snapshot([_replay(100, sig_mod.NO), _replay(50, sig_mod.NO)]) is None
    # a tier with no price on the board is not a bet
    unpriced = _replay(100, sig_mod.HIGH)
    unpriced.total_now = None
    assert bg.alert_snapshot([unpriced, _replay(20, sig_mod.LOW)]).lead_h == 20


def test_closing_snapshot_is_the_last_one_inside_six_hours():
    series = [_replay(200, sig_mod.LOW, 45.0), _replay(30, sig_mod.LOW, 44.0),
              _replay(5, sig_mod.LOW, 43.5), _replay(2, sig_mod.LOW, 43.0)]
    close = bg.closing_snapshot(series)
    assert close.lead_h == 2 and close.total_now == 43.0
    far = [_replay(200, sig_mod.LOW, 45.0), _replay(30, sig_mod.LOW, 44.0)]
    assert bg.closing_snapshot(far).lead_h == 30      # nothing inside 6 h -> the last snapshot, flagged by lead
    assert bg.closing_snapshot([]) is None
    assert bg.nearest_lead(series, 24.0).lead_h == 30 and bg.nearest_lead(series, 120.0) is None


def test_tier_rank_orders_persistence():
    assert bg.tier_rank(sig_mod.VERY_HIGH) > bg.tier_rank(sig_mod.HIGH) > bg.tier_rank(sig_mod.MID)
    assert bg.tier_rank("Low (Rain)") == bg.tier_rank(sig_mod.LOW) > bg.tier_rank(sig_mod.NO)
    assert bg.tier_rank(None) == 0 and bg.tier_rank(sig_mod.NO) == 0
    assert bg.lead_band(3.0) == "<=48h" and bg.lead_band(48.0) == "48-120h" and bg.lead_band(999.0) == ">120h"
    assert bg.lead_band(None) is None


# ---- 4. grading ------------------------------------------------------------------------------------

def test_grading_roi_and_clv_sign():
    assert bg.american_payout(-110) == pytest.approx(100 / 110)
    assert bg.american_payout(100) == 1.0 and bg.american_payout(150) == 1.5
    assert bg.american_payout(None) == pytest.approx(100 / 110)   # missing price -> the -110 default
    assert bg.roi_of("W", -110) == pytest.approx(0.909, abs=1e-3) and bg.roi_of("W", 100) == 1.0
    assert bg.roi_of("L", 100) == -1.0 and bg.roi_of("P", -110) == 0.0 and bg.roi_of(None, -110) is None

    def row(alert_total, close_total, actual, odds=-110.0):
        r = bt.GameRow(game_id="cfb:2025:11:a@b", sport="cfb", hist=True, alert_total=alert_total,
                       alert_under_odds=odds, total_close=close_total, close_under_odds=-110.0,
                       total_open=alert_total, home_score=int(actual // 2), away_score=int(actual - actual // 2))
        return bg.finalize_hist_row(r)

    win = row(48.5, 47.0, 40)
    assert win.under_result == "W" and win.close_result == "W" and win.margin == 8.5
    assert win.roi_alert == pytest.approx(100 / 110) and win.clv_pts == 1.5      # total came down: our way
    loss = row(40.0, 44.0, 50)
    assert loss.under_result == "L" and loss.roi_alert == -1.0 and loss.clv_pts == -4.0
    push = row(44.0, 44.0, 44)
    assert push.under_result == "P" and push.roi_alert == 0.0 and push.margin == 0.0
    plus = row(48.5, 47.0, 40, odds=100.0)
    assert plus.roi_alert == 1.0
    # the close bet is graded on its own line: the under wins at the alert total, loses at the close
    split = row(48.5, 44.0, 46)
    assert split.under_result == "W" and split.close_result == "L" and split.roi_close == -1.0
    assert row(None, 44.0, 40).under_result is None and row(None, 44.0, 40).close_result == "W"


def test_finalize_row_never_regrades_a_historical_row():
    r = bg.finalize_hist_row(bt.GameRow(game_id="g", sport="cfb", hist=True, alert_total=48.5, total_close=44.0,
                                        home_score=20, away_score=26))
    assert r.under_result == "W" and r.close_result == "L"
    assert bt.finalize_row(r) is r and r.under_result == "W"     # the close grading must not overwrite the alert bet


# ---- 5. buckets + aggregation -------------------------------------------------------------------------

def test_bucket_assignment_reproduces_the_legacy_signal_id(defs):
    # temp 55 / wind 16 / |spread| 6.5 / Positive -> wind>=15, temp [50,60], spread <=20, Positive = id 56
    # (the same hand-checked row as tests/test_backtest_grid.py)
    r = bt.GameRow(game_id="cfb:2025:11:a@b", sport="cfb", season=SEASON, hist=True, temp_fc=55.0, wind_fc=16.0,
                   spread_open=-6.5, total_open=47.0, total_close=45.0, alert_total=47.0,
                   home_score=20, away_score=20)
    bg.finalize_hist_row(r)
    assert r.clv_status == "Positive"
    assert bt.first_match(defs, r.sport, *bt.bucket_inputs(r), r.spread_abs, r.clv_status).id == 56
    assert bg.hist_game_rows([r], defs)[0]["Signal"] == 56


def test_by_season_aggregation_sums_and_splits_the_two_bets(defs):
    def game(season, alert_total, close_total, actual):
        r = bt.GameRow(game_id=f"cfb:{season}:11:a{actual}@b", sport="cfb", season=season, hist=True,
                       temp_fc=80.0, wind_fc=10.0, spread_open=-12.0, total_open=alert_total + 1,
                       total_close=close_total, alert_total=alert_total, alert_under_odds=-110.0,
                       close_under_odds=-110.0, home_score=actual, away_score=0)
        return bg.finalize_hist_row(r)

    # under 60 at the alert: 50 W, 70 L, 40 W; under 45 at the close: 50 L, 70 L, 40 W
    rows = [game(2024, 60.0, 45.0, 50), game(2024, 60.0, 45.0, 70), game(2025, 60.0, 45.0, 40)]
    grid = bg.season_grid(rows, defs)
    close = bg.season_grid(rows, defs, result_field="close_result", margin_field="close_margin")
    # bucket 1 = NCAAF wind 8-15, temp 75-100, |spread| 10-20
    assert grid[1]["2024"]["Sample"] == 2 and grid[1]["2025"]["Sample"] == 1
    assert grid[1][bg.ALL_HIST]["Sample"] == 3
    for key in ("Wins", "Losses", "Push", "Sample"):
        assert grid[1][bg.ALL_HIST][key] == grid[1]["2024"][key] + grid[1]["2025"][key]
    assert grid[1][bg.ALL_HIST]["Wins"] == 2 and grid[1][bg.ALL_HIST]["Losses"] == 1
    assert close[1][bg.ALL_HIST]["Wins"] == 1 and close[1][bg.ALL_HIST]["Losses"] == 2   # 45 is a tighter number
    assert grid[103][bg.ALL_HIST]["Sample"] == 0        # the separator row never matches
    assert set(grid[1][bg.ALL_HIST]) == set(bg.STAT_KEYS)


def test_tier_scorecard_keys_on_the_peak_tier(hist):
    """alert_tier is the FIRST tier of any kind, so a game that opened Low and became Very High
    is an alert-Low; the scorecard keys on peak_tier or it undercounts the severe tiers."""
    assert hist.scorecard, "the fixture week must produce at least one alerted tier"
    for row in hist.scorecard:
        assert row["sport"] in ("nfl", "cfb") and row["lead_band"] in [b[0] for b in bg.LEAD_BANDS]
        assert row["n"] == row["wins"] + row["losses"] + row["push"]
        assert row["win_pct"] is None or 0.0 <= row["win_pct"] <= 1.0
        assert row["persistence"] is None or 0.0 <= row["persistence"] <= 1.0
        assert row["evaporated"] is None or 0.0 <= row["evaporated"] <= 1.0
        assert row["n_actual"] <= row["n"]
        assert row["tier"] != sig_mod.NO
    assert sum(r["n"] for r in hist.scorecard) == hist.meta["n_peak_graded"]
    escalated = [r for r in hist.rows if r.alert_tier and r.peak_tier]
    assert escalated and all(bg.tier_rank(r.peak_tier) >= bg.tier_rank(r.alert_tier) for r in escalated)


def test_peak_snapshot_is_the_first_visit_to_the_worst_tier():
    """The escalation the alert rule misses: opens Low, becomes Very High, settles back to Mid."""
    series = [_replay(200, sig_mod.LOW, 48.0), _replay(150, sig_mod.LOW, 47.5),
              _replay(90, sig_mod.VERY_HIGH, 46.0), _replay(60, sig_mod.VERY_HIGH, 45.0),
              _replay(10, sig_mod.MID, 44.5)]
    alert = bg.alert_snapshot(series)
    peak = bg.peak_snapshot(series)
    assert alert.lead_h == 200 and alert.tier == sig_mod.LOW        # first tier of any kind
    assert peak.lead_h == 90 and peak.tier == sig_mod.VERY_HIGH     # first visit to the worst tier
    assert peak.total_now == 46.0                                    # and the number available there
    assert bg.peak_snapshot([_replay(50, sig_mod.NO)]) is None and bg.peak_snapshot([]) is None
    # a game that never escalates has peak == alert
    flat = [_replay(100, sig_mod.LOW, 45.0), _replay(20, sig_mod.LOW, 44.0)]
    assert bg.peak_snapshot(flat).lead_h == bg.alert_snapshot(flat).lead_h == 100


def test_replay_never_bets_before_the_week_opens(hist):
    """Same rule as alerts.py: no snapshot before Monday 00:00 ET of the game's own week."""
    from utils.timeutil import bet_week_open
    from utils.timeutil import parse_iso as _p

    for r in hist.rows:
        kick = _p(r.kickoff_utc)
        opened = bet_week_open(kick)
        for lead in (r.alert_lead_h, r.peak_lead_h, r.close_lead_h):
            if lead is not None:
                assert kick - timedelta(hours=lead) >= opened - timedelta(minutes=1), (r.game_id, lead)


def test_tier_on_actual_answers_whether_the_weather_showed_up(hist):
    known = [r for r in hist.rows if r.tier_on_actual]
    assert known, "ERA5 actuals should let most rows be re-scored"
    assert all(r.wind_act is not None for r in known)
    # the same function scores forecast and actual, so the labels come from one vocabulary
    labels = {r.tier_on_actual for r in known} | {r.peak_tier for r in hist.rows if r.peak_tier}
    assert labels <= {sig_mod.NO, sig_mod.MID, sig_mod.HIGH, sig_mod.VERY_HIGH,
                      "Low (Wind)", "Low (Rain)", "Low (Temp)"}
    assert bg.tier_for("nfl", 18.0, 40.0, 0.0, {}) == sig_mod.HIGH
    assert bg.tier_for("nfl", 9.0, 55.0, 0.0, {}) == "Low (Wind)"
    assert bg.tier_for("cfb", 2.0, 70.0, 0.0, {"open_spread": 3.0, "weekday": 5}) == sig_mod.NO


# ---- 6. ERA5 -------------------------------------------------------------------------------------------

def test_era5_index_covers_and_window_cache(caches, hist):
    _, era5 = caches
    index = bg.era5_index(era5)
    assert index, "the fixture ships one hourly file per stadium"
    sid, entries = next(iter(sorted(index.items())))
    start, end, path = entries[0]
    assert bg.era5_covers(entries, datetime.fromisoformat(start).date()) == path
    assert bg.era5_covers(entries, datetime(2000, 1, 1).date()) is None
    assert bg.half_year_window(datetime(2025, 11, 23).date()) == ("2025-07-01", "2025-12-31")
    assert bg.half_year_window(datetime(2026, 1, 4).date()) == ("2026-01-01", "2026-06-30")
    assert bg.missing_era5(hist.rows, index) == []
    assert bg.window_cache_path(era5).is_file()      # the replay memoised its windows
    with_actual = [r for r in hist.rows if r.wind_act is not None]
    assert len(with_actual) >= len(hist.rows) - 1
    assert all(r.src_actual.startswith("era5:") for r in with_actual)


# ---- 7. the whole replay -------------------------------------------------------------------------------

def test_replay_reproduces_the_archived_model_numbers(hist):
    assert hist.meta["model_match_rate"] >= 0.99
    assert hist.meta["n_unresolved"] == 0
    # every FBS / NFL row found its schedule game; the Other sheet (FCS-vs-FCS) is expected to miss
    sheets = hist.meta["rows_by_sheet"]
    assert sheets.get("nfl:unmatched:csv", 0) == 0 and sheets.get("cfb:unmatched:FBS", 0) == 0
    assert sheets["cfb:rows:Other"] > 0


def test_hist_rows_carry_both_bets_and_the_replay_metadata(hist):
    assert hist.meta["n_games"] >= 40 and hist.meta["n_graded"] >= 40
    assert hist.meta["n_alerted"] >= 5 and hist.meta["n_bet_graded"] <= hist.meta["n_alerted"]
    for r in hist.rows:
        assert r.hist is True and r.season == SEASON and r.line_book == bg.LINE_BOOK[r.sport]
        assert r.game_id.startswith(f"{r.sport}:{SEASON}:")
        assert r.n_snapshots >= 1 and r.kickoff_utc.endswith("Z")
        assert (r.stadium_id is None) == (r.lat is None)
        if r.alert_tier:
            assert 0 < r.alert_lead_h <= bg.ALERT_MAX_LEAD_H and r.alert_total is not None
            assert r.wind_alert is not None
        if r.close_result is not None:
            assert r.total_close is not None and r.actual_total is not None
    # a venue missing from stadiums.csv is the only reason a row has no coordinates (and so no ERA5)
    located = [r for r in hist.rows if r.stadium_id]
    assert len(located) >= 0.9 * len(hist.rows)
    graded = [r for r in hist.rows if r.under_result is not None]
    assert graded and all(r.roi_alert is not None for r in graded)
    assert all(r.clv_pts == pytest.approx(r.alert_total - r.total_close, abs=1e-6)
               for r in graded if r.total_close is not None)


def test_coverage_and_lead_tables(hist):
    cov = hist.meta["coverage"]
    assert cov and {c["sport"] for c in cov} == {"nfl", "cfb"}
    for c in cov:
        assert c["season"] == SEASON and c["week"] > 0
        assert c["graded"] <= c["priced"] <= c["games"] and c["bet_graded"] <= c["alerted"]
    assert sum(c["games"] for c in cov) == hist.meta["n_games"]
    leads = hist.leads
    assert leads and {row["lead"] for row in leads} <= {"alert", "close", *(f"l{n}" for n in bg.ERR_LEADS)}
    assert all(row["wind_err"] == pytest.approx(abs(row["wind_fg"] - row["wind_act"]), abs=0.011) for row in leads)


def test_block_shape_feeds_the_payload(hist, defs):
    block = hist.block()
    assert set(block) == {"by_season", "by_season_close", "stadium_results", "tier_scorecard", "hist_games",
                          "leads", "meta"}
    assert set(block["by_season"]) == {str(b.id) for b in defs}
    assert set(block["by_season"]["1"]) == {str(SEASON), bg.ALL_HIST}
    assert block["hist_games"] and all(g["game_id"] and g["kickoff_utc"] for g in block["hist_games"])
    assert all(s["season"] == SEASON for s in block["stadium_results"])


# ---- 8. CLI end to end ------------------------------------------------------------------------------------

def test_cli_from_git_no_network_writes_the_historical_blocks(tmp_path: Path, caches):
    git, era5 = caches
    board, pq = tmp_path / "board", tmp_path / "pq"
    rc = bt.main(["--from-git", "--seasons", str(SEASON), "--git-cache", str(git), "--era5-cache", str(era5),
                  "--board-dir", str(board), "--parquet-dir", str(pq), "--snapshot-dir", str(tmp_path / "none"),
                  "--state-dir", str(tmp_path / "state"), "--no-network", "--now", "2026-08-01T00:00:00Z"])
    assert rc == 0
    payload = json.loads((board / json_out.BACKTEST_FILE).read_text(encoding="utf-8"))
    assert len(payload["grid"]) == 118
    hist_meta = payload["meta"]["hist"]
    assert hist_meta["seasons"] == [SEASON] and hist_meta["n_graded"] >= 40 and hist_meta["model_match_rate"] >= 0.99
    assert payload["tier_scorecard"] and payload["hist_games"]
    row = next(g for g in payload["grid"] if g["by_season"].get(bg.ALL_HIST, {}).get("Sample"))
    assert set(row["by_season"]) == {str(SEASON), bg.ALL_HIST}
    assert set(row["by_season"][bg.ALL_HIST]) == set(bg.STAT_KEYS)
    assert row["by_season_close"][bg.ALL_HIST]["Sample"] >= row["by_season"][bg.ALL_HIST]["Sample"]
    assert row["legacy"]["Sample"] is not None          # the sheet's numbers still ride along
    assert {s["season"] for s in payload["stadium_results"]} == {SEASON}
    for name in ("hist_games", "hist_grid", "hist_stadiums", "hist_leads", "hist_scorecard"):
        assert (pq / f"{name}.parquet").is_file()
    grid_pq = pd.read_parquet(pq / "hist_grid.parquet")
    assert set(grid_pq["bet"]) == {"alert", "close"} and len(grid_pq) == 118 * 2 * 2

    # a later weekly run without --from-git must keep the historical groups
    rc = bt.main(["--board-dir", str(board), "--parquet-dir", str(pq), "--snapshot-dir", str(tmp_path / "none"),
                  "--state-dir", str(tmp_path / "state"), "--no-network", "--now", "2026-08-02T00:00:00Z"])
    assert rc == 0
    carried = json.loads((board / json_out.BACKTEST_FILE).read_text(encoding="utf-8"))
    assert carried["hist_games"] == payload["hist_games"]
    assert carried["tier_scorecard"] == payload["tier_scorecard"]
    assert carried["meta"]["hist"]["n_graded"] == hist_meta["n_graded"]
    assert next(g for g in carried["grid"] if g["id"] == row["id"])["by_season"] == row["by_season"]
    assert {s["season"] for s in carried["stadium_results"]} == {SEASON}


def test_hist_from_previous_ignores_a_payload_without_history(tmp_path: Path):
    assert bt.hist_from_previous(tmp_path / "missing.json") == {}
    plain = tmp_path / "backtest.json"
    plain.write_text(json.dumps({"meta": {}, "grid": [{"id": 1}]}), encoding="utf-8")
    assert bt.hist_from_previous(plain) == {}
    plain.write_text("not json", encoding="utf-8")
    assert bt.hist_from_previous(plain) == {}
