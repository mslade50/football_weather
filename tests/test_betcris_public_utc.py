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
def test_global_generation_cannot_confirm_unobserved_stale_league_payload(sport):
    data = payload(sport)
    data["feed_fetched_at"] = (NOW - timedelta(hours=2)).isoformat()
    data["generated_at"] = NOW.isoformat()
    data["last_good_at"] = NOW.isoformat()
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, sport, now=NOW)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("field,value", [
    ("id", 999), ("id", True), ("id", "1"), ("name", "OTHER LEAGUE"),
    ("name", None), ("sport", "SOCCER"), ("sport", None),
])
def test_payload_must_identify_expected_full_game_football_league(sport, field, value):
    data = payload(sport)
    data["league"][field] = value
    with pytest.raises(ValueError, match="full-game league"):
        betcris.parse_public(data, sport, now=NOW)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-02T20:30:00Z"])
def test_generated_time_does_not_replace_missing_invalid_or_future_league_time(sport, timestamp):
    data = payload(sport)
    data["feed_fetched_at"] = timestamp
    data["generated_at"] = NOW.isoformat()
    with pytest.raises(ValueError):
        betcris.parse_public(data, sport, now=NOW)


@pytest.mark.parametrize("version", [True, "1", None, 2])
def test_public_schema_version_must_be_supported_integer(version):
    data = payload("nfl")
    data["version"] = version
    with pytest.raises(ValueError, match="schema version"):
        betcris.parse_public(data, "nfl", now=NOW)


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
    rows = merge.merge_odds("cfb", [game], betcris.parse_public(data, "cfb", now=NOW),
                            now=NOW, save=False).lines
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: NOW))

    async def scrape(*args):
        return {"betcris": rows}, {}

    monkeypatch.setattr(build, "scrape_books", scrape)
    build.stage_odds(RunContext(sport="cfb", git_sha="test"), "cfb", [game], None,
                     NullRawStore("cfb", "expiry"), ["betcris"], tmp_path, 2026)
    archive = state.load_archive_last(tmp_path)
    observed = rows[0].scraped_at
    expiry = observed + timedelta(seconds=1800)
    carried = build.carry_forward_lines(archive, "cfb", {game.game_id}, ["betonline"], now=expiry)
    assert len(carried) == 6 and {r.expires_at for r in carried} == {expiry}
    assert {r.source_updated_at for r in carried} == {observed}
    assert build.carry_forward_lines(archive, "cfb", {game.game_id}, ["betonline"], now=expiry + timedelta(seconds=1)) == []
    values = odds_block(game.game_id, carried, {})["betcris"]
    assert all(x["updated_at"] == observed and x["expires_at"] == expiry
               and x["source_updated_at"] == carried[0].source_updated_at
               and x["actionable_until"] == observed + timedelta(seconds=750) for x in values.values())
    assert all(r.reference_only(expiry) for r in carried)


@pytest.mark.parametrize("expiry", [None, "invalid", "2026-10-02T20:28:59Z"])
def test_archive_with_invalid_or_expired_provider_expiry_is_not_carried(expiry):
    game_id = "cfb:2026:5:away@home"
    archive = {"last": {f"{game_id}|ml|home|betcris": {
        "odds": -110, "line": None, "ts": NOW.isoformat(), "available": True,
        "expires_at": expiry,
    }}}
    assert build.carry_forward_lines(archive, "cfb", {game_id}, ["betonline"], now=NOW) == []


def test_source_expiry_survives_neutral_home_away_flip():
    data = payload("nfl")
    raw = data["games"][0]
    data["games"] = [raw]
    kick = datetime.fromisoformat(raw["starts_at"].replace("Z", "+00:00"))
    source_home = merge.candidate_team_ids("nfl", raw["home"])[0]
    source_away = merge.candidate_team_ids("nfl", raw["visitor"])[0]
    game = Game(game_id=f"nfl:2026:4:{source_home}@{source_away}", sport="nfl", season=2026, week=4,
                kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id=source_away,
                away_id=source_home, stadium_id=None, neutral=True)
    rows = betcris.parse_public(data, "nfl", now=NOW)
    result = merge.merge_odds("nfl", [game], rows, now=NOW, save=False)
    assert len(result.lines) == 6 and not result.unmatched
    assert {r.source_updated_at for r in result.lines} == {rows[0].source_updated_at}
    assert {r.expires_at for r in result.lines} == {rows[0].scraped_at + timedelta(seconds=betcris.NFL_CARRY_MAX_AGE_S)}
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
    ("nfl", 300, 300, True), ("nfl", 300, 301, False),
    ("cfb", 750, 750, True), ("cfb", 750, 1800, True), ("cfb", 750, 1801, False),
    ("nfl", 9000, 0, True), ("nfl", 9000, -1, False),
])
def test_freshness_ceiling_is_thirty_minutes(sport, ttl, age, accepted):
    """CFB stays at 30 minutes. NFL is min(provider TTL, the one-hour carry window)."""
    assert build.ODDS_CARRY_MAX_AGE == timedelta(seconds=betcris.NFL_CARRY_MAX_AGE_S)
    data = payload(sport)
    observed = NOW - timedelta(seconds=age)
    data.update(stale_after_seconds=ttl, feed_fetched_at=observed.isoformat())
    if accepted:
        rows = betcris.parse_public(data, sport, now=NOW)
        assert len(rows) == 12
        max_age = min(ttl, betcris.NFL_CARRY_MAX_AGE_S) if sport == "nfl" else betcris.DISPLAY_MAX_AGE_S
        assert {r.expires_at for r in rows} == {observed + timedelta(seconds=max_age)}
    else:
        with pytest.raises(ValueError, match="stale or invalid observation"):
            betcris.parse_public(data, sport, now=NOW)


