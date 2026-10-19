from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.resident import LIVE_KEY, ConditionalPublisher, OwnerLock, ResidentCollector

NOW = datetime(2030, 10, 1, 12, tzinfo=timezone.utc)


def game(gid='nfl:2030:4:lar@phi', run='generation'):
    return {'game_id': gid, 'sport': gid[:3], 'run_id': run, 'kickoff_utc': '2030-10-02T17:00:00Z',
            'execution_markets': [{'book': 'kalshi', 'line': 46.5}]}


def snapshot(now=NOW):
    return {'checked_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=15)).isoformat(),
            'quotes': [{'book': 'kalshi', 'line': 46.5, 'liquidity_status': 'verified', 'depth_fetched_at': now.isoformat()}]}


def test_single_process_owner_cannot_be_stolen_and_releases(tmp_path):
    path = tmp_path / 'owner.lock'
    with OwnerLock(path):
        with pytest.raises(RuntimeError, match='Another football'):
            with OwnerLock(path):
                pytest.fail('second owner acquired')
    with OwnerLock(path):
        assert path.is_file()


def test_revision_heartbeat_and_generation_invalidation_do_not_relabel_clocks(tmp_path):
    current = {'run_id': 'generation', 'git_sha': 'a' * 40}
    now = [NOW]
    sent = []
    worker = ResidentCollector(tmp_path, 'a' * 40, lambda sport: (current, [game(run=current['run_id'])]),
                               lambda cards, raw: {cards[0]['game_id']: snapshot(now[0])}, sent.append, lambda: now[0])
    receipt = worker.cycle('nfl')
    assert receipt['git_sha'] == 'a' * 40
    assert receipt['status'] == 'fresh'
    assert receipt['publication_status'] == 'published'
    assert receipt['snapshots'][game()['game_id']]['checked_at'] == NOW.isoformat()
    assert sent[0]['base_run_id'] == 'generation'
    heartbeat = json.loads((tmp_path / 'heartbeat.json').read_text())
    assert heartbeat['owner_id'] == worker.owner_id and heartbeat['sequence'] == 1
    assert 'snapshots' not in heartbeat
    current['run_id'] = 'new-generation'
    worker.collector = lambda *_: {}
    receipt = worker.cycle('nfl')
    assert receipt['snapshots'] == {} and receipt['base_run_id'] == 'new-generation'


def test_expired_future_and_failed_quotes_never_get_a_new_clock(tmp_path):
    rows = [game('nfl:2030:4:a@b'), game('nfl:2030:4:c@d'), game('nfl:2030:4:e@f')]
    results = {rows[0]['game_id']: snapshot(NOW - timedelta(seconds=16)),
               rows[1]['game_id']: snapshot(NOW + timedelta(seconds=1)),
               rows[2]['game_id']: {**snapshot(), 'quotes': [{'liquidity_status': 'unknown'}]}}
    worker = ResidentCollector(tmp_path, 'a' * 40, lambda sport: ({'run_id': 'generation', 'git_sha': 'a' * 40}, rows),
                               lambda *_: results, clock=lambda: NOW)
    receipt = worker.cycle('nfl')
    assert receipt['status'] == 'degraded' and receipt['snapshots'] == {}


def test_mixed_generation_never_fetches_or_publishes_and_errors_hide_secrets(tmp_path):
    def forbidden(*_):
        pytest.fail('fetch/publication forbidden')
    worker = ResidentCollector(tmp_path, 'a' * 40, lambda sport: ({'run_id': 'generation'}, [game(run='other')]),
                               forbidden, forbidden, clock=lambda: NOW)
    assert worker.cycle('nfl')['failure_type'] == 'ValueError'
    def broken(_):
        raise RuntimeError('signed secret URL must not appear')
    worker.loader = broken
    receipt = worker.cycle('nfl')
    assert receipt['failure_type'] == 'RuntimeError'
    assert 'secret' not in json.dumps(receipt)


def test_cohorts_rotate_without_gating_on_liquidity(tmp_path):
    rows = [game(f'nfl:2030:4:a{i}@b{i}') for i in range(5)]
    checked = []
    def collector(cards, raw):
        checked.extend(c['game_id'] for c in cards)
        return {}
    worker = ResidentCollector(tmp_path, 'a' * 40, lambda sport: ({'run_id': 'generation', 'git_sha': 'a' * 40}, rows), collector,
                               clock=lambda: NOW, max_games=2)
    for _ in range(3):
        worker.cycle('nfl')
    assert {r['game_id'] for r in rows}.issubset(checked)


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class Client:
    def __init__(self, prior=None):
        self.prior, self.calls = prior, []

    def get_object(self, **kwargs):
        assert kwargs['Key'] == LIVE_KEY
        if self.prior is None:
            raise Missing()
        return {'ETag': 'prior-etag', 'Body': io.BytesIO(json.dumps(self.prior).encode())}

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        return {'ETag': f'etag-{len(self.calls)}'}


def test_conditional_publication_refuses_remote_owner_and_uses_cas():
    active = Client({'owner_id': 'other', 'heartbeat_at': NOW.isoformat()})
    with pytest.raises(RuntimeError, match='owner is active'):
        ConditionalPublisher(active, 'bucket', 'mine', NOW)
    assert not active.calls
    missing = Client()
    writer = ConditionalPublisher(missing, 'bucket', 'mine', NOW)
    writer({'owner_id': 'mine'})
    assert missing.calls[0]['IfNoneMatch'] == '*'
    writer({'owner_id': 'mine'})
    assert missing.calls[1]['IfMatch'] == 'etag-1'
    stale = Client({'owner_id': 'other', 'heartbeat_at': (NOW - timedelta(seconds=61)).isoformat()})
    ConditionalPublisher(stale, 'bucket', 'mine', NOW)({'owner_id': 'mine'})
    assert stale.calls[0]['IfMatch'] == 'prior-etag'


def test_lost_conditional_write_is_not_blindly_retried():
    client = Client()
    def reject(**_):
        raise RuntimeError('PreconditionFailed')
    client.put_object = reject
    writer = ConditionalPublisher(client, 'bucket', 'mine', NOW)
    with pytest.raises(RuntimeError, match='PreconditionFailed'):
        writer({'owner_id': 'mine'})
    assert writer.etag is None


def test_different_source_revision_never_collects_or_publishes(tmp_path):
    def forbidden(*_):
        pytest.fail('Mismatched source must never collect or publish')
    worker = ResidentCollector(tmp_path, 'a' * 40,
                               lambda sport: ({'run_id': 'generation', 'git_sha': 'b' * 40}, [game()]),
                               forbidden, forbidden, clock=lambda: NOW)
    assert worker.cycle('nfl')['failure_type'] == 'ValueError'
