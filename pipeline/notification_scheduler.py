"""Explicitly activated local notification clock; default mode prints a plan.

The quote resident never sends. This runner requires an operator-selected local
notification owner in R2, and the pipeline refuses delivery while that owner is
selected. No schedules, ownership configuration or credentials are created here.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from pipeline import alert_policy, published_alerts, state
from pipeline.outputs import r2
from pipeline.publication import digest, verify_generation
from pipeline.resident import OwnerLock, atomic_json, load_board, revision, stamp
from utils.env import load_repo_dotenv

OWNER_KEY = 'board/notification_owner.json'


def check_owner(owner, expected_sha, hostname, root):
    if (not isinstance(owner, dict) or owner.get('schema_version') != 1 or owner.get('kind') != 'local'
            or owner.get('git_sha') != expected_sha or owner.get('hostname') != hostname
            or str(owner.get('root', '')).casefold() != Path(root).resolve().as_posix().casefold()):
        raise RuntimeError('Explicit local notification ownership unavailable or mismatched')


def seconds_until_tick(now, interval=10):
    return min(interval, max(0.05, (alert_policy.next_routine_at(now) - now).total_seconds()))


def inspect_inputs(client, bucket, sha, hostname, root):
    """Read and validate remote inputs, with no receipt writes or transports."""
    from pipeline import alerts
    from pipeline.run_context import RunContext

    meta, cards = load_board(client, bucket, 'all')
    if meta.get('git_sha') != sha:
        raise RuntimeError('Published source differs from selected notification revision')
    owner_raw = r2.get_object(client, bucket, OWNER_KEY)
    try:
        check_owner(json.loads(owner_raw) if owner_raw else None, sha, hostname, root)
        owner_ready = True
    except RuntimeError:
        owner_ready = False
    with tempfile.TemporaryDirectory(prefix='football-notification-inspection-') as directory:
        path = Path(directory)
        r2.get_state(client, bucket, path, names=('alerts', 'telegram_state', 'bet_confirmations'))
        result = alerts.run_alerts(RunContext(sport='all', scope='notification-inspection', run_id=meta['run_id'], git_sha=sha),
            {sport: [card for card in cards if card.get('sport') == sport] for sport in ('nfl', 'cfb')}, path, enabled=False)
    return {'verified_run_id': meta['run_id'], 'generation': meta['publication']['generation'], 'git_sha': sha,
            'owner_ready': owner_ready, 'candidate_count': len(result.candidates), 'notification_transports': 0,
            'remote_writes': 0, 'timezone': 'America/New_York',
            'next_routine_at': stamp(alert_policy.next_routine_at(datetime.now(timezone.utc)))}


class NotificationClock:
    """Pure clock selection. Failed work retries; completed slots/runs dedupe."""
    def __init__(self):
        self.completed_run = None
        self.completed_slot = None
        self.pending_kickoffs = []
        self.completed_confirmations = None

    def due(self, now, run_id, confirmation_identity=None):
        slot = alert_policy.routine_slot(now)
        late_pending = (not slot or slot == self.completed_slot) and any(
            now < kickoff <= alert_policy.next_routine_at(now) for kickoff in self.pending_kickoffs)
        return bool((slot and slot != self.completed_slot) or run_id != self.completed_run or late_pending
                    or confirmation_identity != self.completed_confirmations)

    def complete(self, now, run_id, confirmation_identity=None, result=None):
        self.completed_run = run_id
        self.completed_confirmations = confirmation_identity
        if result is not None:
            self.pending_kickoffs = [c.kickoff_utc for c in result.candidates if c.family == 'edge'
                                    and c.kickoff_utc and not state.alert_sent(result.alerts, c.key)]
        if alert_policy.routine_slot(now):
            self.completed_slot = alert_policy.routine_slot(now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--root', type=Path, default=Path('data/notification-resident'))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true', help='Explicit notification activation; approval required')
    modes.add_argument('--verify-only', action='store_true', help='Read-only input verification; no notification or receipt writes')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    hostname = os.environ.get('COMPUTERNAME') or platform.node()
    print(json.dumps({'activation': bool(args.run), 'hostname': hostname, 'expected_sha': args.expected_sha,
                      'next_routine_at': stamp(alert_policy.next_routine_at(now)), 'timezone': 'America/New_York'}))
    if not args.run and not args.verify_only:
        return 0
    if revision() != args.expected_sha:
        raise RuntimeError('Selected notification source must match clean checkout')
    load_repo_dotenv()
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError('Existing R2 credentials unavailable')
    client = r2.make_client(cfg, bounded=True)
    root = args.root.resolve()
    if args.verify_only:
        print(json.dumps(inspect_inputs(client, cfg.bucket, args.expected_sha, hostname, root)))
        return 0
    from pipeline.alerts import Config
    if os.environ.get('TELEGRAM_DISABLED') == '1' or not os.environ.get('TELEGRAM_BOT_TOKEN') or not Config.from_env().chat_default:
        raise RuntimeError('Notification activation requires enabled transport and one shared routine chat')
    stop, clock = threading.Event(), NotificationClock()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def assert_owner():
        if revision() != args.expected_sha:
            raise RuntimeError('Selected notification source changed during execution')
        raw = r2.get_object(client, cfg.bucket, OWNER_KEY)
        check_owner(json.loads(raw) if raw else None, args.expected_sha, hostname, root)

    def confirmations():
        from pipeline.bet_confirmations import load_confirmations
        assert_owner()  # Revoke the selected owner before the next external send.
        raw = r2.get_object(client, cfg.bucket, 'board/bet_confirmations.json')
        atomic_json(root / 'state' / 'bet_confirmations.json', json.loads(raw) if raw else {'schema_version': 1, 'bets': {}})
        return load_confirmations(root / 'state')

    with OwnerLock(root / 'owner.lock'):
        while not stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                assert_owner()
                raw = r2.get_object(client, cfg.bucket, 'board/meta.json')
                meta = json.loads(raw) if raw else {}
                if not meta.get('run_id') or not meta.get('publication') or meta.get('git_sha') != args.expected_sha:
                    raise RuntimeError('Current publication identity unavailable or mismatched')
                publication_identity = f"{meta['run_id']}|{meta['publication'].get('generation')}"
                confirmation_bytes = r2.get_object(client, cfg.bucket, 'board/bet_confirmations.json')
                confirmation_identity = digest(confirmation_bytes or b'')
                if clock.due(now, publication_identity, confirmation_identity):
                    meta, _ = load_board(client, cfg.bucket, 'all')
                    if meta.get('git_sha') != args.expected_sha:
                        raise RuntimeError('Published source differs from selected notification owner')
                    generation = meta['publication']['generation']
                    board = root / 'board'
                    atomic_json(board / 'meta.json', meta)
                    for sport in ('nfl', 'cfb'):
                        payload = r2.get_object(client, cfg.bucket, f'board/generations/{generation}/games_{sport}.json')
                        atomic_json(board / f'games_{sport}.json', json.loads(payload))
                    r2.get_state(client, cfg.bucket, root / 'state', names=('alerts', 'telegram_state'))
                    confirmations()

                    def verifier(bucket, run_id):
                        assert_owner()
                        current = json.loads(r2.get_object(client, bucket, 'board/meta.json'))
                        if current.get('run_id') != run_id:
                            raise RuntimeError('Published run changed before notification')
                        verify_generation(current, lambda key: r2.get_object(client, bucket, key))

                    def upload(bucket, key, path, content_type):
                        assert_owner()
                        r2.put_file(client, bucket, key, path)

                    result = published_alerts.notify_published(board, root / 'state', meta['run_id'], cfg.bucket,
                        verifier=verifier, uploader=upload, confirmation_reader=confirmations)
                    if result is not None and not result.outcome.failed:
                        clock.complete(datetime.now(timezone.utc), publication_identity, confirmation_identity, result)
                atomic_json(root / 'heartbeat.json', {'status': 'running', 'checked_at': stamp(datetime.now(timezone.utc)),
                    'completed_run': clock.completed_run, 'completed_slot': clock.completed_slot, 'git_sha': args.expected_sha})
            except Exception as exc:
                atomic_json(root / 'heartbeat.json', {'status': 'degraded', 'checked_at': stamp(datetime.now(timezone.utc)),
                    'failure_type': type(exc).__name__, 'git_sha': args.expected_sha})
                if args.once:
                    return 1
            if args.once:
                return 0
            stop.wait(seconds_until_tick(datetime.now(timezone.utc)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
