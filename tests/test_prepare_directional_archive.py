from datetime import datetime

from pipeline.stadiums.loader import load_stadium_book
from scripts.prepare_directional_archive import (
    _available_at,
    _closing_quote,
    _compass,
    _coordinate_stadium,
    _timestamp,
)
from utils.timeutil import ET, UTC


def test_timestamp_naive_legacy_values_are_eastern():
    parsed = _timestamp("2024-10-05T10:01:00")
    assert parsed is not None
    assert parsed.tzinfo == ET


def test_forecast_availability_uses_later_git_commit_time():
    snapshot = datetime(2024, 10, 5, 10, tzinfo=ET)
    commit = "2024-10-05T15:05:00+00:00"
    assert _available_at(snapshot, commit) == datetime(2024, 10, 5, 15, 5, tzinfo=UTC)


def test_stale_archived_quote_is_not_mislabeled_as_close():
    kickoff = datetime(2024, 10, 5, 22, tzinfo=UTC)
    available = datetime(2024, 10, 5, 12, tzinfo=UTC)
    assert _closing_quote("cfb", 51.5, 10, None, kickoff, available) == (None, None, None, None)
    assert _closing_quote("cfb", 51.5, 5, None, kickoff, available)[0] == 51.5
    assert _closing_quote("nfl", 51.5, 10, 48.5, kickoff, available)[0] == 48.5


def test_compass_direction_and_venue_name_coordinate_fallback():
    assert _compass("ESE") == 112.5
    book = load_stadium_book()
    stadium = _coordinate_stadium("33.7724449, -84.3928054", book, "Bobby Dodd Stadium")
    assert stadium is not None
    assert stadium.stadium_id == "bobby-dodd-stadium-at-historic-grant-field"
    assert _coordinate_stadium("33.7724449, -84.3928054", book, "Unknown Stadium") is None
