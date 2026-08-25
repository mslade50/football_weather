"""Team normalization + odds merge (PLAN Phase 2: test_merge_aliases).

rapidfuzz is stubbed with MagicMock by conftest, so these tests exercise the
difflib fallback path; the real rapidfuzz path is identical apart from scoring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import state as state_mod
from pipeline.contracts import Game, GameLine, make_game_id
from pipeline.odds import merge as merge_mod
from pipeline.odds import teams as teams_mod
from pipeline.odds.merge import (
    RawGame,
    canonicalize,
    merge_odds,
    opener_for,
    parse_provisional,
    pivot,
    select_main,
)
from pipeline.odds.teams import normalize_team, reset_unresolved, unresolved_names

UTC = timezone.utc
KICK = datetime(2026, 9, 5, 19, 30, tzinfo=UTC)


def _game(sport: str, away: str, home: str, kick: datetime = KICK, neutral: bool = False, week: int = 1) -> Game:
    return Game(
        game_id=make_game_id(sport, 2026, week, away, home), sport=sport, season=2026, week=week,
        kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id=home, away_id=away, stadium_id=None,
        neutral=neutral,
    )


def _ln(game_id: str, book: str, market: str, side: str, odds: int, line: float | None = None,
        is_main: bool = True, sport: str = "cfb", prob: float | None = None) -> GameLine:
    return GameLine(sport=sport, game_id=game_id, book=book, market=market, side=side, odds=odds,
                    line=line, is_main=is_main, prob_raw=prob)


@pytest.fixture(autouse=True)
def _fresh_register():
    reset_unresolved()
    yield
    reset_unresolved()


# ---- normalize_team -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Miami Florida", "miami-fl"),
    ("Miami FL", "miami-fl"),
    ("Miami (FL)", "miami-fl"),
    ("Miami Hurricanes", "miami-fl"),
    ("Miami", "miami-fl"),
    ("Miami Ohio", "miami-oh"),
    ("Miami (OH)", "miami-oh"),
    ("Miami OH RedHawks", "miami-oh"),
    ("Ohio", "ohio"),
    ("Ohio St", "ohio-state"),
    ("Sacramento St", "sacramento-state"),
    ("Sacramento St.", "sacramento-state"),
    ("sacramento-st", "sacramento-state"),          # betcris/betonline slug form
    ("Michigan State", "michigan-state"),
    ("Michigan St.", "michigan-state"),
    ("Appalachian St", "appalachian-state"),
    ("Florida Intl", "florida-international"),
    ("UConn", "connecticut"),
    ("TCU", "tcu"),
    ("UNC", "north-carolina"),
    ("SJSU", "san-jose-state"),
])
def test_cfb_aliases(raw, expected):
    assert normalize_team("cfb", raw, "test") == expected


def test_miami_never_crosses():
    assert normalize_team("cfb", "Miami Florida") != normalize_team("cfb", "Miami Ohio")
    assert normalize_team("cfb", "Miami (OH)") == "miami-oh"
    assert normalize_team("cfb", "Miami (FL)") == "miami-fl"


@pytest.mark.parametrize("raw,expected", [
    ("NYG", "nyg"), ("NYJ", "nyj"), ("SF", "sf"), ("LAR", "la"), ("LA", "la"), ("LAC", "lac"),
    ("JAX", "jax"), ("JAC", "jax"), ("WSH", "was"), ("LV", "lv"), ("KC", "kc"), ("TB", "tb"),
    ("N.Y. Giants", "nyg"), ("l.a. chargers", "lac"), ("san-francisco-49ers", "sf"),
])
def test_kalshi_nfl_abbreviations(raw, expected):
    assert normalize_team("nfl", raw, "kalshi") == expected


@pytest.mark.parametrize("raw,expected", [
    ("DEL", "delaware"),      # Delaware (fbs) beats Delta State (ii)
    ("FOR", "fordham"),       # Fordham (fcs) beats Fort Lewis / Fort Valley State (ii)
    ("HOW", "howard"),        # Howard (fcs) beats Howard Payne (iii)
    ("SOU", "southern"),      # Southern (fcs) beats Southern Arkansas / Southern Connecticut (ii)
    ("MER", None),            # Mercer / Merrimack / Mercyhurst are all fcs -> still ambiguous
])
def test_shared_abbreviation_resolves_to_highest_level(raw, expected):
    assert normalize_team("cfb", raw, "kalshi") == expected


@pytest.mark.parametrize("book,raw,expected", [
    ("fanduel", "Long Island", "long-island-university"),
    ("kalshi", "LIU", "long-island-university"),
    ("novig", "Long Island University", "long-island-university"),
    ("novig", "Long Island University Sharks", "long-island-university"),
    ("fanduel", "UTRGV", "ut-rio-grande-valley"),
    ("novig", "UT Rio Grande Valley", "ut-rio-grande-valley"),
    ("novig", "UT Rio Grande Valley Vaqueros", "ut-rio-grande-valley"),
    ("betcris", "UT Rio Grande", "ut-rio-grande-valley"),
    ("fanduel", "West Florida", "west-florida"),
    ("kalshi", "UWF", "west-florida"),
    ("novig", "West Florida Argonauts", "west-florida"),
])
def test_new_fcs_programs_resolve_without_fuzzy(book, raw, expected):
    # LIU (NEC), UTRGV (Southland, new 2025) and West Florida (UAC from 2026) paged OPS daily as unresolved
    assert normalize_team("cfb", raw, book, fuzzy=False) == expected
    assert unresolved_names("cfb") == []


def test_ambiguous_city_is_unresolved():
    assert normalize_team("nfl", "Los Angeles", "x") is None
    assert normalize_team("nfl", "New York", "x") is None
    assert "nfl|x|Los Angeles" in unresolved_names("nfl")


def test_fuzzy_fallback_difflib(monkeypatch):
    # conftest stubs rapidfuzz -> _ratio must fall back to difflib and still resolve typos
    assert teams_mod._ratio("abc", "abc") == 100.0
    assert normalize_team("cfb", "Nrth Carolina", "test") == "north-carolina"
    assert normalize_team("cfb", "Sacremento State", "test") == "sacramento-state"


def test_unresolved_logged_once_and_registered(caplog):
    with caplog.at_level("WARNING", logger="pipeline.odds.teams"):
        assert normalize_team("cfb", "Zzzz Polytechnic Univ", "novig") is None
        assert normalize_team("cfb", "Zzzz Polytechnic Univ", "novig") is None
    assert sum("Zzzz Polytechnic" in r.message for r in caplog.records) == 1
    assert unresolved_names("cfb") == ["cfb|novig|Zzzz Polytechnic Univ"]
    reset_unresolved("cfb")
    assert unresolved_names() == []


def test_blank_and_none():
    assert normalize_team("cfb", None) is None
    assert normalize_team("cfb", "  ") is None
    assert unresolved_names() == []


# ---- provisional ids --------------------------------------------------------------

def test_parse_provisional_shapes():
    a = parse_provisional("cfb:raw:20260905:sacramento-st@miami-florida", "betcris")
    assert (a.away, a.home, a.date_only) == ("sacramento-st", "miami-florida", True)
    assert a.kickoff_utc == datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    b = parse_provisional("nfl:raw:2026-09-13:DAL@NYG")
    assert (b.away, b.home, b.kickoff_utc.date().isoformat()) == ("DAL", "NYG", "2026-09-13")
    c = parse_provisional("cfb:raw:2026-09-05T19:30:TCU Horned Frogs@North Carolina Tar Heels")
    assert c.kickoff_utc == KICK and c.away == "TCU Horned Frogs" and not c.date_only
    d = parse_provisional("cfb:raw:unknown:Ohio@Ohio State")
    assert d.kickoff_utc is None
    assert parse_provisional("cfb:2026:1:ohio@ohio-state") is None


# ---- canonicalize: match + neutral flip + window ----------------------------------

def test_match_and_neutral_flip():
    # schedule says UNC (away) @ TCU (home) at a neutral site; the book lists TCU @ North Carolina
    games = [_game("cfb", "north-carolina", "tcu", neutral=True)]
    pid = "cfb:raw:20260905:tcu@north-carolina"
    lines = [
        _ln(pid, "betcris", "spread", "home", -110, -3.0),    # book "home" = North Carolina -3
        _ln(pid, "betcris", "spread", "away", -110, 3.0),
        _ln(pid, "betcris", "total", "over", -110, 51.5),
        _ln(pid, "betcris", "total", "under", -110, 51.5),
        _ln(pid, "betcris", "ml", "home", -150),
        _ln(pid, "betcris", "ml", "away", 130),
    ]
    res = canonicalize("cfb", lines, games)
    gid = games[0].game_id
    assert res.game_map == {pid: (gid, True)}
    assert res.unmatched == [] and res.unresolved == []
    by = {(ln.market, ln.side): ln for ln in res.lines}
    assert all(ln.game_id == gid for ln in res.lines)
    assert by[("spread", "away")].line == -3.0          # UNC is the schedule away side
    assert by[("spread", "home")].line == 3.0
    assert by[("ml", "away")].odds == -150 and by[("ml", "home")].odds == 130
    assert by[("total", "over")].line == 51.5           # totals untouched
    board = pivot(res.lines)
    assert board[gid]["betcris"]["spread"]["line"] == 3.0   # home-relative (TCU +3)
    assert board[gid]["betcris"]["total"]["line"] == 51.5


def test_direct_match_preferred_over_flip():
    games = [_game("cfb", "north-carolina", "tcu"), _game("cfb", "tcu", "north-carolina", KICK + timedelta(days=1), week=2)]
    pid = "cfb:raw:2026-09-05T19:30:North Carolina@TCU"
    res = canonicalize("cfb", [_ln(pid, "novig", "ml", "home", -120)], games)
    assert res.game_map[pid] == (games[0].game_id, False)


def test_kickoff_window_36h():
    games = [_game("cfb", "ohio", "ohio-state", KICK)]
    inside = "cfb:raw:2026-09-07T07:00:Ohio@Ohio State"       # +35.5 h
    outside = "cfb:raw:2026-09-07T09:00:Ohio@Ohio State"      # +37.5 h
    res = canonicalize("cfb", [_ln(inside, "pinnacle", "ml", "home", -300), _ln(outside, "pinnacle", "ml", "home", -300)], games)
    assert res.game_map == {inside: (games[0].game_id, False)}
    assert res.unmatched == [f"pinnacle|{outside}|no-schedule-match"]


def test_date_only_stamp_matches_and_picks_closest_meeting():
    early = _game("nfl", "dal", "nyg", datetime(2026, 9, 13, 20, 25, tzinfo=UTC), week=1)
    late = _game("nfl", "dal", "nyg", datetime(2026, 12, 6, 18, 0, tzinfo=UTC), week=14)
    pid = "nfl:raw:2026-12-06:DAL@NYG"
    res = canonicalize("nfl", [_ln(pid, "kalshi", "ml", "home", 110, sport="nfl", prob=0.47)], [early, late])
    assert res.game_map[pid] == (late.game_id, False)


def test_unresolved_team_drops_row_and_reports():
    games = [_game("cfb", "ohio", "ohio-state")]
    pid = "cfb:raw:20260905:zzz-tech@ohio-state"
    res = canonicalize("cfb", [_ln(pid, "betonline", "ml", "home", -500)], games)
    assert res.lines == []
    assert res.unmatched == [f"betonline|{pid}|no-schedule-match"]
    assert res.unresolved == ["cfb|betonline|zzz-tech"]


def test_raw_games_registry_overrides_slugged_ids():
    games = [_game("cfb", "miami-oh", "ohio-state")]
    pid = "cfb:raw:20260905:miami-ohio@ohio-state"
    raw = {pid: RawGame(book="betcris", game_id=pid, sport="cfb", away="Miami Ohio", home="Ohio State", kickoff_utc=KICK)}
    res = canonicalize("cfb", [_ln(pid, "betcris", "ml", "away", 900)], games, raw)
    assert res.game_map[pid] == (games[0].game_id, False)
    assert res.counts == {"betcris": {"ml": 1}}


# ---- main-line selection ----------------------------------------------------------

def test_select_main_prefers_is_main_then_consensus_then_balance():
    gid = "cfb:2026:1:ohio@ohio-state"
    ladder = [
        _ln(gid, "kalshi", "spread", "home", -110, -20.5, is_main=False, prob=0.52),
        _ln(gid, "kalshi", "spread", "away", -110, 20.5, is_main=False, prob=0.48),
        _ln(gid, "kalshi", "spread", "home", -105, -24.5, is_main=True, prob=0.50),
        _ln(gid, "kalshi", "spread", "away", -105, 24.5, is_main=True, prob=0.50),
        _ln(gid, "kalshi", "spread", "home", 150, -27.5, is_main=False, prob=0.40),
        _ln(gid, "kalshi", "spread", "away", -170, 27.5, is_main=False, prob=0.60),
    ]
    assert select_main(ladder)["home"].line == -24.5
    no_main = [_ln(x.game_id, x.book, x.market, x.side, x.odds, x.line, is_main=False, prob=x.prob_raw) for x in ladder]
    assert select_main(no_main)["home"].line == -24.5                 # most balanced
    assert select_main(no_main, consensus=-21.0)["home"].line == -20.5  # nearest consensus
    board = pivot(ladder + [
        _ln(gid, "pinnacle", "spread", "home", -108, -20.5), _ln(gid, "pinnacle", "spread", "away", -112, 20.5),
    ])
    assert board[gid]["kalshi"]["spread"]["line"] == -24.5   # is_main wins over consensus
    assert board[gid]["kalshi"]["spread"]["n_alt"] == 2
    assert board[gid]["pinnacle"]["spread"]["home"]["odds"] == -108


# ---- openers: stable across two runs ---------------------------------------------

def test_opener_stability_across_runs(tmp_path: Path):
    games = [_game("cfb", "north-carolina", "tcu", neutral=True), _game("cfb", "ohio", "ohio-state")]
    unc_tcu, osu = games[0].game_id, games[1].game_id
    p1 = "cfb:raw:2026-09-05T19:30:North Carolina@TCU"
    p2 = "cfb:raw:20260905:ohio@ohio-state"

    def run(fd_home: float, fd_total: float, t: datetime, extra: list[GameLine] | None = None):
        lines = [
            _ln(p1, "fanduel", "spread", "home", -110, fd_home), _ln(p1, "fanduel", "spread", "away", -110, -fd_home),
            _ln(p1, "fanduel", "total", "over", -105, fd_total), _ln(p1, "fanduel", "total", "under", -115, fd_total),
            _ln(p2, "betcris", "spread", "home", -110, -24.0), _ln(p2, "betcris", "spread", "away", -110, 24.0),
        ] + (extra or [])
        return merge_odds("cfb", games, lines, data_dir=tmp_path, now=t)

    t1 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    r1 = run(-3.0, 51.5, t1)
    assert r1.new_openers == 6
    assert (tmp_path / "openers.json").exists()
    assert opener_for(r1.openers, unc_tcu, "spread", "fanduel")["line"] == -3.0
    assert opener_for(r1.openers, unc_tcu, "total", "fanduel")["line"] == 51.5
    assert r1.board[unc_tcu]["fanduel"]["spread"]["line"] == -3.0

    # run 2: FanDuel moved, betcris unchanged, a new book appears (only its keys are new openers)
    r2 = run(-4.5, 53.0, t1 + timedelta(hours=6), extra=[_ln(p2, "pinnacle", "total", "over", -110, 44.5)])
    assert r2.new_openers == 1
    fd = opener_for(r2.openers, unc_tcu, "spread", "fanduel")
    assert fd["line"] == -3.0 and fd["home"]["ts"] == t1.isoformat()   # Fd_open unchanged
    assert opener_for(r2.openers, unc_tcu, "total", "fanduel")["line"] == 51.5
    assert r2.board[unc_tcu]["fanduel"]["spread"]["line"] == -4.5      # FD_now moved
    assert r2.board[unc_tcu]["fanduel"]["total"]["line"] == 53.0
    assert opener_for(r2.openers, osu, "total", "pinnacle")["line"] == 44.5
    assert opener_for(r2.openers, osu, "ml", "betcris") == {}

    # persisted file round-trips with schema_version and identical opener values
    saved = state_mod.load_openers(tmp_path)
    assert saved["schema_version"] == state_mod.SCHEMA_VERSION
    assert saved["openers"][state_mod.odds_key(unc_tcu, "spread", "home", "fanduel")]["line"] == -3.0


def test_openers_ignore_alternates_and_provisional_rows(tmp_path: Path):
    games = [_game("cfb", "ohio", "ohio-state")]
    gid = games[0].game_id
    lines = [
        _ln(gid, "kalshi", "total", "over", -110, 44.5, is_main=False, prob=0.5),
        _ln(gid, "kalshi", "total", "over", -110, 47.5, is_main=True, prob=0.5),
        _ln("cfb:raw:unknown:Nobody@Ohio State", "kalshi", "total", "over", -110, 40.5),
    ]
    res = merge_odds("cfb", games, lines, data_dir=tmp_path, save=False)
    assert res.new_openers == 1
    assert opener_for(res.openers, gid, "total", "kalshi")["line"] == 47.5
    assert not (tmp_path / "openers.json").exists()
    assert res.unmatched and res.unresolved == ["cfb|kalshi|Nobody"]


def test_candidate_team_ids_exposes_shared_aliases():
    assert merge_mod.candidate_team_ids("cfb", "MER") == ["mercer", "merchant-marine", "mercyhurst", "merrimack"]
    assert merge_mod.candidate_team_ids("cfb", "DEL") == ["delaware", "delta-state"]
    assert merge_mod.candidate_team_ids("cfb", "Ohio State") == ["ohio-state"]
    assert merge_mod.candidate_team_ids("cfb", "Zzzz Polytechnic") == []


def test_ambiguous_abbreviation_settled_by_schedule():
    """Kalshi 'MER @ DEL': teams.py alone returns None for MER (Mercer / Merrimack /
    Mercyhurst, all FCS); the schedule says Merrimack visits Delaware this week."""
    assert normalize_team("cfb", "MER", "kalshi") is None
    reset_unresolved("cfb")
    games = [_game("cfb", "merrimack", "delaware"), _game("cfb", "towson", "mercer", kick=KICK + timedelta(days=1))]
    pid = "cfb:raw:2026-09-05T19:30:MER@DEL"
    lines = [_ln(pid, "kalshi", "ml", "away", 250, prob=0.28), _ln(pid, "kalshi", "ml", "home", -300, prob=0.75)]
    res = canonicalize("cfb", lines, games)
    assert {ln.game_id for ln in res.lines} == {games[0].game_id}
    assert {ln.side for ln in res.lines} == {"away", "home"}      # sides kept: MER is the away side on both
    assert res.unmatched == [] and res.unresolved == []
    assert unresolved_names("cfb") == []                          # never registered as unresolved
    # swapped listing (DEL @ MER) still lands on the same game, flipped
    res2 = canonicalize("cfb", [_ln("cfb:raw:2026-09-05T19:30:DEL@MER", "kalshi", "ml", "away", -300)], games)
    assert [(ln.game_id, ln.side) for ln in res2.lines] == [(games[0].game_id, "home")]


def test_ambiguous_abbreviation_stays_unresolved_without_schedule_support():
    # Delaware plays this week, but not any MER candidate -> unresolved + registered, row dropped
    games = [_game("cfb", "towson", "delaware")]
    pid = "cfb:raw:2026-09-05T19:30:MER@DEL"
    res = canonicalize("cfb", [_ln(pid, "kalshi", "ml", "away", 250)], games)
    assert res.lines == [] and res.unmatched == [f"kalshi|{pid}|no-schedule-match"]
    assert res.unresolved == ["cfb|kalshi|MER"]
    # the resolved side has no schedule game in the window at all (FCS-vs-FCS listing
    # under a division=fbs schedule, or a meeting a week away): dropped quietly, never
    # registered — this is the live Kalshi case (PRE@MER, ETAM@MER = Mercer home games)
    reset_unresolved("cfb")
    far = [_game("cfb", "merrimack", "delaware", kick=KICK + timedelta(days=7))]
    res = canonicalize("cfb", [_ln(pid, "kalshi", "ml", "away", 250)], far)
    assert res.lines == [] and res.unmatched == [f"kalshi|{pid}|no-schedule-match"]
    assert res.unresolved == [] and unresolved_names("cfb") == []
    res = canonicalize("cfb", [_ln("cfb:raw:2026-09-05:PRE@MER", "kalshi", "ml", "away", 250)], games)
    assert res.lines == [] and res.unresolved == [] and unresolved_names("cfb") == []
    # two candidates both meet Delaware inside the window -> still ambiguous
    reset_unresolved("cfb")
    both = [_game("cfb", "merrimack", "delaware"), _game("cfb", "delaware", "mercer", kick=KICK + timedelta(hours=6), week=2)]
    res = canonicalize("cfb", [_ln(pid, "kalshi", "ml", "away", 250)], both)
    assert res.lines == [] and res.unresolved == ["cfb|kalshi|MER"]
    # both sides ambiguous: nothing to anchor on
    reset_unresolved("cfb")
    res = canonicalize("cfb", [_ln("cfb:raw:2026-09-05T19:30:MER@MER", "kalshi", "ml", "away", 250)], games)
    assert res.lines == [] and res.unresolved == ["cfb|kalshi|MER"]


def test_horizon_schedule_matches_the_meeting_the_stamp_points_at():
    """The odds horizon hands the matcher several weeks at once: an NFL divisional pair
    meets twice, and a row stamped for the later meeting must land on that game_id
    (never on the earlier one), with the earlier meeting's openers left untouched."""
    first = _game("nfl", "buf", "kc", kick=KICK, week=1)
    rematch = _game("nfl", "buf", "kc", kick=KICK + timedelta(days=28), week=5)
    stamp = (KICK + timedelta(days=28)).strftime("%Y%m%d")
    lines = [
        _ln(f"nfl:raw:{stamp}:buffalo-bills@kansas-city-chiefs", "betonline", "total", "under", -110, 44.5, sport="nfl"),
        _ln(f"nfl:raw:{KICK.strftime('%Y%m%d')}:buffalo-bills@kansas-city-chiefs", "betonline", "total", "under", -110, 47.0, sport="nfl"),
    ]
    openers = state_mod.migrate(None, "openers")
    res = merge_odds("nfl", [first, rematch], lines, openers=openers, now=KICK - timedelta(days=20), save=False)
    by_game = {ln.game_id: ln.line for ln in res.lines}
    assert by_game == {rematch.game_id: 44.5, first.game_id: 47.0}
    assert res.unmatched == []
    assert openers["openers"][f"{rematch.game_id}|total|under|betonline"]["line"] == 44.5
    assert openers["openers"][f"{first.game_id}|total|under|betonline"]["line"] == 47.0


def test_merge_result_from_scraper_registry():
    class G:
        def __init__(self):
            self.game_id = "cfb:raw:20260905:miami-florida@sacramento-st"
            self.away, self.home, self.kickoff_utc, self.neutral = "Miami Florida", "Sacramento St", KICK, True

    class S:
        last_games = [G()]

    reg = merge_mod.raw_games_from_scraper(S(), "betcris", "cfb")
    rg = reg[G().game_id]
    assert (rg.away, rg.home, rg.neutral, rg.kickoff_utc) == ("Miami Florida", "Sacramento St", True, KICK)
