"""Kalshi parser: scrubbed live fixtures (2026-08-23) -> explicit GameLine rows.

Fixtures: ``tests/fixtures/raw/kalshi/{nfl,cfb}.json`` = ``{series_ticker: [events]}``
with nested markets, 20 games each. The CFB file carries Wisconsin vs Notre Dame
(Lambeau Field, neutral) and Clemson @ LSU as ML-only events, plus the illiquid
Maine vs Towson ladders that must not produce a main line.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers import kalshi as kp

FIX = Path(__file__).parent / "fixtures" / "raw" / "kalshi"


@pytest.fixture(scope="module")
def nfl_payload() -> dict:
    return json.loads((FIX / "nfl.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfb_payload() -> dict:
    return json.loads((FIX / "cfb.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nfl_lines(nfl_payload) -> list[GameLine]:
    return kp.parse(nfl_payload, "nfl")


@pytest.fixture(scope="module")
def cfb_lines(cfb_payload) -> list[GameLine]:
    return kp.parse(cfb_payload, "cfb")


def rows(lines: list[GameLine], game_suffix: str, market: str | None = None, main_only: bool = False) -> list[GameLine]:
    out = [ln for ln in lines if ln.game_id.endswith(game_suffix)]
    if market:
        out = [ln for ln in out if ln.market == market]
    if main_only:
        out = [ln for ln in out if ln.is_main]
    return out


def by_source(lines: list[GameLine], ticker: str) -> dict[str, GameLine]:
    return {ln.side: ln for ln in lines if ln.source_id == ticker}


# ── price helpers ────────────────────────────────────────────────────────────
def test_dollar_to_american():
    assert kp.dollar_to_american(0.5) == -100
    assert kp.dollar_to_american(0.6) == -150
    assert kp.dollar_to_american(0.25) == 300
    assert kp.dollar_to_american(0.0) == 0
    assert kp.dollar_to_american(1.0) == 0


def test_effective_price_adds_taker_fee():
    assert kp.taker_fee(0.5) == pytest.approx(0.0175)
    assert kp.effective_price(0.52) == pytest.approx(0.52 + 0.07 * 0.52 * 0.48)
    assert kp.effective_price(0.999) == 0.99


def test_quote_filters():
    assert kp.quote({"status": "active", "yes_bid_dollars": "0.5100", "yes_ask_dollars": "0.5200"}) == (0.51, 0.52)
    assert kp.quote({"status": "finalized", "yes_bid_dollars": "0.5100", "yes_ask_dollars": "0.5200"}) is None
    assert kp.quote({"status": "active", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0100"}) is None
    assert kp.quote({"status": "active", "yes_bid_dollars": "0.9900", "yes_ask_dollars": "1.0000"}) is None
    assert kp.quote({"status": "active", "yes_bid_dollars": "0.0800", "yes_ask_dollars": "0.6000"}) is None
    assert kp.quote({"status": "active", "yes_bid_dollars": None, "yes_ask_dollars": "0.5"}) is None


# ── identity ─────────────────────────────────────────────────────────────────
def test_parse_event_ticker():
    assert kp.parse_event_ticker("KXNFLGAME-26SEP13DALNYG") == ("KXNFLGAME", date(2026, 9, 13), "DALNYG")
    assert kp.parse_event_ticker("KXNCAAFSPREAD-26AUG27METOWS") == ("KXNCAAFSPREAD", date(2026, 8, 27), "METOWS")
    assert kp.parse_event_ticker("garbage") is None


def test_split_abbrs_prefers_sub_title_and_falls_back_to_market_suffix():
    ev = {"sub_title": "SEA vs TEN (Aug 23)", "markets": []}
    assert kp.split_abbrs(ev, "SEATEN") == ("SEA", "TEN")
    ev = {"sub_title": "", "markets": [{"ticker": "KXNFLSPREAD-26SEP13DALNYG-NYG3"}]}
    assert kp.split_abbrs(ev, "DALNYG") == ("DAL", "NYG")
    ev = {"sub_title": "", "markets": [{"ticker": "KXNFLGAME-26SEP13DALNYG-DAL"}]}
    assert kp.split_abbrs(ev, "DALNYG") == ("DAL", "NYG")


def test_split_names():
    assert kp.split_names("Dallas vs New York: Spread") == ("Dallas", "New York")
    assert kp.split_names("Wisconsin vs Notre Dame") == ("Wisconsin", "Notre Dame")


def test_event_teams(cfb_payload):
    teams = kp.event_teams(cfb_payload, "cfb")
    assert len(teams) == 20
    assert teams["cfb:raw:2026-09-06:WIS@ND"] == {
        "away": "WIS", "home": "ND", "away_name": "Wisconsin", "home_name": "Notre Dame", "date": date(2026, 9, 6),
    }
    assert teams["cfb:raw:2026-09-05:CLEM@LSU"]["home_name"] == "LSU"


# ── NFL fixture ──────────────────────────────────────────────────────────────
def test_nfl_fixture_shape(nfl_payload, nfl_lines):
    assert set(nfl_payload) == {"KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"}
    games = {ln.game_id for ln in nfl_lines}
    assert len(games) == 20
    assert len(nfl_lines) == 1534
    assert all(ln.book == "kalshi" and ln.sport == "nfl" for ln in nfl_lines)
    assert all(0.0 < ln.prob_raw < 1.0 and ln.odds != 0 for ln in nfl_lines)
    # every priced game carries a main ML pair; 17 games carry full main spread+total
    main = [ln for ln in nfl_lines if ln.is_main]
    assert sum(1 for ln in main if ln.market == "ml") == 40
    assert sum(1 for ln in main if ln.market == "spread") == 34
    assert sum(1 for ln in main if ln.market == "total") == 34
    # each game has at most one main line per market side
    seen = {(ln.game_id, ln.market, ln.side) for ln in main}
    assert len(seen) == len(main)


def test_nfl_dal_nyg_main_lines(nfl_lines):
    main = {(ln.market, ln.side): ln for ln in rows(nfl_lines, "2026-09-13:DAL@NYG", main_only=True)}
    assert set(main) == {("ml", "home"), ("ml", "away"), ("spread", "home"), ("spread", "away"), ("total", "over"), ("total", "under")}
    # ML: NYG yes 0.40/0.42 -> mid 0.41; ask 0.42 + fee -> +129 ; DAL 0.59/0.60 -> -161
    assert main[("ml", "home")].odds == 129
    assert main[("ml", "home")].prob_raw == pytest.approx(0.41)
    assert main[("ml", "home")].source_id == "KXNFLGAME-26SEP13DALNYG-NYG"
    assert main[("ml", "away")].odds == -161
    assert main[("ml", "away")].prob_raw == pytest.approx(0.595)
    assert main[("ml", "away")].line is None
    # Spread: "Dallas wins by over 2.5" 0.51/0.52 -> away -2.5 at -116, home +2.5 at -103
    assert main[("spread", "away")].line == -2.5
    assert main[("spread", "away")].odds == -116
    assert main[("spread", "away")].prob_raw == pytest.approx(0.515)
    assert main[("spread", "home")].line == 2.5
    assert main[("spread", "home")].odds == -103
    assert main[("spread", "home")].prob_raw == pytest.approx(0.485)
    assert main[("spread", "home")].source_id == "KXNFLSPREAD-26SEP13DALNYG-DAL3"
    # Total: "over 48.5" 0.47/0.50 -> over -107 (0.485), under -121 (0.515)
    assert main[("total", "over")].line == 48.5
    assert main[("total", "over")].odds == -107
    assert main[("total", "over")].prob_raw == pytest.approx(0.485)
    assert main[("total", "under")].line == 48.5
    assert main[("total", "under")].odds == -121
    assert main[("total", "under")].source_id == "KXNFLTOTAL-26SEP13DALNYG-49"


def test_nfl_alternate_rung_is_not_main(nfl_lines):
    alt = by_source(nfl_lines, "KXNFLSPREAD-26SEP13DALNYG-NYG3")  # "New York wins by over 2.5" 0.26/0.38
    assert alt["home"].line == -2.5 and alt["home"].odds == 152 and alt["home"].prob_raw == pytest.approx(0.32)
    assert alt["away"].line == 2.5 and alt["away"].odds == -306 and alt["away"].prob_raw == pytest.approx(0.68)
    assert not alt["home"].is_main and not alt["away"].is_main
    spread = rows(nfl_lines, "2026-09-13:DAL@NYG", "spread")
    assert len(spread) == 50  # 25 priced rungs x 2 sides
    assert len({ln.line for ln in spread}) > 10


def test_nfl_plus_price_on_main_line(nfl_lines):
    pit = by_source(nfl_lines, "KXNFLSPREAD-26SEP13ATLPIT-PIT3")  # PIT -2.5 at 0.53/0.54
    assert pit["home"].is_main and pit["away"].is_main
    assert pit["home"].line == -2.5 and pit["home"].odds == -126
    assert pit["away"].line == 2.5 and pit["away"].odds == 105
    assert pit["away"].prob_raw == pytest.approx(0.465)


def test_nfl_home_favorite_spread_sign(nfl_lines):
    main = {(ln.market, ln.side): ln for ln in rows(nfl_lines, "2026-09-09:NE@SEA", main_only=True)}
    assert main[("spread", "home")].line == -3.5
    assert main[("spread", "away")].line == 3.5
    assert main[("spread", "home")].source_id == "KXNFLSPREAD-26SEP09NESEA-SEA4"
    assert main[("ml", "away")].odds == 159  # NE 0.36/0.37


def test_nfl_finalized_and_unquoted_markets_skipped(nfl_payload, nfl_lines):
    tickers = {ln.source_id for ln in nfl_lines}
    assert "KXNFLTOTAL-26AUG23SEATEN-18" not in tickers  # status finalized
    assert "KXNFLSPREAD-26AUG23SEATEN-TEN17" not in tickers  # 0.00 / 0.01 one-sided
    ml_only = rows(nfl_lines, "2026-08-27:PIT@BUF")
    assert {ln.market for ln in ml_only} == {"ml"}  # preseason game: no spread/total series


def test_market_filter_and_metadata(nfl_payload):
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    lines = kp.parse({"KXNFLTOTAL": nfl_payload["KXNFLTOTAL"]}, "nfl", scraped_at=ts, run_id="r1")
    assert lines and all(ln.market == "total" for ln in lines)
    assert all(ln.scraped_at == ts and ln.run_id == "r1" for ln in lines)
    # a series from the other sport is ignored
    assert kp.parse({"KXNFLTOTAL": nfl_payload["KXNFLTOTAL"]}, "cfb") == []
    # {"events": [...]} envelope accepted too
    assert len(kp.parse({"KXNFLTOTAL": {"events": nfl_payload["KXNFLTOTAL"]}}, "nfl")) == len(lines)
    with pytest.raises(ValueError):
        kp.parse({}, "xfl")


# ── CFB fixture ──────────────────────────────────────────────────────────────
def test_cfb_fixture_shape(cfb_payload, cfb_lines):
    assert set(cfb_payload) == {"KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL"}
    assert len({ln.game_id for ln in cfb_lines}) == 20
    assert len(cfb_lines) == 924
    assert all(ln.sport == "cfb" for ln in cfb_lines)


def test_cfb_haw_stan_main(cfb_lines):
    main = {(ln.market, ln.side): ln for ln in rows(cfb_lines, "2026-08-29:HAW@STAN", main_only=True)}
    assert main[("ml", "home")].odds == -218 and main[("ml", "home")].prob_raw == pytest.approx(0.665)
    assert main[("ml", "away")].odds == 166 and main[("ml", "away")].prob_raw == pytest.approx(0.355)
    assert main[("spread", "home")].line == -4.5 and main[("spread", "home")].odds == -126
    assert main[("spread", "away")].line == 4.5 and main[("spread", "away")].odds == -112
    assert main[("spread", "home")].source_id == "KXNCAAFSPREAD-26AUG29HAWSTAN-STAN5"
    assert main[("total", "over")].line == 48.5 and main[("total", "over")].odds == -121
    assert main[("total", "under")].line == 48.5 and main[("total", "under")].odds == -116


def test_cfb_neutral_and_ml_only_games(cfb_lines):
    wis_nd = rows(cfb_lines, "2026-09-06:WIS@ND")
    assert {ln.market for ln in wis_nd} == {"ml"}
    ml = {ln.side: ln for ln in wis_nd}
    assert ml["home"].odds == -1428 and ml["home"].prob_raw == pytest.approx(0.925)
    assert ml["away"].odds == 1074 and ml["away"].prob_raw == pytest.approx(0.075)
    assert ml["away"].source_id == "KXNCAAFGAME-26SEP06WISND-WIS"
    clem = {ln.side: ln for ln in rows(cfb_lines, "2026-09-05:CLEM@LSU")}
    assert clem["home"].odds == -381 and clem["away"].odds == 296


def test_cfb_illiquid_ladder_has_no_main(cfb_lines):
    me_tows = rows(cfb_lines, "2026-08-27:ME@TOWS")
    assert {ln.market for ln in me_tows} == {"ml"}  # every ladder rung wider than MAX_WIDTH
    # priced rungs but none within MAIN_TOL of 0.5 -> rows emitted, nothing main
    ev = {"event_ticker": "KXNCAAFTOTAL-26AUG29AAABBB", "sub_title": "AAA vs BBB (Aug 29)", "title": "A vs B: Total Points",
          "markets": [{"ticker": "KXNCAAFTOTAL-26AUG29AAABBB-50", "status": "active", "floor_strike": 49.5,
                       "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.2000"},
                      {"ticker": "KXNCAAFTOTAL-26AUG29AAABBB-56", "status": "active", "floor_strike": 55.5,
                       "yes_bid_dollars": "0.0500", "yes_ask_dollars": "0.1500"}]}
    synth = kp.parse({"KXNCAAFTOTAL": [ev]}, "cfb")
    assert len(synth) == 4 and not any(ln.is_main for ln in synth)


def test_pick_main_tolerance():
    assert kp._pick_main([(2.5, 0.52), (3.5, 0.45), (1.5, 0.6)]) == 2.5
    assert kp._pick_main([(-1.5, 0.48), (1.5, 0.52)]) == -1.5  # tie -> smaller |line| then first
    assert kp._pick_main([(10.5, 0.2), (13.5, 0.1)]) is None
    assert kp._pick_main([]) is None
