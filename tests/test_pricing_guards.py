"""Regression guards: state pruning, corrupt state, GameLine validation, BetOnline points."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import GameLine, game_line
from pipeline.odds.parsers import betonline as bo


class _Ctx:
    def __init__(self) -> None:
        self.degradations: list = []

    def degrade(self, component, reason, severity="warn"):
        self.degradations.append((component, reason, severity))


def _ln(**kw):
    base = dict(sport="nfl", game_id="nfl:2026:1:a@b", book="x", market="spread", side="home", odds=-110, line=-3.0)
    base.update(kw)
    return GameLine(**base)


def test_openers_survive_empty_or_reduced_schedule(tmp_path):
    openers = pstate.migrate({}, "openers")
    pstate.record_openers(openers, [_ln(), _ln(game_id="nfl:2026:2:c@d")], "2026-09-01T00:00:00Z")
    pstate.save_openers(tmp_path, openers)
    before = json.loads((tmp_path / pstate.OPENERS_FILE).read_text())
    reloaded = pstate.load_openers(tmp_path)
    assert pstate.prune_openers(reloaded, set()) == 0
    pstate.save_openers(tmp_path, reloaded)
    assert json.loads((tmp_path / pstate.OPENERS_FILE).read_text()) == before


def test_prune_guard_skips_empty_and_shrunk_schedule():
    ctx, store = _Ctx(), {}
    assert build._prune_allowed(store, "nfl", 16, ctx, "archive_last")
    assert not build._prune_allowed(store, "nfl", 0, ctx, "archive_last")
    assert not build._prune_allowed(store, "nfl", 5, ctx, "archive_last")  # >30% shrink
    assert store["prune_guard"]["nfl"] == 13  # reference decays 15% per skipped run
    assert len(ctx.degradations) == 2
    assert build._prune_allowed(store, "nfl", 14, ctx, "archive_last")
    assert build._prune_allowed(store, "cfb", 3, ctx, "archive_last")  # per-sport reference


def test_prune_guard_resumes_after_genuine_shrink():
    ctx, store = _Ctx(), {}
    assert build._prune_allowed(store, "cfb", 60, ctx, "history")
    skipped = 0
    while not build._prune_allowed(store, "cfb", 12, ctx, "history"):
        skipped += 1
        assert skipped < 15
    assert store["prune_guard"]["cfb"] == 12


def test_corrupt_state_raises_and_is_not_overwritten(tmp_path):
    for loader, name in ((pstate.load_openers, pstate.OPENERS_FILE),
                         (pstate.load_archive_last, pstate.ARCHIVE_LAST_FILE),
                         (pstate.load_history, pstate.HISTORY_FILE)):
        assert loader(tmp_path) is not None  # missing file -> fresh state
        (tmp_path / name).write_text("{not json")
        with pytest.raises(pstate.StateSchemaError):
            loader(tmp_path)
        assert (tmp_path / name).read_text() == "{not json"


@pytest.mark.parametrize("kw", [
    {"odds": 0}, {"odds": 99}, {"odds": float("nan")}, {"odds": float("inf")},
    {"line": float("nan")}, {"line": float("inf")}, {"market": "spread", "side": "over"},
    {"market": "total", "side": "home"}, {"odds": None},
])
def test_gameline_rejects_invalid(kw):
    with pytest.raises(ValueError):
        _ln(**kw)
    base = dict(sport="nfl", game_id="g", book="x", market="spread", side="home", odds=-110, line=-3.0)
    assert game_line(**{**base, **kw}) is None


def test_gameline_accepts_valid_and_game_line_returns_row():
    assert _ln(odds=100).odds == 100
    assert game_line(sport="nfl", game_id="g", book="x", market="ml", side="away", odds=-250) is not None
    assert math.isfinite(_ln(line=0.0).line)


def test_consensus_pseudo_row_has_no_price():
    cons = {("nfl:2026:1:a@b", "spread"): SimpleNamespace(line=-2.5, odds=-108, src="s")}
    pseudo = build.consensus_spread_lines(cons, "nfl", datetime(2026, 9, 1, tzinfo=timezone.utc), "r1")
    assert [p.odds for p in pseudo] == [None]
    hist = pstate.migrate({}, "history")
    assert pstate.update_history(hist, pseudo, "2026-09-01T00:00:00Z") == 1
    with pytest.raises(ValueError):
        _ln(book="fanduel", odds=None)


def _entry(a_pt, h_pt):
    return {"Game": {
        "GameId": 1, "AwayTeam": "A", "HomeTeam": "B", "WagerCutOff": "2026-11-26T17:30:00", "Comments": "",
        "AwayLine": {"SpreadLine": {"Point": a_pt, "Line": -110}, "MoneyLine": {"Line": 120}},
        "HomeLine": {"SpreadLine": {"Point": h_pt, "Line": -110}, "MoneyLine": {"Line": -140}},
        "TotalLine": {"TotalLine": {"Point": None, "Over": {"Line": -110}, "Under": {"Line": -110}}},
    }}


def test_betonline_missing_point_drops_spread_and_total_not_fake_pickem():
    g = bo.parse_game(_entry(None, None), "nfl", "nfl")
    assert g.away_spread is None and g.home_spread is None and g.total is None
    assert {ln.market for ln in bo.game_lines(g)} == {"ml"}
    g2 = bo.parse_game(_entry(0, 0), "nfl", "nfl")  # a real pick'em stays
    assert g2.away_spread == 0.0 and g2.home_spread == 0.0


def test_betonline_november_kickoff_uses_request_offset():
    # We send `utc-offset: 240`; the book answers in that fixed offset all year (not Eastern/EST).
    assert bo.UTC_OFFSET_MINUTES == 240
    assert bo.parse_cutoff("2026-11-26T17:30:00") == datetime(2026, 11, 26, 21, 30, tzinfo=timezone.utc)
