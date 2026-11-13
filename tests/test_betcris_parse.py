"""Betcris public JSON parser contracts and retained identity helpers."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.odds.parsers import betcris as p

FIX = Path(__file__).parent / "fixtures" / "raw" / "betcris"


@pytest.fixture
def public_cfb():
    return json.loads((FIX / "cfb_public.json").read_text(encoding="utf-8"))


PUBLIC_NOW = datetime(2026, 9, 26, 3, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("change", [
    {"feed_ok": False}, {"version": 2}, {"feed_fetched_at": None}, {"feed_fetched_at": "invalid"},
    {"feed_fetched_at": "2026-09-25T03:00:00Z"},
    {"feed_fetched_at": "2026-09-26T04:00:00Z"},
    {"league": {"id": 108}}, {"games": {}},
])
def test_public_rejects_bad_or_stale_feed(public_cfb, change):
    public_cfb.update(change)
    with pytest.raises(ValueError):
        p.parse_public(public_cfb, "cfb", now=PUBLIC_NOW)


@pytest.mark.parametrize("change", [{"live": True}, {"period_no": 1}, {"period_no": None},
                                   {"parent": 123}, {"starts_at": "invalid"}])
def test_public_skips_live_partial_and_invalid_games(public_cfb, change):
    game = copy.deepcopy(public_cfb["games"][0])
    game.update(change)
    public_cfb["games"] = [game]
    assert p.parse_public(public_cfb, "cfb", now=PUBLIC_NOW) == []


@pytest.mark.parametrize("change", [{"status": "suspended"}, {"status": "unavailable"},
                                   {"price": None}, {"price": 0}, {"line": "NaN"}])
def test_public_skips_invalid_quote_without_inventing_opposite_side(public_cfb, change):
    game = copy.deepcopy(public_cfb["games"][0])
    game["markets"] = {"total": {"under": {"line": "44.50", "price": -107, "status": "open", **change}}}
    public_cfb["games"] = [game]
    assert p.parse_public(public_cfb, "cfb", now=PUBLIC_NOW) == []


def test_public_excludes_games_after_kickoff_even_if_live_flag_is_late(public_cfb):
    kickoff = datetime(2026, 9, 26, 22, tzinfo=timezone.utc)
    public_cfb["games"] = [g for g in public_cfb["games"] if g["home"] == "Old Dominion"]
    public_cfb["feed_fetched_at"] = kickoff.isoformat()
    assert p.parse_public(public_cfb, "cfb", now=kickoff + timedelta(seconds=1)) == []


@pytest.mark.parametrize("payload", ["<html></html>", "{}", [], None, 1])
def test_parse_rejects_non_json_objects(payload):
    with pytest.raises(ValueError, match="requires a decoded public JSON object"):
        p.parse(payload, "cfb", now=PUBLIC_NOW)


def test_parse_delegates_to_public_json(public_cfb):
    assert p.parse(public_cfb, "cfb", now=PUBLIC_NOW, market="ml", run_id="json") == p.parse_public(
        public_cfb, "cfb", now=PUBLIC_NOW, market="ml", run_id="json")


def test_retained_game_identity_helpers():
    kickoff = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)
    game = p.BetcrisGame(1, "nfl", "public", "Away Team", "Home Team", kickoff,
                        None, False, False, None, None, None, None, None)
    assert game.game_id == "nfl:raw:20261004:away-team@home-team"
    assert p.dedupe_games([game, game]) == [game]
