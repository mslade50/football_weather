"""Single-owner, exchange-only resident collector. Installation/activation is separate.

No weather, opener, alert, order or account writes. Optional publishing owns only
board/live_quotes.json, with conditional writes and a remote owner heartbeat.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.outputs import r2
from utils.env import load_repo_dotenv

LIVE_KEY = 'board/live_quotes.json'
REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / 'scripts' / 'resident_quotes.mjs'


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def age(value: Any, now: datetime) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (now - dt.astimezone(timezone.utc)).total_seconds() if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, allow_nan=False).encode('utf8')
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as out:
            name = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


class OwnerLock:
    """OS-held lock releases on process death; no stale-file deletion or stealing."""
    def __init__(self, path: Path):
        self.path, self.file = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError('Another football resident collector owns this directory') from exc
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def revision(repo: Path = REPO) -> str:
    args = ['git', '-c', f'safe.directory={repo.as_posix()}', '-C', str(repo)]
    sha = subprocess.run([*args, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    dirty = subprocess.run([*args, 'status', '--porcelain', '--untracked-files=no'], capture_output=True,
                           text=True, check=True, timeout=5).stdout.strip()
    if dirty:
        raise RuntimeError('Resident collector refuses modified tracked source')
    return sha


def collect(cards: list[dict], raw_dir: Path) -> dict:
    result = subprocess.run(['node', str(BRIDGE)], input=json.dumps({'games': cards, 'raw_dir': str(raw_dir)}, allow_nan=False),
                            text=True, encoding='utf8', capture_output=True, check=True, timeout=28)
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError('Invalid collector response')
    return parsed


class ConditionalPublisher:
    """Never overwrite a fresh remote owner or silently retry a lost CAS."""
    def __init__(self, client: Any, bucket: str, owner_id: str, now: datetime):
        self.client, self.bucket, self.etag = client, bucket, None
        try:
            response = client.get_object(Bucket=bucket, Key=LIVE_KEY)
        except Exception as exc:
            if not r2.is_no_such_key(exc):
                raise
        else:
            prior = json.loads(response['Body'].read())
            heartbeat_age = age(prior.get('heartbeat_at'), now)
            if prior.get('owner_id') != owner_id and (heartbeat_age is None or heartbeat_age < 60):
                raise RuntimeError('Remote football collector owner is active or has an unknown clock')
            self.etag = response.get('ETag')
            if not self.etag:
                raise RuntimeError('Remote ownership ETag unavailable')

    def __call__(self, overlay: dict) -> None:
        condition = {'IfMatch': self.etag} if self.etag else {'IfNoneMatch': '*'}
        response = self.client.put_object(Bucket=self.bucket, Key=LIVE_KEY,
                                          Body=json.dumps(overlay, allow_nan=False).encode(), ContentType='application/json', **condition)
        self.etag = response.get('ETag')
        if not self.etag:
            raise RuntimeError('Conditional publication receipt has no ETag')


def load_board(client: Any, bucket: str, sport: str) -> tuple[dict, list[dict]]:
    meta_bytes = r2.get_object(client, bucket, 'board/meta.json')
    if not meta_bytes:
        raise RuntimeError('Published meta unavailable')
    meta = json.loads(meta_bytes)
    if meta.get('publication'):
        from pipeline.publication import verify_generation
        verify_generation(meta, lambda key: r2.get_object(client, bucket, key))
    else:
        raise RuntimeError('Immutable publication is required before resident activation')
    games = []
    for selected in ('nfl', 'cfb') if sport == 'all' else (sport,):
        generation = meta['publication']['generation']
        payload = r2.get_object(client, bucket, f'board/generations/{generation}/games_{selected}.json')
        if payload is None:
            raise RuntimeError('Published sport unavailable')
        parsed = json.loads(payload)
        rows = parsed if isinstance(parsed, list) else parsed.get('games')
        if not isinstance(rows, list):
            raise ValueError('Invalid published sport')
        games.extend(rows)
    return meta, games


class ResidentCollector:
    def __init__(self, root: Path, sha: str, loader: Callable, collector: Callable = collect,
                 publisher: Callable | None = None, clock: Callable = utc_now, max_games: int = 4):
        self.root, self.sha, self.loader, self.collector, self.publisher, self.clock = root, sha, loader, collector, publisher, clock
        self.owner_id, self.sequence, self.max_games = uuid.uuid4().hex, 0, max_games
        self.snapshots: dict[str, dict] = {}
        self.last_checked: dict[str, str] = {}
        self.base_run_id: str | None = None

    def cycle(self, sport: str) -> dict:
        self.sequence += 1
        now = self.clock()
        receipt = {'schema_version': 1, 'owner_id': self.owner_id, 'owner_name': 'football-resident',
                   'pid': os.getpid(), 'git_sha': self.sha, 'sport': sport, 'sequence': self.sequence,
                   'heartbeat_at': stamp(now), 'status': 'collecting', 'base_run_id': self.base_run_id}
        atomic_json(self.root / 'heartbeat.json', receipt)
        try:
            meta, games = self.loader(sport)
            if meta.get('git_sha') != self.sha:
                raise ValueError('Published board source differs from selected resident revision')
            if not meta.get('run_id') or any(not isinstance(g, dict) or g.get('run_id') != meta['run_id'] for g in games):
                raise ValueError('Mixed board generation')
            if self.base_run_id != meta['run_id']:
                self.snapshots.clear()
                self.last_checked.clear()
            self.base_run_id = meta['run_id']
            candidates = [g for g in games if age(g.get('kickoff_utc'), now) is not None
                          and age(g['kickoff_utc'], now) < 0 and g.get('execution_markets')
                          and (g.get('stadium') or {}).get('roof_type') != 'dome'
                          and (g.get('stadium') or {}).get('roof_state') not in ('dome', 'closed', 'indoor')
                          and not (g.get('sport') == 'cfb' and isinstance((g.get('consensus') or {}).get('spread_open'), (int, float))
                                   and abs(g['consensus']['spread_open']) > 10)
                          and not any(term in str(g.get('status', '')).lower() for term in ('final', 'cancel', 'postpon', 'suspend', 'live', 'progress'))]
            candidates.sort(key=lambda g: (self.last_checked.get(g['game_id'], ''), g['kickoff_utc'], g['game_id']))
            selected = candidates[:self.max_games]
            raw = self.root / 'raw' / f'{self.sequence:012d}-{uuid.uuid4().hex}'
            used = sum(p.stat().st_size for p in (self.root / 'raw').rglob('*') if p.is_file())
            if used > 512 * 1024 * 1024:
                raise RuntimeError('Raw capture storage limit reached; retained evidence is not silently deleted')
            snapshots = self.collector(selected, raw) if selected else {}
            now = self.clock()
            for game in selected:
                gid = game['game_id']
                self.last_checked[gid] = stamp(now)
                snap = snapshots.get(gid)
                checked_age = age(snap.get('checked_at'), now) if isinstance(snap, dict) else None
                expiry_age = age(snap.get('expires_at'), now) if isinstance(snap, dict) else None
                if checked_age is None or not 0 <= checked_age < 15 or expiry_age is None or expiry_age >= 0:
                    self.snapshots.pop(gid, None)
                    continue
                verified = [q for q in snap.get('quotes', []) if q.get('liquidity_status') == 'verified'
                            and age(q.get('depth_fetched_at'), now) is not None and 0 <= age(q['depth_fetched_at'], now) < 15]
                if verified:
                    self.snapshots[gid] = {**snap, 'quotes': verified, 'board_run_id': self.base_run_id,
                                           'quote_request_id': f'{self.owner_id}-{self.sequence}'}
                else:
                    self.snapshots.pop(gid, None)
            active = {g['game_id'] for g in candidates}
            self.snapshots = {gid: s for gid, s in self.snapshots.items() if gid in active
                              and age(s['expires_at'], now) is not None and age(s['expires_at'], now) < 0}
            receipt.update(base_run_id=self.base_run_id, heartbeat_at=stamp(now), status='fresh' if self.snapshots else 'degraded',
                           games_checked=len(selected), mapped_upcoming_games=len(candidates), fresh_games=len(self.snapshots),
                           recommendation_status='weather_watch', snapshots=self.snapshots)
            atomic_json(self.root / 'live_quotes.json', receipt)
            if self.publisher:
                self.publisher(receipt)
            receipt['publication_status'] = 'published' if self.publisher else 'local_only'
        except Exception as exc:
            # Exception text from transports may contain signed URLs or secrets.
            receipt.update(status='degraded', failure_type=type(exc).__name__, publication_status='unavailable', snapshots={})
        atomic_json(self.root / 'heartbeat.json', {k: v for k, v in receipt.items() if k != 'snapshots'})
        return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport', choices=('nfl', 'cfb', 'all'), default='all')
    parser.add_argument('--root', type=Path, default=REPO / 'data' / 'resident')
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--max-games', type=int, default=4)
    parser.add_argument('--publish', action='store_true', help='Activate separate conditional live-quote publishing; requires approval')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if not 5 <= args.interval <= 60 or not 1 <= args.max_games <= 12:
        parser.error('interval must be 5-60 seconds and max-games 1-12')
    sha = revision()
    if sha != args.expected_sha:
        raise RuntimeError('Resident source does not match explicitly selected revision')
    load_repo_dotenv()
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError('Existing R2 SDK configuration unavailable; no credentials are provisioned')
    client = r2.make_client(cfg, bounded=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with OwnerLock(args.root / 'owner.lock'):
        worker = ResidentCollector(args.root, sha, lambda sport: load_board(client, cfg.bucket, sport), max_games=args.max_games)
        if args.publish:
            worker.publisher = ConditionalPublisher(client, cfg.bucket, worker.owner_id, utc_now())
        while not stop.is_set():
            started = time.monotonic()
            receipt = worker.cycle(args.sport)
            print(json.dumps({k: v for k, v in receipt.items() if k != 'snapshots'}), flush=True)
            if args.once:
                return 0 if receipt['status'] == 'fresh' else 1
            stop.wait(max(.1, args.interval - (time.monotonic() - started)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
