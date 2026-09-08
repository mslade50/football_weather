"""OPS-notice noise controls.

* Telegram SYSTEM notices are off by default
* opt-in notices group repeated provider failures under one component key/day
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
from tests.test_alerts_rules import CFG, NOW, _ctx, _fresh, card


def _ops_keys(ctx) -> list[str]:
    alerts, _ = _fresh()
    return sorted(c.key for c in A.ops_candidates(ctx, {}, alerts, NOW))


def test_degradation_key_is_stable_across_changing_counts():
    a, b = _ctx(), _ctx()
    a.degrade("odds.merge", "cfb: 136 unresolved book games/names (of 249)", "warn")
    b.degrade("odds.merge", "cfb: 239 unresolved book games/names (of 312)", "warn")
    assert _ops_keys(a) == _ops_keys(b) == ["degr|odds.merge|2026-09-18"]


def test_collect_candidates_keeps_system_chatter_off_telegram_by_default():
    ctx = _ctx()
    for i in range(85):
        ctx.degrade("weather", f"game {i}: open-meteo unavailable; NWS-only forecast", "warn")
    alerts, _ = _fresh()

    assert A.collect_candidates(ctx, {}, alerts, A.Config(), NOW) == []
    bet_candidates = A.collect_candidates(ctx, {"nfl": [card()]}, alerts, A.Config(), NOW)
    assert [candidate.family for candidate in bet_candidates] == ["edge"]

    enabled = A.Config(system_alerts=True)
    candidates = A.collect_candidates(ctx, {}, alerts, enabled, NOW)
    assert len(candidates) == 1
    assert candidates[0].key == "degr|weather|2026-09-18"
    assert "85 issues this run" in candidates[0].text


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


def test_fatal_degradations_are_owned_by_the_workflow_failure_ping():
    ctx = _ctx()
    ctx.degrade("weather", "provider failed", "error")
    assert _ops_keys(ctx) == []


def test_volume_drop_is_scoped_and_bypasses_quiet_hours():
    nfl, cfb = _ctx(), _ctx()
    nfl.degrade("odds.volume", "nfl:2026:3: betcris total: 5 (usual ~30)", "warn")
    cfb.degrade("odds.volume", "cfb:2026:3: betcris total: 5 (usual ~30)", "warn")
    alerts, tg = _fresh()
    nfl_c = A.ops_candidates(nfl, {}, alerts, NOW)[0]
    cfb_c = A.ops_candidates(cfb, {}, alerts, NOW)[0]
    assert nfl_c.key != cfb_c.key and nfl_c.bypass_quiet and cfb_c.bypass_quiet

    quiet_plan = A.plan([nfl_c], alerts, tg, NOW.replace(hour=6), CFG)  # 02:00 ET
    assert quiet_plan.queued == [] and quiet_plan.ops == [nfl_c]

    # Failed transport leaves alerts.json unmarked, so the still-active incident
    # retries. A successful retry marks it and subsequent runs are silent.
    failed = A.dispatch(quiet_plan, alerts, lambda text, chat: False, NOW, CFG)
    assert failed.failed == [nfl_c] and not pstate.alert_sent(alerts, nfl_c.key)
    retry = A.ops_candidates(nfl, {}, alerts, NOW)
    assert len(retry) == 1 and retry[0].key == nfl_c.key
    sent = A.dispatch(A.plan(retry, alerts, tg, NOW, CFG), alerts, lambda text, chat: True, NOW, CFG)
    assert sent.n_messages == 1 and pstate.alert_sent(alerts, nfl_c.key)
    assert A.ops_candidates(nfl, {}, alerts, NOW) == []


def test_no_schedule_match_names_are_not_unresolved():
    ctx = _ctx()
    ctx.unresolved_names.extend(["fanduel:Bryant@Stonehill:no-schedule-match", "kalshi:Lehigh@Holy Cross:no-schedule-match"])
    assert _ops_keys(ctx) == []
    ctx.unresolved_names.append("fanduel:Long Island")
    assert _ops_keys(ctx) == ["names|fanduel|2026-09-18"]


def test_ops_candidates_are_one_grouped_message():
    ctx = _ctx()
    for i in range(6):
        ctx.degrade("weather", f"thing {i} broke badly", "warn")
    alerts, tg = _fresh()
    cands = A.ops_candidates(ctx, {}, alerts, NOW)
    assert len(cands) == 1
    plan = A.plan(cands, alerts, tg, NOW, CFG)
    assert plan.send == [] and len(plan.ops) == 1
    sent: list[str] = []
    out = A.dispatch(plan, alerts, lambda text, chat: sent.append(text) or True, NOW, CFG)
    assert out.n_messages == 1 and "SYSTEM" in sent[0]
    assert "6 issues this run" in sent[0]
    assert all(pstate.alert_sent(alerts, c.key) for c in cands)
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
