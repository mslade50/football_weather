"""Durable owner-review outbox producer; never sends notifications.

Default prints a plan. --verify-only reads source inputs without writes. --run
evaluates into a local atomic outbox. A separately authorized --publish also
writes one independently owned R2 outbox. No delivery/claim/ack writer exists
here: a supported assistant automation must own actual ChatGPT delivery.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pipeline import alert_policy
from pipeline.bet_confirmations import validate_confirmations
from pipeline.outputs import r2
from pipeline.publication import digest, encoded
from pipeline.resident import OwnerLock, atomic_json, load_board, revision, stamp
from utils.env import load_repo_dotenv

# Intentionally separate from notification_owner/alerts/telegram_state. Selecting
# an outbox producer must not retire or take ownership of the Telegram sender.
OWNER_KEY = 'board/review_outbox_owner.json'
OUTBOX_KEY = 'board/owner_review_outbox.json'
MAX_OUTBOX_BYTES = 4 * 1024 * 1024


def check_owner(owner, expected_sha, hostname, root):
    if (not isinstance(owner, dict) or owner.get('schema_version') != 1 or owner.get('kind') != 'review_outbox'
            or owner.get('git_sha') != expected_sha or owner.get('hostname') != hostname
            or str(owner.get('root', '')).casefold() != Path(root).resolve().as_posix().casefold()):
        raise RuntimeError('Explicit review-outbox ownership unavailable or mismatched')


def seconds_until_tick(now, interval=10):
    return min(interval, max(0.05, (alert_policy.next_routine_at(now) - now).total_seconds()))


def make_outbox(meta, cards, confirmations, previous, now):
    """Pure evidence evaluation; generated items are never delivery receipts.

    Reuse encoded signal predicates and scheduling selection, but no transport
    configuration, Telegram receipts, liquidity fetch, dispatch, or _mark call.
    Until the delivery integration exists, first notices remain unacknowledged;
    this producer cannot know when to transition to pre-bet reminders.
    """
    from pipeline import alerts

    if previous is not None and (previous.get('schema_version') != 1
            or previous.get('kind') != 'owner_review_outbox' or not isinstance(previous.get('observations'), dict)):
        raise RuntimeError('Unsupported review outbox; refusing to overwrite durable observations')
    cfg = alerts.Config(interview_policy=True, chat_default='owner-review', board_url=alerts.DEFAULT_BOARD_URL)
    # Only observations are persisted. No Telegram marker can imply ChatGPT delivery.
    ledger = {'first_signals': json.loads(json.dumps((previous or {}).get('observations', {})))}
    candidates = alert_policy.collect(SimpleNamespace(run_id=meta['run_id']),
        {sport: [card for card in cards if card.get('sport') == sport] for sport in ('nfl', 'cfb')},
        ledger, cfg, now, confirmations)
    plan = alert_policy.plan(candidates, ledger, {}, now, cfg)
    kickoff_by_game = {card.get('game_id'): card.get('kickoff_utc') for card in cards}

    def candidate_row(candidate):
        watch = candidate.family != 'clear'
        label = '<b>WEATHER WATCH - owner review only</b>\n' if watch else ''
        return {'notice_id': digest(encoded({'key': candidate.key, 'family': candidate.family})),
                'key': candidate.key, 'family': candidate.family, 'sport': candidate.sport,
                'game_id': candidate.game_id, 'tier': candidate.tier,
                'kickoff_utc': kickoff_by_game.get(candidate.game_id),
                'text_html': label + candidate.text + ('\nNo freshly verified $500 cash depth; prices are context only.' if watch else ''),
                'summary_html': label + candidate.summary, 'record': candidate.record,
                'classification': 'clear_invalidation' if candidate.family == 'clear' else 'weather_watch',
                'verified_cash_recommendation': False}

    items = []

    def item(identity, timing, rows):
        content = {'timing': timing, 'candidates': rows, 'source': {
            'run_id': meta['run_id'], 'git_sha': meta['git_sha'], 'generation': meta['publication']['generation'],
            'confirmation_sha256': digest(encoded(confirmations))}}
        items.append({'outbox_id': digest(encoded(identity)), 'revision_sha256': digest(encoded(content)),
                      'status': 'generated', 'delivery_state': 'unacknowledged', **content})

    for candidate in plan.send:
        item({'notice': candidate.key}, 'immediate', [candidate_row(candidate)])
    if plan.digest:
        item({'routine_slot': alert_policy.routine_slot(now)}, 'routine', [candidate_row(c) for c in plan.digest])
    waiting = [candidate_row(c) for c in plan.queued]
    return {'schema_version': 1, 'kind': 'owner_review_outbox', 'checked_at': stamp(now),
            'valid_until': stamp(now + timedelta(seconds=30)), 'source': {
                'run_id': meta['run_id'], 'git_sha': meta['git_sha'], 'generation': meta['publication']['generation'],
                'confirmation_sha256': digest(encoded(confirmations))},
            'observations': ledger.get('first_signals', {}), 'items': items, 'waiting': waiting,
            'next_routine_at': stamp(alert_policy.next_routine_at(now)), 'timezone': 'America/New_York',
            'delivery_integration': 'unwired', 'notification_transports': 0,
            'routine_delivery_limit_per_local_day': 2,
            'receipt_contract': 'Generation/read/claim are not delivery. A future authenticated adapter must bind actual ChatGPT message ID and delivery time to outbox_id plus revision_sha256; unknown outcomes remain unacknowledged.'}


def read_outbox(root, now):
    """Read-only local consumption. A crashed/degraded producer expires promptly."""
    payload = json.loads((Path(root) / 'outbox.json').read_text(encoding='utf8'))
    heartbeat = json.loads((Path(root) / 'heartbeat.json').read_text(encoding='utf8'))
    expires = alert_policy.evidence_time(payload.get('valid_until'))
    checked = alert_policy.evidence_time(payload.get('checked_at'))
    if (payload.get('schema_version') != 1 or payload.get('kind') != 'owner_review_outbox'
            or heartbeat.get('status') != 'running' or expires is None or checked is None
            or not checked <= now < expires or expires - checked > timedelta(seconds=30)
            or heartbeat.get('outbox_sha256') != digest(encoded(payload))):
        raise RuntimeError('Review outbox unavailable, degraded, stale or uncommitted')
    return payload


def read_confirmations(client, bucket):
    raw = r2.get_object(client, bucket, 'board/bet_confirmations.json')
    # Use the same strict validation as production; missing ledger means no bets,
    # whereas malformed/unavailable reads stop evaluation rather than imply silence.
    return validate_confirmations(json.loads(raw) if raw else {'schema_version': 1, 'bets': {}})


def inspect_inputs(client, bucket, sha, hostname, root):
    """Read inputs and evaluate in memory; zero receipt/owner/outbox writes."""
    meta, cards = load_board(client, bucket, 'all')
    if meta.get('git_sha') != sha:
        raise RuntimeError('Published source differs from selected review revision')
    owner_raw = r2.get_object(client, bucket, OWNER_KEY)
    try:
        check_owner(json.loads(owner_raw) if owner_raw else None, sha, hostname, root)
        owner_ready = True
    except RuntimeError:
        owner_ready = False
    result = make_outbox(meta, cards, read_confirmations(client, bucket), None, datetime.now(timezone.utc))
    return {'verified_run_id': meta['run_id'], 'generation': meta['publication']['generation'], 'git_sha': sha,
            'remote_outbox_owner_ready': owner_ready, 'candidate_count': sum(len(i['candidates']) for i in result['items']) + len(result['waiting']),
            'notification_transports': 0, 'remote_writes': 0, 'delivery_integration': 'unwired',
            'timezone': result['timezone'], 'next_routine_at': result['next_routine_at']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--root', type=Path, default=Path('data/review-outbox'))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true', help='Explicit local outbox evaluation; no sends')
    modes.add_argument('--verify-only', action='store_true', help='Read-only input verification; no outbox/receipt writes')
    modes.add_argument('--read-outbox', action='store_true', help='Read-only local outbox consumption; no network or acknowledgment')
    parser.add_argument('--publish', action='store_true', help='Separately authorized R2 outbox write; requires independently selected owner')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if args.publish and not args.run:
        parser.error('--publish requires --run')
    now = datetime.now(timezone.utc)
    hostname = os.environ.get('COMPUTERNAME') or platform.node()
    root = args.root.resolve()
    if args.read_outbox:
        payload = read_outbox(root, now)
        if payload['source']['git_sha'] != args.expected_sha:
            raise RuntimeError('Review outbox source differs from selected revision')
        print(json.dumps(payload))
        return 0
    print(json.dumps({'activation': bool(args.run), 'hostname': hostname, 'expected_sha': args.expected_sha,
                      'next_routine_at': stamp(alert_policy.next_routine_at(now)), 'timezone': 'America/New_York',
                      'notification_transports': 0, 'delivery_integration': 'unwired', 'publish_outbox': args.publish}))
    if not args.run and not args.verify_only:
        return 0
    if revision() != args.expected_sha:
        raise RuntimeError('Selected review source must match clean checkout')
    load_repo_dotenv()
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError('Existing R2 read credentials unavailable')
    client = r2.make_client(cfg, bounded=True)
    if args.verify_only:
        print(json.dumps(inspect_inputs(client, cfg.bucket, args.expected_sha, hostname, root)))
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def assert_source():
        if revision() != args.expected_sha:
            raise RuntimeError('Selected review source changed during execution')
        if args.publish:
            raw = r2.get_object(client, cfg.bucket, OWNER_KEY)
            check_owner(json.loads(raw) if raw else None, args.expected_sha, hostname, root)

    with OwnerLock(root / 'owner.lock'):
        while not stop.is_set():
            try:
                assert_source()
                meta, cards = load_board(client, cfg.bucket, 'all')
                if meta.get('git_sha') != args.expected_sha:
                    raise RuntimeError('Published source differs from selected review owner')
                confirmations = read_confirmations(client, cfg.bucket)
                path = root / 'outbox.json'
                previous = json.loads(path.read_text(encoding='utf8')) if path.exists() else None
                result = make_outbox(meta, cards, confirmations, previous, datetime.now(timezone.utc))
                # A newer publication/confirmation invalidates the proposed batch.
                assert_source()
                current = json.loads(r2.get_object(client, cfg.bucket, 'board/meta.json'))
                if (current.get('run_id') != meta['run_id'] or current.get('publication') != meta['publication']
                        or current.get('git_sha') != args.expected_sha or read_confirmations(client, cfg.bucket) != confirmations):
                    raise RuntimeError('Publication or confirmations changed before outbox commit')
                body = encoded(result)
                if len(body) > MAX_OUTBOX_BYTES:
                    raise RuntimeError('Outbox retention capacity reached; archive review required')
                atomic_json(path, result)
                if args.publish:
                    assert_source()
                    # Owner object is independently controlled. CAS prevents an
                    # intervening competing write from being silently replaced.
                    try:
                        remote = client.get_object(Bucket=cfg.bucket, Key=OUTBOX_KEY)
                    except Exception as exc:
                        if not r2.is_no_such_key(exc):
                            raise
                        remote = None
                    condition = {'IfMatch': remote['ETag']} if remote else {'IfNoneMatch': '*'}
                    client.put_object(Bucket=cfg.bucket, Key=OUTBOX_KEY, Body=body, ContentType='application/json', **condition)
                atomic_json(root / 'heartbeat.json', {'status': 'running', 'checked_at': stamp(datetime.now(timezone.utc)),
                    'outbox_sha256': digest(body), 'git_sha': args.expected_sha, 'notification_transports': 0})
            except Exception as exc:
                atomic_json(root / 'heartbeat.json', {'status': 'degraded', 'checked_at': stamp(datetime.now(timezone.utc)),
                    'failure_type': type(exc).__name__, 'git_sha': args.expected_sha, 'notification_transports': 0})
                if args.once:
                    return 1
            if args.once:
                return 0
            stop.wait(seconds_until_tick(datetime.now(timezone.utc)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
