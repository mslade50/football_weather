"""ProphetX partner-API parser: scrubbed live fixtures (2026-08-23) -> GameLine rows.

Fixtures: tests/fixtures/raw/prophetx/{nfl,cfb}.json = {"events": [...], "markets": {eid: [...]}}
scrubbed to <=20 games and order ladders trimmed to 3 per side.

* nfl.json: the only NFL event on the board today is the in-play preseason game
  Seattle Seahawks at Tennessee Titans (status ``live``) -- dropped by default,
  parsed with ``include_live=True`` (home +240 plus price).
* cfb.json: 8 week-0 games incl. the neutral-site North Carolina @ TCU (Dublin);
  ProphetX lists TCU as nominal home.  First event also carries First Half /
  1st Quarter markets that must be filtered out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers import prophetx as px

FIX = Path(__file__).parent / "fixtures" / "raw" / "prophetx"

UNC_TCU = "cfb:raw:2026-08-29T16:00:North Carolina@TCU"
SEA_TEN = "nfl:raw:2026-08-24T00:00:Seattle Seahawks@Tennessee Titans"


@pytest.fixture(scope="module")
def nfl_payload() -> dict:
    return json.loads((FIX / "nfl.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfb_payload() -> dict:
    return json.loads((FIX / "cfb.json").read_text(encoding="utf-8"))


def _mains(lines: list[GameLine], game_id: str) -> dict[tuple[str, str], GameLine]:
    return {(ln.market, ln.side): ln for ln in lines if ln.game_id == game_id and ln.is_main}


def test_fixture_sizes(nfl_payload: dict, cfb_payload: dict) -> None:
    assert len(nfl_payload["events"]) <= 20
    assert len(cfb_payload["events"]) <= 20


def test_tournament_sport_mapping() -> None:
    fb = {"name": "American Football"}
    assert px.tournament_sport({"name": "NFL", "sport": fb}) == "nfl"
    assert px.tournament_sport({"name": "College Football", "sport": fb}) == "cfb"
    assert px.tournament_sport({"name": "NFL Futures", "sport": fb}) is None
    assert px.tournament_sport({"name": "NCAAF Futures", "sport": fb}) is None
    assert px.tournament_sport({"name": "NFL", "sport": {"name": "Soccer"}}) is None
    assert px.tournament_sport({"name": "Premier League", "sport": {"name": "Soccer"}}) is None


def test_cfb_events(cfb_payload: dict) -> None:
    events = px.parse_events(cfb_payload["events"], "cfb")
    assert len(events) == 8
    assert px.parse_events(cfb_payload["events"], "nfl") == []
    ev = next(e for e in events if e.event_id == "50016484")
    assert (ev.away, ev.home) == ("North Carolina", "TCU")
    assert (ev.away_cid, ev.home_cid) == (50000021, 50000114)
    assert ev.start_utc == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    assert ev.status == "not_started"
    assert ev.tournament == "College Football"
    assert ev.game_id == UNC_TCU


def test_cfb_main_lines_explicit(cfb_payload: dict) -> None:
    lines = px.parse_payload(cfb_payload, "cfb")
    assert all(isinstance(ln, GameLine) and ln.book == "prophetx" and ln.sport == "cfb" for ln in lines)
    assert len({ln.game_id for ln in lines}) == 8
    assert len(lines) == 159
    assert sum(ln.is_main for ln in lines) == 44

    # neutral-site game (Dublin), nominal home TCU
    m = _mains(lines, UNC_TCU)
    assert set(m) == {
        ("ml", "home"), ("ml", "away"),
        ("spread", "home"), ("spread", "away"),
        ("total", "over"), ("total", "under"),
    }
    assert (m["ml", "home"].line, m["ml", "home"].odds) == (None, -300)
    assert (m["ml", "away"].line, m["ml", "away"].odds) == (None, 260)  # plus price
    assert (m["spread", "home"].line, m["spread", "home"].odds) == (-7.5, -111)
    assert (m["spread", "away"].line, m["spread", "away"].odds) == (7.5, -108)
    assert (m["total", "over"].line, m["total", "over"].odds) == (49.5, 112)
    assert (m["total", "under"].line, m["total", "under"].odds) == (49.5, -154)
    assert m["ml", "home"].prob_raw == pytest.approx(0.75)
    assert m["ml", "away"].prob_raw == pytest.approx(100 / 360)
    assert m["ml", "away"].source_id == "50016484:0afe27e6fe1a60871e6cf621893c1d16"
    assert all(ln.source_id.startswith("50016484:") for ln in m.values())

    # Hawaii @ Stanford: best-of-ladder picks the highest American price per side
    m2 = _mains(lines, "cfb:raw:2026-08-29T23:00:Hawaii@Stanford")
    assert (m2["ml", "home"].odds, m2["ml", "away"].odds) == (-205, 200)
    assert (m2["spread", "home"].line, m2["spread", "home"].odds) == (-5.5, -111)
    assert (m2["spread", "away"].line, m2["spread", "away"].odds) == (5.5, -114)
    assert (m2["total", "over"].line, m2["total", "over"].odds) == (50.5, 106)
    assert (m2["total", "under"].line, m2["total", "under"].odds) == (50.5, -146)


def test_cfb_missing_sides_and_alternates(cfb_payload: dict) -> None:
    lines = px.parse_payload(cfb_payload, "cfb")
    # San Jose State @ USC: moneyline has no posted liquidity -> no ml rows, spread/total still present
    gid = "cfb:raw:2026-08-29T19:00:San Jose State@USC"
    m = _mains(lines, gid)
    assert set(m) == {("spread", "home"), ("spread", "away"), ("total", "over"), ("total", "under")}
    assert (m["spread", "home"].line, m["spread", "home"].odds) == (-38.5, -105)
    assert (m["spread", "away"].line, m["spread", "away"].odds) == (38.5, -142)
    assert (m["total", "over"].line, m["total", "over"].odds) == (59.5, -110)

    # alternates carry is_main=False and are dropped by include_alternates=False
    alts = [ln for ln in lines if ln.game_id == UNC_TCU and not ln.is_main]
    assert len(alts) == 16
    assert ("spread", -8.0) in {(ln.market, ln.line) for ln in alts}
    mains_only = px.parse_payload(cfb_payload, "cfb", include_alternates=False)
    assert all(ln.is_main for ln in mains_only)
    assert len(mains_only) == 44


def test_cfb_derivative_markets_filtered(cfb_payload: dict) -> None:
    first = cfb_payload["markets"]["50016484"]
    names = {m["name"] for m in first}
    assert "First Half Moneyline" in names and "1st Quarter Spread" in names
    assert px.market_kind({"id": 64, "name": "First Half Moneyline", "type": "moneyline"}) is None
    assert px.market_kind({"id": 1323, "name": "1st Quarter Spread", "type": "spread"}) is None
    assert px.market_kind({"id": 930000007, "name": "MPHS: Team Total Points", "type": "total"}) is None
    assert px.market_kind({"id": 223, "name": "Spread", "type": "spread"}) == "spread"
    # spread lines for the game come only from market 223 (home-relative lines +-7.5 main)
    lines = px.parse_payload(cfb_payload, "cfb", market="spread")
    assert all(ln.market == "spread" for ln in lines)
    unc = [ln for ln in lines if ln.game_id == UNC_TCU and ln.is_main]
    assert sorted(ln.line for ln in unc) == [-7.5, 7.5]


def test_market_filter(cfb_payload: dict) -> None:
    totals = px.parse_payload(cfb_payload, "cfb", market="total")
    assert totals and all(ln.market == "total" and ln.side in ("over", "under") for ln in totals)
    assert all(ln.line is not None for ln in totals)


def test_nfl_live_event_skipped_by_default(nfl_payload: dict) -> None:
    assert px.parse_payload(nfl_payload, "nfl") == []
    lines = px.parse_payload(nfl_payload, "nfl", include_live=True)
    assert {ln.game_id for ln in lines} == {SEA_TEN}
    m = _mains(lines, SEA_TEN)
    # in-play preseason: only ML + one spread priced, no total liquidity
    assert set(m) == {("ml", "home"), ("ml", "away"), ("spread", "home"), ("spread", "away")}
    assert (m["ml", "home"].line, m["ml", "home"].odds) == (None, 240)  # plus price
    assert (m["ml", "away"].line, m["ml", "away"].odds) == (None, -270)
    assert (m["spread", "home"].line, m["spread", "home"].odds) == (5.5, -104)
    assert (m["spread", "away"].line, m["spread", "away"].odds) == (-5.5, -125)
    assert m["ml", "home"].source_id == "19739:d5209e33cd9f89acb8d022bca74fd412"
    assert px.parse_payload(nfl_payload, "cfb", include_live=True) == []


def test_american_to_prob() -> None:
    assert px.american_to_prob(-110) == pytest.approx(110 / 210)
    assert px.american_to_prob(150) == pytest.approx(0.4)
    assert px.american_to_prob(100) == pytest.approx(0.5)


def test_scraped_at_threaded(cfb_payload: dict) -> None:
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    lines = px.parse_payload(cfb_payload, "cfb", scraped_at=ts)
    assert all(ln.scraped_at == ts for ln in lines)
