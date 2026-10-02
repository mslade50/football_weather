"""October JSON UTC contract and source-to-second-publish freshness regression."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import build
from pipeline.contracts import Game
from pipeline.odds import merge
from pipeline.odds.parsers import betcris

FIX = Path(__file__).parent / "fixtures/raw/betcris"
NOW = datetime(2026, 10, 2, 20, 29, tzinfo=timezone.utc)


def payload(sport):
    return json.loads((FIX / f"{sport}_public_utc.json").read_text())


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_current_source_utc_matches_schedule_and_survives_second_publish(sport):
    data = payload(sport)
    rows = betcris.parse_public(data, sport, now=NOW)
    # Fixtures retain actual source timestamps, sides, units, and prices.
    assert len(rows) == 12
    games = []
    for raw in data["games"]:
        kick = datetime.fromisoformat(raw["starts_at"].replace("Z", "+00:00"))
        home = merge.candidate_team_ids(sport, raw["home"])[0]
        away = merge.candidate_team_ids(sport, raw["visitor"])[0]
        games.append(Game(game_id=f"{sport}:2026:5:{away}@{home}", sport=sport,
                          season=2026, week=5, kickoff_utc=kick, kickoff_local=kick,
                          tz="UTC", home_id=home, away_id=away, stadium_id=None))
        source_rows = [r for r in rows if r.source_id == f"public:{raw['id']}"]
        assert all(r.game_id.startswith(f"{sport}:raw:{kick:%Y-%m-%dT%H:%M}:")
                   for r in source_rows)
    result = merge.merge_odds(sport, games, rows, now=NOW, save=False)
    assert not result.unmatched and not result.unresolved
    assert len(result.lines) == 12 and len(result.board) == 2
    archive = {"last": {r.key: {"line": r.line, "odds": r.odds,
                               "ts": r.scraped_at.isoformat(), "available": True,
                               "source_id": r.source_id} for r in result.lines}}
    carried = build.carry_forward_lines(archive, sport, {g.game_id for g in games},
                                        ["betonline"], now=NOW + timedelta(minutes=10))
    assert len(carried) == 12
    assert merge.pivot(carried) == result.board
    assert {(r.source_id, r.scraped_at, r.market, r.side, r.line, r.odds) for r in carried} == {
        (r.source_id, r.scraped_at, r.market, r.side, r.line, r.odds) for r in result.lines}


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("stamp,expected", [
    ("2026-03-08T09:30:00Z", "2026-03-08T09:30"),
    ("2026-03-08T03:30:00-07:00", "2026-03-08T10:30"),
    ("2026-11-01T01:30:00-07:00", "2026-11-01T08:30"),
    ("2026-11-01T01:30:00-08:00", "2026-11-01T09:30"),
    ("2026-12-20T23:30:00-08:00", "2026-12-21T07:30"),
    ("2026-10-04T13:30:00Z", "2026-10-04T13:30"),
])
def test_current_aware_timestamps_respect_offsets_dst_and_dates(sport, stamp, expected):
    data = payload(sport)
    data["games"] = [copy.deepcopy(data["games"][0])]
    data["games"][0]["starts_at"] = stamp
    now = datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(minutes=30)
    data["feed_fetched_at"] = now.isoformat()
    rows = betcris.parse_public(data, sport, now=now)
    assert len(rows) == 6
    assert all(r.game_id.startswith(f"{sport}:raw:{expected}:") for r in rows)
    # Keep the feed healthy when testing the independent kickoff cutoff.
    data["feed_fetched_at"] = (now + timedelta(minutes=30)).isoformat()
    assert betcris.parse_public(data, sport, now=now + timedelta(minutes=30)) == []


@pytest.mark.parametrize("sport,ttl,age,accepted", [
    ("nfl", 9000, 3600, True), ("nfl", 9000, 3601, False),
    ("cfb", 750, 750, True), ("cfb", 750, 751, False),
    ("nfl", 9000, 0, True), ("nfl", 9000, -1, False),
])
def test_freshness_never_exceeds_board_ceiling_or_provider_ttl(sport, ttl, age, accepted):
    assert build.ODDS_CARRY_MAX_AGE == timedelta(seconds=3600)
    data = payload(sport)
    data.update(stale_after_seconds=ttl, feed_fetched_at=(NOW - timedelta(seconds=age)).isoformat())
    if accepted:
        assert len(betcris.parse_public(data, sport, now=NOW)) == 12
    else:
        with pytest.raises(ValueError, match="stale or invalid observation"):
            betcris.parse_public(data, sport, now=NOW)


def test_production_nfl_drop_was_age_gate_not_schedule_mapping():
    data = payload("nfl")
    # Exact observation/build times from production run 37043620309.
    data["feed_fetched_at"] = "2026-10-02T16:46:02Z"
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, "nfl", now=datetime(2026, 10, 2, 17, 53, 39, tzinfo=timezone.utc))
