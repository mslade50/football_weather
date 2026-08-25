"""Odds orchestration in pipeline/build.py: scope->books, gather with
return_exceptions, provisional-id matching, consensus, legacy odds columns, openers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline import build
from pipeline import state as pstate
from pipeline.build import (
    BOOK_ORDER,
    HTTPX_BOOKS,
    books_for_scope,
    consensus_lines,
    fallback_match,
    legacy_odds,
    parse_provisional,
    scrape_books,
    weighted_median,
)
from pipeline.contracts import Game, GameLine

KC_BUF = "nfl:2026:1:buf@kc"
KICK = datetime(2026, 9, 13, 20, 25, tzinfo=timezone.utc)


def _game(game_id: str = KC_BUF, away: str = "buf", home: str = "kc", kick: datetime = KICK, neutral: bool = False) -> Game:
    return Game(game_id=game_id, sport="nfl", season=2026, week=1, kickoff_utc=kick, kickoff_local=kick,
                tz="America/Chicago", home_id=home, away_id=away, stadium_id="arrowhead", neutral=neutral)


def _ln(book: str, market: str, side: str, line: float | None, odds: int = -110, game_id: str = KC_BUF, main: bool = True) -> GameLine:
    return GameLine(sport="nfl", game_id=game_id, book=book, market=market, side=side, odds=odds, line=line, is_main=main)


class _Book:
    """Minimal StadiumBook stand-in: resolve_team via a fixed alias table."""

    aliases = {"buffalo bills": "buf", "bills": "buf", "buffalo": "buf", "kansas city chiefs": "kc", "chiefs": "kc",
               "kansas city": "kc", "kc": "kc", "buf": "buf"}

    def resolve_team(self, sport: str, raw: str) -> str | None:
        return self.aliases.get(raw.strip().lower())


# ---- scope / books --------------------------------------------------------------

def test_books_for_scope():
    assert books_for_scope("weather") == []
    assert books_for_scope("light") == list(HTTPX_BOOKS)
    assert "betonline" not in books_for_scope("light")
    assert books_for_scope("full") == list(BOOK_ORDER)
    assert books_for_scope("odds", ["betonline"]) == ["betonline"]
    assert books_for_scope("odds") == list(BOOK_ORDER)
    assert books_for_scope("light", ["betonline", "pinnacle"]) == ["pinnacle"]  # light never runs Playwright
    with pytest.raises(ValueError):
        books_for_scope("full", ["nosuchbook"])


# ---- gather with return_exceptions -----------------------------------------------

class _Ok:
    BOOK_NAME = "ok"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None) -> None:
        self.raw_store = raw_store

    async def scrape_with_retry(self, sport: str, market: str | None = None, **kw: Any) -> list[GameLine]:
        return [_ln("ok", "total", "over", 47.5)]


class _Boom:
    BOOK_NAME = "boom"

    def __init__(self) -> None:  # no ctor kwargs at all
        pass

    async def scrape_with_retry(self, sport: str, market: str | None = None, **kw: Any) -> list[GameLine]:
        raise RuntimeError("site changed")


def test_scrape_books_isolates_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build, "load_scraper_class", lambda name: {"ok": _Ok, "boom": _Boom, "missing": None}.get(name))
    degs: list[tuple[str, str, str]] = []
    res, scrapers = build._run_async(scrape_books("nfl", ["ok", "boom", "missing"], None, "r1", lambda c, r, s: degs.append((c, r, s))))
    assert [ln.book for ln in res["ok"]] == ["ok"]
    assert set(scrapers) == {"ok", "boom"}
    assert res["boom"] == []
    assert "missing" not in res
    assert any("boom failed: RuntimeError: site changed" in r for _, r, _ in degs)
    assert any("missing unavailable" in r for _, r, _ in degs)
    assert all(s == "warn" for _, _, s in degs)


# ---- provisional id matching --------------------------------------------------------

def test_parse_provisional():
    assert parse_provisional("nfl:raw:20260913:buffalo-bills@kansas-city-chiefs") == ("20260913", "buffalo-bills", "kansas-city-chiefs")
    assert parse_provisional("cfb:raw:2026-08-29:Iowa State@Kansas State") == ("2026-08-29", "Iowa State", "Kansas State")
    assert parse_provisional(KC_BUF) is None


def test_fallback_match_maps_ids_and_flips_swapped_sides():
    games = [_game()]
    lines = [
        _ln("betonline", "spread", "home", -2.5, game_id="nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"),
        _ln("betonline", "spread", "away", 2.5, game_id="nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"),
        _ln("fanduel", "total", "under", 48.5, game_id="nfl:raw:2026-09-13:Buffalo Bills@Kansas City Chiefs"),
        # book lists the pairing the other way round (neutral / display order): sides flip
        _ln("pinnacle", "spread", "home", 2.5, game_id="nfl:raw:2026-09-13T20:25:Kansas City Chiefs@Buffalo Bills"),
        _ln("pinnacle", "total", "over", 48.0, game_id="nfl:raw:2026-09-13T20:25:Kansas City Chiefs@Buffalo Bills"),
        # too far from kickoff: outside the schedule span -> dropped silently, not "unresolved"
        _ln("betcris", "total", "over", 40.0, game_id="nfl:raw:20260920:buffalo-bills@kansas-city-chiefs"),
        # unknown team
        _ln("betcris", "total", "over", 40.0, game_id="nfl:raw:20260913:nowhere-nobodies@kansas-city-chiefs"),
        _ln("novig", "ml", "home", None, game_id=KC_BUF),  # already canonical
    ]
    matched, unresolved = fallback_match("nfl", games, lines, _Book())
    ids = {ln.game_id for ln in matched}
    assert ids == {KC_BUF}
    by = {(ln.book, ln.market, ln.side): ln.line for ln in matched}
    assert by[("betonline", "spread", "home")] == -2.5
    assert by[("fanduel", "total", "under")] == 48.5
    assert by[("pinnacle", "spread", "away")] == 2.5      # flipped home->away, line kept with the team
    assert by[("pinnacle", "total", "over")] == 48.0      # totals never flip
    assert by[("novig", "ml", "home")] is None
    assert unresolved == ["betcris:nowhere-nobodies@kansas-city-chiefs"]


# ---- consensus ------------------------------------------------------------------------

def test_weighted_median():
    assert weighted_median([(1.0, 1), (2.0, 1), (3.0, 1)]) == 2.0
    assert weighted_median([(1.0, 1), (10.0, 5)]) == 10.0
    assert weighted_median([]) is None


def test_consensus_is_weighted_median_with_ref_book():
    lines = [
        _ln("pinnacle", "spread", "home", -3.0, -108), _ln("pinnacle", "spread", "away", 3.0, -112),
        _ln("betonline", "spread", "home", -2.5, -110),
        _ln("betcris", "spread", "home", -2.5, -105),
        _ln("kalshi", "spread", "home", -2.5, -102),
        _ln("pinnacle", "total", "under", 47.5, -105), _ln("betonline", "total", "under", 47.0, -110),
        _ln("betonline", "total", "under", 46.0, -130, main=False),  # alternate ignored
        _ln("novig", "ml", "home", None, -150),
    ]
    c = consensus_lines("nfl", lines)
    sp = c[(KC_BUF, "spread")]
    assert sp.line == -2.5 and sp.n_books == 4 and sp.ref_book == "betonline" and sp.odds == -110 and sp.side == "home"
    to = c[(KC_BUF, "total")]
    assert to.line == 47.5 and to.ref_book == "pinnacle" and to.n_books == 2 and to.side == "under"
    assert (KC_BUF, "ml") not in c


# ---- legacy odds columns ------------------------------------------------------------------

def _by_game(lines: list[GameLine]) -> dict[str, list[GameLine]]:
    out: dict[str, list[GameLine]] = {}
    for ln in lines:
        out.setdefault(ln.game_id, []).append(ln)
    return out


def test_nfl_legacy_odds_from_betonline_with_openers():
    lines = [
        _ln("betonline", "spread", "home", -2.5, -115), _ln("betonline", "total", "under", 47.0, -108),
        _ln("pinnacle", "spread", "home", -3.0, -108), _ln("pinnacle", "total", "under", 47.5, -105),
    ]
    cons = consensus_lines("nfl", lines)
    openers = pstate.migrate(None, "openers")
    pstate.record_openers(openers, [_ln("betonline", "spread", "home", -1.5, -110), _ln("betonline", "total", "under", 48.5, -110)], "t0")
    o = legacy_odds("nfl", KC_BUF, _by_game(lines), cons, openers)
    assert (o["spread_now"], o["odds_now"], o["total_now"], o["under_now"]) == (-2.5, -115, 47.0, -108)
    assert (o["spread_open"], o["odds_open"], o["total_open"], o["under_open"]) == (-1.5, -110, 48.5, -110)
    assert o["ref_book"] == "betonline"
    assert o["spread"] == -3.0 or o["spread"] == -2.5  # weighted median of {-3 (3), -2.5 (2)} -> -3.0
    assert o["spread"] == -3.0 and o["total_proj"] == 47.5
    assert o["n_books"] == 2


def test_nfl_legacy_odds_fall_back_to_consensus_when_betonline_missing():
    lines = [_ln("pinnacle", "spread", "home", -3.0, -108), _ln("betcris", "total", "under", 47.5, -105)]
    cons = consensus_lines("nfl", lines)
    openers = pstate.migrate(None, "openers")
    pstate.record_openers(openers, build.consensus_pseudo_lines(cons, "nfl"), "t0")
    o = legacy_odds("nfl", KC_BUF, _by_game(lines), cons, openers)
    assert o["ref_book"] == "consensus"
    assert (o["spread_now"], o["odds_now"], o["total_now"], o["under_now"]) == (-3.0, -108, 47.5, -105)
    assert (o["spread_open"], o["total_open"]) == (-3.0, 47.5)
    assert o["spread_ref_book"] == "pinnacle" and o["total_ref_book"] == "betcris"


def test_cfb_legacy_odds_from_fanduel():
    gid = "cfb:2026:1:iowa-state@kansas-state"

    def ln(book: str, market: str, side: str, line: float, odds: int = -110) -> GameLine:
        return GameLine(sport="cfb", game_id=gid, book=book, market=market, side=side, odds=odds, line=line)

    lines = [ln("fanduel", "total", "under", 51.5, -112), ln("fanduel", "spread", "home", -3.5, -108),
             ln("pinnacle", "total", "under", 52.0), ln("pinnacle", "spread", "home", -3.0)]
    cons = consensus_lines("cfb", lines)
    openers = pstate.migrate(None, "openers")
    pstate.record_openers(openers, [ln("fanduel", "total", "under", 50.5, -110), ln("fanduel", "spread", "home", -2.5, -110)], "t0")
    o = legacy_odds("cfb", gid, _by_game(lines), cons, openers)
    assert (o["fd_now"], o["odds_n"], o["fd_open"], o["odds_o"]) == (51.5, -112, 50.5, -110)
    assert (o["current"], o["open"]) == (-3.5, -2.5)
    assert (o["spread"], o["total_proj"]) == (-3.0, 52.0)  # pinnacle weight 3 beats fanduel 1
    assert "spread_now" not in o


def test_legacy_odds_empty_when_no_lines():
    o = legacy_odds("nfl", KC_BUF, {}, {}, pstate.migrate(None, "openers"))
    assert o["spread_now"] is None and o["total_proj"] is None and o["ref_book"] is None


# ---- stage_odds end-to-end (scrapers monkeypatched) -----------------------------------------

def test_stage_odds_persists_openers_and_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prov = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"
    calls = {"n": 0}

    async def fake_scrape(sport, books, raw, run_id, degrade):
        calls["n"] += 1
        spread = -2.5 if calls["n"] == 1 else -3.5
        return {"betonline": [_ln("betonline", "spread", "home", spread, game_id=prov),
                              _ln("betonline", "total", "under", 47.0, game_id=prov)],
                "pinnacle": [_ln("pinnacle", "spread", "home", -3.0, game_id=prov)]}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "_send_alert", lambda text: True)
    # concurrent modules may or may not exist: force the built-in paths for determinism
    monkeypatch.setattr(build, "_import", lambda name: None if name in ("pipeline.odds.merge", "pipeline.model.fair") else __import__(name, fromlist=["_"]))
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    games = [_game()]
    res = build.stage_odds(ctx, "nfl", games, _Book(), NullRawStore("nfl", "r"), ["betonline", "pinnacle"], tmp_path, 2026)
    assert {ln.game_id for ln in res.lines} == {KC_BUF}
    assert res.per_book == {"betonline": 2, "pinnacle": 1}
    assert (tmp_path / "openers.json").exists() and (tmp_path / "scrape_baseline.json").exists() and (tmp_path / "archive_last.json").exists()
    o1 = legacy_odds("nfl", KC_BUF, res.by_game, res.consensus, res.openers)
    assert (o1["spread_now"], o1["spread_open"]) == (-2.5, -2.5)

    # second run: line moved, opener stays
    ctx2 = RunContext(sport="nfl", scope="full", git_sha="t")
    res2 = build.stage_odds(ctx2, "nfl", games, _Book(), NullRawStore("nfl", "r"), ["betonline", "pinnacle"], tmp_path, 2026)
    o2 = legacy_odds("nfl", KC_BUF, res2.by_game, res2.consensus, res2.openers)
    assert (o2["spread_now"], o2["spread_open"]) == (-3.5, -2.5)
    assert ctx2.counts["betonline"]["nfl"] == 2
    # baseline scope keys on the weather-window games ('nfl:2026:1' once the game is
    # inside 10 days, 'nfl:2026:?' while it is only on the odds horizon)
    scope = build.baseline_scope("nfl", [g for g in games if build.in_window(g.kickoff_utc, ctx2.now_utc)], games, 2026)
    bl = pstate.load_baseline(tmp_path, scope)
    assert bl["peaks"]["betonline|spread"] == 1


def test_stage_odds_tolerates_missing_merge_and_fair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fake_scrape(sport, books, raw, run_id, degrade):
        return {"pinnacle": []}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    real_import = build._import
    monkeypatch.setattr(build, "_import", lambda name: None if name in ("pipeline.odds.merge", "pipeline.model.fair") else real_import(name))
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.stage_odds(ctx, "nfl", [_game()], _Book(), NullRawStore("nfl", "r"), ["pinnacle"], tmp_path, 2026, dry_run=True)
    assert res.lines == []
    comps = {d.component: d.severity for d in ctx.degradations}
    assert comps.get("odds.merge") == "info" and comps.get("model.fair") == "info"
    assert not (tmp_path / "openers.json").exists()  # dry-run writes no state


def test_external_merge_and_fair_real_modules():
    """The concurrent modules exist: merge_odds re-keys provisional ids via data/aliases,
    fair.consensus gives the Pinnacle-weighted line; both feed the same ConsensusLine shape."""
    merge_mod = pytest.importorskip("pipeline.odds.merge")
    fair_mod = pytest.importorskip("pipeline.model.fair")
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    prov = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"
    raw_lines = [_ln("pinnacle", "spread", "home", -3.0, -108, game_id=prov), _ln("pinnacle", "spread", "away", 3.0, -112, game_id=prov),
                 _ln("betcris", "spread", "home", -2.5, -110, game_id=prov), _ln("betcris", "total", "under", 47.5, -105, game_id=prov),
                 # next week's card: outside the span, must not count as unresolved
                 _ln("betcris", "total", "under", 40.0, game_id="nfl:raw:20260927:buffalo-bills@kansas-city-chiefs")]
    openers = pstate.migrate(None, "openers")
    merged = build._external_merge(merge_mod, "nfl", [_game()], raw_lines, {}, openers, ctx)
    assert merged is not None
    lines, unresolved = merged
    assert {ln.game_id for ln in lines} == {KC_BUF} and len(lines) == 4
    assert unresolved == []
    cons = build._external_consensus(fair_mod, "nfl", lines, ctx)
    assert cons is not None
    sp = cons[(KC_BUF, "spread")]
    assert sp.line == -3.0 and sp.ref_book == "pinnacle" and sp.odds == -108 and sp.n_books == 2
    to = cons[(KC_BUF, "total")]
    assert to.line == 47.5 and to.odds == -105 and to.side == "under"
    assert not any(d.severity == "warn" for d in ctx.degradations)


def test_external_merge_failure_falls_back(monkeypatch: pytest.MonkeyPatch):
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="full", git_sha="t")

    def bad(*a, **kw):
        raise KeyError("boom")

    assert build._external_merge(SimpleNamespace(merge_odds=bad), "nfl", [_game()], [], {}, {}, ctx) is None
    assert any(d.component == "odds.merge" and d.severity == "warn" for d in ctx.degradations)
    assert build._external_merge(SimpleNamespace(), "nfl", [_game()], [], {}, {}, ctx) is None
    assert build._external_consensus(SimpleNamespace(), "nfl", [], ctx) is None


def test_carry_forward_lines_only_for_unscraped_books():
    archive = pstate.migrate(None, "archive_last")
    last = archive["last"]
    last[f"{KC_BUF}|total|under|fanduel"] = {"line": 47.5, "odds": -108, "ts": "t"}
    last[f"{KC_BUF}|spread|home|betonline"] = {"line": -2.5, "odds": -110, "ts": "t"}   # scraped now: not carried
    last[f"{KC_BUF}|total|under|consensus"] = {"line": 47.5, "odds": -110, "ts": "t"}   # pseudo book: never
    last["nfl:2026:1:den@lv|total|under|fanduel"] = {"line": 40.0, "odds": -110, "ts": "t"}  # inactive game
    last["cfb:2026:1:a@b|total|under|fanduel"] = {"line": 50.0, "odds": -110, "ts": "t"}    # other sport
    last[f"{KC_BUF}|ml|home|kalshi"] = {"line": None, "odds": None, "ts": "t"}              # no odds
    got = build.carry_forward_lines(archive, "nfl", {KC_BUF}, ["betonline"])
    assert [(ln.book, ln.market, ln.side, ln.line, ln.odds, ln.is_main) for ln in got] == [("fanduel", "total", "under", 47.5, -108, True)]


def test_playwright_style_run_keeps_fanduel_now_via_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """light run (fanduel) then odds run (betonline only): CFB FD_now still comes from
    FanDuel's last snapshot, and the consensus sees both books."""
    gid = "cfb:2026:1:iowa-state@kansas-state"
    kick = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    game = Game(game_id=gid, sport="cfb", season=2026, week=1, kickoff_utc=kick, kickoff_local=kick, tz="America/Chicago",
                home_id="kansas-state", away_id="iowa-state", stadium_id="x")

    def ln(book: str, market: str, side: str, line: float, odds: int = -110) -> GameLine:
        return GameLine(sport="cfb", game_id=gid, book=book, market=market, side=side, odds=odds, line=line)

    runs = iter([
        {"fanduel": [ln("fanduel", "total", "under", 51.5, -112), ln("fanduel", "spread", "home", -3.5)]},
        {"betonline": [ln("betonline", "total", "under", 52.5, -105), ln("betonline", "spread", "home", -3.0)]},
    ])

    async def fake_scrape(sport, books, raw, run_id, degrade):
        return next(runs), {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "_send_alert", lambda text: True)
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    r1 = build.stage_odds(RunContext(sport="cfb", git_sha="t"), "cfb", [game], _Book(), NullRawStore("cfb", "r"), ["fanduel"], tmp_path, 2026)
    o1 = legacy_odds("cfb", gid, r1.by_game, r1.consensus, r1.openers)
    assert (o1["fd_now"], o1["fd_open"], o1["ref_book"]) == (51.5, 51.5, "fanduel")

    r2 = build.stage_odds(RunContext(sport="cfb", git_sha="t"), "cfb", [game], _Book(), NullRawStore("cfb", "r"), ["betonline"], tmp_path, 2026)
    o2 = legacy_odds("cfb", gid, r2.by_game, r2.consensus, r2.openers)
    assert (o2["fd_now"], o2["odds_n"], o2["fd_open"], o2["ref_book"]) == (51.5, -112, 51.5, "fanduel")
    assert r2.consensus[(gid, "total")].n_books == 2
    assert {ln.book for ln in r2.lines} == {"betonline", "fanduel"}


