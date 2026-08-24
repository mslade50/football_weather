"""nflverse games.csv -> Game (fixture: tests/fixtures/raw/nflverse/games_sample.csv)."""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest

from pipeline.schedule.nfl import POST_WEEK, parse_nflverse_games
from pipeline.stadiums.loader import load_stadium_book

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "raw" / "nflverse" / "games_sample.csv"


@pytest.fixture(scope="module")
def payload() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def book():
    return load_stadium_book(ROOT / "data")


def test_parses_only_requested_season_and_skips_finals(payload: str) -> None:
    games = parse_nflverse_games(payload, 2026)
    ids = {g.game_id for g in games}
    assert "nfl:2026:1:ne@sea" in ids
    assert all(g.season == 2026 for g in games)
    assert len(games) == 7
    finals = parse_nflverse_games(payload, 2025, include_final=True)
    assert {g.status for g in finals} == {"final"}
    assert parse_nflverse_games(payload, 2025) == []


def test_game_fields_without_book(payload: str) -> None:
    g = next(x for x in parse_nflverse_games(payload, 2026) if x.game_id == "nfl:2026:1:ne@sea")
    assert g.sport == "nfl" and g.week == 1
    assert g.home_id == "sea" and g.away_id == "ne"
    assert g.stadium_id == "SEA00"  # raw nflverse id when no book supplied
    assert g.kickoff_utc.tzinfo is not None
    assert g.kickoff_utc.astimezone(timezone.utc).isoformat() == "2026-09-10T00:20:00+00:00"  # 20:20 ET
    assert g.tz == "America/New_York" and g.kickoff_local.hour == 20
    assert g.roof_state == "outdoors" and g.neutral is False
    assert g.source == "nflverse:2026_01_NE_SEA"


def test_book_maps_stadium_and_local_time(payload: str, book) -> None:
    games = {g.game_id: g for g in parse_nflverse_games(payload, 2026, book=book)}
    sea = games["nfl:2026:1:ne@sea"]
    assert sea.stadium_id == "lumen-field" and sea.tz == "America/Los_Angeles"
    assert sea.kickoff_local.hour == 17 and sea.kickoff_local.minute == 20
    mel = games["nfl:2026:1:sf@la"]
    assert mel.neutral is True and mel.stadium_id == "melbourne-cricket-ground"
    assert mel.tz == "Australia/Melbourne" and mel.kickoff_local.day == 11  # Friday local
    mex = games["nfl:2026:11:min@sf"]
    assert mex.neutral is True and mex.stadium_id == "estadio-banorte"


def test_roof_and_tbd(payload: str) -> None:
    games = {g.game_id: g for g in parse_nflverse_games(payload, 2026)}
    assert games["nfl:2026:1:no@det"].roof_state == "dome"
    dal = games["nfl:2026:2:was@dal"]
    assert dal.roof_state is None and dal.status == "tbd"
    assert dal.kickoff_local.hour == 13  # default placeholder time


def test_postseason_weeks(payload: str) -> None:
    games = {g.game_id: g for g in parse_nflverse_games(payload, 2025, include_final=True)}
    assert "nfl:2025:19:la@car" in games
    sb = games["nfl:2025:22:sea@ne"]
    assert sb.week == POST_WEEK["SB"] and sb.neutral is True


def test_week_filter(payload: str) -> None:
    games = parse_nflverse_games(payload, 2026, weeks=[11])
    assert [g.game_id for g in games] == ["nfl:2026:11:min@sf"]
