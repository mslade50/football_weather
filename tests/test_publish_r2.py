"""Bounded Wrangler publisher contracts."""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from scripts import publish_r2

_R2_VARS = ("CF_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET", "R2_ENDPOINT")


@pytest.fixture(autouse=True)
def _clean_r2_env(monkeypatch):
    """Existing tests exercise the Wrangler path; never inherit S3 keys from the host."""
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)
    publish_r2.reset_clients()
    yield
    publish_r2.reset_clients()


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


# ---- boto3 transport (fake S3 client, no network, no real boto3) -----------------------

class NoSuchKey(Exception):
    """Name-matched by r2.is_no_such_key."""


class FakeS3:
    def __init__(self, fail_first: int = 0, objects: dict | None = None):
        self.fail_first = fail_first
        self.objects = objects or {}
        self.puts: list[dict] = []
        self.gets: list[dict] = []
        self._lock = threading.Lock()

    def put_object(self, **kwargs):
        with self._lock:
            self.puts.append(kwargs)
            if len(self.puts) <= self.fail_first:
                raise ConnectionError("boom")
        return {}

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        self.gets.append({"Bucket": Bucket, "Key": Key})
        if Key not in self.objects:
            raise NoSuchKey(Key)
        import io
        return {"Body": io.BytesIO(self.objects[Key])}


def _no_wrangler(*args, **kwargs):
    raise AssertionError("wrangler must not be spawned on the boto3 path")


def _s3_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(publish_r2, "_boto3_available", lambda: True)


def test_boto3_used_when_env_configured_and_content_type_preserved(tmp_path, monkeypatch):
    _s3_env(monkeypatch)
    fake = FakeS3()
    made = []
    monkeypatch.setattr(publish_r2.r2, "make_client", lambda cfg, bounded=False, config=None: made.append(bounded) or fake)
    src = tmp_path / "a.json"
    src.write_bytes(b"{}")
    publish_r2.upload_one("bkt", "board/a.json", src, "application/json", run=_no_wrangler)
    assert fake.puts == [{"Bucket": "bkt", "Key": "board/a.json", "Body": b"{}", "ContentType": "application/json"}]
    assert made == [False]
    assert publish_r2.transport_name() == "boto3"


def test_wrangler_used_when_keys_absent_or_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")  # secret missing -> not configured
    monkeypatch.setattr(publish_r2.r2, "make_client", lambda *a, **k: pytest.fail("no S3 client expected"))
    calls = []

    class Result:
        returncode = 0
        stderr = stdout = ""

    publish_r2.upload_one("b", "raw/a", tmp_path / "a", "text/csv",
                          run=lambda cmd, **kw: calls.append(cmd) or Result(), sleep=lambda _: None)
    assert calls and calls[0][:4] == ["npx", "--yes", "wrangler@4", "r2"]
    assert publish_r2.transport_name() == "wrangler"


def test_boto3_missing_package_falls_back_to_wrangler(monkeypatch):
    _s3_env(monkeypatch)
    monkeypatch.setattr(publish_r2, "_boto3_available", lambda: False)
    assert publish_r2.s3_config() is None


def test_boto3_retry_then_success(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(publish_r2.r2, "jitter", lambda: 0.0)
    fake = FakeS3(fail_first=2)
    sleeps = []
    src = tmp_path / "a"
    src.write_bytes(b"x")
    publish_r2.upload_one("b", "raw/a", src, "application/octet-stream", client=fake, sleep=sleeps.append, run=_no_wrangler)
    assert len(fake.puts) == 3
    assert sleeps == [publish_r2.r2.backoff_delay(1), publish_r2.r2.backoff_delay(2)]
    assert sleeps[1] == sleeps[0] * 2
    err = capsys.readouterr().err
    assert "::warning::R2 put raw/a failed (attempt 1/3): ConnectionError: boom" in err
    assert "attempt 2/3" in err
    assert "retrying in" in err


def test_put_retries_slowdown_and_internal_error_with_full_jitter_window(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_r2.r2, "jitter", lambda: 1.0)

    class Boom(Exception):
        def __init__(self, code: str):
            super().__init__(code)
            self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 503}}

    class Flaky:
        def __init__(self):
            self.puts: list[dict] = []

        def put_object(self, **kwargs):
            self.puts.append(kwargs)
            if len(self.puts) == 1:
                raise Boom("SlowDown")
            if len(self.puts) == 2:
                raise Boom("InternalError")
            return {}

    sleeps: list[float] = []
    src = tmp_path / "a"
    src.write_bytes(b"x")
    publish_r2.upload_one("b", "board/wx_last.json", src, "application/json", client=Flaky(), sleep=sleeps.append)
    assert sleeps == [
        publish_r2.r2.backoff_delay(1, rng=lambda: 1.0),
        publish_r2.r2.backoff_delay(2, rng=lambda: 1.0),
    ]
    assert sleeps[1] == sleeps[0] * 2


def test_boto3_final_failure_message(tmp_path):
    fake = FakeS3(fail_first=99)
    src = tmp_path / "a"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError, match=r"^R2 put raw/a failed after 3 attempts$"):
        publish_r2.upload_one("b", "raw/a", src, "text/csv", client=fake, sleep=lambda _: None)
    assert len(fake.puts) == 3