# ---- odds horizon vs weather window ------------------------------------------------------

def test_split_schedule_window_is_subset_of_odds_horizon():
    from datetime import timedelta

    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    near = _game("nfl:2026:1:buf@kc", kick=now + timedelta(days=3))
    far = _game("nfl:2026:3:kc@buf", away="kc", home="buf", kick=now + timedelta(days=20))
    beyond = _game("nfl:2026:8:buf@kc", kick=now + timedelta(days=build.ODDS_WINDOW_AFTER_D + 1))
    played = _game("nfl:2026:0:buf@kc", kick=now - timedelta(hours=7))
    window, horizon = build.split_schedule([played, beyond, far, near], now)
    assert [g.game_id for g in window] == [near.game_id]
    assert [g.game_id for g in horizon] == [far.game_id, near.game_id]
    assert build.in_odds_horizon(far.kickoff_utc, now) and not build.in_window(far.kickoff_utc, now)


def test_entered_window_ids_and_board_new_opener_keys():
    from datetime import timedelta

    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    entering = _game("nfl:2026:1:buf@kc", kick=now + timedelta(days=9, hours=20))     # entry ~4 h ago
    settled = _game("nfl:2026:1:kc@buf", away="kc", home="buf", kick=now + timedelta(days=5))  # entered days ago
    prev = now - timedelta(hours=6)
    assert build.entered_window_ids([entering, settled], prev, now) == {entering.game_id}
    assert build.entered_window_ids([entering, settled], None, now) == {entering.game_id, settled.game_id}

    openers = pstate.migrate(None, "openers")
    pstate.record_openers(openers, [_ln("betonline", "total", "under", 47.0, game_id=entering.game_id),
                                    _ln("betonline", "total", "under", 44.0, game_id=settled.game_id),
                                    _ln("consensus", "total", "under", 47.0, game_id=entering.game_id)], "t0")
    odds = build.OddsResult([], {}, openers, {}, [], {}, new_opener_keys=[f"{settled.game_id}|spread|home|pinnacle"])
    res = build.SportResult("nfl", [], [], [], [entering, settled], {}, {}, {}, {}, odds)
    keys = build.board_new_opener_keys(res, prev, now)
    # this run's genuinely-new key + every opener of the game that just entered the window
    assert keys == sorted([f"{settled.game_id}|spread|home|pinnacle", f"{entering.game_id}|total|under|betonline",
                           f"{entering.game_id}|total|under|consensus"])


