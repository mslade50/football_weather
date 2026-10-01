from dataclasses import replace
from datetime import datetime, timezone

import pytest

from pipeline import state
from pipeline.contracts import GameLine
from pipeline.odds.weekly_openers import opening_window, record_weekly_totals
from tests.test_build_odds import _game


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize("sport,kick,start,end", [
    ("cfb", "2026-10-03T20:00:00Z", "2026-09-27T16:00:00Z", "2026-09-28T04:00:00Z"),
    ("nfl", "2026-10-01T23:00:00Z", "2026-09-27T22:00:00Z", "2026-09-28T16:00:00Z"),
    ("nfl", "2026-10-04T17:00:00Z", "2026-09-27T22:00:00Z", "2026-09-28T16:00:00Z"),
    ("nfl", "2026-10-06T00:00:00Z", "2026-09-27T22:00:00Z", "2026-09-28T16:00:00Z"),
    ("cfb", "2026-09-29T23:00:00Z", "2026-09-27T16:00:00Z", "2026-09-28T04:00:00Z"),
    ("nfl", "2026-11-08T18:00:00Z", "2026-11-01T23:00:00Z", "2026-11-02T17:00:00Z"),
])
def test_weekly_opening_windows_follow_eastern_football_week(sport, kick, start, end):
    game = replace(_game(), sport=sport, kickoff_utc=dt(kick))
    assert tuple(value.astimezone(timezone.utc) for value in opening_window(game)) == (dt(start), dt(end))


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_first_betonline_observation_wins_and_other_books_cannot_replace_it(sport):
    game = replace(_game(), sport=sport, kickoff_utc=dt("2026-10-03T20:00:00Z"))
    key = state.odds_key(game.game_id, "total", "under", "betonline")
    other = state.odds_key(game.game_id, "total", "under", "fanduel")
    openers = {"openers": {key: {"ts": "2026-09-22T12:00:00Z", "line": 48, "odds": -110}}}
    history = {"series": {key: [
        ["2026-09-27T23:00:00Z", 45.5, -105],
        ["2026-09-28T01:00:00Z", 44.5, -115],
    ], other: [["2026-09-27T22:00:00Z", 55, -110]]}}
    record_weekly_totals(openers, history, [], [game], dt("2026-10-01T12:00:00Z"))
    value = openers["weekly_totals"][game.game_id]
    assert (value["line"], value["under"], value["book"], value["ts"]) == (
        45.5, -105, "betonline", "2026-09-27T23:00:00Z")
    assert openers["openers"][key]["line"] == 48  # Retain existing model baselines.
    record_weekly_totals(openers, {}, [], [game], dt("2026-10-02T12:00:00Z"))
    assert openers["weekly_totals"][game.game_id] == value


def test_unchanged_live_scrape_is_captured_without_a_history_change_point():
    game = replace(_game(), kickoff_utc=dt("2026-10-04T17:00:00Z"))
    now = dt("2026-09-28T01:00:00Z")
    quote = GameLine(sport="nfl", game_id=game.game_id, book="betonline", market="total",
                     side="under", line=46.5, odds=-108, scraped_at=now)
    openers = {}
    record_weekly_totals(openers, {}, [quote], [game], now)
    assert openers["weekly_totals"][game.game_id]["under"] == -108
    record_weekly_totals(openers, {}, [replace(quote, line=45.5, scraped_at=dt("2026-09-28T14:00:00Z"))],
                         [game], dt("2026-09-28T14:00:00Z"))
    assert openers["weekly_totals"][game.game_id]["line"] == 46.5


@pytest.mark.parametrize("stamp", ["2026-09-27T21:59:59Z", "2026-09-28T16:00:00Z",
                                  "2026-09-29T01:00:00Z", "2026-09-20T23:00:00Z"])
def test_outside_window_quotes_never_become_weekly_openers(stamp):
    game = replace(_game(), kickoff_utc=dt("2026-10-04T17:00:00Z"))
    key = state.odds_key(game.game_id, "total", "under", "betonline")
    openers = {"openers": {key: {"line": 46, "odds": -110, "ts": stamp}}}
    record_weekly_totals(openers, {"series": {key: [[stamp, 46, -110]]}}, [], [game], dt("2026-10-01T12:00:00Z"))
    assert openers["weekly_totals"][game.game_id]["line"] is None


def test_weekly_opener_persists_and_prunes_with_existing_state(tmp_path):
    openers = state.migrate(None, "openers")
    openers["weekly_totals"] = {"active": {"line": 44}, "old": {"line": 42}}
    state.save_openers(tmp_path, openers)
    loaded = state.load_openers(tmp_path)
    state.prune_openers(loaded, ["active"])
    assert loaded["weekly_totals"] == {"active": {"line": 44}}


def test_cfb_sunday_boundary_and_missing_under_juice():
    game = replace(_game(), sport="cfb", kickoff_utc=dt("2026-10-03T20:00:00Z"))
    over = state.odds_key(game.game_id, "total", "over", "betonline")
    history = {"series": {over: [["2026-09-27T15:59:59Z", 49, -110],
                                 ["2026-09-27T16:00:00Z", 48.5, -105],
                                 ["2026-09-28T04:00:00Z", 47.5, -115]]}}
    openers = {}
    record_weekly_totals(openers, history, [], [game], dt("2026-09-28T12:00:00Z"))
    value = openers["weekly_totals"][game.game_id]
    assert (value["line"], value["over"], value["under"]) == (48.5, -105, None)


def test_future_and_alternate_quotes_cannot_become_openers():
    game = replace(_game(), kickoff_utc=dt("2026-10-04T17:00:00Z"))
    now = dt("2026-09-27T23:00:00Z")
    quote = GameLine(sport="nfl", game_id=game.game_id, book="betonline", market="total",
                     side="under", line=46.5, odds=-108, scraped_at=now)
    openers = {}
    record_weekly_totals(openers, {}, [replace(quote, is_main=False),
                                      replace(quote, scraped_at=dt("2026-09-28T01:00:00Z"))], [game], now)
    assert openers["weekly_totals"][game.game_id]["line"] is None
