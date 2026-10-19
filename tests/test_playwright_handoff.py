"""Parallel Playwright split: ``build --scrape-only`` -> handoff file -> ``build --ingest-lines``.

Pins: every GameLine field (scraped_at above all) survives the round trip, lines older
than ODDS_CARRY_MAX_AGE / expired / future-dated / undated are rejected at ingest, a
missing handoff reads as a failed BetOnline scrape (0 lines + degradation), the ingest
path never launches the Playwright scraper, and archive_last keeps the original
scrape time rather than the publish time."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import Game, GameLine
from pipeline.odds import handoff
from pipeline.outputs.raw_out import NullRawStore
from pipeline.run_context import RunContext

KC_BUF = "nfl:2026:1:buf@kc"
PROV = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"
KICK = datetime(2026, 9, 13, 20, 25, tzinfo=timezone.utc)
MAX_AGE = build.ODDS_CARRY_MAX_AGE


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _game() -> Game:
    return Game(game_id=KC_BUF, sport="nfl", season=2026, week=1, kickoff_utc=KICK, kickoff_local=KICK,
                tz="America/Chicago", home_id="kc", away_id="buf", stadium_id="arrowhead")


def _bol(market: str, side: str, line: float | None, odds: int = -110, *, at: datetime | None,
         game_id: str = PROV, **kw) -> GameLine:
    return GameLine(sport="nfl", game_id=game_id, book="betonline", market=market, side=side, odds=odds,
                    line=line, scraped_at=at, run_id="scrape-run", **kw)


def _raw_game() -> SimpleNamespace:
    return SimpleNamespace(game_id=PROV, away="Buffalo Bills", home="Kansas City Chiefs", kickoff_utc=KICK, neutral=False)


class _Book:
    aliases = {"buffalo bills": "buf", "kansas city chiefs": "kc", "buf": "buf", "kc": "kc"}

    def resolve_team(self, sport: str, raw: str) -> str | None:
        return self.aliases.get(raw.strip().lower())


# ---- handoff module -----------------------------------------------------------------

def test_round_trip_preserves_every_field_and_raw_games(tmp_path: Path):
    at = _now() - timedelta(minutes=7)
    full = _bol("total", "under", 47.5, -105, at=at, source_id="src-1", outcome_ids={"under": "u1"},
                source_updated_at=at - timedelta(minutes=1), expires_at=at + timedelta(minutes=30),
                opening_line=46.5, opening_odds=-110, opened_at=at - timedelta(days=2), opening_source="provider")
    ml = _bol("ml", "home", None, -150, at=at)
    other_book = GameLine(sport="nfl", game_id=PROV, book="pinnacle", market="ml", side="away", odds=130, scraped_at=at)
    path = handoff.dump_lines(tmp_path, "nfl", "betonline", [full, ml, other_book], run_id="scrape-run",
                              raw_games=[_raw_game()], fetch_errors={"cfb": "dark"})
    assert path == tmp_path / "nfl" / "betonline.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == handoff.SCHEMA_VERSION and payload["run_id"] == "scrape-run"
    assert len(payload["lines"]) == 2   # only this sport/book is written

    ing = handoff.load_lines(tmp_path, "nfl", "betonline", now=_now(), max_age=MAX_AGE)
    assert not ing.missing and ing.problems == []
    assert ing.lines == [full, ml]      # dataclass equality: every field, datetimes tz-aware UTC
    assert ing.lines[0].scraped_at == at
    assert ing.run_id == "scrape-run"
    (g,) = ing.scraper.last_games
    assert (g.game_id, g.away, g.home, g.kickoff_utc, g.neutral) == (PROV, "Buffalo Bills", "Kansas City Chiefs", KICK, False)
    assert ing.scraper.fetch_errors == {"cfb": "dark"}


def test_ingest_rejects_stale_expired_future_and_undated_lines(tmp_path: Path):
    now = _now()
    fresh = _bol("spread", "home", -3.0, at=now - MAX_AGE + timedelta(minutes=1))
    stale = _bol("spread", "away", 3.0, at=now - MAX_AGE - timedelta(minutes=1))
    expired = _bol("total", "over", 47.5, at=now - timedelta(minutes=5), expires_at=now - timedelta(seconds=1))
    future = _bol("total", "under", 47.5, at=now + timedelta(minutes=10))
    undated = _bol("ml", "home", None, -150, at=None)
    handoff.dump_lines(tmp_path, "nfl", "betonline", [fresh, stale, expired, future, undated], run_id="r")
    ing = handoff.load_lines(tmp_path, "nfl", "betonline", now=now, max_age=MAX_AGE)
    assert ing.lines == [fresh]
    assert (ing.rejected_stale, ing.rejected_expired, ing.rejected_invalid) == (2, 1, 1)


def test_ingest_tolerates_small_clock_skew(tmp_path: Path):
    now = _now()
    ahead = _bol("spread", "home", -3.0, at=now + timedelta(seconds=30))
    handoff.dump_lines(tmp_path, "nfl", "betonline", [ahead], run_id="r")
    assert handoff.load_lines(tmp_path, "nfl", "betonline", now=now, max_age=MAX_AGE).lines == [ahead]


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"schema_version": 99, "sport": "nfl", "book": "betonline"}),
                                     json.dumps({"schema_version": 1, "sport": "cfb", "book": "betonline", "lines": []})])
def test_missing_or_bad_handoff_reads_as_failed_scrape(tmp_path: Path, content: str | None):
    if content is not None:
        path = handoff.handoff_path(tmp_path, "nfl", "betonline")
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    ing = handoff.load_lines(tmp_path, "nfl", "betonline", now=_now(), max_age=MAX_AGE)
    assert ing.missing and ing.lines == [] and ing.problems
    assert ing.scraper.last_games == [] and "nfl" in ing.scraper.fetch_errors


def test_bad_rows_are_counted_not_fatal(tmp_path: Path):
    good = _bol("spread", "home", -3.0, at=_now())
    path = handoff.dump_lines(tmp_path, "nfl", "betonline", [good], run_id="r")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lines"].append({**payload["lines"][0], "odds": 5})        # |odds| < 100
    payload["lines"].append({**payload["lines"][0], "book": "pinnacle"})  # wrong book
    path.write_text(json.dumps(payload), encoding="utf-8")
    ing = handoff.load_lines(tmp_path, "nfl", "betonline", now=_now(), max_age=MAX_AGE)
    assert ing.lines == [good] and ing.rejected_invalid == 2


# ---- build --scrape-only ------------------------------------------------------------

def test_scrape_only_writes_raw_captures_and_handoff_without_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    at = _now() - timedelta(seconds=5)
    seen: dict = {}

    async def fake_scrape(sport, books, raw, run_id, degrade):
        seen.setdefault("books", []).append((sport, list(books), run_id))
        raw.put("betonline_offering", {"verbatim": True}, "https://example.invalid/offering")
        lines = [_bol("spread", "home", -3.0, at=at), _bol("spread", "away", 3.0, at=at)] if sport == "nfl" else []
        scraper = SimpleNamespace(last_games=[_raw_game()] if sport == "nfl" else [], fetch_errors={} if sport == "nfl" else {"cfb": "dark"})
        return {"betonline": lines}, {"betonline": scraper}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    out, raw_dir, state_dir = tmp_path / "pw-lines", tmp_path / "raw_runs", tmp_path / "state"
    rc = build.main(["--sport", "all", "--scrape-only", str(out), "--books", "betonline",
                     "--raw-dir", str(raw_dir), "--state-dir", str(state_dir), "--run-id", "scrape-run"])
    assert rc == 0
    assert seen["books"] == [("nfl", ["betonline"], "scrape-run"), ("cfb", ["betonline"], "scrape-run")]
    # raw-first: verbatim capture + manifest under the scrape run id (pushed by the publish job)
    for sport in ("nfl", "cfb"):
        run_dir = raw_dir / sport / "scrape-run"
        assert (run_dir / "betonline_offering.json").is_file() and (run_dir / "manifest.json").is_file()
    nfl = json.loads((out / "nfl" / "betonline.json").read_text(encoding="utf-8"))
    assert len(nfl["lines"]) == 2 and nfl["games"][0]["home"] == "Kansas City Chiefs"
    assert nfl["lines"][0]["scraped_at"] == build.utc_iso(at)
    cfb = json.loads((out / "cfb" / "betonline.json").read_text(encoding="utf-8"))
    assert cfb["lines"] == [] and cfb["fetch_errors"] == {"cfb": "dark"}
    assert not state_dir.exists()   # no state read or written


def test_scrape_only_defaults_to_playwright_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    books_seen = []

    async def fake_scrape(sport, books, raw, run_id, degrade):
        books_seen.append(list(books))
        return {}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    assert build.scrape_only(["nfl"], tmp_path / "out", raw_dir=tmp_path / "raw", run_id="r") == 0
    assert books_seen == [list(build.PLAYWRIGHT_BOOKS)]
    ing = handoff.load_lines(tmp_path / "out", "nfl", "betonline", now=_now(), max_age=MAX_AGE)
    assert not ing.missing and ing.lines == [] and ing.scraper.fetch_errors == {"nfl": "scraper unavailable"}


# ---- build --ingest-lines (stage_odds) ----------------------------------------------

def _publish_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ingest_dir: Path, *, books=("betonline", "betcris")):
    calls: list[list[str]] = []
    now = _now()

    async def fake_scrape(sport, books, raw, run_id, degrade):
        calls.append(list(books))
        assert "betonline" not in books, "ingest path must not launch the Playwright scraper"
        return {"betcris": [GameLine(sport="nfl", game_id=PROV, book="betcris", market="spread", side="home",
                                     odds=-110, line=-2.5, scraped_at=now)]}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    ctx = RunContext(sport="nfl", scope="odds", git_sha="t", run_id="publish-run")
    res = build.stage_odds(ctx, "nfl", [_game()], _Book(), NullRawStore("nfl", "publish-run"), list(books),
                           tmp_path / "state", 2026, ingest_dir=ingest_dir)
    return ctx, res, calls


def test_ingest_round_trip_keeps_scrape_time_in_archive_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("pipeline.odds.merge")
    at = _now() - timedelta(minutes=6)   # roughly one light job old at publish
    handoff.dump_lines(tmp_path / "pw", "nfl", "betonline",
                       [_bol("spread", "home", -3.0, at=at), _bol("spread", "away", 3.0, -110, at=at),
                        _bol("total", "under", 47.5, -108, at=at)],
                       run_id="scrape-run", raw_games=[_raw_game()])
    ctx, res, calls = _publish_stage(tmp_path, monkeypatch, tmp_path / "pw")
    assert calls == [["betcris"]]
    assert res.per_book == {"betcris": 1, "betonline": 3}
    bol = [ln for ln in res.lines if ln.book == "betonline"]
    assert {ln.game_id for ln in bol} == {KC_BUF} and len(bol) == 3   # provisional ids resolved via handoff games
    assert all(ln.scraped_at == at for ln in bol)
    last = pstate.load_archive_last(tmp_path / "state")["last"]
    entry = last[f"{KC_BUF}|spread|home|betonline"]
    assert entry["ts"] == build.utc_iso(at) and entry["available"] is True   # scrape time, not publish time
    assert not [d for d in ctx.degradations if d.component == "odds.ingest"]
    # Carry-forward sees the true age: the line ages out at scraped_at + ODDS_CARRY_MAX_AGE.
    later = at + MAX_AGE + timedelta(seconds=1)
    assert not build.carry_forward_lines({"last": last}, "nfl", {KC_BUF}, ["betcris"], now=later)
    assert build.carry_forward_lines({"last": last}, "nfl", {KC_BUF}, ["betcris"], now=at + timedelta(minutes=10))


def test_ingest_drops_stale_handoff_and_withdraws_previous_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old = _now() - MAX_AGE - timedelta(minutes=5)
    handoff.dump_lines(tmp_path / "pw", "nfl", "betonline", [_bol("spread", "home", -3.0, at=old)],
                       run_id="scrape-run", raw_games=[_raw_game()])
    ctx, res, _calls = _publish_stage(tmp_path, monkeypatch, tmp_path / "pw")
    assert res.per_book["betonline"] == 0
    assert not [ln for ln in res.lines if ln.book == "betonline"]
    msgs = [d.reason for d in ctx.degradations if d.component == "odds.ingest"]
    assert msgs and "dropped 1 line" in msgs[0] and d_sev(ctx, "odds.ingest") == "warn"
    assert f"{KC_BUF}|spread|home|betonline" not in pstate.load_archive_last(tmp_path / "state")["last"]


def test_missing_handoff_publishes_betonline_as_failed_scrape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ctx, res, calls = _publish_stage(tmp_path, monkeypatch, tmp_path / "absent")
    assert calls == [["betcris"]]
    assert res.per_book == {"betcris": 1, "betonline": 0}
    assert any(d.component == "odds.ingest" and "no handoff file" in d.reason for d in ctx.degradations)
    assert any(d.component == "odds" and "betonline returned 0 lines" in d.reason for d in ctx.degradations)
    health = json.loads((tmp_path / "state" / "book_health.json").read_text(encoding="utf-8"))
    assert "betonline" in json.dumps(health) and "no handoff file" in json.dumps(health)


def test_without_ingest_dir_betonline_is_scraped_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def fake_scrape(sport, books, raw, run_id, degrade):
        calls.append(list(books))
        return {}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    build.stage_odds(RunContext(sport="nfl", git_sha="t"), "nfl", [_game()], _Book(), NullRawStore("nfl", "r"),
                     ["betonline", "betcris"], tmp_path, 2026)
    assert calls == [["betonline", "betcris"]]


def test_ingest_lines_flag_reaches_build(monkeypatch: pytest.MonkeyPatch):
    seen = {}
    monkeypatch.setattr(build, "build", lambda sports, **kw: seen.update(kw) or 0)
    assert build.main(["--scope", "odds", "--books", "betonline,betcris", "--ingest-lines", "data/pw-lines"]) == 0
    assert seen["ingest_dir"] == Path("data/pw-lines") and seen["books"] == ["betonline", "betcris"]


def d_sev(ctx: RunContext, component: str) -> str | None:
    return next((d.severity for d in ctx.degradations if d.component == component), None)
