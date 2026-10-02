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


def catalog(sport, generated=NOW):
    return {"version": 1, "feed_ok": True, "generated_at": generated.isoformat(),
            "leagues": [{"id": betcris.PUBLIC_LEAGUES[sport],
                         "name": "NFL" if sport == "nfl" else "COLLEGE FOOTBALL", "sport": "FOOTBALL",
                         "path": f"league/{betcris.PUBLIC_LEAGUES[sport]}.json",
                         "hash": "0123456789abcdef", "stale_after_seconds": 750 if sport == "cfb" else 9000}]}


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_global_catalog_cannot_confirm_unobserved_stale_league_payload(sport):
    data = payload(sport)
    old_payload = NOW - timedelta(hours=2)
    data["feed_fetched_at"] = old_payload.isoformat()
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, sport, now=NOW)
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, sport, now=NOW, catalog=catalog(sport))


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("failure", ["down", "future", "stale", "missing_time", "unlisted",
                                    "duplicate", "wrong_path", "bad_hash", "zero_ttl"])
def test_catalog_cannot_renew_quotes_when_confirmation_is_invalid(sport, failure):
    data, index = payload(sport), catalog(sport)
    if failure == "down":
        index["feed_ok"] = False
    elif failure == "future":
        index["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif failure == "stale":
        index["generated_at"] = (NOW - timedelta(seconds=3601 if sport == "nfl" else 751)).isoformat()
    elif failure == "missing_time":
        index.pop("generated_at")
    elif failure == "unlisted":
        index["leagues"] = []
    elif failure == "duplicate":
        index["leagues"] *= 2
    elif failure == "wrong_path":
        index["leagues"][0]["path"] = "https://other-book.invalid/feed.json"
    elif failure == "bad_hash":
        index["leagues"][0]["hash"] = "invalid"
    else:
        index["leagues"][0]["stale_after_seconds"] = 0
    with pytest.raises(ValueError):
        betcris.parse_public(data, sport, now=NOW, catalog=index)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("target,field,value", [
    ("catalog", "name", "OTHER LEAGUE"), ("catalog", "sport", "SOCCER"),
    ("payload", "id", 999), ("payload", "name", "OTHER LEAGUE"),
    ("payload", "sport", "SOCCER"),
])
def test_catalog_and_payload_identities_must_match(sport, target, field, value):
    data, index = payload(sport), catalog(sport)
    (index["leagues"][0] if target == "catalog" else data["league"])[field] = value
    with pytest.raises(ValueError):
        betcris.parse_public(data, sport, now=NOW, catalog=index)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-02T20:30:00Z"])
def test_fresh_catalog_does_not_replace_missing_invalid_or_future_league_time(sport, timestamp):
    data = payload(sport)
    data["feed_fetched_at"] = timestamp
    with pytest.raises(ValueError):
        betcris.parse_public(data, sport, now=NOW, catalog=catalog(sport))


def test_provider_expiry_and_original_payload_time_survive_archive_and_json(tmp_path, monkeypatch):
    from pipeline import state
    from pipeline.outputs.json_out import odds_block
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    data = payload("cfb")
    raw = data["games"][0]
    data["games"] = [raw]
    kick = datetime.fromisoformat(raw["starts_at"].replace("Z", "+00:00"))
    home = merge.candidate_team_ids("cfb", raw["home"])[0]
    away = merge.candidate_team_ids("cfb", raw["visitor"])[0]
    game = Game(game_id=f"cfb:2026:5:{away}@{home}", sport="cfb", season=2026, week=5,
                kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id=home, away_id=away, stadium_id=None)
    rows = merge.merge_odds("cfb", [game], betcris.parse_public(data, "cfb", now=NOW, catalog=catalog("cfb")),
                            now=NOW, save=False).lines
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: NOW))

    async def scrape(*args):
        return {"betcris": rows}, {}

    monkeypatch.setattr(build, "scrape_books", scrape)
    build.stage_odds(RunContext(sport="cfb", git_sha="test"), "cfb", [game], None,
                     NullRawStore("cfb", "catalog"), ["betcris"], tmp_path, 2026)
    archive = state.load_archive_last(tmp_path)
    observed = rows[0].scraped_at
    expiry = observed + timedelta(seconds=750)
    carried = build.carry_forward_lines(archive, "cfb", {game.game_id}, ["betonline"], now=expiry)
    assert len(carried) == 6 and {r.expires_at for r in carried} == {expiry}
    assert {r.source_updated_at for r in carried} == {observed}
    assert build.carry_forward_lines(archive, "cfb", {game.game_id}, ["betonline"], now=expiry + timedelta(seconds=1)) == []
    values = odds_block(game.game_id, carried, {})["betcris"]
    assert all(x["updated_at"] == observed and x["expires_at"] == expiry
               and x["source_updated_at"] == carried[0].source_updated_at for x in values.values())


