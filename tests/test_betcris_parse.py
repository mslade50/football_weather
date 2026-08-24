"""bookmaker.eu (Betcris) lines-viewer parser: scrubbed fixtures -> explicit GameLine rows.

Fixtures captured live 2026-08-23 and trimmed to 20 games each
(tests/fixtures/raw/betcris/{nfl,cfb}.html).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.odds.parsers import betcris as p

FIX = Path(__file__).parent / "fixtures" / "raw" / "betcris"


@pytest.fixture(scope="module")
def nfl_html() -> str:
    return (FIX / "nfl.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cfb_html() -> str:
    return (FIX / "cfb.html").read_text(encoding="utf-8")


def _by_key(lines: list[GameLine]) -> dict[tuple[str, str, str], GameLine]:
    return {(ln.game_id, ln.market, ln.side): ln for ln in lines}


# ---------------------------------------------------------------- cell helpers

@pytest.mark.parametrize("text,expected", [
    ("+9½", 9.5), ("-3", -3.0), ("47½", 47.5), ("54", 54.0), ("PK", 0.0), ("EV", 0.0),
    ("-", None), ("", None), (None, None), ("abc", None),
])
def test_parse_number(text, expected):
    assert p.parse_number(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("+245", 245), ("-300", -300), ("EVEN", 100), ("-", None), ("", None), (None, None), ("+1904", 1904),
])
def test_parse_odds(text, expected):
    assert p.parse_odds(text) == expected


def test_start_title_pt_to_utc():
    # 8/29 9:00am PDT == 16:00Z
    assert p.parse_start_title("START 8/29 9:00am PT", 2026) == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    # 12:00pm and 12:xxam edge cases
    assert p.parse_start_title("START 8/29 12:00pm PT", 2026) == datetime(2026, 8, 29, 19, 0, tzinfo=timezone.utc)
    assert p.parse_start_title("START 9/03 12:30am PT", 2026) == datetime(2026, 9, 3, 7, 30, tzinfo=timezone.utc)
    # January playoff dates roll into the following calendar year (PST => +8h)
    assert p.parse_start_title("START 1/10 1:30pm PT", 2026) == datetime(2027, 1, 10, 21, 30, tzinfo=timezone.utc)
    assert p.parse_start_title("garbage", 2026) is None


def test_script_starts_carry_the_year(nfl_html):
    starts = p.parse_script_starts(nfl_html)
    assert len(starts) == 20
    assert starts[1] == datetime(2026, 8, 24, 0, 15, tzinfo=timezone.utc)  # 8/23 5:15pm PDT


# ---------------------------------------------------------------- NFL fixture

def test_nfl_games(nfl_html):
    games = p.parse_games(nfl_html, "nfl", page="nfl")
    assert len(games) == 20
    assert [g.number for g in games] == list(range(1, 21))

    g1 = games[0]
    assert (g1.away, g1.home) == ("Seattle Seahawks", "Tennessee Titans")
    assert g1.preseason is True and g1.neutral is False and g1.venue is None
    assert g1.kickoff_utc == datetime(2026, 8, 24, 0, 15, tzinfo=timezone.utc)
    assert (g1.away_spread, g1.home_spread, g1.total, g1.away_ml, g1.home_ml) == (-7.0, 7.0, 47.5, -358, 259)
    assert g1.game_id == "nfl:raw:20260824:seattle-seahawks@tennessee-titans"
    assert g1.source_id == "nfl:1"

    # off-the-board game keeps its identity but no numbers
    g2 = games[1]
    assert (g2.away, g2.home) == ("Pittsburgh Steelers", "Buffalo Bills")
    assert (g2.away_spread, g2.home_spread, g2.total, g2.away_ml, g2.home_ml) == (None,) * 5

    # subtitle 'NFL' (no extra line) resets preseason + venue
    assert games[5].preseason is False and games[5].venue is None

    # neutral / international game from oddsSubTitle venue line
    g19 = games[18]
    assert (g19.away, g19.home) == ("San Francisco 49ers", "Los Angeles Rams")
    assert g19.neutral is True
    assert g19.venue == "Melbourne Cricket Ground - East Melbourne, AUS"
    assert g19.kickoff_utc == datetime(2026, 9, 11, 0, 35, tzinfo=timezone.utc)
    assert (g19.away_spread, g19.home_spread, g19.total, g19.away_ml, g19.home_ml) == (4.0, -4.0, 48.5, 158, -188)
    # venue does not leak onto the next game (a fresh 'NFL' subtitle follows)
    assert games[19].neutral is False and games[19].venue is None


def test_nfl_lines(nfl_html):
    scraped = datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc)
    lines = p.parse(nfl_html, "nfl", page="nfl", scraped_at=scraped, run_id="r1")
    # 4 priced games x 6 rows; 16 off-board games contribute nothing
    assert len(lines) == 24
    assert {ln.book for ln in lines} == {"betcris"}
    assert all(ln.sport == "nfl" and ln.is_main and ln.scraped_at == scraped and ln.run_id == "r1" for ln in lines)

    k = _by_key(lines)
    gid = "nfl:raw:20260911:san-francisco-49ers@los-angeles-rams"
    assert k[(gid, "spread", "away")].line == 4.0
    assert k[(gid, "spread", "home")].line == -4.0
    assert k[(gid, "spread", "away")].odds == p.DEFAULT_JUICE == -110
    assert k[(gid, "spread", "home")].odds == -110
    assert k[(gid, "total", "over")].line == 48.5
    assert k[(gid, "total", "under")].line == 48.5
    assert k[(gid, "ml", "away")].odds == 158  # plus price
    assert k[(gid, "ml", "away")].line is None
    assert k[(gid, "ml", "home")].odds == -188
    assert k[(gid, "ml", "home")].source_id == "nfl:19"
    assert k[(gid, "ml", "home")].prob_raw is None

    g20 = "nfl:raw:20260913:cleveland-browns@jacksonville-jaguars"
    assert (k[(g20, "spread", "away")].line, k[(g20, "total", "over")].line, k[(g20, "ml", "away")].odds) == (7.0, 40.5, 298)


def test_market_filter(nfl_html):
    totals = p.parse(nfl_html, "nfl", page="nfl", market="total")
    assert len(totals) == 8 and {ln.market for ln in totals} == {"total"}
    mls = p.parse(nfl_html, "nfl", page="nfl", market="ml")
    assert len(mls) == 8 and {ln.market for ln in mls} == {"ml"}


# ---------------------------------------------------------------- CFB fixture

def test_cfb_games(cfb_html):
    games = p.parse_games(cfb_html, "cfb", page="college-football")
    assert len(games) == 20

    g1 = games[0]  # neutral-site season opener in Dublin
    assert (g1.away, g1.home) == ("North Carolina", "TCU")
    assert g1.neutral is True and g1.venue == "Aviva Stadium - Dublin, Ireland"
    assert g1.preseason is False
    assert g1.kickoff_utc == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)  # 9:00am PT
    assert (g1.away_spread, g1.home_spread, g1.total, g1.away_ml, g1.home_ml) == (8.0, -8.0, 47.5, 245, -300)
    assert g1.source_id == "college-football:1"

    # plain 'COLLEGE FOOTBALL' subtitle clears the venue for game 2
    g2 = games[1]
    assert (g2.away, g2.home) == ("New Mexico State", "Florida State")
    assert g2.neutral is False and g2.venue is None
    assert (g2.away_spread, g2.home_spread, g2.total, g2.away_ml, g2.home_ml) == (31.0, -31.0, 54.0, 1904, -10000)

    # half-point handling ('+9½' / '-9½')
    g3 = games[2]
    assert (g3.away, g3.home) == ("Sacramento St", "Eastern Michigan")
    assert (g3.away_spread, g3.home_spread, g3.total) == (9.5, -9.5, 52.0)

    # spread + total posted, moneyline off the board ('-')
    g5 = games[4]
    assert (g5.away, g5.home) == ("San Jose State", "USC")
    assert (g5.away_spread, g5.total, g5.away_ml, g5.home_ml) == (38.0, 60.0, None, None)

    # away favourite
    g16 = games[15]
    assert (g16.away, g16.home) == ("Miami Florida", "Stanford")
    assert (g16.away_spread, g16.home_spread, g16.away_ml, g16.home_ml) == (-23.5, 23.5, -2600, 1198)

    # midnight-PT edge: 9/04 12:00am? no — 5:00pm PT 9/04 => 00:00Z 9/05
    g14 = games[13]
    assert (g14.away, g14.home) == ("Toledo", "Michigan State")
    assert g14.kickoff_utc == datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert g14.game_id == "cfb:raw:20260905:toledo@michigan-state"


def test_cfb_lines(cfb_html):
    lines = p.parse(cfb_html, "cfb", page="college-football")
    # 20 games: 18 fully priced (6 rows) + 2 with ML off the board (4 rows)
    assert len(lines) == 18 * 6 + 2 * 4 == 116
    assert {ln.sport for ln in lines} == {"cfb"}
    k = _by_key(lines)
    gid = "cfb:raw:20260829:north-carolina@tcu"
    assert k[(gid, "spread", "away")].line == 8.0 and k[(gid, "spread", "home")].line == -8.0
    assert k[(gid, "total", "over")].line == 47.5 and k[(gid, "total", "under")].line == 47.5
    assert k[(gid, "ml", "away")].odds == 245 and k[(gid, "ml", "home")].odds == -300
    usc = "cfb:raw:20260829:san-jose-state@usc"
    assert (usc, "ml", "away") not in k and (usc, "spread", "home") in k


def test_dedupe_games_across_pages(nfl_html):
    games = p.parse_games(nfl_html, "nfl", page="nfl")
    again = p.parse_games(nfl_html, "nfl", page="nfl-preseason")
    merged = p.dedupe_games(games + again)
    assert len(merged) == 20
    assert all(g.page == "nfl" for g in merged)


def test_empty_and_unknown_sport(nfl_html):
    assert p.parse("<html><body>nothing</body></html>", "nfl") == []
    with pytest.raises(ValueError):
        p.parse_games(nfl_html, "mlb")
