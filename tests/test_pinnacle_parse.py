"""Pinnacle guest-feed parser: scrubbed live fixtures (2026-08-23) -> GameLine rows.

Fixtures: tests/fixtures/raw/pinnacle/{nfl,cfb}.json = {"matchups": [...], "markets": [...]}
scrubbed to <=20 games. Neutral-site games kept: North Carolina @ TCU (Dublin,
Aug 29) and Wisconsin @ Notre Dame (Lambeau, Sep 6) -- Pinnacle lists a nominal
home side and carries no neutral flag, so the parser emits them like any game.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers import pinnacle as pin

FIX = Path(__file__).parent / "fixtures" / "raw" / "pinnacle"


@pytest.fixture(scope="module")
def nfl_payload() -> dict:
    return json.loads((FIX / "nfl.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfb_payload() -> dict:
    return json.loads((FIX / "cfb.json").read_text(encoding="utf-8"))


def _mains(lines: list[GameLine], game_id: str) -> dict[tuple[str, str], GameLine]:
    return {(ln.market, ln.side): ln for ln in lines if ln.game_id == game_id and ln.is_main}


def test_fixture_sizes(nfl_payload: dict, cfb_payload: dict) -> None:
    assert len(nfl_payload["matchups"]) <= 20
    assert len(cfb_payload["matchups"]) <= 20


def test_nfl_events_skip_live_and_map_leagues(nfl_payload: dict) -> None:
    events = pin.parse_events(nfl_payload["matchups"], "nfl")
    # 17 matchups in fixture; the in-progress preseason game (isLive) is dropped
    assert len(events) == 16
    assert all(ev.sport == "nfl" and ev.league == "NFL" for ev in events)
    assert pin.parse_events(nfl_payload["matchups"], "cfb") == []
    ev = next(e for e in events if e.matchup_id == 1630889899)
    assert ev.away == "Atlanta Falcons"
    assert ev.home == "Pittsburgh Steelers"
    assert ev.start_utc == datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
    assert ev.game_id == "nfl:raw:2026-09-13T17:00:Atlanta Falcons@Pittsburgh Steelers"


def test_nfl_main_lines_explicit(nfl_payload: dict) -> None:
    lines = pin.parse_payload(nfl_payload, "nfl")
    assert all(isinstance(ln, GameLine) and ln.book == "pinnacle" and ln.sport == "nfl" for ln in lines)
    # 16 pre-game events, but Miami @ Las Vegas was suspended (every market status=closed) -> dropped
    assert len({ln.game_id for ln in lines}) == 15
    assert not any(ln.source_id == "1630889900" for ln in lines)

    gid = "nfl:raw:2026-09-13T17:00:Atlanta Falcons@Pittsburgh Steelers"
    m = _mains(lines, gid)
    assert set(m) == {
        ("ml", "home"), ("ml", "away"),
        ("spread", "home"), ("spread", "away"),
        ("total", "over"), ("total", "under"),
    }
    assert (m["ml", "home"].line, m["ml", "home"].odds) == (None, -165)
    assert (m["ml", "away"].line, m["ml", "away"].odds) == (None, 145)  # plus price
    assert (m["spread", "home"].line, m["spread", "home"].odds) == (-3.0, -110)
    assert (m["spread", "away"].line, m["spread", "away"].odds) == (3.0, -102)
    assert (m["total", "over"].line, m["total", "over"].odds) == (41.5, -121)
    assert (m["total", "under"].line, m["total", "under"].odds) == (41.5, 104)
    assert all(ln.source_id == "1630889899" for ln in m.values())

    # home underdog: Chicago Bears @ Carolina Panthers, home +2.5 at +101
    gid2 = "nfl:raw:2026-09-13T17:00:Chicago Bears@Carolina Panthers"
    m2 = _mains(lines, gid2)
    assert (m2["spread", "home"].line, m2["spread", "home"].odds) == (2.5, 101)
    assert (m2["spread", "away"].line, m2["spread", "away"].odds) == (-2.5, -113)
    assert (m2["ml", "home"].odds, m2["ml", "away"].odds) == (131, -148)


def test_nfl_alternates_flagged_not_main(nfl_payload: dict) -> None:
    lines = pin.parse_payload(nfl_payload, "nfl")
    gid = "nfl:raw:2026-09-13T17:00:Atlanta Falcons@Pittsburgh Steelers"
    alts = [ln for ln in lines if ln.game_id == gid and not ln.is_main]
    assert alts, "alternate ladders expected in fixture"
    assert {ln.market for ln in alts} == {"spread", "total"}
    alt_spread_home = {ln.line: ln.odds for ln in alts if ln.market == "spread" and ln.side == "home"}
    assert alt_spread_home[-7.5] == 198
    assert alt_spread_home[3.5] == -274
    # exactly one main line per side per market
    for market in ("spread", "total"):
        for side in (("home", "away") if market == "spread" else ("over", "under")):
            n = sum(1 for ln in lines if ln.game_id == gid and ln.is_main and ln.market == market and ln.side == side)
            assert n == 1, (market, side, n)
    # no alternates when disabled
    only_main = pin.parse_payload(nfl_payload, "nfl", include_alternates=False)
    assert all(ln.is_main for ln in only_main)
    assert len(only_main) == 15 * 6


def test_market_filter_and_scraped_at(nfl_payload: dict) -> None:
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    totals = pin.parse_payload(nfl_payload, "nfl", market="total", scraped_at=ts)
    assert totals and all(ln.market == "total" and ln.scraped_at == ts for ln in totals)
    assert not any(ln.market == "ml" for ln in totals)


def test_cfb_neutral_and_plus_prices(cfb_payload: dict) -> None:
    lines = pin.parse_payload(cfb_payload, "cfb")
    assert len({ln.game_id for ln in lines}) == 20
    assert all(ln.sport == "cfb" for ln in lines)
    assert pin.parse_payload(cfb_payload, "nfl") == []

    # Dublin neutral game: Pinnacle nominal home = TCU
    gid = "cfb:raw:2026-08-29T16:00:North Carolina@TCU"
    m = _mains(lines, gid)
    assert (m["ml", "home"].odds, m["ml", "away"].odds) == (-325, 255)
    assert (m["spread", "home"].line, m["spread", "home"].odds) == (-7.5, -112)
    assert (m["spread", "away"].line, m["spread", "away"].odds) == (7.5, -104)
    assert (m["total", "over"].line, m["total", "over"].odds) == (47.5, -109)
    assert (m["total", "under"].odds) == -111

    # Lambeau neutral game: nominal home = Notre Dame
    gid2 = "cfb:raw:2026-09-06T23:30:Wisconsin@Notre Dame"
    m2 = _mains(lines, gid2)
    assert (m2["spread", "home"].line, m2["spread", "home"].odds) == (-20.5, -106)
    assert (m2["ml", "away"].odds) == 766

    # big favourites have no moneyline: spread + total only
    gid3 = "cfb:raw:2026-08-29T19:00:San Jose State@USC"
    m3 = _mains(lines, gid3)
    assert ("ml", "home") not in m3
    assert (m3["spread", "home"].line, m3["spread", "home"].odds) == (-38.0, -103)
    assert (m3["total", "over"].line) == 59.5


def test_ignores_team_totals_periods_and_closed() -> None:
    matchup = {
        "id": 1, "type": "matchup", "isLive": False, "parentId": None,
        "league": {"name": "NFL"}, "startTime": "2026-09-13T17:00:00Z",
        "participants": [{"alignment": "home", "name": "H"}, {"alignment": "away", "name": "A"}],
    }
    markets = [
        {"matchupId": 1, "type": "team_total", "period": 0, "isAlternate": False, "side": "home",
         "prices": [{"designation": "over", "points": 21.5, "price": -115}]},
        {"matchupId": 1, "type": "spread", "period": 1, "isAlternate": False,
         "prices": [{"designation": "home", "points": -1.5, "price": -110}]},
        {"matchupId": 1, "type": "total", "period": 0, "isAlternate": False, "status": "closed",
         "prices": [{"designation": "over", "points": 40.0, "price": -110}]},
        {"matchupId": 1, "type": "moneyline", "period": 0, "isAlternate": False,
         "prices": [{"designation": "home", "price": -120}, {"designation": "away", "price": 100}]},
        {"matchupId": 2, "type": "moneyline", "period": 0, "isAlternate": False,
         "prices": [{"designation": "home", "price": -120}]},
    ]
    lines = pin.parse([matchup], markets, "nfl")
    assert [(ln.market, ln.side, ln.odds, ln.line) for ln in lines] == [
        ("ml", "home", -120, None),
        ("ml", "away", 100, None),
    ]
    assert lines[0].game_id == "nfl:raw:2026-09-13T17:00:A@H"
