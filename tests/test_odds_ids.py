"""Historical provisional identity encodings share one reversible codec."""
from datetime import datetime, timezone

import pytest

from pipeline.odds.ids import parse_provisional_id, parse_stamp, provisional_game_id


@pytest.mark.parametrize("precision,stamp", [
    ("compact", "20260905"), ("day", "2026-09-05"), ("minute", "2026-09-05T19:30"),
])
@pytest.mark.parametrize("style,away", [
    ("raw", "A&M St.'s"), ("hyphen", "a-m-st-s"), ("and", "aandm-st-s"),
])
def test_identity_codec(precision, stamp, style, away):
    kick = datetime(2026, 9, 5, 19, 30, tzinfo=timezone.utc)
    gid = provisional_game_id("cfb", "A&M St.'s", "Home", kick, precision=precision, slug=style)
    parsed = parse_provisional_id(gid)
    assert (parsed.sport, parsed.stamp, parsed.away) == ("cfb", stamp, away)
    decoded, date_only = parse_stamp(parsed.stamp)
    assert date_only == (precision != "minute")
    assert provisional_game_id(parsed.sport, parsed.away, parsed.home, decoded, precision=precision) == gid


def test_unknown_stamp_and_pipe_sanitizing():
    gid = provisional_game_id("nfl", "Away|Club", "Home")
    parsed = parse_provisional_id(gid)
    assert parsed.stamp == "unknown" and parsed.away == "Away Club"
    assert parse_stamp(parsed.stamp) == (None, False)
    assert provisional_game_id(parsed.sport, parsed.away, parsed.home) == gid
    assert parse_provisional_id("nfl:not-a-provisional-id") is None
