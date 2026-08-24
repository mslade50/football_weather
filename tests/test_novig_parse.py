"""Novig parser: scrubbed live fixtures (captured 2026-08-23) -> expected GameLine rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers.novig import parse, prob_to_american, provisional_game_id

FIX = Path(__file__).parent / "fixtures" / "raw" / "novig"


def _load(sport: str) -> dict:
    return json.loads((FIX / f"{sport}.json").read_text(encoding="utf-8"))


def _by(rows: list[GameLine], game_id: str, market: str, side: str, main: bool = True) -> GameLine:
    hits = [r for r in rows if r.game_id == game_id and r.market == market and r.side == side and r.is_main == main]
    assert len(hits) == 1, f"{len(hits)} rows for {game_id} {market} {side} main={main}"
    return hits[0]


@pytest.fixture(scope="module")
def nfl_rows() -> list[GameLine]:
    return parse(_load("nfl"), "nfl")


@pytest.fixture(scope="module")
def cfb_rows() -> list[GameLine]:
    return parse(_load("cfb"), "cfb")


def test_prob_to_american() -> None:
    assert prob_to_american(0.5) == -100
    assert prob_to_american(0.63) == -170
    assert prob_to_american(0.415) == 141
    assert prob_to_american(0.98) == -4900
    assert prob_to_american(0.0) == 0
    assert prob_to_american(1.0) == 0


def test_fixture_sizes() -> None:
    assert len(_load("nfl")["data"]["event"]) <= 20
    assert len(_load("cfb")["data"]["event"]) <= 20


def test_nfl_counts(nfl_rows: list[GameLine]) -> None:
    assert len(nfl_rows) == 192
    assert len({r.game_id for r in nfl_rows}) == 19
    assert sum(r.is_main for r in nfl_rows) == 102
    assert all(r.book == "novig" and r.sport == "nfl" for r in nfl_rows)
    assert all(r.prob_raw is not None and 0 < r.prob_raw < 1 for r in nfl_rows)


def test_nfl_falcons_steelers_main_lines(nfl_rows: list[GameLine]) -> None:
    gid = provisional_game_id("nfl", datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc), "Atlanta Falcons", "Pittsburgh Steelers")
    assert gid == "nfl:raw:2026-09-13T17:00:Atlanta Falcons@Pittsburgh Steelers"

    ml_away = _by(nfl_rows, gid, "ml", "away")
    assert (ml_away.odds, ml_away.prob_raw, ml_away.line) == (141, 0.415, None)  # plus-price
    ml_home = _by(nfl_rows, gid, "ml", "home")
    assert (ml_home.odds, ml_home.prob_raw) == (-170, 0.63)

    sp_home = _by(nfl_rows, gid, "spread", "home")
    sp_away = _by(nfl_rows, gid, "spread", "away")
    assert (sp_home.line, sp_home.odds, sp_home.prob_raw) == (-3.5, -125, 0.555)
    assert (sp_away.line, sp_away.odds, sp_away.prob_raw) == (3.5, -125, 0.555)

    over = _by(nfl_rows, gid, "total", "over")
    under = _by(nfl_rows, gid, "total", "under")
    assert (over.line, over.odds, over.prob_raw) == (41.5, -120, 0.545)
    assert (under.line, under.odds, under.prob_raw) == (41.5, -102, 0.505)

    assert sp_home.source_id == "019fed1f-55fc-74a3-8b65-677f965d86fd:019fed1f-567a-7902-a1ab-69a07289e11f"


def test_spread_strike_is_home_relative(nfl_rows: list[GameLine]) -> None:
    raw = _load("nfl")
    ev = raw["data"]["event"][0]
    strike = next(m["strike"] for m in ev["markets"] if m["type"] == "SPREAD" and m["is_consensus"])
    assert strike == -3.5
    gid = "nfl:raw:2026-09-13T17:00:Atlanta Falcons@Pittsburgh Steelers"
    assert _by(nfl_rows, gid, "spread", "home").line == strike
    assert _by(nfl_rows, gid, "spread", "away").line == -strike


def test_alternates_are_not_main(nfl_rows: list[GameLine]) -> None:
    alts = [r for r in nfl_rows if not r.is_main]
    assert alts, "fixture should retain alternate lines"
    assert all(r.market in ("spread", "total") for r in alts)
    assert all(r.line is not None for r in alts)


def test_null_available_is_skipped() -> None:
    payload = {"data": {"event": [{
        "id": "e1", "league": "NFL", "scheduled_start": "2026-09-13T17:00:00+00:00",
        "game": {"homeTeam": {"name": "H"}, "awayTeam": {"name": "A"}},
        "markets": [{"id": "m1", "type": "TOTAL", "strike": 44.5, "is_consensus": True, "outcomes": [
            {"index": 0, "type": "Over", "available": None, "last": 0.5},
            {"index": 1, "type": "Under", "available": 0.52, "last": 0.5},
        ]}],
    }]}}
    rows = parse(payload, "nfl")
    assert [(r.side, r.line, r.odds) for r in rows] == [("under", 44.5, -108)]


def test_league_filter_and_missing_game() -> None:
    payload = {"data": {"event": [
        {"id": "x", "league": "NCAAF", "game": {"homeTeam": {"name": "H"}, "awayTeam": {"name": "A"}}, "markets": []},
        {"id": "y", "league": "NFL", "game": None, "markets": []},
    ]}}
    assert parse(payload, "nfl") == []
    assert parse({"data": {}}, "cfb") == []


def test_cfb_counts(cfb_rows: list[GameLine]) -> None:
    assert len(cfb_rows) == 141
    assert len({r.game_id for r in cfb_rows}) == 20
    assert sum(r.is_main for r in cfb_rows) == 86
    assert sum(1 for r in cfb_rows if r.is_main and r.odds > 0) == 23


def test_cfb_neutral_site_notre_dame_wisconsin(cfb_rows: list[GameLine]) -> None:
    # Played at Lambeau Field; Novig lists Wisconsin as home. Parser keeps book's home/away as-is
    # (merge layer handles the neutral flip against the schedule).
    gid = "cfb:raw:2026-09-06T23:30:Notre Dame@Wisconsin"
    ml_home = _by(cfb_rows, gid, "ml", "home")
    assert (ml_home.odds, ml_home.prob_raw) == (111, 0.475)  # plus-price
    ml_away = _by(cfb_rows, gid, "ml", "away")
    assert (ml_away.odds, ml_away.prob_raw) == (-4900, 0.98)
    sp_home = _by(cfb_rows, gid, "spread", "home")
    sp_away = _by(cfb_rows, gid, "spread", "away")
    assert (sp_home.line, sp_home.odds, sp_home.prob_raw) == (20.5, -113, 0.53)
    assert (sp_away.line, sp_away.odds, sp_away.prob_raw) == (-20.5, -117, 0.54)
    over = _by(cfb_rows, gid, "total", "over")
    under = _by(cfb_rows, gid, "total", "under")
    assert (over.line, over.odds, over.prob_raw) == (47.5, -117, 0.54)
    assert (under.line, under.odds, under.prob_raw) == (47.5, -108, 0.52)


def test_cfb_mercyhurst_youngstown(cfb_rows: list[GameLine]) -> None:
    gid = "cfb:raw:2026-08-27T22:00:Mercyhurst@Youngstown State"
    assert _by(cfb_rows, gid, "ml", "home").odds == -4900
    assert _by(cfb_rows, gid, "ml", "away").odds == 1900
    sp_home = _by(cfb_rows, gid, "spread", "home")
    assert (sp_home.line, sp_home.odds, sp_home.prob_raw) == (-27.5, -186, 0.65)
    sp_away = _by(cfb_rows, gid, "spread", "away")
    assert (sp_away.line, sp_away.odds, sp_away.prob_raw) == (27.5, -120, 0.545)


def test_stamps_scraped_at_and_run_id() -> None:
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    rows = parse(_load("nfl"), "nfl", scraped_at=ts, run_id="r1")
    assert all(r.scraped_at == ts and r.run_id == "r1" for r in rows)
    assert all("|" not in r.game_id for r in rows)
