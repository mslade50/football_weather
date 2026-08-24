"""BetOnline offering-by-league fixture -> expected GameLine rows.

Fixtures captured live 2026-08-23 (scrubbed to <=20 games):
  tests/fixtures/raw/betonline/nfl.json  (League 'nfl', 18 games)
  tests/fixtures/raw/betonline/cfb.json  (League 'ncaa', 20 games)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.odds.parsers import betonline as p


@pytest.fixture(scope="module")
def nfl_payload(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "raw" / "betonline" / "nfl.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfb_payload(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "raw" / "betonline" / "cfb.json").read_text(encoding="utf-8"))


def _rows(lines, game_id: str) -> dict[tuple[str, str], tuple]:
    return {(ln.market, ln.side): (ln.line, ln.odds) for ln in lines if ln.game_id == game_id}


# --- kickoff -----------------------------------------------------------------

def test_parse_cutoff_applies_utc_offset():
    assert p.parse_cutoff("2026-09-13T13:00:00") == datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
    # November game still uses the fixed 240-minute request offset (not EST)
    assert p.parse_cutoff("2026-11-26T17:30:00") == datetime(2026, 11, 26, 21, 30, tzinfo=timezone.utc)
    assert p.parse_cutoff("0001-01-01T00:00:00") is None
    assert p.parse_cutoff(None) is None
    assert p.parse_cutoff("garbage") is None


# --- NFL ---------------------------------------------------------------------

def test_nfl_games_and_counts(nfl_payload):
    games = p.parse_games(nfl_payload, "nfl")
    assert len(games) == 18
    assert {g.sport for g in games} == {"nfl"}
    assert all(g.league == "nfl" for g in games)
    assert all(g.kickoff_utc is not None for g in games)

    lines = p.parse(nfl_payload, "nfl")
    by_market = {m: sum(1 for ln in lines if ln.market == m) for m in ("spread", "total", "ml")}
    assert by_market == {"spread": 36, "total": 36, "ml": 28}  # 4 games have ML off (0/0)
    assert len(lines) == 100
    assert all(ln.book == "betonline" for ln in lines)


def test_nfl_full_game_explicit_values(nfl_payload):
    lines = p.parse(nfl_payload, "nfl")
    gid = "nfl:raw:20260913:cleveland-browns@jacksonville-jaguars"
    assert _rows(lines, gid) == {
        ("spread", "away"): (7.5, -110),
        ("spread", "home"): (-7.5, -110),
        ("total", "over"): (40.5, -110),
        ("total", "under"): (40.5, -110),
        ("ml", "away"): (None, 289),
        ("ml", "home"): (None, -360),
    }
    assert {ln.source_id for ln in lines if ln.game_id == gid} == {"nfl:491038562"}


def test_nfl_neutral_site_from_comments(nfl_payload):
    games = {g.game_id: g for g in p.parse_games(nfl_payload, "nfl")}
    g = games["nfl:raw:20260911:san-francisco-49ers@los-angeles-rams"]
    assert g.neutral is True
    assert g.venue == "Melbourne Cricket Ground, Australia"
    # 20:35 local (UTC-4) on 9/10 -> 00:35 UTC on 9/11
    assert g.kickoff_utc == datetime(2026, 9, 11, 0, 35, tzinfo=timezone.utc)
    assert g.total == 48.5 and g.over_odds == -110 and g.under_odds == -110
    non_neutral = games["nfl:raw:20260913:cleveland-browns@jacksonville-jaguars"]
    assert non_neutral.neutral is False and non_neutral.venue is None


def test_nfl_pickem_and_plus_price(nfl_payload):
    lines = p.parse(nfl_payload, "nfl")
    # Packers @ Vikings: pick'em with +100 on the away side; ML off the board
    rows = _rows(lines, "nfl:raw:20260913:green-bay-packers@minnesota-vikings")
    assert rows[("spread", "away")] == (0.0, 100)
    assert rows[("spread", "home")] == (0.0, -120)
    assert ("ml", "away") not in rows and ("ml", "home") not in rows
    # Cowboys @ Giants: +100 on the home spread (plus-price survives int cast)
    rows = _rows(lines, "nfl:raw:20260914:dallas-cowboys@new-york-giants")
    assert rows[("spread", "away")] == (-2.5, -120)
    assert rows[("spread", "home")] == (2.5, 100)
    assert rows[("ml", "away")] == (None, -150) and rows[("ml", "home")] == (None, 130)


def test_nfl_ml_off_board_dropped_totals_kept(nfl_payload):
    lines = p.parse(nfl_payload, "nfl")
    rows = _rows(lines, "nfl:raw:20260918:detroit-lions@buffalo-bills")
    assert rows == {
        ("spread", "away"): (3.0, -110),
        ("spread", "home"): (-3.0, -110),
        ("total", "over"): (51.5, -110),
        ("total", "under"): (51.5, -110),
    }


def test_market_filter(nfl_payload):
    totals = p.parse(nfl_payload, "nfl", market="total")
    assert totals and {ln.market for ln in totals} == {"total"}
    assert len(totals) == 36


# --- CFB ---------------------------------------------------------------------

def test_cfb_games_and_counts(cfb_payload):
    games = p.parse_games(cfb_payload, "cfb")
    assert len(games) == 20
    assert all(g.sport == "cfb" and g.league == "ncaa" for g in games)
    lines = p.parse(cfb_payload, "cfb")
    by_market = {m: sum(1 for ln in lines if ln.market == m) for m in ("spread", "total", "ml")}
    assert by_market == {"spread": 40, "total": 40, "ml": 32}  # 4 big favourites have no ML
    assert len(lines) == 112


def test_cfb_neutral_games(cfb_payload):
    games = {g.game_id: g for g in p.parse_games(cfb_payload, "cfb")}
    neutral = sorted(gid for gid, g in games.items() if g.neutral)
    assert neutral == [
        "cfb:raw:20260829:nc-state@virginia",
        "cfb:raw:20260829:north-carolina@tcu",
        "cfb:raw:20260905:baylor@auburn",
        "cfb:raw:20260906:wisconsin@notre-dame",
    ]
    assert all(games[gid].venue == "Neutral Field" for gid in neutral)
    g = games["cfb:raw:20260829:north-carolina@tcu"]
    assert g.kickoff_utc == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    assert (g.away_spread, g.home_spread, g.total, g.away_ml, g.home_ml) == (7.5, -7.5, 47.5, 245, -300)


def test_cfb_explicit_rows_and_plus_price(cfb_payload):
    lines = p.parse(cfb_payload, "cfb")
    # Away favourite: negative away point, positive home ML
    rows = _rows(lines, "cfb:raw:20260905:miami-florida@stanford")
    assert rows[("spread", "away")] == (-24.5, -105)
    assert rows[("spread", "home")] == (24.5, -115)
    assert rows[("ml", "away")] == (None, -2800) and rows[("ml", "home")] == (None, 1216)
    assert rows[("total", "over")] == (48.5, -110)
    # Wyoming @ Colorado State: +100 on the home spread
    rows = _rows(lines, "cfb:raw:20260905:wyoming@colorado-state")
    assert rows[("spread", "away")] == (3.5, -120)
    assert rows[("spread", "home")] == (-3.5, 100)
    assert rows[("ml", "away")] == (None, 140) and rows[("ml", "home")] == (None, -160)
    # Big favourite with ML off: only spread + total
    rows = _rows(lines, "cfb:raw:20260829:san-jose-state@usc")
    assert set(rows) == {("spread", "away"), ("spread", "home"), ("total", "over"), ("total", "under")}
    assert rows[("spread", "away")] == (39.0, -115)
    assert rows[("total", "under")] == (60.5, -110)


# --- edge cases --------------------------------------------------------------

def test_null_offering_and_error_payloads():
    assert p.parse({"GameOffering": None, "IsError": False}, "cfb") == []
    assert p.parse({"GameOffering": {"GamesDescription": []}, "IsError": True}, "nfl") == []
    assert p.parse(None, "nfl") == []


def test_spread_off_board_when_both_prices_zero():
    entry = {"Game": {
        "GameId": 1, "AwayTeam": "A", "HomeTeam": "B", "WagerCutOff": "2026-09-13T13:00:00", "Comments": "",
        "AwayLine": {"SpreadLine": {"Point": 0, "Line": 0}, "MoneyLine": {"Line": 120}},
        "HomeLine": {"SpreadLine": {"Point": 0, "Line": 0}, "MoneyLine": {"Line": -140}},
        "TotalLine": {"TotalLine": {"Point": 0, "Over": {"Line": 0}, "Under": {"Line": 0}}},
    }}
    g = p.parse_game(entry, "nfl", "nfl")
    assert g is not None and g.away_spread is None and g.total is None
    lines = p.game_lines(g)
    assert {(ln.market, ln.side, ln.odds) for ln in lines} == {("ml", "away", 120), ("ml", "home", -140)}


def test_dedupe_across_leagues(nfl_payload):
    games = p.parse_games(nfl_payload, "nfl", league="nfl") + p.parse_games(nfl_payload, "nfl", league="nfl-preseason")
    assert len(p.dedupe_games(games)) == 18


def test_scraped_at_and_run_id_threaded(nfl_payload):
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    lines = p.parse(nfl_payload, "nfl", scraped_at=ts, run_id="r1")
    assert all(ln.scraped_at == ts and ln.run_id == "r1" for ln in lines)