def test_previous_run_finished_reads_state_status(tmp_path: Path):
    from pipeline.outputs import json_out

    assert build.previous_run_finished(tmp_path, "nfl") is None
    json_out.dump_json(tmp_path / json_out.STATUS_FILE, {"runs": [
        {"run_id": "r3", "sport": "cfb", "finished_at": "2026-09-01T12:00:00Z"},
        {"run_id": "r2", "sport": "all", "finished_at": "2026-09-01T06:00:00Z"},
    ]})
    assert build.previous_run_finished(tmp_path, "nfl") == datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    assert build.previous_run_finished(tmp_path, "cfb") == datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class _NoVenueBook(_Book):
    stadiums: dict[str, Any] = {}

    def resolve(self, game: Game, ctx: Any) -> None:
        return None


def test_run_sport_records_openers_and_history_for_horizon_game_without_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A game 20 days out is matched for odds (openers, history, archive_last, D1 games row)
    but gets no forecast / card / legacy row; the 3-day game gets both."""
    from datetime import timedelta

    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    now = datetime.now(timezone.utc)
    near = _game("nfl:2026:1:buf@kc", kick=now + timedelta(days=3))
    far = _game("nfl:2026:3:kc@buf", away="kc", home="buf", kick=now + timedelta(days=20))
    stamp_near, stamp_far = near.kickoff_utc.strftime("%Y%m%d"), far.kickoff_utc.strftime("%Y%m%d")
    prov_near = f"nfl:raw:{stamp_near}:buffalo-bills@kansas-city-chiefs"
    prov_far = f"nfl:raw:{stamp_far}:kansas-city-chiefs@buffalo-bills"

    async def fake_scrape(sport, books, raw, run_id, degrade):
        return {"betonline": [_ln("betonline", "spread", "home", -2.5, game_id=prov_near), _ln("betonline", "total", "under", 47.0, game_id=prov_near),
                              _ln("betonline", "spread", "home", 3.0, game_id=prov_far), _ln("betonline", "total", "under", 41.5, game_id=prov_far)],
                "pinnacle": [_ln("pinnacle", "total", "under", 47.5, game_id=prov_near), _ln("pinnacle", "total", "under", 41.0, game_id=prov_far)]}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "_send_alert", lambda text: True)
    monkeypatch.setattr(build, "stage_stadiums", lambda ctx, sport: _NoVenueBook())
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [near, far])
    monkeypatch.setattr(build, "stage_weather", lambda *a, **kw: {})
    real_import = build._import
    monkeypatch.setattr(build, "_import", lambda name: None if name in ("pipeline.odds.merge", "pipeline.model.fair") else real_import(name))

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.run_sport(ctx, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline", "pinnacle"], state_dir=tmp_path, alerts=False)

    assert [g.game_id for g in res.games] == [near.game_id]
    assert {g.game_id for g in res.odds_games} == {near.game_id, far.game_id}
    assert [c["game_id"] for c in res.cards] == [near.game_id]          # no card for the horizon game
    assert len(res.records) == 1 and res.records[0].game_id == near.game_id
    assert {ln.game_id for ln in res.odds.lines} == {near.game_id, far.game_id}
    store = res.odds.openers["openers"]
    assert store[f"{far.game_id}|total|under|betonline"]["line"] == 41.5
    # consensus pseudo-book opener too: pinnacle 41.0 (w3) beats betonline 41.5 (w2)
    assert store[f"{far.game_id}|total|under|consensus"]["line"] == 41.0
    assert res.odds.consensus[(far.game_id, "total")].line == 41.0
    assert ctx.counts["schedule"] == {"nfl": 1, "nfl.odds": 2} and ctx.counts["odds_games"]["nfl"] == 2
    assert {g.game_id for g in res.d1_games} == {near.game_id, far.game_id}
    hist = pstate.load_history(tmp_path)
    assert f"{far.game_id}|total|under|betonline" in hist["series"] and f"{near.game_id}|spread|home|betonline" in hist["series"]
    archive = pstate.load_archive_last(tmp_path)
    assert f"{far.game_id}|spread|home|betonline" in archive["last"]
    assert comp_sev(ctx, "schedule") is None   # window populated: no schedule degradation

    # D1: the horizon game is upserted with NULL impact columns so odds_history joins work
    stmts = build.d1_statements(ctx, [res], now)
    games_sql = "\n".join(s for s in stmts if s.startswith("INSERT INTO games"))
    assert near.game_id in games_sql and far.game_id in games_sql
    assert any(far.game_id in s for s in stmts if s.startswith("INSERT OR IGNORE INTO odds_history"))


def comp_sev(ctx: Any, component: str) -> str | None:
    return next((d.severity for d in ctx.degradations if d.component == component), None)


def test_run_sport_preseason_is_lines_only_and_info_not_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Nothing inside the weather window but week-1 lines on the odds horizon: openers are
    recorded, no cards, and the empty window is an ``info`` (not ``warn``) degradation."""
    from datetime import timedelta

    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    now = datetime.now(timezone.utc)
    far = _game("nfl:2026:1:buf@kc", kick=now + timedelta(days=17))
    prov = f"nfl:raw:{far.kickoff_utc.strftime('%Y%m%d')}:buffalo-bills@kansas-city-chiefs"

    async def fake_scrape(sport, books, raw, run_id, degrade):
        return {"betonline": [_ln("betonline", "total", "under", 47.0, game_id=prov)]}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "_send_alert", lambda text: True)
    monkeypatch.setattr(build, "stage_stadiums", lambda ctx, sport: _NoVenueBook())
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [far])
    monkeypatch.setattr(build, "stage_weather", lambda *a, **kw: {})
    real_import = build._import
    monkeypatch.setattr(build, "_import", lambda name: None if name in ("pipeline.odds.merge", "pipeline.model.fair") else real_import(name))

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.run_sport(ctx, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline"], state_dir=tmp_path, alerts=False)
    assert res.games == [] and res.cards == [] and [g.game_id for g in res.odds_games] == [far.game_id]
    assert f"{far.game_id}|total|under|betonline" in res.odds.openers["openers"]
    assert comp_sev(ctx, "schedule") == "info"
    assert res.season_week == (2026, 1)      # meta season/week falls back to the odds horizon
    # volume baseline keeps week '?' until a game enters the window (fresh peaks for week 1)
    assert build.baseline_scope("nfl", res.games, res.odds_games) == "nfl:2026:?"
    assert build.baseline_scope("nfl", res.odds_games, res.odds_games) == "nfl:2026:1"
    assert pstate.load_baseline(tmp_path, "nfl:2026:?")["peaks"]["betonline|total"] == 1
    # nothing scheduled anywhere -> warn, as before
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [])
    ctx2 = RunContext(sport="nfl", scope="light", git_sha="t")
    build.run_sport(ctx2, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline"], state_dir=tmp_path, alerts=False)
    assert comp_sev(ctx2, "schedule") == "warn"


def test_alerts_stdout_flag_parses_and_print_sender_is_console_safe(capsys: pytest.CaptureFixture[str]):
    from pipeline import alerts as alerts_mod

    args = build.parse_args(["--sport", "nfl", "--alerts-stdout"])
    assert args.alerts_stdout is True and args.alerts is True
    assert build.parse_args(["--sport", "nfl"]).alerts_stdout is False
    assert alerts_mod.print_sender()("⚠ <b>x</b>\nline2", None) is True
    out = capsys.readouterr().out
    assert "[alert] chat=-" in out and "line2" in out
