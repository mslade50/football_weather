"""One main pair per (game, book, market): select_main_lines + the consumers that must agree."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import GameLine
from pipeline.model import fair
from pipeline.odds.main_select import select_main_lines
from pipeline.outputs import json_out

GID = "nfl:2026:1:buf@kc"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _ln(book, market, side, odds, line=None, main=True, t=0, sid=None):
    return GameLine(sport="nfl", game_id=GID, book=book, market=market, side=side, odds=odds, line=line,
                    is_main=main, scraped_at=T0 + timedelta(minutes=t), source_id=sid)


def _spread(book, home, ho=-110, ao=-110, **kw):
    return [_ln(book, "spread", "home", ho, home, **kw), _ln(book, "spread", "away", ao, -home, **kw)]


def _total(book, num, oo=-110, uo=-110, **kw):
    return [_ln(book, "total", "over", oo, num, **kw), _ln(book, "total", "under", uo, num, **kw)]


def _mains(rows):
    return sorted((r.book, r.market, r.side, r.line, r.odds) for r in rows if r.is_main)


def _prophetx_rungs():
    rows = []
    for hl, ho, ao in [(-2.5, -135, 115), (-3.0, -108, -112), (-3.5, 110, -130), (-4.5, 140, -165)]:
        rows += _spread("prophetx", hl, ho, ao)
    return rows


def test_multi_main_prophetx_collapses_to_most_balanced_pair():
    out = select_main_lines(_prophetx_rungs())
    assert len(out) == 8
    assert _mains(out) == [("prophetx", "spread", "away", 3.0, -112), ("prophetx", "spread", "home", -3.0, -108)]


def test_order_independent_and_preserves_input_order():
    rows = _prophetx_rungs() + _total("prophetx", 47, -105, -115) + _total("prophetx", 45.5, -130, 110) + _spread("pinnacle", -3)
    base = _mains(select_main_lines(rows))
    rng = random.Random(7)
    for _ in range(20):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        out = select_main_lines(shuffled)
        assert _mains(out) == base
        assert [(r.book, r.market, r.side, r.line) for r in out] == [(r.book, r.market, r.side, r.line) for r in shuffled]


def test_book_without_main_is_excluded_not_promoted():
    rows = _spread("kalshi", -3, main=False) + _spread("kalshi", -4, main=False) + _spread("pinnacle", -3)
    out = select_main_lines(rows)
    assert _mains(out) == [("pinnacle", "spread", "away", 3, -110), ("pinnacle", "spread", "home", -3, -110)]
    assert fair.main_lines(out, "spread").keys() == {"pinnacle"}


def test_complete_pair_beats_more_balanced_incomplete_pair():
    rows = [_ln("betonline", "spread", "home", -110, -3.0)] + _spread("betonline", -4.5, -150, 130)
    out = select_main_lines(rows)
    assert _mains(out) == [("betonline", "spread", "away", 4.5, 130), ("betonline", "spread", "home", -4.5, -150)]


def test_away_only_book_keeps_its_single_main_row():
    out = select_main_lines([_ln("betonline", "spread", "away", -115, 3.5)])
    assert _mains(out) == [("betonline", "spread", "away", 3.5, -115)]


def test_duplicate_side_keeps_latest_scrape():
    rows = [_ln("novig", "spread", "home", -110, -1.5, t=0), _ln("novig", "spread", "home", -112, -1.5, t=5),
            _ln("novig", "spread", "away", -108, 1.5, t=1)]
    out = select_main_lines(rows)
    assert _mains(out) == [("novig", "spread", "away", 1.5, -108), ("novig", "spread", "home", -1.5, -112)]
    assert [r.is_main for r in out] == [False, True, True]


def test_smaller_line_breaks_balance_ties():
    rows = _spread("pinnacle", -7) + _spread("pinnacle", -3)
    assert _mains(select_main_lines(rows))[1][3] == -3


def test_board_fair_and_opener_agree_for_a_multi_rung_book():
    rows = select_main_lines(_prophetx_rungs() + _total("prophetx", 47, -105, -115) + _total("prophetx", 48.5, 115, -135))
    openers = pstate.migrate({}, "openers")
    pstate.record_openers(openers, [r for r in rows if r.is_main], T0.isoformat())
    board = json_out.odds_block(GID, rows, openers)["prophetx"]
    fair_spread = fair.main_lines(rows, "spread")["prophetx"]
    fair_total = fair.main_lines(rows, "total")["prophetx"]
    assert board["spread"]["home_line"] == fair_spread["home"].line == -fair_spread["away"].line == board["spread"]["open_line"] == -3.0
    assert board["total"]["line"] == fair_total["over"].line == fair_total["under"].line == board["total"]["open_line"] == 47
    assert fair.consensus("nfl", rows, "spread").books["prophetx"] == -3.0
    assert build.consensus_lines("nfl", rows)[(GID, "spread")].line == -3.0