def test_forty_five_minute_nfl_feed_is_accepted_and_cfb_feed_is_rejected():
    """Hourly NFL file at :01 is 30-60 minutes old. CFB still drops at 30 minutes."""
    age = timedelta(minutes=45)
    observed = NOW - age
    nfl = payload("nfl")
    nfl.update(stale_after_seconds=9000, feed_fetched_at=observed.isoformat())
    rows = betcris.parse_public(nfl, "nfl", now=NOW)
    assert len(rows) == 12
    assert {r.expires_at for r in rows} == {observed + timedelta(seconds=betcris.NFL_CARRY_MAX_AGE_S)}
    assert all(r.actionable_until == observed + timedelta(seconds=9000) for r in rows)
    cfb = payload("cfb")
    cfb.update(stale_after_seconds=9000, feed_fetched_at=observed.isoformat())
    with pytest.raises(
        betcris.StaleObservationError,
        match=f"age_seconds={int(age.total_seconds())}, effective_ttl_seconds={betcris.DISPLAY_MAX_AGE_S}, provider_ttl_seconds=9000",
    ):
        betcris.parse_public(cfb, "cfb", now=NOW)


def test_production_nfl_drop_was_age_gate_not_schedule_mapping():
    data = payload("nfl")
    # Exact observation/build times from production run 37043620309.
    data["feed_fetched_at"] = "2026-10-02T16:46:02Z"
    with pytest.raises(ValueError, match="stale"):
        betcris.parse_public(data, "nfl", now=datetime(2026, 10, 2, 17, 53, 39, tzinfo=timezone.utc))


def test_october_fifth_cfb_drop_reports_the_exact_deadline_without_using_catalog_time():
    data = payload("cfb")
    data.update(feed_fetched_at="2026-10-05T21:01:02Z", stale_after_seconds=9000,
                generated_at="2026-10-05T22:03:56Z", last_good_at="2026-10-05T22:03:56Z")
    for game in data["games"]:
        game["starts_at"] = "2026-10-10T19:30:00Z"
    # 28 minutes old is inside the 30-minute board window. Catalog clocks do not extend it.
    first = betcris.parse_public(data, "cfb", now=datetime(2026, 10, 5, 21, 29, 22, tzinfo=timezone.utc))
    assert len(first) == 12
    assert all(r.expires_at == datetime(2026, 10, 5, 21, 31, 2, tzinfo=timezone.utc) for r in first)
    with pytest.raises(betcris.StaleObservationError, match="age_seconds=1801, effective_ttl_seconds=1800, provider_ttl_seconds=9000"):
        betcris.parse_public(data, "cfb", now=datetime(2026, 10, 5, 21, 31, 3, tzinfo=timezone.utc))


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