def test_catalog_lineage_survives_neutral_home_away_flip():
    data = payload("nfl")
    raw = data["games"][0]
    data["games"] = [raw]
    kick = datetime.fromisoformat(raw["starts_at"].replace("Z", "+00:00"))
    source_home = merge.candidate_team_ids("nfl", raw["home"])[0]
    source_away = merge.candidate_team_ids("nfl", raw["visitor"])[0]
    game = Game(game_id=f"nfl:2026:4:{source_home}@{source_away}", sport="nfl", season=2026, week=4,
                kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id=source_away,
                away_id=source_home, stadium_id=None, neutral=True)
    rows = betcris.parse_public(data, "nfl", now=NOW, catalog=catalog("nfl"))
    result = merge.merge_odds("nfl", [game], rows, now=NOW, save=False)
    assert len(result.lines) == 6 and not result.unmatched
    assert {r.source_updated_at for r in result.lines} == {rows[0].source_updated_at}
    assert {r.expires_at for r in result.lines} == {rows[0].scraped_at + timedelta(hours=1)}
    assert next(r for r in result.lines if r.market == "ml" and r.side == "home").odds == raw["markets"]["moneyline"]["visitor"]["price"]


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


def test_final_job_replaces_expired_college_capture_with_new_prices(tmp_path, monkeypatch):
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    data = payload("cfb")
    raw_game = data["games"][0]
    kick = datetime.fromisoformat(raw_game["starts_at"].replace("Z", "+00:00"))
    home = merge.candidate_team_ids("cfb", raw_game["home"])[0]
    away = merge.candidate_team_ids("cfb", raw_game["visitor"])[0]
    game = Game(game_id=f"cfb:2026:5:{away}@{home}", sport="cfb", season=2026, week=5,
                kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id=home,
                away_id=away, stadium_id=None)
    data["games"] = [raw_game]
    first = merge.merge_odds("cfb", [game], betcris.parse_public(data, "cfb", now=NOW),
                             now=NOW, save=False).lines
    later = NOW + timedelta(minutes=35)
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, "cfb", now=later)
    fresh = copy.deepcopy(data)
    fresh["feed_fetched_at"] = (later - timedelta(minutes=1)).isoformat()
    fresh["games"][0]["markets"]["total"]["under"]["price"] = -125
    second = merge.merge_odds("cfb", [game], betcris.parse_public(fresh, "cfb", now=later),
                              now=later, save=False).lines
    clock = [NOW]
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: clock[0]))
    calls = []

    async def scrape(sport, books, raw, run_id, degrade):
        calls.append(books)
        return ({"betcris": first} if len(calls) == 1 else
                {"betonline": [], "betcris": second}), {}

    monkeypatch.setattr(build, "scrape_books", scrape)
    build.stage_odds(RunContext(sport="cfb", git_sha="test"), "cfb", [game], None,
                     NullRawStore("cfb", "first"), ["betcris"], tmp_path, 2026)
    clock[0] = later
    result = build.stage_odds(RunContext(sport="cfb", git_sha="test"), "cfb", [game], None,
                              NullRawStore("cfb", "final"), ["betonline", "betcris"], tmp_path, 2026)
    actual = [r for r in result.lines if r.book == "betcris"]
    assert len(actual) == 6 and calls[-1] == ["betonline", "betcris"]
    assert {r.scraped_at for r in actual} == {later - timedelta(minutes=1)}
    assert next(r for r in actual if r.market == "total" and r.side == "under").odds == -125
