from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.contracts import (
    Degradation,
    Edge,
    Game,
    GameLine,
    RunMeta,
    Stadium,
    Team,
    WeatherForecast,
    WeatherPoint,
    make_game_id,
    odds_key,
)
from pipeline.run_context import RunContext
from utils.timeutil import ET, date_label, naive_et_iso, parse_iso, run_id_for, time_label, to_et, utc_iso


def _game(**kw) -> Game:
    base = dict(
        game_id=make_game_id("nfl", 2026, 1, "sea", "ne"),
        sport="nfl",
        season=2026,
        week=1,
        kickoff_utc=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
        kickoff_local=datetime(2026, 9, 13, 13, 0, tzinfo=ET),
        tz="America/New_York",
        home_id="ne",
        away_id="sea",
        stadium_id="gillette-stadium",
    )
    base.update(kw)
    return Game(**base)


def test_keys() -> None:
    assert make_game_id("cfb", 2026, 3, "miami-fl", "texas-am") == "cfb:2026:3:miami-fl@texas-am"
    assert odds_key("g", "total", "under", "pinnacle") == "g|total|under|pinnacle"


def test_game_frozen_and_validated() -> None:
    g = _game()
    assert g.month == 9
    assert g.neutral is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.week = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        _game(sport="xfl")
    with pytest.raises(ValueError):
        _game(roof_state="tent")
    assert "kickoff_utc" in g.to_dict()


def test_stadium_defaults_and_coords() -> None:
    s = Stadium(stadium_id="gillette-stadium", name="Gillette Stadium", lat=42.09, lon=-71.26, roof_type="open")
    assert s.aliases == [] and s.avg_wind_by_month == {}
    assert s.is_dome is False
    with pytest.raises(ValueError):
        Stadium(stadium_id="x", name="x", lat=95.0, lon=0.0)


def test_team() -> None:
    t = Team(team_id="ne", sport="nfl", name="New England Patriots")
    assert t.short is None
    with pytest.raises(ValueError):
        Team(team_id="x", sport="mlb", name="x")


def test_gameline_rules() -> None:
    now = datetime.now(timezone.utc)
    gl = GameLine(sport="nfl", game_id="g", book="pinnacle", market="spread", side="home", odds=-110, line=-3.5, scraped_at=now)
    assert gl.key == "g|spread|home|pinnacle"
    assert gl.is_main is True
    GameLine(sport="cfb", game_id="g", book="kalshi", market="ml", side="away", odds=120, prob_raw=0.45)
    with pytest.raises(ValueError):
        GameLine(sport="nfl", game_id="g", book="b", market="ml", side="home", odds=-110, line=3.0)
    with pytest.raises(ValueError):
        GameLine(sport="nfl", game_id="g", book="b", market="total", side="over", odds=-110)
    with pytest.raises(ValueError):
        GameLine(sport="nfl", game_id="g", book="b", market="total", side="up", odds=-110, line=44.5)
    with pytest.raises(ValueError):
        GameLine(sport="nfl", game_id="g", book="b", market="ml", side="home", odds=-110, prob_raw=1.2)


def test_edge_and_degradation() -> None:
    e = Edge("g", "pinnacle", "total", "under", 44.5, -110, 42.0, 0.56, 0.5, 2.5, 0.06, 0.8, "edge")
    assert e.model_version == "v1"
    with pytest.raises(ValueError):
        Edge("g", "b", "total", "under", 44.5, -110, 42.0, 0.56, 0.5, 2.5, 0.06, 1.5, "edge")
    with pytest.raises(ValueError):
        Edge("g", "b", "total", "under", 44.5, -110, 42.0, 0.56, 0.5, 2.5, 0.06, 0.5, "huge")
    d = Degradation("weather.ensemble", "missing")
    assert d.severity == "warn"
    with pytest.raises(ValueError):
        Degradation("x", "y", severity="fatal")


def test_weather_forecast() -> None:
    p = WeatherPoint(t=datetime(2026, 9, 13, 17, tzinfo=timezone.utc), wind=12.0)
    f = WeatherForecast(game_id="g", source="hrrr", wind_fg=12.0, hourly=[p])
    assert f.hourly[0].wind == 12.0
    assert f.to_dict()["hourly"][0]["wind"] == 12.0
    with pytest.raises(ValueError):
        WeatherForecast(game_id="g", source="x", roof_state="leaky")


def test_run_meta_and_context() -> None:
    ctx = RunContext(sport="nfl", scope="weather", started_at=datetime(2026, 8, 23, 14, 15, tzinfo=timezone.utc))
    assert ctx.run_id == "20260823T141500Z-nfl"
    with ctx.stage("schedule"):
        pass
    assert "schedule" in ctx.stage_timings
    ctx.degrade("weather.nws", "timeout", "info")
    ctx.count("pinnacle", "total", 3)
    ctx.count("pinnacle", "total", 2)
    assert ctx.counts == {"pinnacle": {"total": 5}}
    meta = RunMeta(run_id=ctx.run_id, sport="nfl", stage_timings=ctx.stage_timings, degradations=list(ctx.degradations))
    assert meta.degradations[0].run_id == ctx.run_id
    assert any("DEGRADED" in line for line in ctx.summary_lines())


def test_timeutil_labels() -> None:
    dt = datetime(2025, 11, 9, 13, 0, tzinfo=ET)
    assert date_label(dt) == "SUN 11/09"
    assert time_label(dt) == "01:00 PM"
    assert time_label(datetime(2025, 11, 9, 0, 5)) == "12:05 AM"
    assert time_label(datetime(2025, 11, 9, 12, 30)) == "12:30 PM"
    assert naive_et_iso(datetime(2026, 4, 15, 14, 0, 55, 123, tzinfo=timezone.utc)) == "2026-04-15T10:00:55"
    assert utc_iso(datetime(2026, 4, 15, 14, 0, 55, tzinfo=timezone.utc)) == "2026-04-15T14:00:55Z"
    assert run_id_for(datetime(2026, 4, 15, 14, 0, 55, tzinfo=timezone.utc)) == "20260415T140055Z"
    parsed = parse_iso("2026-04-15T14:00:55Z")
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert to_et(parsed).hour == 10
    assert parse_iso("2026-04-15T10:00:55", default_tz="America/New_York").utcoffset() == timedelta(hours=-4)
