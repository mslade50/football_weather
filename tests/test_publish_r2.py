"""Bounded Wrangler publisher contracts."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from scripts import publish_r2


def test_phase_uploads_keep_meta_out_of_board_batch_and_require_meta(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    board = Path("data/board")
    board.mkdir(parents=True)
    (board / "games_nfl.json").write_text("{}")
    (board / "meta.json").write_text("{}")
    assert [key for key, _, _ in publish_r2.files_for_phase("board")] == ["board/games_nfl.json"]
    assert publish_r2.files_for_phase("meta")[0][0] == "board/meta.json"
    (board / "meta.json").unlink()
    with pytest.raises(RuntimeError, match="meta.json is missing"):
        publish_r2.publish_phase("meta", "bucket", upload_fn=lambda *args: None)


def test_upload_retries_with_same_idempotent_request(tmp_path):
    calls = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            Result.returncode = 1
        else:
            Result.returncode = 0
        return Result()

    publish_r2.upload_one("bucket", "raw/a.json", tmp_path / "a", "application/json",
                          run=run, sleep=lambda _: None)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert "--remote" in calls[0]


def test_meta_commit_marker_uses_short_retry_deadline(tmp_path):
    observed = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def run(command, **kwargs):
        observed.append(kwargs["timeout"])
        return Result()

    publish_r2.upload_one("bucket", "board/meta.json", tmp_path / "meta", "application/json",
                          run=run, sleep=lambda _: None)
    assert observed == [15]


def test_upload_files_never_exceeds_worker_limit(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_upload(bucket, key, path, content_type):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1

    files = {f"raw/{i}.json": tmp_path / f"{i}.json" for i in range(20)}
    assert len(publish_r2.upload_files("bucket", files, workers=3, upload_fn=fake_upload)) == 20
    assert peak == 3


def test_wrangler_build_cannot_overwrite_interleaved_resident_ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = Path('data/state')
    local.mkdir(parents=True)
    remote = {'board/notification_owner.json': {'schema_version': 1, 'kind': 'local'}}
    for name in ('alerts', 'telegram_state', 'notification_owner', 'alerts_live_feed'):
        (local / f'{name}.json').write_text('{}')  # Previously fetched build snapshot.
    (local / 'openers.json').write_text('{"original":"preserved"}')
    monkeypatch.setenv('STATE_FILES', 'alerts telegram_state notification_owner alerts_live_feed openers')
    latest = {'sent': {'first': 'new'}, 'routine_delivery': {'chat|date|08': 'new'}, 'cleared_bets': {'bet': 'new'}}
    remote['board/alerts.json'] = latest
    remote['board/telegram_state.json'] = {'queue': [{'key': 'new-pending'}]}
    remote['board/alerts_live_feed.json'] = {'alerts': [{'key': 'new-clear'}]}
    before = json.loads(json.dumps(remote))
    pushed = publish_r2.publish_phase('state', 'bucket', upload_fn=lambda bucket, key, path, content_type: remote.__setitem__(key, json.loads(path.read_bytes())))
    assert pushed == ['board/openers.json']
    assert all(remote[key] == value for key, value in before.items())


def test_board_batch_also_excludes_mutable_owner_ledgers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    board = Path('data/board')
    board.mkdir(parents=True)
    for name in ('alerts', 'telegram_state', 'notification_owner', 'alerts_live_feed', 'games_nfl'):
        (board / f'{name}.json').write_text('{}')
    assert [key for key, *_ in publish_r2.files_for_phase('board')] == ['board/games_nfl.json']
