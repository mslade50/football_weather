"""Odds orchestration in pipeline/build.py: scope->books, gather with
return_exceptions, provisional-id matching, consensus, board fair inputs, openers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    card_odds,
    scrape_books,
)
from pipeline.contracts import Game, GameLine, WeatherForecast
from pipeline.model import signals
from pipeline.odds.base import BaseScraper
from pipeline.run_context import RunContext
from pipeline.stadiums.loader import load_stadium_book

KC_BUF = "nfl:2026:1:buf@kc"
KICK = datetime(2026, 9, 13, 20, 25, tzinfo=timezone.utc)


@pytest.mark.parametrize("sport,venue,roof", [
    ("nfl", "us-bank-stadium", "outdoors"),
    ("nfl", "nrg-stadium", None),
    ("nfl", "att-stadium", "closed"),
    ("cfb", "carrier-dome", None),
    ("cfb", "allegiant-stadium", None),
    ("cfb", "alamodome", None),
    ("cfb", "fargodome", None),
    ("cfb", "superior-dome", None),
    ("cfb", "mercedes-benz-stadium", None),
])
@pytest.mark.parametrize("temp,wind,rain", [(41, 25, 5), (90, 10, 0)])
def test_covered_venues_never_generate_outdoor_weather_signals(sport, venue, roof, temp, wind, rain):
    game = replace(_game(), sport=sport, stadium_id=venue, roof_state=roof)
    rg = load_stadium_book().resolve(game)
    fc = WeatherForecast(game_id=game.game_id, source="test", temp_fg=temp, wind_fg=wind,
                         rain_fg_mm=rain, roof_state=roof)
    rec, impact, sig, flags = build.build_record(
        sport, game, rg.stadium, None, None, fc, roof_state=rg.roof_state,
        signal_open_spread=0, home_temp=45, away_temp=45, travel_alt=1000,
    )
    assert sig.level == signals.NO and not sig.drivers and not flags
    assert impact.gs_fg_pct == 0 and impact.heat_away == 0
    assert set(rec) == {"avg_wind", "avg_wind_month"}
    v2 = build._compute_v2(build.RunContext(sport=sport), sport, game, rg, fc, impact, {})
    assert v2 is not None and v2.gs_fg_pct == 0 and v2.heat_away == 0
    card = build.json_out.build_card(sport, game, rg.stadium, None, None, fc, impact, sig, flags)
    assert (card["weather"]["wind_fg"], card["weather"]["temp_fg"], card["weather"]["rain_fg"]) == (wind, temp, rain)
    assert card["signal"]["level"] == signals.NO and card["signal"]["flags"] == []
    expected_roof = "dome" if rg.stadium.roof_type == "dome" else roof
    assert card["stadium"]["roof_state"] == expected_roof


@pytest.mark.parametrize("venue,roof", [("nrg-stadium", "open"), ("gillette-stadium", None)])
def test_exposed_venues_keep_weather_signals(venue, roof):
    game = replace(_game(), stadium_id=venue, roof_state=roof)
    rg = load_stadium_book().resolve(game)
    fc = WeatherForecast(game_id=game.game_id, source="test", temp_fg=41, wind_fg=25, rain_fg_mm=5)
    _, impact, sig, flags = build.build_record("nfl", game, rg.stadium, None, None, fc, roof_state=rg.roof_state)
    assert sig.level == signals.HIGH and impact.gs_fg_pct < 0 and flags


def _game(game_id: str = KC_BUF, away: str = "buf", home: str = "kc", kick: datetime = KICK, neutral: bool = False) -> Game:
    return Game(game_id=game_id, sport="nfl", season=2026, week=1, kickoff_utc=kick, kickoff_local=kick,
                tz="America/Chicago", home_id=home, away_id=away, stadium_id="arrowhead", neutral=neutral)


def _ln(book: str, market: str, side: str, line: float | None, odds: int = -110, game_id: str = KC_BUF, main: bool = True) -> GameLine:
    return GameLine(sport="nfl", game_id=game_id, book=book, market=market, side=side, odds=odds, line=line, is_main=main,
                    scraped_at=datetime.now(timezone.utc))


_OPP = {"home": "away", "away": "home", "over": "under", "under": "over"}


def _opp(ln: GameLine) -> GameLine:
    """The other side of ``ln`` at the same number (openers are recorded per book as a pair)."""
    line = -ln.line if ln.market == "spread" and ln.line is not None else ln.line
    return replace(ln, side=_OPP[ln.side], line=line)


def _both(*lns: GameLine) -> list[GameLine]:
    return [x for ln in lns for x in (ln, _opp(ln))]


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


def test_exchange_scope_scrapes_only_enabled_exchanges(monkeypatch):
    exchanges = ["kalshi", "novig", "prophetx", "polymarket_us"]
    for book in exchanges:
        monkeypatch.setenv(f"BOOK_{book.upper()}_ENABLED", "1")
    assert books_for_scope("exchanges") == exchanges
    assert books_for_scope("exchanges", ["betonline", "fanduel", "novig"]) == ["novig"]
    monkeypatch.setenv("BOOK_PROPHETX_ENABLED", "0")
    assert books_for_scope("exchanges") == ["kalshi", "novig", "polymarket_us"]
    assert build.parse_args(["--scope", "exchanges"]).scope == "exchanges"


# ---- gather with return_exceptions -----------------------------------------------

class _Ok(BaseScraper):
    BOOK_NAME = "ok"

    async def scrape(self, sport, market=None, **kwargs):
        return [_ln("ok", "total", "over", 47.5)]

    async def scrape_with_retry(self, sport: str, market: str | None = None, **kw: Any) -> list[GameLine]:
        return [_ln("ok", "total", "over", 47.5)]


class _Boom(_Ok):
    BOOK_NAME = "boom"

    async def scrape_with_retry(self, sport: str, market: str | None = None, **kw: Any) -> list[GameLine]:
        raise RuntimeError("site changed")


def test_scrape_books_isolates_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build, "load_scraper_class", lambda name: {"ok": _Ok, "boom": _Boom, "missing": None}.get(name))
    degs: list[tuple[str, str, str]] = []
    res, scrapers = build._run_async(scrape_books("nfl", ["ok", "boom", "missing"], None, "r1", lambda c, r, s, **kw: degs.append((c, r, s))))
    assert [ln.book for ln in res["ok"]] == ["ok"]
    assert set(scrapers) == {"ok", "boom"}
    assert res["boom"] == []
    assert "missing" not in res
    assert any("boom failed: RuntimeError: site changed" in r for _, r, _ in degs)
    assert any("missing unavailable" in r for _, r, _ in degs)
    assert all(s == "warn" for _, _, s in degs)


# ---- canonical consensus -------------------------------------------------------------

def _consensus(sport, lines):
    return build._build_consensus(sport, lines, RunContext(sport=sport))


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
    c = _consensus("nfl", lines)
    sp = c[(KC_BUF, "spread")]
    # spread = simple average of pinnacle/betonline (-3.0, -2.5); kalshi is not a member and betcris is display-only
    # No book quotes the average itself: the real fair module shifts the devigged
    # consensus probability to that number, then the adapter converts it to odds.
    assert sp.line == -2.75 and sp.n_books == 3 and sp.ref_book == "pinnacle" and sp.odds == -103 and sp.side == "home"
    assert sp.src == "bol+pin"
    to = c[(KC_BUF, "total")]
    assert to.line == 47.5 and to.ref_book == "pinnacle" and to.n_books == 2 and to.side == "under" and to.src is None
    assert (KC_BUF, "ml") not in c
    # only a non-member posts a spread -> weighted-median fallback
    fb = _consensus("nfl", [_ln("kalshi", "spread", "home", -2.5, -102)])
    assert fb[(KC_BUF, "spread")].line == -2.5 and fb[(KC_BUF, "spread")].src == "fallback"
    assert fb[(KC_BUF, "spread")].ref_book == "kalshi" and fb[(KC_BUF, "spread")].odds == -102


def test_cfb_total_baseline_uses_t_minus_six_and_rebuilds_consensus():
    gid = "cfb:2026:3:maine@nevada"
    kick = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)
    target = "2026-09-13T20:00:00Z"
    game = Game(
        game_id=gid,
        sport="cfb",
        season=2026,
        week=3,
        kickoff_utc=kick,
        kickoff_local=kick,
        tz="America/Los_Angeles",
        home_id="nevada",
        away_id="maine",
        stadium_id="mackay",
    )
    op = pstate.migrate(None, "openers")
    hist = pstate.migrate(None, "history")
    pin = pstate.odds_key(gid, "total", "under", "pinnacle")
    bol = pstate.odds_key(gid, "total", "under", "betonline")
    fd = pstate.odds_key(gid, "total", "under", "fanduel")
    spread = pstate.odds_key(gid, "spread", "home", "pinnacle")
    op["openers"] = {
        pin: {"line": 47.0, "odds": -110, "ts": "2026-09-01T12:00:00Z"},
        bol: {"line": 47.5, "odds": -110, "ts": "2026-09-02T12:00:00Z"},
        fd: {"line": 60.0, "odds": -110, "ts": "2026-09-14T12:00:00Z"},
        spread: {"line": -4.0, "odds": -108, "ts": "2026-09-01T12:00:00Z"},
    }
    hist["series"] = {
        pin: [["2026-09-13T18:00:00Z", 50.0, -105], ["2026-09-14T01:00:00Z", 51.0, -110]],
        bol: [["2026-09-13T19:00:00Z", 48.0, -112], ["2026-09-14T02:00:00Z", 49.0, -110]],
        # First seen after T-6, so this book must not enter a consensus that has pre-target books.
        fd: [["2026-09-14T12:00:00Z", 60.0, -110]],
    }

    changed = build.retarget_cfb_total_openers(
        op,
        hist,
        [],
        [game],
        datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc),
    )

    consensus_key = pstate.odds_key(gid, "total", "under", "consensus")
    assert set(changed) == {pin, bol, fd, consensus_key}
    assert op['openers'][pin]['line'] == 47.0
    assert op['openers'][bol]['line'] == 47.5
    refs = op['references']['t_minus_6d']
    assert refs[pin]['line'] == 50.0
    assert refs[bol]['line'] == 48.0
    assert refs[fd]['line'] == 60.0
    assert refs[consensus_key] == {
        "line": 50.0,
        "odds": -105,
        "ts": "2026-09-13T19:00:00Z",
        "basis": "t_minus_6d",
        "target_ts": target,
        "ref_book": "pinnacle",
        "n_books": 2,
    }
    assert op["openers"][spread]["line"] == -4.0


# ---- board fair inputs ------------------------------------------------------------------

def _by_game(lines: list[GameLine]) -> dict[str, list[GameLine]]:
    out: dict[str, list[GameLine]] = {}
    for ln in lines:
        out.setdefault(ln.game_id, []).append(ln)
    return out


@pytest.mark.parametrize("sport,reference", [("nfl", "betonline"), ("cfb", "fanduel")])
def test_card_odds_preserves_reference_total_and_consensus(sport, reference):
    lines = [_ln(reference, "spread", "home", -2.5, -115), _ln(reference, "total", "under", 47, -108),
             _ln("pinnacle", "spread", "home", -3, -108), _ln("pinnacle", "total", "under", 47.5, -105)]
    lines = [replace(line, sport=sport) for line in lines]
    cons = _consensus(sport, lines)
    out = card_odds(sport, KC_BUF, _by_game(lines), cons)
    assert out["current_total"] == 47 and out["total_proj"] == 47.5
    assert out["ref_book"] == reference
    assert out["spread"] == (-2.75 if sport == "nfl" else -3)
    impact = SimpleNamespace(gs_fg_pct=-4, away_fg_pct=-2)
    block = build.json_out.fair_block(None, build._legacy_derived(build.fair_mod, sport, out, impact))
    assert block["my_total"] == pytest.approx(47.5 * 0.96)
    assert block["edge_legacy"] == pytest.approx((47 - 47.5 * 0.96) / (47.5 * 0.96))
    assert block["my_spread"] == pytest.approx(out["spread"] * 0.98)
    assert block["edge_s_legacy"] == pytest.approx(out["spread"] - out["spread"] * 0.98)


def test_card_odds_falls_back_to_consensus_per_market():
    lines = [_ln("pinnacle", "spread", "home", -3), _ln("pinnacle", "total", "under", 47.5),
             _ln("betcris", "total", "under", 60)]
    out = card_odds("nfl", KC_BUF, _by_game(lines), _consensus("nfl", lines))
    assert out["ref_book"] == "consensus"
    assert out["current_total"] == 47.5 and out["spread"] == -3


def test_cfb_signal_gate_uses_the_consensus_opener_argument():
    gid = "cfb:2026:1:maine@appalachian-state"
    game = Game(
        game_id=gid,
        sport="cfb",
        season=2026,
        week=1,
        kickoff_utc=KICK,
        kickoff_local=KICK,
        tz="America/New_York",
        home_id="appalachian-state",
        away_id="maine",
        stadium_id="kidd-brewer",
    )
    forecast = WeatherForecast(
        game_id=gid,
        source="test",
        temp_fg=78.3,
        wind_fg=6.7,
        rain_fg_mm=0.0,
    )

    # A narrow FanDuel opener cannot override the displayed consensus DQ.
    _, _, disqualified, flags = build.build_record(
        "cfb",
        game,
        None,
        None,
        None,
        forecast,
        travel_alt=955.4,
        home_temp=52.4,
        away_temp=47.2,
        signal_open_spread=-10.01,
    )
    assert disqualified.level == signals.NO and flags == []

    # Likewise, a wide FanDuel opener does not veto an eligible consensus opener.
    _, _, eligible, flags = build.build_record(
        "cfb",
        game,
        None,
        None,
        None,
        forecast,
        travel_alt=955.4,
        home_temp=52.4,
        away_temp=47.2,
        signal_open_spread=-10.0,
    )
    assert eligible.level == signals.MID
    assert eligible.drivers == ("altitude_warmth",) and flags == ["Alt+Heat"]


def test_zero_lines_warns_only_when_the_league_has_games(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    async def fake_scrape(sport, books, raw, run_id, degrade):
        del raw, run_id, degrade
        return {name: [] for name in books}, {
            name: SimpleNamespace(fetch_errors={sport: "HTTP 403"}) for name in books
        }

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    ctx = RunContext(sport="nfl", scope="light", git_sha="t", run_id="empty-league")
    build.stage_odds(ctx, "nfl", [], _Book(), NullRawStore("nfl", "r"), ["novig"], tmp_path, 2026, dry_run=True)
    assert not any("returned 0 lines" in d.reason for d in ctx.degradations)
    assert "::warning::" not in capsys.readouterr().out

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    ctx2 = RunContext(sport="nfl", scope="light", git_sha="t", run_id="dark-book")
    build.stage_odds(
        ctx2, "nfl", [_game()], _Book(), NullRawStore("nfl", "r2"), ["novig"], tmp_path / "state", 2026, dry_run=True,
    )
    assert any(
        d.severity == "warn" and d.reason.startswith("nfl: novig returned 0 lines") and "HTTP 403" in d.reason
        for d in ctx2.degradations
    )
    assert "::warning::nfl: novig returned 0 lines" in capsys.readouterr().out
    assert "HTTP 403" in summary.read_text(encoding="utf-8")


def test_stale_betcris_warns_and_stays_off_the_dark_and_down_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pipeline.outputs import d1_out
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    def lines(book: str) -> list[GameLine]:
        return [_ln(book, "total", "under", 47.0) for _ in range(12)]

    class Stale:
        BOOK_NAME = "betcris"
        upstream_stale = True
        fetch_errors = {"nfl": "upstream stale, age 3774s: Betcris public feed has a stale observation"}

    async def fake_scrape(sport, books, raw, run_id, degrade):
        del sport, books, raw, run_id, degrade
        return {"pinnacle": lines("pinnacle"), "fanduel": lines("fanduel"), "betcris": []}, {
            "pinnacle": SimpleNamespace(BOOK_NAME="pinnacle", fetch_errors={}),
            "fanduel": SimpleNamespace(BOOK_NAME="fanduel", fetch_errors={}),
            "betcris": Stale(),
        }

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    ctx = RunContext(sport="nfl", scope="light", git_sha="t", run_id="stale-betcris")
    build.stage_odds(
        ctx, "nfl", [_game()], _Book(), NullRawStore("nfl", "r"),
        ["pinnacle", "fanduel", "betcris"], tmp_path, 2026,
    )
    reasons = [item.reason for item in ctx.degradations]
    assert any("betcris upstream stale" in reason and "age 3774s" in reason for reason in reasons)
    assert not any("fetch failed" in reason or "returned 0 lines" in reason or "DARK|betcris" in reason for reason in reasons)
    assert all(item.severity != "error" for item in ctx.degradations)
    assert d1_out.run_status(ctx) == "warn"
    entry = pstate.load_book_health(tmp_path)["books"]["betcris"]["nfl"]
    assert entry["rows"] == 0 and entry["consecutive_empty"] == 0 and entry["last_error"] == "upstream stale"
    borrowed = SimpleNamespace(BOOK_NAME="pinnacle", fetch_errors={"nfl": "upstream stale: borrowed phrase"})
    mixed = SimpleNamespace(
        BOOK_NAME="betcris", fetch_errors={"nfl": "upstream stale: old", "extra": "ConnectTimeout"},
    )
    assert build.upstream_stale_detail(borrowed) is None
    assert build.upstream_stale_detail(mixed) is None


def test_card_odds_empty_when_no_lines():
    o = card_odds("nfl", KC_BUF, {}, {})
    assert o["current_total"] is None and o["total_proj"] is None and o["ref_book"] is None


# ---- stage_odds end-to-end (scrapers monkeypatched) -----------------------------------------

def test_stage_odds_persists_openers_and_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prov = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"
    calls = {"n": 0}

    async def fake_scrape(sport, books, raw, run_id, degrade):
        calls["n"] += 1
        spread = -2.5 if calls["n"] == 1 else -3.5
        return {"betonline": _both(_ln("betonline", "spread", "home", spread, game_id=prov),
                                   _ln("betonline", "total", "under", 47.0, game_id=prov)),
                "pinnacle": _both(_ln("pinnacle", "spread", "home", -3.0, game_id=prov))}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    games = [_game()]
    res = build.stage_odds(ctx, "nfl", games, _Book(), NullRawStore("nfl", "r"), ["betonline", "pinnacle"], tmp_path, 2026)
    assert {ln.game_id for ln in res.lines} == {KC_BUF}
    assert res.per_book == {"betonline": 4, "pinnacle": 2}
    assert (tmp_path / "openers.json").exists() and (tmp_path / "scrape_baseline.json").exists() and (tmp_path / "archive_last.json").exists()
    o1 = build.json_out.odds_block(KC_BUF, res.lines, res.openers)["betonline"]["spread"]
    assert (o1["home_line"], o1["open_line"]) == (-2.5, -2.5)

    # second run: line moved, opener stays
    ctx2 = RunContext(sport="nfl", scope="full", git_sha="t")
    res2 = build.stage_odds(ctx2, "nfl", games, _Book(), NullRawStore("nfl", "r"), ["betonline", "pinnacle"], tmp_path, 2026)
    o2 = build.json_out.odds_block(KC_BUF, res2.lines, res2.openers)["betonline"]["spread"]
    assert (o2["home_line"], o2["open_line"]) == (-3.5, -2.5)
    assert ctx2.counts["betonline"]["nfl"] == 4
    # baseline scope keys on the weather-window games ('nfl:2026:1' once the game is
    # inside 10 days, 'nfl:2026:?' while it is only on the odds horizon)
    scope = build.baseline_scope("nfl", [g for g in games if build.in_window(g.kickoff_utc, ctx2.now_utc)], games, 2026)
    bl = pstate.load_baseline(tmp_path, scope)
    assert bl["peaks"]["betonline|spread"] == 2


def test_stage_odds_tolerates_merge_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fake_scrape(sport, books, raw, run_id, degrade):
        return {"pinnacle": []}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)

    def bad(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(build.odds_merge, "merge_odds", bad)
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.stage_odds(ctx, "nfl", [_game()], _Book(), NullRawStore("nfl", "r"), ["pinnacle"], tmp_path, 2026, dry_run=True)
    assert res.lines == []
    comps = {d.component: d.severity for d in ctx.degradations}
    assert comps.get("odds.merge") == "warn"
    assert not (tmp_path / "openers.json").exists()  # dry-run writes no state


def test_external_merge_and_fair_real_modules():
    """The concurrent modules exist: merge_odds re-keys provisional ids via data/aliases,
    fair.consensus gives the Pinnacle-weighted line; both feed the same ConsensusLine shape."""
    from pipeline.run_context import RunContext

    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    prov = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"
    raw_lines = [_ln("pinnacle", "spread", "home", -3.0, -108, game_id=prov), _ln("pinnacle", "spread", "away", 3.0, -112, game_id=prov),
                 _ln("betcris", "spread", "home", -2.5, -110, game_id=prov),
                 _ln("pinnacle", "total", "under", 47.5, -105, game_id=prov),
                 _ln("betcris", "total", "under", 60.0, -110, game_id=prov),
                 # next week's card: outside the span, must not count as unresolved
                 _ln("betcris", "total", "under", 40.0, game_id="nfl:raw:20260927:buffalo-bills@kansas-city-chiefs")]
    openers = pstate.migrate(None, "openers")
    merged = build._merge_lines("nfl", [_game()], raw_lines, {}, openers, ctx)
    assert merged is not None
    lines, unresolved = merged
    assert {ln.game_id for ln in lines} == {KC_BUF} and len(lines) == 5
    assert unresolved == []
    cons = build._build_consensus("nfl", lines, ctx)
    assert cons is not None
    sp = cons[(KC_BUF, "spread")]
    # BetCris -2.5 is display-only, so the spread is Pinnacle's -3.0.
    assert sp.line == -3.0 and sp.ref_book == "pinnacle" and sp.n_books == 1 and sp.src == "pin" and sp.odds == -108
    to = cons[(KC_BUF, "total")]
    assert to.line == 47.5 and to.odds == -105 and to.side == "under" and to.n_books == 1
    assert not any(d.severity == "warn" for d in ctx.degradations)


def test_merge_failure_returns_empty_with_original_degradation(monkeypatch: pytest.MonkeyPatch):
    ctx = RunContext(sport="nfl", scope="full", git_sha="t")

    def bad(*a, **kw):
        raise KeyError("boom")

    monkeypatch.setattr(build.odds_merge, "merge_odds", bad)
    assert build._merge_lines("nfl", [_game()], [], {}, {}, ctx) == ([], [])
    assert [(d.component, d.reason, d.severity) for d in ctx.degradations] == [
        ("odds.merge", "nfl: merge_odds failed (KeyError: 'boom'); using built-in matcher", "warn")]


def test_consensus_failure_drops_only_the_failing_game(monkeypatch: pytest.MonkeyPatch):
    ctx = RunContext(sport="nfl")
    other = "nfl:2026:1:den@lv"
    rows = [_ln("pinnacle", "spread", "home", -3), _ln("pinnacle", "total", "under", 47),
            _ln("pinnacle", "total", "under", 41, game_id=other)]
    real_consensus = build.fair_mod.consensus

    def fail_total(sport, lines, market, **kwargs):
        if lines[0].game_id == KC_BUF and market == "total":
            raise ValueError("boom")
        return real_consensus(sport, lines, market, **kwargs)

    monkeypatch.setattr(build.fair_mod, "consensus", fail_total)
    result = build._build_consensus("nfl", rows, ctx)
    assert set(result) == {(other, "total")}
    assert result[(other, "total")].line == 41
    assert [(d.component, d.reason, d.severity) for d in ctx.degradations] == [
        ("model.fair", "nfl: consensus failed (ValueError: boom); using built-in", "warn")]


def test_carry_forward_lines_only_for_unscraped_books():
    archive = pstate.migrate(None, "archive_last")
    last = archive["last"]
    now = datetime(2026, 9, 25, 21, 45, tzinfo=timezone.utc)
    quoted = now - timedelta(minutes=10)
    last[f"{KC_BUF}|total|under|fanduel"] = {"line": 47.5, "odds": -108, "ts": quoted.isoformat(),
                                           "available": True, "source_id": "total-market-123",
                                           "outcome_ids": {"under": "u", "over": "o"}}
    last[f"{KC_BUF}|spread|home|betonline"] = {"line": -2.5, "odds": -110, "ts": "t"}   # scraped now: not carried
    last[f"{KC_BUF}|total|under|consensus"] = {"line": 47.5, "odds": -110, "ts": "t"}   # pseudo book: never
    last["nfl:2026:1:den@lv|total|under|fanduel"] = {"line": 40.0, "odds": -110, "ts": "t"}  # inactive game
    last["cfb:2026:1:a@b|total|under|fanduel"] = {"line": 50.0, "odds": -110, "ts": "t"}    # other sport
    last[f"{KC_BUF}|ml|home|kalshi"] = {"line": None, "odds": None, "ts": "t"}              # no odds
    got = build.carry_forward_lines(archive, "nfl", {KC_BUF}, ["betonline"], now=now)
    assert [(ln.book, ln.market, ln.side, ln.line, ln.odds, ln.is_main) for ln in got] == [("fanduel", "total", "under", 47.5, -108, True)]
    assert got[0].scraped_at == quoted
    assert got[0].source_id == "total-market-123"
    assert got[0].outcome_ids == {"under": "u", "over": "o"}


@pytest.mark.parametrize("fields", [
    {"available": False},
    {"available": None},  # legacy archive: no verified current availability
    {"ts": "2026-09-25T20:44:59Z"},  # more than one hour old
    {"ts": "2026-09-25T21:45:01Z"},  # future timestamp
    {"ts": "invalid"},
    {"ts": None},
])
def test_carry_forward_rejects_unavailable_or_unverifiable_quotes(fields):
    value = {"line": 47.5, "odds": -108, "ts": "2026-09-25T21:40:00Z", "available": True, **fields}
    archive = {"last": {f"{KC_BUF}|total|under|fanduel": value}}
    assert build.carry_forward_lines(
        archive, "nfl", {KC_BUF}, ["betonline"], now=datetime(2026, 9, 25, 21, 45, tzinfo=timezone.utc),
    ) == []


@pytest.mark.parametrize("remaining", [[], _both(_ln("fanduel", "spread", "home", -3.5))])
def test_playwright_does_not_restore_quotes_missing_from_latest_scrape(tmp_path, monkeypatch, remaining):
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    runs = iter([
        {"fanduel": _both(_ln("fanduel", "total", "under", 60.5), _ln("fanduel", "spread", "home", -3.5))},
        {"fanduel": remaining},  # failed book or withdrawn total
        {"betonline": _both(_ln("betonline", "total", "under", 44.5))},
    ])

    async def fake_scrape(*args):
        return next(runs), {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    for book in ("fanduel", "fanduel", "betonline"):
        res = build.stage_odds(RunContext(sport="nfl", git_sha="t"), "nfl", [_game()], _Book(),
                               NullRawStore("nfl", "r"), [book], tmp_path, 2026)
    assert not any(ln.book == "fanduel" and ln.market == "total" for ln in res.lines)
    assert res.consensus[(KC_BUF, "total")].line == 44.5
    assert res.consensus[(KC_BUF, "total")].n_books == 1
    # Retain the old observation for history/delta comparisons and the original opener.
    key = f"{KC_BUF}|total|under|fanduel"
    assert pstate.load_archive_last(tmp_path)["last"][key]["line"] == 60.5
    assert pstate.load_openers(tmp_path)["openers"][key]["line"] == 60.5


def test_playwright_style_run_keeps_fanduel_now_via_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """light run (fanduel) then odds run (betonline only): CFB FD_now still comes from
    FanDuel's last snapshot, and the consensus sees both books."""
    gid = "cfb:2026:1:iowa-state@kansas-state"
    kick = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    game = Game(game_id=gid, sport="cfb", season=2026, week=1, kickoff_utc=kick, kickoff_local=kick, tz="America/Chicago",
                home_id="kansas-state", away_id="iowa-state", stadium_id="x")

    def ln(book: str, market: str, side: str, line: float, odds: int = -110) -> GameLine:
        return GameLine(sport="cfb", game_id=gid, book=book, market=market, side=side, odds=odds, line=line,
                        scraped_at=datetime.now(timezone.utc))

    runs = iter([
        {"fanduel": [ln("fanduel", "total", "under", 51.5, -112), ln("fanduel", "spread", "home", -3.5)]},
        {"betonline": [ln("betonline", "total", "under", 52.5, -105), ln("betonline", "spread", "home", -3.0)]},
    ])

    async def fake_scrape(sport, books, raw, run_id, degrade):
        return next(runs), {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext

    r1 = build.stage_odds(RunContext(sport="cfb", git_sha="t"), "cfb", [game], _Book(), NullRawStore("cfb", "r"), ["fanduel"], tmp_path, 2026)
    o1 = build.json_out.odds_block(gid, r1.lines, r1.openers)["fanduel"]["total"]
    assert (o1["line"], o1["open_line"]) == (51.5, 51.5)

    r2 = build.stage_odds(RunContext(sport="cfb", git_sha="t"), "cfb", [game], _Book(), NullRawStore("cfb", "r"), ["betonline"], tmp_path, 2026)
    o2 = build.json_out.odds_block(gid, r2.lines, r2.openers)["fanduel"]["total"]
    assert (o2["line"], o2["under"], o2["open_line"]) == (51.5, -112, 51.5)
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
    pstate.record_openers(openers, [*_both(_ln("betonline", "total", "under", 47.0, game_id=entering.game_id)),
                                    *_both(_ln("betonline", "total", "under", 44.0, game_id=settled.game_id)),
                                    _ln("consensus", "total", "under", 47.0, game_id=entering.game_id)], "t0")
    odds = build.OddsResult([], {}, openers, {}, [], {}, new_opener_keys=[f"{settled.game_id}|spread|home|pinnacle"])
    res = build.SportResult("nfl", [], [], [entering, settled], {}, {}, {}, {}, odds)
    keys = build.board_new_opener_keys(res, prev, now)
    # this run's genuinely-new key + every opener of the game that just entered the window
    assert keys == sorted([f"{settled.game_id}|spread|home|pinnacle", f"{entering.game_id}|total|under|betonline",
                           f"{entering.game_id}|total|over|betonline", f"{entering.game_id}|total|under|consensus"])


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
        return {"betonline": _both(_ln("betonline", "spread", "home", -2.5, game_id=prov_near), _ln("betonline", "total", "under", 47.0, game_id=prov_near),
                                   _ln("betonline", "spread", "home", 3.0, game_id=prov_far), _ln("betonline", "total", "under", 41.5, game_id=prov_far)),
                "pinnacle": _both(_ln("pinnacle", "total", "under", 47.5, game_id=prov_near), _ln("pinnacle", "total", "under", 41.0, game_id=prov_far))}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "stage_stadiums", lambda ctx, sport: _NoVenueBook())
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [near, far])
    monkeypatch.setattr(build, "stage_weather", lambda *a, **kw: {})

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.run_sport(ctx, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline", "pinnacle"], state_dir=tmp_path)

    assert [g.game_id for g in res.games] == [near.game_id]
    assert {g.game_id for g in res.odds_games} == {near.game_id, far.game_id}
    assert [c["game_id"] for c in res.cards] == [near.game_id]          # no card for the horizon game
    assert ctx.counts["legacy"]["nfl"] == len(res.cards)
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
        return {"betonline": _both(_ln("betonline", "total", "under", 47.0, game_id=prov))}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "stage_stadiums", lambda ctx, sport: _NoVenueBook())
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [far])
    monkeypatch.setattr(build, "stage_weather", lambda *a, **kw: {})

    ctx = RunContext(sport="nfl", scope="light", git_sha="t")
    res = build.run_sport(ctx, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline"], state_dir=tmp_path)
    assert res.games == [] and res.cards == [] and [g.game_id for g in res.odds_games] == [far.game_id]
    assert f"{far.game_id}|total|under|betonline" in res.odds.openers["openers"]
    assert comp_sev(ctx, "schedule") == "info"
    assert res.season_week == (2026, 1)      # meta season/week falls back to the odds horizon
    # volume baseline keeps week '?' until a game enters the window (fresh peaks for week 1)
    assert build.baseline_scope("nfl", res.games, res.odds_games) == "nfl:2026:?"
    assert build.baseline_scope("nfl", res.odds_games, res.odds_games) == "nfl:2026:1"
    assert pstate.load_baseline(tmp_path, "nfl:2026:?")["peaks"]["betonline|total"] == 2
    # nothing scheduled anywhere -> warn, as before
    monkeypatch.setattr(build, "fetch_schedule", lambda ctx, sport, raw, season, book: [])
    ctx2 = RunContext(sport="nfl", scope="light", git_sha="t")
    build.run_sport(ctx2, "nfl", NullRawStore("nfl", "r"), 2026, books=["betonline"], state_dir=tmp_path)
    assert comp_sev(ctx2, "schedule") == "warn"


def test_alerts_stdout_flag_parses_and_print_sender_is_console_safe(capsys: pytest.CaptureFixture[str]):
    from pipeline import alerts as alerts_mod

    args = build.parse_args(["--sport", "nfl", "--alerts-stdout"])
    assert args.alerts_stdout is True and args.alerts is True
    assert build.parse_args(["--sport", "nfl"]).alerts_stdout is False
    assert alerts_mod.print_sender()("⚠ <b>x</b>\nline2", None) is True
    out = capsys.readouterr().out
    assert "[alert] chat=-" in out and "line2" in out
