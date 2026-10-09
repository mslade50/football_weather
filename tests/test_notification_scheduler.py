from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from pipeline import alert_policy as P
from pipeline import alerts as A
from pipeline.notification_scheduler import NotificationClock, check_owner, inspect_inputs, main, seconds_until_tick
from tests.test_interview_alert_policy import CFG, CTX, GID, NOW, card, run


def test_exact_local_targets_follow_both_dst_transitions():
    for before, expected in (
        ('2026-03-07T22:00:01+00:00', '2026-03-08T12:00:00+00:00'),
        ('2026-10-31T21:00:01+00:00', '2026-11-01T13:00:00+00:00'),
        ('2026-10-09T11:59:59+00:00', '2026-10-09T12:00:00+00:00'),
        ('2026-10-09T12:00:00+00:00', '2026-10-09T21:00:00+00:00'),
    ):
        assert P.next_routine_at(datetime.fromisoformat(before)).isoformat() == expected
    assert seconds_until_tick(datetime.fromisoformat('2026-10-09T11:59:59+00:00')) == 1
    assert P.routine_slot(NOW + timedelta(minutes=4))
    assert P.routine_slot(NOW + timedelta(minutes=5)) is None


def test_clock_failed_work_retries_completed_slots_and_runs_dedupe():
    clock = NotificationClock()
    assert clock.due(NOW - timedelta(minutes=1), 'run', 'ledger')
    assert clock.due(NOW - timedelta(minutes=1), 'run', 'ledger')  # failure never completes
    clock.complete(NOW - timedelta(minutes=1), 'run', 'ledger')
    assert not clock.due(NOW - timedelta(seconds=1), 'run', 'ledger')
    assert clock.due(NOW, 'run', 'ledger')
    clock.complete(NOW, 'run', 'ledger')
    assert not clock.due(NOW + timedelta(seconds=20), 'run', 'ledger')
    assert clock.due(NOW + timedelta(seconds=20), 'next-run', 'ledger')
    assert clock.due(NOW + timedelta(seconds=20), 'run', 'new-confirmation')
    assert clock.due(NOW + timedelta(hours=9), 'run', 'ledger')


def test_late_pending_overflow_rechecks_after_routine_recovery_window():
    clock = NotificationClock()
    candidate = A.Candidate('unsent', 'edge', 'nfl', 'text', kickoff_utc=NOW + timedelta(hours=2))
    clock.complete(NOW, 'run', 'ledger', SimpleNamespace(candidates=[candidate], alerts={'sent': {}}))
    assert clock.due(NOW + timedelta(minutes=1), 'run', 'ledger')
    assert clock.due(NOW + timedelta(minutes=5), 'run', 'ledger')
    clock.complete(NOW + timedelta(minutes=5), 'run', 'ledger', SimpleNamespace(candidates=[candidate], alerts={'sent': {'unsent': 'sent'}}))
    assert not clock.due(NOW + timedelta(minutes=6), 'run', 'ledger')


def test_first_low_signal_before_next_slot_is_immediate_once_then_no_unscheduled_reminders():
    alerts, sent = {}, []
    when = NOW + timedelta(hours=10)  # 18:00 Eastern
    data = card()
    data['kickoff_utc'] = (when + timedelta(hours=2)).isoformat()
    assert run(alerts, when, data, lambda text, chat: sent.append(text) or True).sent
    assert 'First notice now' in sent[0] and 'Probably too late' in sent[0]
    assert not run(alerts, when + timedelta(minutes=1), data, lambda *_: True).sent
    confirmed = {'bets': {'fixture': {'bet_id': 'fixture', 'game_id': GID, 'confirmed': True,
                  'source': 'explicit_user_confirmation', 'line': 48, 'stake': 125}}}
    assert not P.collect(CTX, {'nfl': [data]}, {}, CFG, when, confirmed)


def test_started_games_do_not_get_late_notice_and_failed_notice_retries():
    alerts, data = {}, card()
    when = NOW + timedelta(hours=10)
    data['kickoff_utc'] = (when + timedelta(minutes=10)).isoformat()
    assert run(alerts, when, data, lambda *_: False).failed
    assert run(alerts, when + timedelta(minutes=1), data, lambda *_: True).sent
    assert not run({}, when + timedelta(minutes=10), data, lambda *_: pytest.fail('At kickoff')).sent


