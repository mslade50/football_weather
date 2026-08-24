"""OPS-notice noise controls (added after the first live day sent 25 OPS messages).

* keys strip counts so ``cfb: 136 unresolved`` and ``cfb: 239 unresolved`` are one alert/day
* expected conditions (off-season window, optional keys, disabled/dark books) never page
* ``:no-schedule-match`` names (FCS games a book lists) are not "unresolved"
* every OPS candidate of a run rides in ONE grouped message
* ``BOOK_<NAME>_ENABLED=0`` removes the book from the run entirely
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from pipeline import alerts as A
from pipeline import build
from pipeline import state as pstate
from tests.test_alerts_rules import CFG, NOW, _ctx, _fresh


def _ops_keys(ctx) -> list[str]:
    alerts, _ = _fresh()
    return sorted(c.key for c in A.ops_candidates(ctx, {}, alerts, NOW))


def test_degradation_key_is_stable_across_changing_counts():
    a, b = _ctx(), _ctx()
    a.degrade("odds.merge", "cfb: 136 unresolved book games/names (of 249)", "warn")
    b.degrade("odds.merge", "cfb: 239 unresolved book games/names (of 312)", "warn")
    assert _ops_keys(a) == _ops_keys(b) == ["degr|odds.merge|cfb-unresolved-book-games-names-of|2026-09-18"]


@pytest.mark.parametrize("reason", [
    "nfl: 0 games within window (272 in season 2026)",
    "CFBD_API_KEY missing, using ESPN scoreboard",
    "cfb: prophetx returned 0 lines",
    "[fanduel] disabled via BOOK_FANDUEL_ENABLED=0",
])
def test_expected_degradations_never_page(reason):
    ctx = _ctx()
    ctx.degrade("odds", reason, "warn")
    assert _ops_keys(ctx) == []


def test_real_degradations_still_page():
    ctx = _ctx()
    ctx.degrade("odds.volume", "betcris: 0 rows (dark) while peers report ≥10", "warn")
    ctx.degrade("stadiums", "cfb:2026:1:north-carolina@tcu: neutral site '3504' unknown, using home stadium", "warn")
    assert len(_ops_keys(ctx)) == 2


def test_no_schedule_match_names_are_not_unresolved():
    ctx = _ctx()
    ctx.unresolved_names.extend(["fanduel:Bryant@Stonehill:no-schedule-match", "kalshi:Lehigh@Holy Cross:no-schedule-match"])
    assert _ops_keys(ctx) == []
    ctx.unresolved_names.append("fanduel:Long Island")
    assert _ops_keys(ctx) == ["names|fanduel|2026-09-18"]


def test_ops_candidates_are_one_grouped_message():
    ctx = _ctx()
    for i in range(6):
        ctx.degrade(f"c{i}", f"thing {i} broke badly", "warn")
    alerts, tg = _fresh()
    cands = A.ops_candidates(ctx, {}, alerts, NOW)
    assert len(cands) == 6
    plan = A.plan(cands, alerts, tg, NOW, CFG)
    assert plan.send == [] and len(plan.ops) == 6
    sent: list[str] = []
    out = A.dispatch(plan, alerts, lambda text, chat: sent.append(text) or True, NOW, CFG)
    assert out.n_messages == 1 and "Ops notices" in sent[0]
    assert all(pstate.alert_sent(alerts, c.key) for c in cands)          # all six keys marked
    again = A.ops_candidates(ctx, {}, alerts, NOW + timedelta(hours=2))
    assert again == []                                                    # same ET day → dedup


def test_ops_group_counts_against_run_cap():
    ctx = _ctx()
    ctx.degrade("x", "boom", "warn")
    alerts, tg = _fresh()
    plan = A.plan(A.ops_candidates(ctx, {}, alerts, NOW), alerts, tg, NOW, CFG)
    assert plan.ops and plan.digest == []


def test_disabled_book_is_dropped_from_run(monkeypatch):
    monkeypatch.setenv("BOOK_FANDUEL_ENABLED", "0")
    monkeypatch.setenv("BOOK_NOVIG_ENABLED", " 0 ")
    books = build.books_for_scope("light")
    assert "fanduel" not in books and "novig" not in books and "pinnacle" in books
    assert build.books_for_scope("odds", ["fanduel", "betonline"]) == ["betonline"]
    monkeypatch.delenv("BOOK_FANDUEL_ENABLED")
    assert "fanduel" in build.books_for_scope("light")
