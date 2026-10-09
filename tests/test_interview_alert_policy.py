from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
            "point_stage": "refined_multimodel",
            "ensemble_status": "full_members",
            "ensemble_eligible": True,
            "ensemble_members": 82,
            "ensemble_verification_errors": {},
            "ensemble_fetched_at": {source: NOW.isoformat() for source in ('ifs', 'gefs')},
            "ensemble_source_versions": {
                source: {model: {
                    'last_run_initialisation_time': int((NOW - timedelta(hours=2)).timestamp()),
                    'last_run_modification_time': int((NOW - timedelta(hours=1)).timestamp()),
                    'last_run_availability_time': int((NOW - timedelta(hours=1)).timestamp()),
                    'update_interval_seconds': 21600,
                    'data_end_time': int((NOW + timedelta(days=10)).timestamp()),
                } for model in models}
                for source, models in {'ifs': ('ecmwf_ifs025_ensemble',), 'gefs': ('ncep_gefs025', 'ncep_gefs05')}.items()
            },
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


@pytest.mark.parametrize('path,value', [
    (('sport',), 'cfb-missing'),
    (('kickoff_utc',), None),
    (('kickoff_utc',), NOW.replace(tzinfo=None).isoformat()),
    (('kickoff_utc',), (NOW - timedelta(minutes=1)).isoformat()),
    (('status',), None),
    (('home',), {}),
    (('away',), None),
    (('stadium',), {'roof_type': 'retractable', 'roof_state': None}),
    (('stadium',), {'roof_type': 'retractable', 'roof_state': 'outdoors'}),
    (('stadium',), {}),
    (('weather', 'ensemble_status'), 'unavailable_degraded'),
    (('weather', 'ensemble_status'), 'partial_members_degraded'),
    (('weather', 'ensemble_status'), None),
    (('weather', 'ensemble_verification_errors'), {'gefs': 'Source verification failed'}),
    (('weather', 'ensemble_unverified_sources'), ['ifs']),
    (('weather', 'ensemble_aged_sources'), ['gefs']),
    (('weather', 'point_aged'), True),
    (('weather', 'point_stage'), 'unavailable'),
    (('weather', 'point_stage'), 'nws_first_pass'),  # Missing original source clock.
    (('weather', 'fetched_at'), (NOW - timedelta(hours=4)).isoformat()),
    (('weather', 'fetched_at'), (NOW + timedelta(minutes=1)).isoformat()),
    (('weather', 'fetched_at'), NOW.replace(tzinfo=None).isoformat()),
    (('weather', 'ensemble_fetched_at'), {}),
    (('weather', 'ensemble_fetched_at', 'ifs'), (NOW - timedelta(hours=4)).isoformat()),
    (('weather', 'ensemble_source_versions'), {}),
    (('weather', 'ensemble_source_versions', 'gefs', 'ncep_gefs025', 'last_run_availability_time'), int((NOW + timedelta(minutes=1)).timestamp())),
    (('weather', 'ensemble_source_versions', 'gefs', 'ncep_gefs025', 'last_run_initialisation_time'), int((NOW - timedelta(hours=24)).timestamp())),
    (('weather', 'ensemble_source_versions', 'ifs', 'ecmwf_ifs025_ensemble', 'data_end_time'), int(NOW.timestamp())),
    (('weather', 'wind_fg'), None),
])
def test_unknown_invalidation_never_sends_or_consumes_clear_then_recovery_can_clear(path, value):
    data = card(A.SIGNAL_NONE)
    data['weather'].update(wind_fg=3, temp_fg=65)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    alerts = state.migrate(None, 'alerts')
    alerts['_confirmations'] = {'bets': {'fixture-bet': {'bet_id': 'fixture-bet', 'game_id': GID,
        'confirmed': True, 'source': 'explicit_user_confirmation', 'line': 48, 'stake': 125}}}
    messages = []
    assert P.clear_reason(data, CFG, NOW) is None
    assert not run(alerts, NOW, data, lambda *_: messages.append('invalid') or True).sent
    assert not messages and not alerts.get('cleared_bets') and not alerts['sent']
    recovered = card(A.SIGNAL_NONE)
    recovered['weather'].update(wind_fg=3, temp_fg=65)
    assert run(alerts, NOW, recovered, lambda *_: messages.append('proven-clear') or True).sent
    assert len(messages) == 1 and 'fixture-bet' in alerts['cleared_bets']


def test_cfb_missing_opening_spread_and_applicable_ancillary_inputs_are_unknown():
    data = card(A.SIGNAL_NONE)
    data['sport'] = 'cfb'
    data['weather'].update(wind_fg=3, temp_fg=65)
    data['consensus'].pop('spread_open')
    assert P.clear_reason(data, CFG, NOW) is None
    data['consensus']['spread_open'] = -3
    assert P.clear_reason(data, CFG, NOW)
    data['weather']['temp_fg'] = 85
    assert P.clear_reason(data, CFG, NOW) is None
    data.update(travel_alt=0, home_temp=65, away_temp=65)
    assert P.clear_reason(data, CFG, NOW)


def test_clear_recomputes_actual_weather_and_ignores_alert_threshold_changes():
    data = card(A.SIGNAL_NONE)  # Stale label says no signal; actual wind still qualifies.
    assert P.clear_reason(data, CFG, NOW) is None
    data['weather'].update(wind_fg=10, temp_fg=55)
    assert P.clear_reason(data, A.Config(min_tier='high'), NOW) is None
    data['stadium'] = {'roof_type': 'open', 'roof_state': 'closed'}
    assert P.clear_reason(data, CFG, NOW) is None  # Fixed construction wins.


def test_clear_rechecks_original_nws_source_age_instead_of_reusing_point_aged_flag():
    data = card(A.SIGNAL_NONE)
    data['weather'].update(wind_fg=3, temp_fg=65, point_stage='nws_first_pass',
        point_source_updated_at={'nws': (NOW - timedelta(hours=13)).isoformat()})
    assert P.clear_reason(data, CFG, NOW) is None
    data['weather']['point_source_updated_at']['nws'] = (NOW - timedelta(hours=1)).isoformat()
    assert P.clear_reason(data, CFG, NOW)