def test_first_notice_after_consumed_slot_does_not_wait_past_near_kickoff():
    alerts, sent = {}, []
    assert run(alerts, NOW, card(), lambda text, chat: sent.append(text) or True).sent
    new_game = card()
    new_game['game_id'] = 'nfl:2026:6:c@d'
    new_game['kickoff_utc'] = (NOW + timedelta(minutes=3)).isoformat()
    outcome = run(alerts, NOW + timedelta(minutes=2), new_game, lambda text, chat: sent.append(text) or True)
    assert outcome.sent and outcome.sent[0].record['first_notice_reason'] == 'kickoff_before_next_routine_slot'
    assert len(alerts['routine_delivery']) == 1  # First-notice exception does not consume another routine slot.


def test_local_owner_pins_host_root_and_sha_and_default_mode_has_no_activation(tmp_path, capsys):
    sha = 'a' * 40
    owner = {'schema_version': 1, 'kind': 'local', 'git_sha': sha, 'hostname': 'fixture', 'root': tmp_path.resolve().as_posix()}
    check_owner(owner, sha, 'fixture', tmp_path)
    for change in ({'kind': 'pipeline'}, {'git_sha': 'b' * 40}, {'hostname': 'other'}, {'root': str(tmp_path / 'other')}):
        with pytest.raises(RuntimeError):
            check_owner({**owner, **change}, sha, 'fixture', tmp_path)
    assert main(['--expected-sha', sha]) == 0
    assert '"activation": false' in capsys.readouterr().out


def test_no_notification_input_inspection_never_sends_or_writes_remote_state(tmp_path, monkeypatch):
    import io
    import json

    from pipeline import notification_scheduler as N

    sha = 'a' * 40
    owner = {'schema_version': 1, 'kind': 'local', 'git_sha': sha, 'hostname': 'fixture', 'root': tmp_path.resolve().as_posix()}
    class Client:
        def get_object(self, **kwargs):
            payload = owner if kwargs['Key'] == N.OWNER_KEY else {'schema_version': 1, 'bets': {}} if kwargs['Key'].endswith('bet_confirmations.json') else {'schema_version': 1}
            return {'Body': io.BytesIO(json.dumps(payload).encode())}
        def put_object(self, **kwargs):
            pytest.fail('Inspection must never write remote state')
    monkeypatch.setattr(N, 'load_board', lambda *_: ({'run_id': 'fixture', 'git_sha': sha, 'publication': {'generation': 'fixture'}}, []))
    monkeypatch.setattr(A, 'default_sender', lambda: pytest.fail('Inspection must never resolve a sender'))
    proof = inspect_inputs(Client(), 'bucket', sha, 'fixture', tmp_path)
    assert proof['owner_ready'] and proof['notification_transports'] == 0 and proof['remote_writes'] == 0
    with pytest.raises(SystemExit):
        main(['--expected-sha', sha, '--run', '--verify-only'])


def test_shared_routine_destination_combines_sports_with_two_messages_total():
    cfg = A.Config(interview_policy=True, chat_default='shared', chat_by_sport={'nfl': 'nfl-only', 'cfb': 'cfb-only'})
    alerts, sent = {}, []
    candidates = [A.Candidate(sport, 'edge', sport, 'notice', game_id=sport, tier='mid', summary=sport) for sport in ('nfl', 'cfb')]
    for when in (NOW, NOW + timedelta(hours=9)):
        current = candidates if when == NOW else [A.Candidate('reminder-' + c.key, 'reminder', c.sport, 'reminder', game_id=c.game_id, summary=c.sport) for c in candidates]
        plan = P.plan(current, alerts, {}, when, cfg)
        P.dispatch(plan, alerts, lambda text, chat: sent.append((text, chat)) or True, when, cfg)
    assert len(sent) == 2 and all(chat == 'shared' for text, chat in sent)
    assert all('nfl' in text and 'cfb' in text for text, chat in sent)