def test_boto3_missing_local_file_is_a_retried_failure(tmp_path):
    fake = FakeS3()
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        publish_r2.upload_one("b", "raw/a", tmp_path / "absent", "text/csv", client=fake, sleep=lambda _: None)
    assert fake.puts == []


def test_boto3_meta_uses_bounded_client_and_two_attempts(tmp_path):
    normal, bounded = FakeS3(fail_first=99), FakeS3(fail_first=99)
    src = tmp_path / "meta"
    src.write_bytes(b"{}")
    with pytest.raises(RuntimeError, match=r"^R2 put board/meta.json failed after 2 attempts$"):
        publish_r2.upload_one("b", "board/meta.json", src, "application/json", client=normal,
                              bounded_client=bounded, sleep=lambda _: None)
    assert len(bounded.puts) == 2 and normal.puts == []


def test_boto3_meta_env_path_requests_bounded_client(tmp_path, monkeypatch):
    _s3_env(monkeypatch)
    fake = FakeS3()
    made = []
    monkeypatch.setattr(publish_r2.r2, "make_client", lambda cfg, bounded=False, config=None: made.append(bounded) or fake)
    src = tmp_path / "meta"
    src.write_bytes(b"{}")
    publish_r2.upload_one("b", "board/meta.json", src, "application/json", run=_no_wrangler)
    assert made == [True]


def test_boto3_meta_stops_retrying_after_deadline(tmp_path, monkeypatch):
    fake = FakeS3(fail_first=99)
    clock = iter([0.0, 100.0, 200.0])
    monkeypatch.setattr(publish_r2.time, "monotonic", lambda: next(clock))
    src = tmp_path / "meta"
    src.write_bytes(b"{}")
    with pytest.raises(RuntimeError, match="board/meta.json failed after 2 attempts"):
        publish_r2.upload_one("b", "board/meta.json", src, "application/json", client=fake, sleep=lambda _: None)
    assert len(fake.puts) == 1


def test_boto3_download_writes_target_and_returns_bytes(tmp_path):
    fake = FakeS3(objects={"board/meta.json": b'{"a":1}'})
    out = publish_r2.download_one("b", "board/meta.json", tmp_path / "chk", client=fake, run=_no_wrangler)
    assert out == b'{"a":1}'
    assert (tmp_path / "chk" / "board_meta.json").read_bytes() == b'{"a":1}'


def test_boto3_download_allow_missing_and_other_errors(tmp_path):
    fake = FakeS3()
    assert publish_r2.download_one("b", "k", tmp_path, client=fake, allow_missing=True, missing_bytes=b"M") == b"M"
    with pytest.raises(RuntimeError, match="Publication verification fetch failed: k"):
        publish_r2.download_one("b", "k", tmp_path, client=fake)

    class Broken(FakeS3):
        def get_object(self, Bucket, Key):  # noqa: N803
            raise TimeoutError("slow")

    with pytest.raises(RuntimeError, match="Publication verification fetch failed: k"):
        publish_r2.download_one("b", "k", tmp_path, client=Broken(), allow_missing=True)


def test_shared_client_created_once_across_threaded_uploads(tmp_path, monkeypatch):
    _s3_env(monkeypatch)
    fake = FakeS3()
    created = []

    def slow_make(cfg, bounded=False, config=None):
        created.append(bounded)
        time.sleep(0.05)  # widen the race window for concurrent creation
        return fake

    monkeypatch.setattr(publish_r2.r2, "make_client", slow_make)
    files = {}
    for i in range(24):
        p = tmp_path / f"{i}.json"
        p.write_text("{}")
        files[f"raw/{i}.json"] = p
    keys = publish_r2.upload_files("b", files, workers=8)
    assert len(keys) == 24 and len(fake.puts) == 24
    assert created == [False]
    assert {p["ContentType"] for p in fake.puts} == {"application/json"}


def test_publish_phase_announces_transport(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    publish_r2.publish_phase("raw", "b", upload_fn=lambda *a: None)
    assert "R2 transport: wrangler (phase raw)" in capsys.readouterr().out
    _s3_env(monkeypatch)
    publish_r2.publish_phase("raw", "b", upload_fn=lambda *a: None)
    out = capsys.readouterr().out
    assert "R2 transport: boto3 (phase raw)" in out and "secret" not in out



def test_boto3_bulk_client_has_bounded_timeouts(monkeypatch):
    # Fake botocore.config so the assertion holds whatever other tests stub in sys.modules.
    fake_config = types.ModuleType("botocore.config")
    fake_config.Config = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config)
    seen: list[tuple[bool, object]] = []
    monkeypatch.setattr(publish_r2.r2, "make_client",
                        lambda cfg, bounded=False, config=None: seen.append((bounded, config)) or object())
    cfg = publish_r2.r2.R2Config("acct", "key", "secret", "football-board")
    publish_r2.get_client(cfg)
    publish_r2.get_client(cfg, bounded=True)
    (bulk_bounded, bulk_cfg), (meta_bounded, meta_cfg) = seen
    assert bulk_bounded is False and meta_bounded is True and meta_cfg is None
    assert bulk_cfg == {"connect_timeout": 5, "read_timeout": 30,
                        "retries": {"mode": "standard", "total_max_attempts": 6},
                        "max_pool_connections": 32}
