"""Bounded Wrangler publisher contracts."""
from __future__ import annotations

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
