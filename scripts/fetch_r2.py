"""Bounded R2 reads for the pipeline workflow (boto3 when S3 keys are set, else Wrangler).

Transport selection and the shared client come from ``scripts.publish_r2``; the Wrangler
fallback is the exact ``npx --yes wrangler@4 r2 object get`` the workflow used before.

    python scripts/fetch_r2.py state [--dest data/state]
    python scripts/fetch_r2.py object KEY DEST [--warn-message TEXT]

``state``  fetches ``board/<name>.json`` for every name in $STATE_FILES plus ``meta`` (saved as
           ``prev_meta.json``). A missing object removes the destination (first run: state
           starts empty); ANY other failure exits 1 and writes nothing, so a run can never
           proceed on blank state.
``object`` fetches one key. Without ``--warn-message`` any failure (missing included) exits 1;
           with it, any failure only prints ``::warning::<text>`` and exits 0.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.outputs import r2  # noqa: E402
from scripts import publish_r2  # noqa: E402

WRANGLER_TIMEOUT_SECONDS = 90
# Same pattern (and case-insensitivity) as the workflow's former `grep -qiE`.
_MISSING_RE = re.compile(r'NoSuchKey|does not exist|not found|404', re.I)
STATE_MAX_WORKERS = 8
META_PREV_NAME = "prev_meta"


class FetchError(Exception):
    """Non-missing failure (auth, 5xx, network, timeout)."""


def _is_missing_boto3(exc: BaseException) -> bool:
    # A missing BUCKET is also HTTP 404; it is a misconfiguration, never "first run".
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict) and str((resp.get("Error") or {}).get("Code") or "") == "NoSuchBucket":
        return False
    return r2.is_no_such_key(exc)


def _sleep(seconds: float) -> None:
    r2._sleep(seconds)


def _warn_retry(key: str, exc: BaseException, attempt: int, tries: int, delay: float) -> None:
    print(
        f"::warning::R2 get {key} failed ({type(exc).__name__}: {exc}); "
        f"retry {attempt}/{tries} in {delay:.2f}s",
        file=sys.stderr,
    )


def fetch_bytes_boto3(client: Any, bucket: str, key: str) -> bytes | None:
    """Object bytes, None when the key is absent, FetchError for anything else.

    InternalError and SlowDown are retried with exponential backoff and jitter
    (``R2_EXPLICIT_ATTEMPTS``, default 3) on top of the client's boto3 standard
    retry mode. AccessDenied, NoSuchBucket, and other errors fail immediately.
    """

    def once() -> bytes | None:
        try:
            data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 - botocore/OS errors
            if _is_missing_boto3(exc):
                return None
            raise
        return data if isinstance(data, bytes) else bytes(data)

    try:
        return r2.call_with_explicit_retry(
            once, f"R2 get {key}", sleep=_sleep,
            on_retry=lambda exc, attempt, tries, delay: _warn_retry(key, exc, attempt, tries, delay),
        )
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc


def fetch_bytes_wrangler(bucket: str, key: str, *, run=None) -> bytes | None:
    """Same CLI call as the old workflow; the file lands in a temp path first."""
    run = run or subprocess.run  # resolved at call time so tests can patch subprocess.run
    with tempfile.TemporaryDirectory(prefix="r2get-") as tmp:
        out = Path(tmp) / "object"
        try:
            result = run([shutil.which("npx") or "npx", "--yes", "wrangler@4", "r2", "object", "get",
                          f"{bucket}/{key}", f"--file={out}", "--remote"],
                         text=True, capture_output=True, check=False, timeout=WRANGLER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise FetchError(f"Wrangler timed out after {exc.timeout}s") from exc
        if result.returncode == 0:
            if not out.is_file():
                raise FetchError("Wrangler reported success but wrote no file")
            return out.read_bytes()
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if _MISSING_RE.search(output):
            return None
        raise FetchError(output or "no CLI output")


def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _state_targets(dest: Path) -> list[tuple[str, str, Path]]:
    names = os.environ.get("STATE_FILES", " ".join(publish_r2.STATE_FILES)).split()
    targets = [(f, f"board/{f}.json", dest / f"{f}.json") for f in names]
    targets.append(("meta", "board/meta.json", dest / f"{META_PREV_NAME}.json"))
    return targets


def fetch_state(bucket: str, dest: Path, *, workers: int = STATE_MAX_WORKERS, run=None,
                client: Any = None) -> int:
    """Fail-closed state fetch; returns the process exit code."""
    cfg = None if client is not None else publish_r2.s3_config()
    use_boto3 = client is not None or cfg is not None
    print(f"R2 transport: {'boto3' if use_boto3 else 'wrangler'} (fetch state)")
    targets = _state_targets(dest)
    dest.mkdir(parents=True, exist_ok=True)
    results: dict[str, bytes | None] = {}
    errors: dict[str, str] = {}

    def one(name: str, key: str) -> None:
        try:
            if use_boto3:
                results[name] = fetch_bytes_boto3(client or publish_r2.get_client(cfg), bucket, key)
            else:
                results[name] = fetch_bytes_wrangler(bucket, key, run=run)
        except FetchError as exc:
            errors[name] = str(exc)

    if use_boto3:
        client = client or publish_r2.get_client(cfg)  # one shared, bounded client for the pool
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-get") as pool:
            for future in [pool.submit(one, name, key) for name, key, _ in targets]:
                future.result()
    else:
        for name, key, _ in targets:  # Wrangler: serial, stop at the first failure (old behaviour)
            one(name, key)
            if name in errors:
                break
    for name, _, _ in targets:
        if name in errors:
            print(errors[name])
            print(f"::error::R2 state fetch failed for {name}.json (not NoSuchKey) - refusing to run on blank state")
    if errors:
        return 1  # nothing has been written or removed: no partial state for later steps
    for name, _, path in targets:
        data = results[name]
        if data is None:
            print(f"::notice::{name}.json absent (first run) - state starts empty")
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, data)
            print(f"{name}.json fetched")
    return 0


def fetch_object(bucket: str, key: str, dest: Path, *, warn_message: str | None = None, run=None,
                 client: Any = None) -> int:
    cfg = None if client is not None else publish_r2.s3_config()
    use_boto3 = client is not None or cfg is not None
    print(f"R2 transport: {'boto3' if use_boto3 else 'wrangler'} (fetch {key})")
    try:
        if use_boto3:
            data = fetch_bytes_boto3(client or publish_r2.get_client(cfg), bucket, key)
        else:
            data = fetch_bytes_wrangler(bucket, key, run=run)
        if data is None:
            raise FetchError(f"{key} does not exist")
    except FetchError as exc:
        if warn_message is None:
            print(f"::error::R2 fetch failed for {key}: {exc}", file=sys.stderr)
            return 1
        print(f"{key}: {exc}")
        print(f"::warning::{warn_message}")
        return 0
    _write_atomic(dest, data)
    print(f"Fetched {key}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "football-board"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("state")
    st.add_argument("--dest", type=Path, default=Path("data/state"))
    st.add_argument("--workers", type=int, default=STATE_MAX_WORKERS)
    ob = sub.add_parser("object")
    ob.add_argument("key")
    ob.add_argument("dest", type=Path)
    ob.add_argument("--warn-message", default=None)
    args = parser.parse_args(argv)
    if args.cmd == "state":
        if not 1 <= args.workers <= 32:
            parser.error("workers must be between 1 and 32")
        return fetch_state(args.bucket, args.dest, workers=args.workers)
    return fetch_object(args.bucket, args.key, args.dest, warn_message=args.warn_message)


if __name__ == "__main__":
    raise SystemExit(main())
