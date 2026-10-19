import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline import alert_policy as P
from pipeline import alerts as A
from pipeline import notification_scheduler as N
from pipeline.publication import BUILD_PROTECTED_NAMES, digest, encoded
from pipeline.resident import atomic_json
from tests.test_interview_alert_policy import GID, NOW, card

SHA = 'a' * 40
META = {'run_id': 'fixture', 'git_sha': SHA, 'publication': {'generation': 'fixture'}}
EMPTY = {'schema_version': 1, 'bets': {}}


def test_exact_local_targets_follow_both_dst_transitions():
    for before, expected in (
        ('2026-03-07T22:00:01+00:00', '2026-03-08T12:00:00+00:00'),
        ('2026-10-31T21:00:01+00:00', '2026-11-01T13:00:00+00:00'),
        ('2026-10-09T11:59:59+00:00', '2026-10-09T12:00:00+00:00'),
        ('2026-10-09T12:00:00+00:00', '2026-10-09T21:00:00+00:00'),
    ):
        assert P.next_routine_at(datetime.fromisoformat(before)).isoformat() == expected
    assert N.seconds_until_tick(datetime.fromisoformat('2026-10-09T11:59:59+00:00')) == 1


def forbid_transports(monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    monkeypatch.setenv('TELEGRAM_DISABLED', '1')
    for name in ('default_sender', 'dispatch', '_mark'):
        monkeypatch.setattr(A, name, lambda *a, **kw: pytest.fail('No delivery/receipt path permitted'))
    monkeypatch.setattr(A.Config, 'from_env', lambda *a, **kw: pytest.fail('No Telegram config permitted'))
    monkeypatch.setattr(P, 'dispatch', lambda *a, **kw: pytest.fail('No policy dispatch permitted'))


class Client:
    def __init__(self, root, cards=None):
        self.owner = {'schema_version': 1, 'kind': 'review_outbox', 'git_sha': SHA,
                      'hostname': 'fixture', 'root': root.resolve().as_posix()}
        self.confirmations = EMPTY
        self.meta = META
        self.writes = []
        self.reads = []
        self.remote = None
    def get_object(self, **kwargs):
        key = kwargs['Key']
        self.reads.append(key)
        if key == N.OUTBOX_KEY and self.remote is None:
            raise RuntimeError('NoSuchKey')
        value = self.owner if key == N.OWNER_KEY else self.confirmations if key.endswith('bet_confirmations.json') else self.remote if key == N.OUTBOX_KEY else self.meta
        return {'Body': io.BytesIO(encoded(value)), 'ETag': 'fixture-etag'}
    def put_object(self, **kwargs):
        self.writes.append(kwargs)
        self.remote = json.loads(kwargs['Body'])
    def head_object(self, **kwargs):
        self.reads.append(kwargs['Key'])
        if self.remote is None:
            raise RuntimeError('NoSuchKey')
        return {'ETag': 'fixture-etag'}


def setup_runtime(monkeypatch, tmp_path, cards=None):
    forbid_transports(monkeypatch)
    client = Client(tmp_path)
    monkeypatch.setenv('COMPUTERNAME', 'fixture')
    monkeypatch.setattr(N, 'revision', lambda: SHA)
    monkeypatch.setattr(N, 'load_repo_dotenv', lambda: None)
    monkeypatch.setattr(N.r2, 'config_from_env', lambda: SimpleNamespace(bucket='bucket'))
    monkeypatch.setattr(N.r2, 'make_client', lambda *a, **kw: client)
    monkeypatch.setattr(N, 'load_board', lambda *_: (client.meta, cards or []))
    return client


def test_default_plan_zero_io_without_any_credentials(tmp_path, monkeypatch, capsys):
    forbid_transports(monkeypatch)
    monkeypatch.setattr(N, 'load_repo_dotenv', lambda: pytest.fail('Default must not load credentials'))
    monkeypatch.setattr(N.r2, 'make_client', lambda *a, **kw: pytest.fail('Default must not resolve client'))
    assert N.main(['--expected-sha', SHA, '--root', str(tmp_path)]) == 0
    assert '"activation": false' in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_verify_only_no_token_no_sends_no_remote_or_local_outbox_write(tmp_path, monkeypatch, capsys):
    client = setup_runtime(monkeypatch, tmp_path)
    assert N.main(['--expected-sha', SHA, '--root', str(tmp_path), '--verify-only']) == 0
    assert not client.writes and not list(tmp_path.iterdir())
    assert '"remote_writes": 0' in capsys.readouterr().out
    assert not any('telegram' in key or key.endswith('/alerts.json') for key in client.reads)


def test_resident_default_run_durable_local_outbox_without_token_sends_or_remote_writes(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    args = ['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once']
    assert N.main(args) == 0
    result = N.read_outbox(tmp_path, datetime.now(timezone.utc))
    assert result['delivery_integration'] == 'unwired' and result['notification_transports'] == 0
    assert not client.writes and N.OWNER_KEY not in client.reads
    assert N.main(args) == 0  # restart preserves observations and never synthesizes receipts
    assert not any('telegram' in key or key.endswith('/alerts.json') for key in client.reads)


def test_generating_reading_and_regenerating_never_mark_delivered(monkeypatch, tmp_path):
    forbid_transports(monkeypatch)
    first = N.make_outbox(META, [card('High Impact')], EMPTY, None, NOW)
    second = N.make_outbox(META, [card('High Impact')], EMPTY, first, NOW + timedelta(seconds=10))
    assert first['items'][0]['outbox_id'] == second['items'][0]['outbox_id']
    assert second['observations'] == first['observations']
    assert second['items'][0]['status'] == 'generated'
    assert second['items'][0]['delivery_state'] == 'unacknowledged'
    assert not any(key in second for key in ('sent', 'cleared_bets', 'routine_delivery', 'claims', 'receipts'))
    atomic_json(tmp_path / 'outbox.json', second)
    atomic_json(tmp_path / 'heartbeat.json', {'status': 'running', 'outbox_sha256': digest(encoded(second))})
    before = (tmp_path / 'outbox.json').read_bytes()
    N.read_outbox(tmp_path, NOW + timedelta(seconds=11))
    assert (tmp_path / 'outbox.json').read_bytes() == before


def test_one_routine_batch_combines_sports_and_waiting_not_delivered():
    cfb = card()
    cfb.update(sport='cfb', game_id='cfb:2026:6:c@d')
    result = N.make_outbox(META, [card(), cfb], EMPTY, None, NOW)
    assert len(result['items']) == 1 and result['items'][0]['timing'] == 'routine'
    assert {c['sport'] for c in result['items'][0]['candidates']} == {'nfl', 'cfb'}
    later = N.make_outbox(META, [card()], EMPTY, result, NOW + timedelta(minutes=6))
    assert not later['items'] and later['waiting']
    assert later['routine_delivery_limit_per_local_day'] == 2


def test_late_first_notice_and_missing_liquidity_do_not_suppress_watch():
    when, data = NOW + timedelta(hours=10), card()
    data['kickoff_utc'] = (when + timedelta(hours=2)).isoformat()
    result = N.make_outbox(META, [data], EMPTY, None, when)
    row = result['items'][0]['candidates'][0]
    assert result['items'][0]['timing'] == 'immediate'
    assert 'First notice now' in row['text_html'] and 'Probably too late' in row['text_html']
    assert row['classification'] == 'weather_watch' and row['verified_cash_recommendation'] is False
    assert not N.make_outbox(META, [data], EMPTY, result, when + timedelta(hours=2))['items']


def test_explicit_confirmation_withdraws_watch_clear_stays_unacknowledged(monkeypatch):
    forbid_transports(monkeypatch)
    bets = {'schema_version': 1, 'bets': {'fixture': {'bet_id': 'fixture', 'game_id': GID, 'confirmed': True,
            'source': 'explicit_user_confirmation', 'line': 48, 'stake': 125}}}
    original = N.make_outbox(META, [card('High Impact')], EMPTY, None, NOW)
    assert original['items']
    assert not N.make_outbox(META, [card('Very High Impact')], bets, original, NOW)['items']
    closed = card()
    closed['stadium']['roof_type'] = 'dome'
    clear = N.make_outbox(META, [closed], bets, original, NOW)
    again = N.make_outbox(META, [closed], bets, clear, NOW + timedelta(seconds=10))
    assert clear['items'][0]['candidates'][0]['classification'] == 'clear_invalidation'
    assert again['items'][0]['outbox_id'] == clear['items'][0]['outbox_id']
    assert again['items'][0]['delivery_state'] == 'unacknowledged' and 'cleared_bets' not in again
    unknown = card('No Impact')
    unknown['weather']['ensemble_status'] = 'point_only'
    assert not N.make_outbox(META, [unknown], bets, clear, NOW)['items']


@pytest.mark.parametrize('mode', ['expired', 'future', 'degraded', 'hash'])
def test_reader_fails_closed_after_expiry_clock_error_failure_or_uncommitted_write(tmp_path, mode):
    payload = N.make_outbox(META, [], EMPTY, None, NOW)
    atomic_json(tmp_path / 'outbox.json', payload)
    atomic_json(tmp_path / 'heartbeat.json', {'status': 'degraded' if mode == 'degraded' else 'running',
        'outbox_sha256': 'wrong' if mode == 'hash' else digest(encoded(payload))})
    when = NOW + timedelta(seconds=30) if mode == 'expired' else NOW - timedelta(seconds=1) if mode == 'future' else NOW
    with pytest.raises(RuntimeError):
        N.read_outbox(tmp_path, when)


def test_source_change_or_confirmation_race_never_commits_batch(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    old = N.read_confirmations
    calls = []
    def changed(*args):
        value = old(*args)
        calls.append(1)
        return value if len(calls) == 1 else {**value, 'changed': True}
    monkeypatch.setattr(N, 'read_confirmations', changed)
    assert N.main(['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once']) == 1
    assert not (tmp_path / 'outbox.json').exists() and not client.writes
    assert json.loads((tmp_path / 'heartbeat.json').read_text())['status'] == 'degraded'


def test_publish_requires_separate_owner_and_cas_never_touches_telegram(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    args = ['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once', '--publish']
    client.owner['kind'] = 'local'
    assert N.main(args) == 1 and not client.writes
    client.owner['kind'] = 'review_outbox'
    assert N.main(args) == 0 and client.writes[0]['IfNoneMatch'] == '*'
    assert N.main(args) == 0 and client.writes[1]['IfMatch'] == 'fixture-etag'
    assert all(w['Key'] == N.OUTBOX_KEY for w in client.writes)
    assert not any('telegram' in key or key.endswith('/notification_owner.json') for key in client.reads)
    assert {'owner_review_outbox', 'review_outbox_owner', 'owner_review_receipts'} <= BUILD_PROTECTED_NAMES


def test_wrong_owner_and_invalid_modes_fail_closed(tmp_path):
    owner = Client(tmp_path).owner
    N.check_owner(owner, SHA, 'fixture', tmp_path)
    for change in ({'kind': 'pipeline'}, {'git_sha': 'b' * 40}, {'hostname': 'other'}, {'root': str(tmp_path / 'other')}):
        with pytest.raises(RuntimeError):
            N.check_owner({**owner, **change}, SHA, 'fixture', tmp_path)
    for flags in (['--run', '--verify-only'], ['--verify-only', '--publish'], ['--publish']):
        with pytest.raises(SystemExit):
            N.main(['--expected-sha', SHA, *flags])


def test_remote_cas_failure_is_not_retried_or_counted_as_delivery(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    attempted = []
    def conflict(**kwargs):
        attempted.append(kwargs)
        raise RuntimeError('PreconditionFailed')
    monkeypatch.setattr(client, 'put_object', conflict)
    assert N.main(['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once', '--publish']) == 1
    assert len(attempted) == 1 and not client.writes
    assert json.loads((tmp_path / 'heartbeat.json').read_text())['status'] == 'degraded'
    with pytest.raises(RuntimeError):
        N.read_outbox(tmp_path, datetime.now(timezone.utc))


def test_invalid_confirmation_or_new_source_stops_without_commit(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    client.confirmations = {'schema_version': 1, 'bets': {'bad': {'confirmed': False}}}
    args = ['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once']
    assert N.main(args) == 1 and not (tmp_path / 'outbox.json').exists()
    client.confirmations = EMPTY
    client.meta = {**META, 'git_sha': 'b' * 40}
    assert N.main(args) == 1 and not (tmp_path / 'outbox.json').exists()


def test_slow_validation_cannot_commit_already_expired_review_material(tmp_path, monkeypatch):
    client = setup_runtime(monkeypatch, tmp_path)
    expired = N.make_outbox(META, [], EMPTY, None, NOW - timedelta(days=1))
    monkeypatch.setattr(N, 'make_outbox', lambda *_: expired)
    assert N.main(['--expected-sha', SHA, '--root', str(tmp_path), '--run', '--once']) == 1
    assert not (tmp_path / 'outbox.json').exists() and not client.writes
