"""A partial refresh must preserve fresh books and label observed stale sources.

Exercise stage_odds, board serialization, generation sealing, the actual Worker
GET route and the default board; no injected post-serialization book fields.
"""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import Game, GameLine, WeatherForecast
from pipeline.outputs import json_out
from pipeline.outputs.raw_out import NullRawStore
from pipeline.publication import prepare_generation
from pipeline.run_context import RunContext
from pipeline.stadiums.loader import load_stadium_book

ROOT = Path(__file__).resolve().parents[1]


def test_partial_and_full_refresh_producer_to_served_default_board(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    book = load_stadium_book(ROOT / "data")
    partial = {}
    for sport, stadium in [("nfl", "soldier-field"), ("cfb", "camp-randall-stadium")]:
        gid = f"{sport}:2026:7:a@h"
        game = Game(gid, sport, 2026, 7, now + timedelta(days=1), now + timedelta(days=1),
                    "America/Chicago", "h", "a", stadium_id=stadium)
        state_dir = tmp_path / sport
        state_dir.mkdir()
        archive = pstate.migrate({}, "archive_last")
        for name, age, extra in [("pinnacle", 10, {}), ("fanduel", 720, {}),
                ("betcris", 10, {"expires_at": (now - timedelta(minutes=5)).isoformat()}),
                ("betonline", 10, {"available": False}), ("polymarket_us", 10, {})]:
            archive["last"][f"{gid}|total|under|{name}"] = {"line": 46.5, "odds": -110,
                "ts": (now - timedelta(minutes=age)).isoformat(), "available": True, **extra}
        pstate.save_archive_last(state_dir, archive)
        op = pstate.migrate({}, "openers")
        pstate.record_openers(op, [{"game_id": gid, "market": "spread", "side": "home",
            "book": "consensus", "line": -3, "odds": -110}], now.isoformat())
        pstate.save_openers(state_dir, op)

        async def scrape(sport, books, *args, game_id=gid):
            return {b: [GameLine(sport, game_id, b, "total", "under", -110, 46.5,
                scraped_at=now, prob_raw=.52 if b == "kalshi" else None)] if b != "polymarket_us" else []
                for b in books}, {}

        monkeypatch.setattr(build, "scrape_books", scrape)
        ctx = RunContext(sport=sport, git_sha="publication-fixture", run_id="book-refresh")
        odds = build.stage_odds(ctx, sport, [game], book, NullRawStore(sport, "fixture"),
                               ["kalshi", "polymarket_us"], state_dir, 2026)
        assert {ln.book for ln in odds.lines} == {"kalshi", "pinnacle"}
        assert odds.last_observed[gid]["fanduel"]["status"] == "stale"
        assert odds.last_observed[gid]["betcris"]["status"] == "expired"
        assert odds.last_observed[gid]["polymarket_us"]["status"] == "unavailable"
        assert odds.last_observed[gid]["pinnacle"]["updated_at"] == (now - timedelta(minutes=10)).isoformat()
        fc = WeatherForecast(gid, "fixture", now, 24, temp_fg=55, wind_fg=18)
        partial[sport] = [json_out.build_card(sport, game, book.find_stadium(stadium), None, None,
            fc, None, None, [], lines=odds.lines, openers=odds.openers, consensus=odds.consensus,
            last_observed=odds.last_observed[gid], run_id="book-refresh")]
        # A regular refresh restores current fields only from newly observed lines.
        full = build.stage_odds(ctx, sport, [game], book, NullRawStore(sport, "fixture"),
                               ["pinnacle", "fanduel", "betonline"], state_dir, 2026)
        assert {ln.book for ln in full.scraped} == {"pinnacle", "fanduel", "betonline"}
        assert all(ln.scraped_at == now for ln in full.scraped)
    out = tmp_path / "board"
    files = json_out.write_board(out, partial, {"run_id": "book-refresh", "git_sha": "publication-fixture",
        "sport_counts": {"nfl": 1, "cfb": 1}})
    immutable, pointer = prepare_generation(files, tmp_path / "generation")
    objects = {key: path.read_text(encoding="utf-8") for key, path in immutable.items()}
    objects["board/meta.json"] = pointer.read_text(encoding="utf-8")
    result = subprocess.run([shutil.which("node") or "node", "tests/helpers/book-refresh-publication.mjs"],
        input=json.dumps({"objects": objects}), text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
