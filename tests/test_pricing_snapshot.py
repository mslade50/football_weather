"""Pricing uses source clocks once; output and generation sealing never reprice."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import build
from pipeline.contracts import Game, GameLine
from pipeline.model import fair
from pipeline.outputs import json_out
from pipeline.publication import prepare_generation, verify_generation
from pipeline.run_context import RunContext

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
GID = "nfl:2026:11:a@h"


def pair(book, value, **clocks):
    scraped_at = clocks.pop("scraped_at", NOW - timedelta(minutes=1))
    return [GameLine("nfl", GID, book, "total", side, -110, value,
                     scraped_at=scraped_at, **clocks)
            for side in ("over", "under")]


@pytest.mark.parametrize("clocks", [
    {"scraped_at": NOW - timedelta(hours=1, seconds=1)},
    {"expires_at": NOW - timedelta(seconds=1)},
    {"actionable_until": NOW - timedelta(seconds=1)},
])
def test_only_usable_quotes_price_consensus_fair_and_edges(clocks):
    lines = pair("pinnacle", 48) + pair("betonline", 80, **clocks) + pair("betcris", 99)
    result = fair.evaluate_game("nfl", GID, lines, -5, 0, now=NOW)
    assert result.total.line == 48
    assert result.total.n_books == 1
    assert result.fair_total == pytest.approx(45.6)
    assert {edge.book for edge in result.edges} == {"pinnacle"}
    assert build._build_consensus("nfl", lines, RunContext("nfl", git_sha="test"), now=NOW)[GID, "total"].line == 48
    fresh = fair.evaluate_game("nfl", GID, pair("betonline", 47, scraped_at=NOW - timedelta(hours=1)), 0, 0, now=NOW)
    assert fresh.total.line == 47  # The existing ceiling is inclusive.


def test_elapsed_time_does_not_mutate_cards_or_sealed_publication(tmp_path, monkeypatch):
    lines = pair("pinnacle", 48, expires_at=NOW + timedelta(seconds=1)) + pair("betonline", 48)
    result = fair.evaluate_game("nfl", GID, lines, -5, 0, now=NOW)
    consensus = build._build_consensus("nfl", lines, RunContext("nfl", git_sha="test"), now=NOW)
    kick = NOW + timedelta(days=1)
    game = Game(GID, "nfl", 2026, 11, kick, kick, "UTC", "h", "a", None)
    card = json_out.build_card("nfl", game, None, None, None, None, None, None, [],
                               lines=lines, fair=result, consensus=consensus, run_id="snapshot", priced_at=NOW)
    before = deepcopy(card)
    odds = build.OddsResult(lines, consensus, {}, {GID: lines}, [], {}, priced_at=NOW)
    sport = build.SportResult("nfl", [], [card], [game], {}, {}, {}, {}, odds, fairs={GID: result})
    ctx = RunContext("nfl", started_at=NOW, run_id="snapshot", git_sha="test", dry_run=True)
    board_dir, state_dir = tmp_path / "board", tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(build, "d1_statements", lambda *a, **kw: [])
    monkeypatch.setattr(RunContext, "now_utc", property(lambda _: NOW + timedelta(hours=3)))
    files = build.write_outputs(ctx, [sport], ["pinnacle", "betonline"], board_dir=board_dir,
                                snapshot_dir=tmp_path / "snapshots", state_dir=state_dir,
                                d1_sql=tmp_path / "inserts.sql", raw_files={})
    assert card == before
    assert json.loads((board_dir / "games_nfl.json").read_text())["games"] == [before]
    assert "quote_expiries" not in json.loads((board_dir / "meta.json").read_text())
    assert "quote_expires_at" not in json.loads((board_dir / "board.json").read_text())["rows"][0]
    sealed, pointer = prepare_generation(files, tmp_path / "sealed")
    objects = {key: path.read_bytes() for key, path in sealed.items()}
    verify_generation(json.loads(pointer.read_bytes()), objects.get)
    assert next(json.loads(data)["games"] for key, data in objects.items() if key.endswith("games_nfl.json")) == [before]
    assert before["odds"]["pinnacle"]["total"]["expires_at"] == "2026-11-13T12:00:01Z"
    assert before["fair"]["priced_at"] == "2026-11-13T12:00:00Z"
