"""pipeline/backtest_git.py: archive parsing, game matching, alert/close selection, grading (offline)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from pipeline import backtest_git as bg

UTC = timezone.utc


def _nfl_raw(rows: list[dict]) -> pd.DataFrame:
    base = {"Game": "denver vs tampa bay", "Date": "SUN 09/22", "Time": "01:00 PM", "wind_fg": 5.0, "temp_fg": 70.0,
            "rain_fg": 0.0, "travel_alt": 0.0, "home_temp": 80.0, "away_temp": 60.0, "gs_fg": -0.02, "away_fg": 0.0,
            "Total_now": 40.5, "Under_now": -115, "Total_open": 39.0, "Spread_now": -6.5, "Spread_open": 6.5,
            "Timestamp": "2024-09-20 10:00:00", "sheet": "csv", "sha": "abc", "commit_date": "2024-09-20T14:05:00+00:00"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_split_game_nfl_and_cfb() -> None:
    assert bg.split_game("nfl", "n.y. giants vs cleveland") == ("n.y. giants", "cleveland")
    assert bg.split_game("cfb", "Miami (OH) @ Ohio") == ("Miami (OH)", "Ohio")
    assert bg.split_game("cfb", "garbage") == (None, None)


def test_parse_run_ts_is_naive_et_across_dst() -> None:
    assert bg.parse_run_ts("2024-09-20 10:00:00", "2024-09-20T00:00:00+00:00") == datetime(2024, 9, 20, 14, tzinfo=UTC)
    assert bg.parse_run_ts("2024-12-01 10:00:00", "2024-12-01T00:00:00+00:00") == datetime(2024, 12, 1, 15, tzinfo=UTC)
    # unparseable Timestamp falls back to the commit date
    assert bg.parse_run_ts("nan", "2024-12-01T16:00:00+00:00") == datetime(2024, 12, 1, 16, tzinfo=UTC)


def test_infer_kickoff_rolls_year_for_january_playoffs() -> None:
    run = datetime(2024, 12, 30, 15, tzinfo=UTC)
    assert bg.infer_kickoff("SAT 01/11", "04:30 PM", run) == datetime(2025, 1, 11, 21, 30, tzinfo=UTC)
    # a September game seen from a September run stays in the same year (EDT = UTC-4)
    assert bg.infer_kickoff("SUN 09/22", "01:00 PM", datetime(2024, 9, 20, tzinfo=UTC)) == datetime(2024, 9, 22, 17, tzinfo=UTC)
    assert bg.infer_kickoff("bad", "01:00 PM", run) is None


def test_normalize_nfl_converts_fractions_and_maps_lines() -> None:
    s = bg.normalize_snapshots(_nfl_raw([{}]), "nfl")
    r = s.iloc[0]
    assert r.gs_fg_archived == pytest.approx(-2.0)
    assert (r.total, r.under_odds, r.open_total) == (40.5, -115, 39.0)
    assert r.spread_abs == 6.5 and r.open_spread_abs == 6.5   # signs in the archive are unreliable
    assert (r.away_raw, r.home_raw, r.line_book) == ("denver", "tampa bay", "betonline")


def test_normalize_cfb_maps_fanduel_columns() -> None:
    raw = pd.DataFrame([{"Game": "Utah @ Oklahoma State", "Date": "SAT 09/21", "Time": "03:30 PM", "wind_fg": 10.0,
                         "temp_fg": 80.0, "gs_fg": -2.0, "FD_now": 52.5, "Odds_n": -110, "Fd_open": 53.0,
                         "Current": 2.5, "Open": -2.5, "Timestamp": "2024-09-19 15:32:07", "sheet": "FBS",
                         "sha": "x", "commit_date": "2024-09-19T19:40:00+00:00"}])
    r = bg.normalize_snapshots(raw, "cfb").iloc[0]
    assert (r.total, r.under_odds, r.open_total, r.spread_abs, r.gs_fg_archived) == (52.5, -110, 53.0, 2.5, -2.0)


def _sched(kick: datetime) -> pd.DataFrame:
    return pd.DataFrame([{"sport": "nfl", "season": 2024, "week": 3, "home_id": "tb", "away_id": "den", "kickoff": kick,
                          "home_score": 7, "away_score": 26, "close_spread_src": 6.5, "close_total_src": 40.5, "roof": "outdoors",
                          "neutral": False, "venue_key": "TAM00", "wind_reported": 6, "temp_reported": 85}])


def test_attach_schedule_matches_and_reports_unresolved() -> None:
    kick = datetime(2024, 9, 22, 17, tzinfo=UTC)
    snaps = bg.normalize_snapshots(_nfl_raw([{}, {"Game": "atlantis vs tampa bay"}]), "nfl")
    m, unresolved = bg.attach_schedule(snaps, _sched(kick))
    assert list(m.game_id) == ["nfl:2024:3:den@tb"]
    assert m.kickoff.iat[0] == kick
    assert unresolved == ["atlantis vs tampa bay"]


def test_attach_schedule_rejects_far_kickoff() -> None:
    snaps = bg.normalize_snapshots(_nfl_raw([{}]), "nfl")
    m, unresolved = bg.attach_schedule(snaps, _sched(datetime(2024, 10, 22, 17, tzinfo=UTC)))
    assert m.empty and unresolved == ["denver vs tampa bay"]


def _series() -> pd.DataFrame:
    kick = datetime(2024, 9, 22, 17, tzinfo=UTC)
    rows = [  # (run time ET, wind, total)
        {"Timestamp": "2024-09-10 10:00:00", "wind_fg": 20.0, "Total_now": 44.0},   # lead > 240 h: not an alert
        {"Timestamp": "2024-09-17 10:00:00", "wind_fg": 5.0, "Total_now": 43.0},
        {"Timestamp": "2024-09-19 10:00:00", "wind_fg": 16.0, "temp_fg": 40.0, "Total_now": 42.5},  # first alert
        {"Timestamp": "2024-09-21 10:00:00", "wind_fg": 18.0, "temp_fg": 40.0, "Total_now": 41.0},
        {"Timestamp": "2024-09-22 09:00:00", "wind_fg": 17.0, "temp_fg": 40.0, "Total_now": 40.5},  # close (4 h)
        {"Timestamp": "2024-09-22 12:45:00", "wind_fg": 17.0, "temp_fg": 40.0, "Total_now": 40.0},  # < 0.5 h: dropped
    ]
    snaps = bg.normalize_snapshots(_nfl_raw(rows), "nfl")
    m, _ = bg.attach_schedule(snaps, _sched(kick))
    return bg.replay(m)


def test_replay_drops_post_kick_leads_and_tiers() -> None:
    s = _series()
    assert len(s) == 5 and s.lead_h.min() >= 0.5
    # current policy: >= 15 mph is a flag even when warm (Low); 16 mph at 40F is High (32-45F band)
    assert list(s.tier) == ["Low Impact", "No Impact", "High Impact", "High Impact", "High Impact"]


def test_summarize_alert_and_close_selection() -> None:
    g = bg.summarize_games(_series()).iloc[0]
    assert g.alert and g.alert_total == 42.5 and g.alert_tier == "High Impact"
    assert g.close_total == 40.5 and g.close_lead_h == pytest.approx(4.0)
    assert g.open_total == 39.0 and g.tier_max == "High Impact"


def test_grading_and_profit() -> None:
    assert bg.grade_under(40.5, 33) == "win" and bg.grade_under(40.5, 41) == "loss" and bg.grade_under(40, 40) == "push"
    assert bg.grade_under(None, 30) is None
    assert bg.unit_profit("win", -110) == pytest.approx(100 / 110)
    assert bg.unit_profit("win", 100) == pytest.approx(1.0)
    assert bg.unit_profit("win", None) == pytest.approx(100 / 110)
    assert bg.unit_profit("loss", -110) == -1.0 and bg.unit_profit("push", -110) == 0.0


def test_attach_results_clv_sign() -> None:
    games = bg.summarize_games(_series())
    out = bg.attach_results(games, _sched(datetime(2024, 9, 22, 17, tzinfo=UTC))).iloc[0]
    assert out.actual_total == 33 and out.alert_result == "win" and out.close_result == "win"
    assert out.clv_pts == pytest.approx(2.0)   # alerted at 42.5, closed 40.5: line moved our way


def _hourly() -> dict:
    return {"time": [f"2024-09-22T{h:02d}:00" for h in range(24)],
            "wind_speed_10m": [float(h) for h in range(24)], "wind_gusts_10m": [2.0 * h for h in range(24)],
            "temperature_2m": [70.0] * 24, "precipitation": [0.5] * 24, "wind_direction_10m": [270.0] * 24}


def test_window_mean_uses_three_sample_contract() -> None:
    w = bg.window_mean(_hourly(), datetime(2024, 9, 22, 17, 25, tzinfo=UTC))
    assert w["wind_act"] == pytest.approx(18.0)      # 17, 18, 19 h (kickoff..+2h), not 20 h
    assert w["rain_act_mm"] == pytest.approx(1.5) and w["dir_act"] == "W"


def test_actuals_cache_raw_payload_and_skip_covered(tmp_path: Path) -> None:
    games = pd.DataFrame([{"game_id": "nfl:2024:3:den@tb", "venue_key": "TAM00", "roof": "outdoors",
                           "kickoff": datetime(2024, 9, 22, 17, tzinfo=UTC)}])
    calls: list[str] = []
    out = bg.attach_actuals(games, {"TAM00": (27.9, -82.5, "open")}, tmp_path,
                            get=lambda u: calls.append(u) or {"hourly": _hourly()}, pause_s=0)
    assert out.wind_act.iat[0] == pytest.approx(18.0) and len(calls) == 1
    again = bg.attach_actuals(games, {"TAM00": (27.9, -82.5, "open")}, tmp_path, network=False)
    assert again.wind_act.iat[0] == pytest.approx(18.0)   # aggregated from the cached raw payload
    dome = bg.attach_actuals(games, {"TAM00": (27.9, -82.5, "dome")}, tmp_path, network=False)
    assert dome.wind_act.isna().all() and dome.venue_roof.iat[0] == "dome"
    closed = bg.attach_actuals(games.assign(roof="closed"), {"TAM00": (27.9, -82.5, "retractable")}, tmp_path, network=False)
    assert closed.wind_act.isna().all()                   # retractable roof closed for this game


def test_cfb_schedule_postseason_week_offset_and_dedupe() -> None:
    gm = {"id": 1, "season": 2025, "week": 1, "seasonType": "postseason", "homeTeam": "Indiana", "awayTeam": "Alabama",
          "startDate": "2026-01-01T21:00:00.000Z", "homePoints": 38, "awayPoints": 3, "neutralSite": True, "venueId": 9}
    lines = [{"id": 1, "lines": [{"spread": -6.5, "overUnder": 47.5}]}]
    s = bg.cfb_schedule([gm, gm], lines)
    assert len(s) == 1 and s.week.iat[0] == 16
    assert s.close_spread_src.iat[0] == 6.5 and s.close_total_src.iat[0] == 47.5   # home favoured by 6.5


def test_load_schedule_requests_postseason_and_venues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    urls: list[str] = []
    monkeypatch.setenv("CFBD_API_KEY", "k")
    monkeypatch.setattr(bg, "_read_url", lambda url, *a, **k: urls.append(url) or b"[]")
    bg.load_schedule("cfb", [2025], tmp_path, True)
    assert any("seasonType=postseason" in u and "/games" in u for u in urls)
    assert any("seasonType=postseason" in u and "/lines" in u for u in urls)
    assert any(u.endswith("/venues") for u in urls)


def test_fixed_leads_use_unpriced_snapshots() -> None:
    base = dict(game_id="g", sport="cfb", season=2024, week=10, kickoff=datetime(2024, 11, 2, 20, tzinfo=UTC), home_id="rice",
                away_id="navy", line_book="fanduel", under_odds=-110, spread_abs=6, open_total=50, temp_fg=65, rain_fg=0,
                gs_fg=-2, gs_fg_archived=-2, tier="Low Impact")
    s = pd.DataFrame([dict(base, run_ts=datetime(2024, 10, 28, 20, tzinfo=UTC), lead_h=120, wind_fg=18, total=None),
                      dict(base, run_ts=datetime(2024, 11, 2, 17, tzinfo=UTC), lead_h=3, wind_fg=10, total=48)])
    g = bg.summarize_games(s).iloc[0]
    assert g.wind_fg_120h == 18 and pd.isna(g.total_120h) and g.close_total == 48


def test_run_keeps_other_sheet_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = pd.DataFrame([{"sheet": "Other", "Game": "Monmouth vs Florida International"}])
    seen: list[int] = []

    class Stop(Exception):
        pass

    def capture(df: pd.DataFrame, sport: str) -> pd.DataFrame:
        seen.append(len(df))
        raise Stop

    monkeypatch.setattr(bg, "extract_archive", lambda *a, **k: raw)
    monkeypatch.setattr(bg, "normalize_snapshots", capture)
    with pytest.raises(Stop):
        bg.run("cfb", [2024], tmp_path, network=False)
    assert seen == [1]
