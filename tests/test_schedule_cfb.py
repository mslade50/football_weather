"""CFBD /games and ESPN scoreboard -> Game (fixtures under tests/fixtures/raw/{cfbd,espn})."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.run_context import RunContext
from pipeline.schedule import cfb as cfb_mod
from pipeline.schedule.cfb import cfb_week, current_week, fetch_cfb_schedule, parse_cfbd_games
from pipeline.schedule.espn import parse_espn_scoreboard
from pipeline.stadiums.loader import load_stadium_book

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "tests" / "fixtures" / "raw"


@pytest.fixture(scope="module")
def cfbd_payload() -> list:
    return json.loads((RAW / "cfbd" / "games_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def espn_payload() -> dict:
    return json.loads((RAW / "espn" / "scoreboard_cfb_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def book():
    return load_stadium_book(ROOT / "data")


def test_parse_cfbd_without_book(cfbd_payload: list) -> None:
    games = parse_cfbd_games(cfbd_payload, 2025)
    ids = [g.game_id for g in games]
    assert ids == [
        "cfb:2025:1:texas-am@miami-fl",
        "cfb:2025:1:navy@air-force",
        "cfb:2025:2:florida-international@ole-miss",
        "cfb:2025:16:connecticut@michigan-state",
    ]  # completed Kyle Field game dropped
    g = games[0]
    assert g.sport == "cfb" and g.neutral is True
    assert g.stadium_id == "3803"  # raw CFBD venueId without a book
    assert g.kickoff_utc == datetime(2025, 8, 30, 19, 30, tzinfo=timezone.utc)
    assert g.tz == "America/New_York" and g.kickoff_local.hour == 15
    assert g.source == "cfbd:401752651"
    assert games[2].status == "tbd"
    assert parse_cfbd_games(cfbd_payload, 2025, include_final=True)[-1].status == "final"


def test_parse_cfbd_with_book_resolves_venue_and_tz(cfbd_payload: list, book) -> None:
    games = {g.game_id: g for g in parse_cfbd_games(cfbd_payload, 2025, book=book)}
    neutral = games["cfb:2025:1:texas-am@miami-fl"]
    assert neutral.stadium_id == "mercedes-benz-stadium"  # by venue name (not a CFB home venue)
    afa = games["cfb:2025:1:navy@air-force"]
    assert afa.stadium_id == "falcon-stadium" and afa.tz == "America/Denver"
    assert afa.kickoff_local.hour == 17
    assert games["cfb:2025:2:florida-international@ole-miss"].stadium_id == "vaught-hemingway-stadium"
    msu = games["cfb:2025:16:connecticut@michigan-state"]
    assert msu.stadium_id == "spartan-stadium" and msu.week == 16
    for g in games.values():
        rg = book.resolve(g)
        assert rg.stadium is not None, g.game_id
        assert rg.home_team is not None and rg.away_team is not None, g.game_id


def test_cfb_week_offsets() -> None:
    assert cfb_week(5, "regular") == 5
    assert cfb_week(1, "postseason") == 16
    assert cfb_week(None, None) == 0


def test_current_week_from_calendar() -> None:
    cal = [
        {"week": 1, "seasonType": "regular", "firstGameStart": "2025-08-23T00:00:00.000Z", "lastGameStart": "2025-09-02T03:00:00.000Z"},
        {"week": 2, "seasonType": "regular", "firstGameStart": "2025-09-02T03:00:00.000Z", "lastGameStart": "2025-09-07T03:00:00.000Z"},
    ]
    assert current_week(cal, datetime(2025, 8, 30, tzinfo=timezone.utc))["week"] == 1
    assert current_week(cal, datetime(2025, 9, 4, tzinfo=timezone.utc))["week"] == 2
    assert current_week(cal, datetime(2025, 12, 4, tzinfo=timezone.utc)) is None


def test_parse_espn_scoreboard(espn_payload: dict, book) -> None:
    games = parse_espn_scoreboard(espn_payload, "cfb", season=2025, book=book)
    assert [g.game_id for g in games] == ["cfb:2025:1:texas-am@miami-fl", "cfb:2025:1:navy@air-force"]
    assert games[0].neutral is True and games[0].stadium_id == "mercedes-benz-stadium"
    assert games[1].stadium_id == "falcon-stadium" and games[1].tz == "America/Denver"
    assert games[0].source == "espn:401752651"
    raw = parse_espn_scoreboard(espn_payload, "cfb", season=2025)
    assert raw[0].home_id == "miami" and raw[0].stadium_id == "5348"


def test_fetch_falls_back_to_espn_without_key(monkeypatch, espn_payload: dict, book) -> None:
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    calls = []

    def fake_espn(sport, season, week=None, season_type=2, raw_dir=None, timeout=30.0):
        calls.append((sport, season, week, season_type))
        return espn_payload if season_type == 2 else {"events": []}

    monkeypatch.setattr(cfb_mod, "fetch_espn_scoreboard", fake_espn)
    ctx = RunContext(sport="cfb", git_sha="test")
    games = fetch_cfb_schedule(2025, week=1, book=book, ctx=ctx)
    assert len(games) == 2
    assert calls == [("cfb", 2025, 1, 2), ("cfb", 2025, 1, 3)]
    assert any("CFBD_API_KEY" in d.reason for d in ctx.degradations)


def test_fetch_uses_cfbd_when_key_present(monkeypatch, cfbd_payload: list, book) -> None:
    monkeypatch.setattr(cfb_mod, "fetch_cfbd_games", lambda *a, **k: cfbd_payload)
    monkeypatch.setattr(cfb_mod, "fetch_espn_scoreboard", lambda *a, **k: pytest.fail("ESPN must not be called"))
    ctx = RunContext(sport="cfb", git_sha="test")
    games = fetch_cfb_schedule(2025, book=book, api_key="k", ctx=ctx)
    assert len(games) == 4 and not ctx.degradations