def test_price_past_provider_ttl_is_reference_only_and_cannot_price_or_alert():
    from pipeline import alerts as A
    from pipeline.contracts import actionable_lines
    from pipeline.model import fair as fair_mod
    from tests.test_alerts_rules import CFG
    from tests.test_alerts_rules import card as alert_card

    data = payload("cfb")
    stale_at = NOW - timedelta(seconds=1200)
    data.update(feed_fetched_at=stale_at.isoformat(), stale_after_seconds=750)
    for game in data["games"]:
        game["starts_at"] = (NOW + timedelta(days=2)).isoformat()
    stale = betcris.parse_public(data, "cfb", now=NOW)
    assert stale and all(r.reference_only(NOW) and r.expires_at == stale_at + timedelta(seconds=1800) for r in stale)
    data["feed_fetched_at"] = (NOW - timedelta(seconds=100)).isoformat()
    fresh = betcris.parse_public(data, "cfb", now=NOW)
    assert fresh and all(not r.reference_only(NOW) for r in fresh)

    from pipeline.contracts import GameLine
    gid = "cfb:2026:5:away@home"
    exchange = [GameLine(sport="cfb", game_id=gid, book="kalshi", market="total", side=side, odds=-110,
                         line=45.0, prob_raw=0.52 if side == "over" else 0.48, scraped_at=NOW,
                         expires_at=NOW + timedelta(minutes=5))
                for side in ("over", "under")]
    reference = [GameLine(sport="cfb", game_id=gid, book="betcris", market="total", side=side, odds=-110,
                          line=60.0, scraped_at=stale_at, source_updated_at=stale_at,
                          expires_at=stale_at + timedelta(seconds=1800),
                          actionable_until=stale_at + timedelta(seconds=750))
                 for side in ("over", "under")]
    assert actionable_lines(reference, NOW) == []
    alone = fair_mod.evaluate_game("cfb", gid, reference, -8.0, 0.0, now=NOW)
    assert alone.edges == [] and alone.total.line is None
    mixed = fair_mod.evaluate_game("cfb", gid, exchange + reference, -8.0, 0.0, now=NOW)
    assert mixed.total.line == 45.0 and {e.book for e in mixed.edges} == {"kalshi"}
    priced = fair_mod.consensus("cfb", exchange + reference, "total", now=NOW)
    assert priced.line == 45.0 and "betcris" not in priced.books

    played = alert_card(edges=[{
        "game_id": gid, "book": "betcris", "market": "total", "side": "under", "line": 60.0, "odds": -110,
        "fair_line": 45.0, "edge_pts": 15.0, "edge_prob": 0.2, "tier": "strong", "model_version": "v1",
    }], signal="Mid Impact", sport="cfb", game_id=gid, kickoff=NOW + timedelta(days=2))
    played["odds"]["betcris"] = {"total": {
        "line": 60.0, "under": -110, "updated_at": stale_at.isoformat(),
        "source_updated_at": stale_at.isoformat(), "expires_at": (stale_at + timedelta(seconds=1800)).isoformat(),
        "actionable_until": (stale_at + timedelta(seconds=750)).isoformat(),
    }}
    signal = A.edge_candidates(played, {}, CFG, now=NOW)[0]
    assert signal.record["last_book"] != "betcris" and signal.record["last_line"] is None
    assert "Betcris" not in signal.text and "60.0" not in signal.text
    assert "no posted price available" in signal.text


def test_betcris_inside_provider_timer_does_not_price_consensus_edges_or_spread_average():
    from pipeline.contracts import GameLine
    from pipeline.model import fair as fair_mod

    gid = "cfb:2026:5:away@home"
    until = NOW + timedelta(minutes=20)
    expires = NOW + timedelta(minutes=30)

    def pair(book, market, line, **kw):
        sides = ("home", "away") if market == "spread" else ("over", "under")
        rows = []
        for side in sides:
            posted = -line if market == "spread" and side == "away" else line
            rows.append(GameLine(sport="cfb", game_id=gid, book=book, market=market, side=side, odds=-110,
                                 line=posted, scraped_at=NOW, source_updated_at=NOW, expires_at=expires,
                                 actionable_until=until, **kw))
        return rows

    cris = pair("betcris", "total", 60.0) + pair("betcris", "spread", -1.0)
    assert cris and all(not row.reference_only(NOW) for row in cris)
    others = (pair("pinnacle", "total", 44.5) + pair("betonline", "total", 46.0)
              + pair("pinnacle", "spread", -3.0) + pair("betonline", "spread", -2.5)
              + pair("kalshi", "total", 47.0))
    mixed = others + cris
    total = fair_mod.consensus("cfb", mixed, "total", now=NOW)
    assert total.line == 45.25 and "betcris" not in total.books and total.n_books == 3
    spread = fair_mod.consensus("cfb", mixed, "spread")
    assert spread.line == -2.75 and spread.src == "bol+pin" and "betcris" not in spread.books and spread.n_books == 2
    priced = fair_mod.evaluate_game("cfb", gid, mixed, -8.0, 0.0, now=NOW)
    assert priced.total.line == 45.25 and priced.spread.line == -2.75
    assert "betcris" not in {edge.book for edge in priced.edges}
    untimed = fair_mod.evaluate_game("cfb", gid, mixed, -8.0, 0.0)
    assert "betcris" not in {edge.book for edge in untimed.edges}
