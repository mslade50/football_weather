"""scripts/fetch_r2.py: fail-closed state fetch, optional objects, transport selection (no network)."""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

from scripts import fetch_r2, publish_r2

_R2_VARS = ("CF_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET", "R2_ENDPOINT", "STATE_FILES")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Never inherit S3 keys / STATE_FILES from the host; reset the shared client cache."""
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)
    publish_r2.reset_clients()
    yield
    publish_r2.reset_clients()


class NoSuchKey(Exception):
    pass


class ClientError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    def __init__(self, objects: dict | None = None, errors: dict | None = None, delay: float = 0.0):
        self.objects = objects or {}
        self.errors = errors or {}
        self.delay = delay
        self.gets: list[str] = []
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        with self._lock:
            self.gets.append(Key)
            self.threads.add(threading.get_ident())
        time.sleep(self.delay)
        if Key in self.errors:
            raise self.errors[Key]
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def _s3_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sekret-value")
    monkeypatch.setattr(publish_r2, "_boto3_available", lambda: True)


def _no_wrangler(*a, **k):
    raise AssertionError("wrangler must not be spawned on the boto3 path")


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_state_boto3_missing_key_removes_dest_and_present_keys_written(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATE_FILES", "openers history")
    dest = tmp_path / "state"
    dest.mkdir()
    (dest / "history.json").write_text("stale")
    fake = FakeS3({"board/openers.json": b'{"o":1}', "board/meta.json": b'{"m":1}'})
    assert fetch_r2.fetch_state("b", dest, client=fake, run=_no_wrangler) == 0
    assert (dest / "openers.json").read_bytes() == b'{"o":1}'
    assert (dest / "prev_meta.json").read_bytes() == b'{"m":1}'
    assert not (dest / "history.json").exists()  # NoSuchKey -> rm -f, as before
    out = capsys.readouterr().out
    assert "R2 transport: boto3" in out and "::notice::history.json absent (first run)" in out
    assert not list(dest.glob("*.tmp"))


@pytest.mark.parametrize("exc", [
    ClientError("AccessDenied", 403), ClientError("InternalError", 500), ConnectionError("reset"),
    TimeoutError("slow"), ClientError("NoSuchBucket", 404),
])
def test_state_boto3_other_error_fails_closed_and_writes_nothing(tmp_path, monkeypatch, capsys, exc):
    monkeypatch.setenv("STATE_FILES", "openers history alerts")
    dest = tmp_path / "state"
    fake = FakeS3({"board/openers.json": b"{}", "board/alerts.json": b"{}", "board/meta.json": b"{}"},
                  errors={"board/history.json": exc})
    assert fetch_r2.fetch_state("b", dest, client=fake) == 1
    assert list(dest.iterdir()) == []  # no half-written state for a later step to trust
    cap = capsys.readouterr()
    assert "::error::R2 state fetch failed for history.json (not NoSuchKey) - refusing to run on blank state" in cap.out


def test_state_failure_does_not_delete_or_replace_existing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_FILES", "openers history")
    dest = tmp_path / "state"
    dest.mkdir()
    (dest / "openers.json").write_text("old")
    fake = FakeS3({"board/openers.json": b"new"}, errors={"board/history.json": ConnectionError("x")})
    assert fetch_r2.fetch_state("b", dest, client=fake) == 1
    assert (dest / "openers.json").read_text() == "old"


def test_state_concurrent_fetch_uses_one_shared_client(tmp_path, monkeypatch):
    _s3_env(monkeypatch)
    names = [f"f{i}" for i in range(12)]
    monkeypatch.setenv("STATE_FILES", " ".join(names))
    fake = FakeS3({f"board/{n}.json": b"{}" for n in names}, delay=0.02)
    made = []
    monkeypatch.setattr(publish_r2.r2, "make_client",
                        lambda cfg, bounded=False, config=None: made.append((bounded, config)) or fake)
    assert fetch_r2.fetch_state("b", tmp_path / "s", run=_no_wrangler) == 0
    assert len(made) == 1 and made[0][0] is False and made[0][1] is not None  # bulk (5s/30s) config
    assert len(fake.gets) == 13 and len(fake.threads) > 1  # 12 state files + meta, in parallel
    assert (tmp_path / "s" / "prev_meta.json").exists() is False  # meta absent in fake -> skipped


def test_state_wrangler_fallback_when_keys_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATE_FILES", "openers history")
    monkeypatch.setattr(publish_r2.r2, "make_client", lambda *a, **k: pytest.fail("no S3 client expected"))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        key = cmd[cmd.index("get") + 1]
        out = next(a for a in cmd if a.startswith("--file=")).split("=", 1)[1]
        if key.endswith("history.json"):
            return Result(1, "", "X [ERROR] The specified key does not exist.")
        Path(out).write_bytes(b'{"k":"' + key.encode() + b'"}')
        return Result()

    dest = tmp_path / "state"
    assert fetch_r2.fetch_state("football-board", dest, run=run) == 0
    assert "R2 transport: wrangler" in capsys.readouterr().out
    assert [c[1:7] for c in calls][0] == ["--yes", "wrangler@4", "r2", "object", "get", "football-board/board/openers.json"]
    assert all(c[-1] == "--remote" for c in calls)
    assert (dest / "openers.json").exists() and (dest / "prev_meta.json").exists()
    assert not (dest / "history.json").exists()


def test_state_wrangler_other_error_exits_1_with_old_messages(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATE_FILES", "openers history")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return Result(1, "", "Authentication error [code: 10000]")

    assert fetch_r2.fetch_state("b", tmp_path / "s", run=run) == 1
    assert len(calls) == 1  # serial wrangler loop stops at the first failure, like `exit 1`
    out = capsys.readouterr().out
    assert "Authentication error" in out
    assert "::error::R2 state fetch failed for openers.json (not NoSuchKey) - refusing to run on blank state" in out
    assert list((tmp_path / "s").iterdir()) == []


def test_state_wrangler_timeout_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_FILES", "openers")

    def run(cmd, **kw):
        raise fetch_r2.subprocess.TimeoutExpired(cmd, 90)

    assert fetch_r2.fetch_state("b", tmp_path / "s", run=run) == 1


def test_wrangler_missing_text_matches_old_grep():
    for text in ("NoSuchKey", "object does not exist", "Not Found", "HTTP 404"):
        assert fetch_r2.fetch_bytes_wrangler("b", "k", run=lambda c, _t=text, **kw: Result(1, "", _t)) is None
    with pytest.raises(fetch_r2.FetchError):
        fetch_r2.fetch_bytes_wrangler("b", "k", run=lambda c, **kw: Result(1, "", "502 Bad Gateway"))


def test_object_strict_missing_and_error_exit_1(tmp_path, capsys):
    dest = tmp_path / "check" / "meta.json"
    assert fetch_r2.fetch_object("b", "board/meta.json", dest, client=FakeS3()) == 1
    assert not dest.exists()
    assert fetch_r2.fetch_object("b", "board/meta.json", dest,
                                 client=FakeS3(errors={"board/meta.json": TimeoutError("t")})) == 1
    assert "::error::R2 fetch failed for board/meta.json" in capsys.readouterr().err
    assert fetch_r2.fetch_object("b", "board/meta.json", dest, client=FakeS3({"board/meta.json": b"{}"})) == 0
    assert dest.read_bytes() == b"{}"


def test_object_optional_warns_on_missing_and_error(tmp_path, capsys):
    dest = tmp_path / "x" / "backtest.json"
    for fake in (FakeS3(), FakeS3(errors={"board/backtest.json": ClientError("AccessDenied", 403)})):
        rc = fetch_r2.fetch_object("b", "board/backtest.json", dest, client=fake, warn_message="Wind history unavailable")
        assert rc == 0 and not dest.exists()
    assert capsys.readouterr().out.count("::warning::Wind history unavailable") == 2


def test_object_wrangler_fallback_and_no_secrets_in_output(tmp_path, monkeypatch, capsys):
    def run(cmd, **kw):
        Path(next(a for a in cmd if a.startswith("--file=")).split("=", 1)[1]).write_bytes(b"{}")
        return Result()

    monkeypatch.setattr(fetch_r2.subprocess, "run", run)
    assert fetch_r2.main(["object", "board/meta.json", str(tmp_path / "m.json")]) == 0
    assert "R2 transport: wrangler" in capsys.readouterr().out
    _s3_env(monkeypatch)
    monkeypatch.setattr(publish_r2.r2, "make_client",
                        lambda *a, **k: FakeS3({"board/meta.json": b"{}"}))
    assert fetch_r2.main(["object", "board/meta.json", str(tmp_path / "m2.json")]) == 0
    out = capsys.readouterr().out
    assert "R2 transport: boto3" in out and "sekret-value" not in out


def test_main_state_cli_reads_bucket_and_workers(tmp_path, monkeypatch):
    _s3_env(monkeypatch)
    monkeypatch.setenv("R2_BUCKET", "other")
    monkeypatch.setenv("STATE_FILES", "openers")
    fake = FakeS3({"board/openers.json": b"{}", "board/meta.json": b"{}"})
    monkeypatch.setattr(publish_r2.r2, "make_client", lambda *a, **k: fake)
    assert fetch_r2.main(["state", "--dest", str(tmp_path / "s"), "--workers", "2"]) == 0
    assert sorted(p.name for p in (tmp_path / "s").iterdir()) == ["openers.json", "prev_meta.json"]
    with pytest.raises(SystemExit):
        fetch_r2.main(["state", "--workers", "0"])
