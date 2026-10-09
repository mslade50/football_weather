from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline import alert_policy as P
from pipeline import alerts as A
from pipeline import state

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)  # 08:00 EDT
GID = "nfl:2026:6:a@b"
CFG = A.Config(interview_policy=True, chat_default="fixture-chat", board_url="https://fixture.invalid")
CTX = SimpleNamespace(run_id="fixture")


def card(label="Mid Impact"):
    return {
        "game_id": GID,
        "sport": "nfl",
        "season": 2026,
        "week": 6,
        "run_id": "fixture",
        "kickoff_utc": (NOW + timedelta(days=2)).isoformat(),
        "status": "scheduled",
        "home": {"short": "B"},
        "away": {"short": "A"},
        "stadium": {"roof_type": "open"},
        "weather": {
            "fetched_at": NOW.isoformat(),
            "point_aged": False,
            "wind_fg": 18,
            "temp_fg": 45,
            "rain_fg": 0,
            "ensemble_unverified_sources": [],
            "ensemble_aged_sources": [],
        },
        "signal": {"label": label, "drivers": ["wind"]},
        "fair": {},
        "odds": {},
        "consensus": {"spread_open": -3, "spread_now": -3, "total_now": 48, "true_total_open": 55, "true_total_open_status": "attested"},
    }


def run(alerts, when, candidate_card, sender):
    candidates = A.collect_candidates(CTX, {"nfl": [candidate_card]}, alerts, CFG, when)
    planned = A.plan(candidates, alerts, {}, when, CFG)
    return A.dispatch(planned, alerts, sender, when, CFG)


def test_routine_slots_dst_daily_cap_and_failed_send_retry_without_silence_as_bet():
    alerts = state.migrate(None, "alerts")
    sent = []

    def sender(text, chat):
        return sent.append((text, chat)) or True

    assert not run(alerts, NOW - timedelta(minutes=1), card(), sender).sent
    failed = run(alerts, NOW, card(), lambda *_: False)
    assert failed.failed and not alerts["sent"] and not alerts.get("routine_delivery")
    assert len(run(alerts, NOW, card(), sender).sent) == 1
    assert "Probably too late" in sent[0][0] and "Original 55" in sent[0][0]
    assert not run(alerts, NOW + timedelta(minutes=15), card(), sender).sent
    assert len(run(alerts, NOW + timedelta(hours=9), card(), sender).sent) == 1
    assert not run(alerts, NOW + timedelta(hours=9, minutes=30), card(), sender).sent
    assert len(sent) == 2 and not alerts.get("_confirmations")
    assert P.routine_slot(datetime(2026, 11, 9, 13, tzinfo=timezone.utc)).endswith("|08")  # EST
    assert P.routine_slot(datetime(2026, 11, 9, 12, tzinfo=timezone.utc)) is None


def test_new_massive_first_signal_immediate_but_confirmed_bet_only_gets_clear_once():
    alerts = state.migrate(None, "alerts")
    sent = []

    def sender(text, chat):
        return sent.append(text) or True

    assert run(alerts, NOW - timedelta(hours=1), card("High Impact"), sender).sent
    bid = "fixture-confirmation"
    alerts["_confirmations"] = {
        "bets": {
            bid: {
                "bet_id": bid,
                "game_id": GID,
                "confirmed": True,
                "source": "explicit_user_confirmation",
                "line": 48,
                "stake": 125,
            }
        }
    }
    assert not run(alerts, NOW, card("Very High Impact"), sender).sent
    clear = card(A.SIGNAL_NONE)
    clear["weather"].update(wind_fg=3, temp_fg=65)
    failed = run(alerts, NOW + timedelta(minutes=1), clear, lambda *_: False)
    assert failed.failed and not alerts.get("cleared_bets")
    assert run(alerts, NOW + timedelta(minutes=2), clear, sender).sent[0].family == "clear"
    assert "<b>CLEAR" in sent[-1] and "$125" in sent[-1]
    assert not run(alerts, NOW + timedelta(minutes=3), card("High Impact"), sender).sent
    assert not run(alerts, NOW + timedelta(minutes=4), clear, sender).sent
    assert len(sent) == 2


def test_degraded_missing_or_stale_weather_cannot_clear_confirmed_bet():
    for change in (
        {"temp_fg": None},
        {"point_aged": True},
        {"ensemble_unverified_sources": ["gefs"]},
        {"ensemble_aged_sources": ["ecmwf"]},
        {"fetched_at": (NOW - timedelta(hours=4)).isoformat()},
    ):
        data = card(A.SIGNAL_NONE)
        data["weather"].update(change)
        assert P.clear_reason(data, CFG, NOW) is None
    assert "roof" in P.clear_reason({**card(), "stadium": {"roof_type": "dome"}}, CFG, NOW)


def test_fake_first_seen_is_never_labeled_true_original_or_late_and_liquidity_does_not_gate():
    data = card()
    data["consensus"].pop("true_total_open_status")
    assert "Probably too late" not in P.late_context(data)
    assert "unknown" in P.late_context(data)
    assert P.collect(CTX, {"nfl": [data]}, {}, CFG, NOW, {})
    data["sport"] = "cfb"
    data["consensus"]["spread_open"] = -11
    assert not P.collect(CTX, {"cfb": [data]}, {}, CFG, NOW, {})


def test_cfb_hard_invalidation_uses_encoded_opening_spread_not_current_move():
    data = card()
    data['sport'] = 'cfb'
    data['consensus']['spread_now'] = -14
    assert P.clear_reason(data, CFG, NOW) is None
    data['consensus']['spread_open'] = -14
    assert 'spread' in P.clear_reason(data, CFG, NOW)


def test_verbose_routine_notices_use_compact_summaries_and_firsts_before_reminders():
    alerts = state.migrate(None, 'alerts')
    candidates = [A.Candidate(f'fixture-{i}', 'reminder' if i == 0 else 'edge', 'nfl', 'x' * 4000,
                              game_id=f'game-{i}', tier='mid', summary=f'compact game {i}') for i in range(10)]
    planned = P.plan(candidates, alerts, {}, NOW, CFG)
    sent = []
    outcome = P.dispatch(planned, alerts, lambda text, chat: sent.append(text) or True, NOW, CFG)
    assert len(outcome.sent) == 10 and len(sent) == 1
    assert sent[0].index('compact game 1') < sent[0].index('compact game 0')


def test_confirmation_arriving_after_planning_suppresses_planned_notice():
    alerts = state.migrate(None, 'alerts')
    candidates = P.collect(CTX, {'nfl': [card()]}, alerts, CFG, NOW, {})
    planned = P.plan(candidates, alerts, {}, NOW, CFG)
    ledger = {'bets': {'fixture-confirmation': {'bet_id': 'fixture-confirmation', 'game_id': GID,
              'confirmed': True, 'source': 'explicit_user_confirmation', 'line': 48, 'stake': 125}}}
    outcome = P.dispatch(planned, alerts, lambda *_: (_ for _ in ()).throw(AssertionError('Must not send')),
                         NOW, CFG, confirmation_reader=lambda: ledger)
    assert not outcome.sent and not alerts.get('routine_delivery')
