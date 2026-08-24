"""FanDuel parser: scrubbed live fixtures (2026-08-23) -> expected GameLine rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers import fanduel as fd


def _load(fixtures_dir: Path, sport: str) -> dict:
    return json.loads((fixtures_dir / "raw" / "fanduel" / f"{sport}.json").read_text(encoding="utf-8"))


def _index(rows: list[GameLine]) -> dict[tuple[str, str, str], GameLine]:
    return {(r.game_id, r.market, r.side): r for r in rows}


@pytest.fixture(scope="module")
def nfl_rows(fixtures_dir: Path) -> list[GameLine]:
    return fd.parse(_load(fixtures_dir, "nfl"), "nfl")


@pytest.fixture(scope="module")
def cfb_rows(fixtures_dir: Path) -> list[GameLine]:
    return fd.parse(_load(fixtures_dir, "cfb"), "cfb")


def test_nfl_fixture_shape(nfl_rows: list[GameLine]) -> None:
    assert len(nfl_rows) == 120  # 20 games x 3 markets x 2 sides
    assert len({r.game_id for r in nfl_rows}) == 20
    assert all(r.sport == "nfl" and r.book == "fanduel" and r.is_main for r in nfl_rows)
    assert all(r.source_id and r.source_id.startswith("fanduel:") for r in nfl_rows)


def test_nfl_regular_season_game(nfl_rows: list[GameLine]) -> None:
    ix = _index(nfl_rows)
    g = "nfl:raw:2026-09-13:New York Jets@Tennessee Titans"
    assert ix[(g, "spread", "away")].line == 2.5 and ix[(g, "spread", "away")].odds == -115
    assert ix[(g, "spread", "home")].line == -2.5 and ix[(g, "spread", "home")].odds == -105
    assert ix[(g, "total", "over")].line == 39.5 and ix[(g, "total", "over")].odds == -118
    assert ix[(g, "total", "under")].line == 39.5 and ix[(g, "total", "under")].odds == -104
    assert ix[(g, "ml", "away")].line is None and ix[(g, "ml", "away")].odds == 118  # plus price
    assert ix[(g, "ml", "home")].odds == -138
    assert ix[(g, "ml", "home")].source_id == "fanduel:35609021"


def test_nfl_preseason_competition_included(nfl_rows: list[GameLine]) -> None:
    ix = _index(nfl_rows)
    g = "nfl:raw:2026-08-24:Seattle Seahawks@Tennessee Titans"
    assert ix[(g, "spread", "away")].line == -6.5 and ix[(g, "spread", "away")].odds == -130
    assert ix[(g, "spread", "home")].line == 6.5 and ix[(g, "spread", "home")].odds == -102
    assert ix[(g, "total", "under")].line == 42.5 and ix[(g, "total", "under")].odds == -125
    assert ix[(g, "ml", "home")].odds == 265


def test_cfb_fixture_shape(cfb_rows: list[GameLine]) -> None:
    # 20 games; 58 markets minus one SUSPENDED moneyline market -> 57 open x 2 sides = 114?
    # North Alabama @ Arkansas ML is SUSPENDED (both runners) and two events have no ML.
    assert len({r.game_id for r in cfb_rows}) == 20
    assert len(cfb_rows) == 113
    assert all(r.sport == "cfb" and r.book == "fanduel" for r in cfb_rows)


def test_cfb_favorite_and_dog_lines(cfb_rows: list[GameLine]) -> None:
    ix = _index(cfb_rows)
    g = "cfb:raw:2026-08-29:New Mexico State@Florida State"
    assert ix[(g, "spread", "away")].line == 30.5 and ix[(g, "spread", "away")].odds == -105
    assert ix[(g, "spread", "home")].line == -30.5 and ix[(g, "spread", "home")].odds == -115
    assert ix[(g, "total", "over")].line == 52.5 and ix[(g, "total", "over")].odds == -115
    assert ix[(g, "ml", "away")].odds == 2500
    assert ix[(g, "ml", "home")].odds == -10000


def test_cfb_plus_price_spread(cfb_rows: list[GameLine]) -> None:
    ix = _index(cfb_rows)
    g = "cfb:raw:2026-09-06:UNLV@Hawaii"
    assert ix[(g, "spread", "home")].line == 2.5 and ix[(g, "spread", "home")].odds == 102
    assert ix[(g, "spread", "away")].line == -2.5 and ix[(g, "spread", "away")].odds == -124
    g2 = "cfb:raw:2026-10-11:Georgia@Alabama"
    assert ix[(g2, "spread", "home")].odds == 100  # even money kept as +100
    assert ix[(g2, "ml", "home")].odds == 122


def test_cfb_suspended_market_dropped(cfb_rows: list[GameLine]) -> None:
    ix = _index(cfb_rows)
    g = "cfb:raw:2026-09-05:North Alabama@Arkansas"
    assert (g, "ml", "home") not in ix and (g, "ml", "away") not in ix
    assert ix[(g, "spread", "home")].line == -40.5 and ix[(g, "spread", "home")].odds == -110
    assert ix[(g, "total", "over")].line == 61.5


def test_cfb_fcs_competition_included(cfb_rows: list[GameLine]) -> None:
    ix = _index(cfb_rows)
    g = "cfb:raw:2026-08-28:William & Mary@Villanova"
    assert ix[(g, "ml", "away")].odds == 340 and ix[(g, "ml", "home")].odds == -500
    assert ix[(g, "spread", "away")].line == 11.5 and ix[(g, "spread", "away")].odds == -113


def test_market_filter_and_stamps(fixtures_dir: Path) -> None:
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    rows = fd.parse(_load(fixtures_dir, "nfl"), "nfl", scraped_at=ts, run_id="r1", market="total")
    assert rows and all(r.market == "total" for r in rows)
    assert all(r.scraped_at == ts and r.run_id == "r1" for r in rows)


def test_events_by_game_id(fixtures_dir: Path) -> None:
    evs = fd.events_by_game_id(_load(fixtures_dir, "nfl"), "nfl")
    ev = evs["nfl:raw:2026-09-13:New York Jets@Tennessee Titans"]
    assert ev.away == "New York Jets" and ev.home == "Tennessee Titans"
    assert ev.kickoff_utc == datetime(2026, 9, 13, 17, 1, tzinfo=timezone.utc)
    assert ev.competition == "NFL" and ev.event_id == "35609021"


def test_non_game_events_ignored() -> None:
    payload = {"attachments": {
        "competitions": {"1": {"name": "NFL"}, "2": {"name": "NFL Futures"}},
        "events": {
            "10": {"name": "Super Bowl Winner", "competitionId": 2, "openDate": "2027-02-08T23:30:00.000Z"},
            "11": {"name": "A @ B", "competitionId": 1, "openDate": "2026-09-13T17:00:00.000Z"},
        },
        "markets": {
            "m1": {"eventId": 10, "marketType": "MONEY_LINE", "marketStatus": "OPEN", "runners": [
                {"result": {"type": "HOME"}, "winRunnerOdds": {"americanDisplayOdds": {"americanOdds": 500}}}]},
            "m2": {"eventId": 11, "marketType": "SUPER_BOWL_WINNER_SGP", "marketStatus": "OPEN", "runners": []},
            "m3": {"eventId": 11, "marketType": "MONEY_LINE", "marketStatus": "OPEN", "runners": [
                {"result": {"type": "HOME"}, "runnerStatus": "ACTIVE",
                 "winRunnerOdds": {"americanDisplayOdds": {"americanOdds": -200}}},
                {"result": {"type": "AWAY"}, "runnerStatus": "SUSPENDED",
                 "winRunnerOdds": {"americanDisplayOdds": {"americanOdds": 170}}},
            ]},
        },
    }}
    rows = fd.parse(payload, "nfl")
    assert [(r.game_id, r.side, r.odds) for r in rows] == [("nfl:raw:2026-09-13:A@B", "home", -200)]


def test_split_event_name() -> None:
    assert fd.split_event_name("Miami Ohio @ Pittsburgh") == ("Miami Ohio", "Pittsburgh")
    assert fd.split_event_name("NFL Draft") is None
