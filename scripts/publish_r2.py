"""Bounded, retryable R2 uploads (Wrangler API-token auth, or boto3 when S3 keys are set).

Transport: when ``pipeline.outputs.r2.config_from_env()`` finds the S3 credentials
(``CF_ACCOUNT_ID``/``CLOUDFLARE_ACCOUNT_ID`` + ``R2_ACCESS_KEY_ID`` + ``R2_SECRET_ACCESS_KEY``)
objects go through one shared boto3 client; otherwise each object spawns a Wrangler process.

Run one phase at a time so board/meta.json remains the final commit marker.
Each object is idempotently overwritten on retry; a phase fails only after all
attempts for an object fail. Concurrency is bounded to avoid overwhelming R2 or
the GitHub runner while removing per-process latency from the serial upload loop.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.outputs import r2  # noqa: E402
from pipeline.publication import BUILD_PROTECTED_NAMES, prepare_generation, retain_unrebuilt_sports, verify_generation  # noqa: E402

ROOT = Path("data")
MAX_WORKERS = 8
ATTEMPTS = 3
STATE_FILES = (
    "openers", "history", "wx_history", "archive_last", "wx_last", "alerts",
    "scrape_baseline", "telegram_state", "cf_heartbeat", "closings", "status",
    "ensemble_cache", "nws_points",
)
META_KEY = "board/meta.json"
META_DEADLINE_SECONDS = 15

_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict[tuple[r2.R2Config, bool], Any] = {}


def _boto3_available() -> bool:
    return importlib.util.find_spec("boto3") is not None


def s3_config() -> r2.R2Config | None:
    """S3 credentials from the environment, or None to keep the Wrangler transport."""
    cfg = r2.config_from_env()
    if cfg is None:
        return None
    if not _boto3_available():
        print("::warning::R2 S3 keys are set but boto3 is not installed; using Wrangler", file=sys.stderr)
        return None
    return cfg


def transport_name() -> str:
    return "boto3" if s3_config() is not None else "wrangler"


def get_client(cfg: r2.R2Config, *, bounded: bool = False) -> Any:
    """One shared S3 client per (config, bounded); creation is serialized, calls are thread-safe."""
    cache_key = (cfg, bounded)
    client = _CLIENTS.get(cache_key)
    if client is None:
        with _CLIENT_LOCK:
            client = _CLIENTS.get(cache_key)
            if client is None:
                client = r2.make_client(cfg, bounded=bounded,
                                        config=None if bounded else _upload_config())
                _CLIENTS[cache_key] = client
    return client


def _upload_config() -> Any:
    """Bulk uploads, including state puts. Standard mode retries InternalError and
    SlowDown with exponential backoff and jitter (default 6 attempts, cap 8).

    Each put is also retried ATTEMPTS times by ``_upload_boto3``. Connect/read
    timeouts stay short so one stalled connection cannot hold the publish step.
    """
    from botocore.config import Config

    return Config(**r2.boto_config_kwargs(bounded=False))


def reset_clients() -> None:
    with _CLIENT_LOCK:
        _CLIENTS.clear()


def _upload_boto3(client: Any, bucket: str, key: str, path: Path, content_type: str, *, sleep, attempts: int,
                  deadline: float | None) -> None:
    for attempt in range(1, attempts + 1):
        try:
            client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), ContentType=content_type)
            return
        except Exception as exc:  # noqa: BLE001 - botocore/OS errors are all retryable here
            detail = f"{type(exc).__name__}: {exc}"
        print(f"::warning::R2 put {key} failed (attempt {attempt}/{attempts}): {detail}", file=sys.stderr)
        if attempt < attempts:
            if deadline is not None and time.monotonic() >= deadline:
                break  # the commit marker must not outlast its freshness budget
            delay = r2.backoff_delay(attempt)
            print(f"::warning::R2 put {key} retrying in {delay:.2f}s after {detail}", file=sys.stderr)
            sleep(delay)
    raise RuntimeError(f"R2 put {key} failed after {attempts} attempts")


def upload_one(bucket: str, key: str, path: Path, content_type: str, *, run=subprocess.run,
               sleep=time.sleep, attempts: int = ATTEMPTS, timeout_seconds: int = 90,
               client: Any = None, bounded_client: Any = None) -> None:
    """Upload a single R2 object, retrying transient CLI/API failures.

    ``client`` (and ``bounded_client`` for the meta commit marker) force the boto3
    transport with injected clients; otherwise boto3 is used only when the S3 keys are
    configured, and the Wrangler CLI path is unchanged.
    """
    is_meta = key == META_KEY
    if client is None:
        cfg = s3_config()
        if cfg is not None:
            client = get_client(cfg, bounded=is_meta)
    elif is_meta and bounded_client is not None:
        client = bounded_client
    if client is not None:
        if is_meta:
            _upload_boto3(client, bucket, key, path, content_type, sleep=sleep, attempts=min(attempts, 2),
                          deadline=time.monotonic() + META_DEADLINE_SECONDS)
        else:
            _upload_boto3(client, bucket, key, path, content_type, sleep=sleep, attempts=attempts, deadline=None)
        return
    command = [
        "npx", "--yes", "wrangler@4", "r2", "object", "put", f"{bucket}/{key}",
        f"--file={path}", f"--content-type={content_type}", "--remote",
    ]
    # The commit marker is tiny and must not hang beyond the quotes' reserved
    # freshness budget. Leave enough retries to ride out one transient response.
    if is_meta:
        attempts = min(attempts, 2)
        timeout_seconds = min(timeout_seconds, META_DEADLINE_SECONDS)
    for attempt in range(1, attempts + 1):
        try:
            result = run(command, text=True, capture_output=True, check=False, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            detail = f"Wrangler timed out after {exc.timeout}s"
        else:
            if result.returncode == 0:
                return
            detail = (result.stderr or result.stdout or "no CLI output").strip()
        print(f"::warning::R2 put {key} failed (attempt {attempt}/{attempts}): {detail}", file=sys.stderr)
        if attempt < attempts:
            delay = r2.backoff_delay(attempt)
            print(f"::warning::R2 put {key} retrying in {delay:.2f}s after {detail}", file=sys.stderr)
            sleep(delay)
    raise RuntimeError(f"R2 put {key} failed after {attempts} attempts")


def files_for_phase(phase: str) -> list[tuple[str, Path, str]]:
    """Return (key, local path, content type) tuples in a deterministic order."""
    if phase == "raw":
        base = ROOT / "raw_runs"
        return [(f"raw/{p.relative_to(base).as_posix()}", p, "application/octet-stream")
                for p in sorted(base.rglob("*")) if p.is_file()] if base.is_dir() else []
    if phase == "snapshots":
        base = ROOT / "snapshots"
        return [(f"snapshots/{p.relative_to(base).as_posix()}", p, "application/json")
                for p in sorted(base.rglob("*.json")) if p.is_file()] if base.is_dir() else []
    if phase == "legacy":
        entries = (
            ("legacy/nfl_weather.csv", ROOT / "nfl_weather.csv", "text/csv"),
            ("legacy/cfb_weather.xlsx", ROOT / "cfb_weather.xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        return [(key, path, content_type) for key, path, content_type in entries if path.is_file()]
    if phase == "board":
        base = ROOT / "board"
        return [(f"board/{p.name}", p, "application/json")
                for p in sorted(base.glob("*.json")) if p.is_file() and p.name != "meta.json"
                and p.stem not in BUILD_PROTECTED_NAMES] if base.is_dir() else []
    if phase == "state":
        names = os.environ.get("STATE_FILES", " ".join(STATE_FILES)).split()
        return [(f"board/{name}.json", ROOT / "state" / f"{name}.json", "application/json")
                for name in names if name not in BUILD_PROTECTED_NAMES and (ROOT / "state" / f"{name}.json").is_file()]
    if phase == "meta":
        path = ROOT / "board" / "meta.json"
        return [("board/meta.json", path, "application/json")] if path.is_file() else []
    raise ValueError(f"unknown publishing phase: {phase}")


def _previous_meta() -> dict | None:
    path = ROOT / "state" / "prev_meta.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Previous board meta is unreadable; refusing to publish ({exc})") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("Previous board meta is unreadable; refusing to publish")
    return loaded


def _live_reader(bucket: str):
    """Return object bytes, or None when the key is absent. Other errors fail the publish."""
    from scripts.fetch_r2 import FetchError, fetch_bytes_boto3, fetch_bytes_wrangler

    def reader(key: str) -> bytes | None:
        cfg = s3_config()
        try:
            if cfg is not None:
                return fetch_bytes_boto3(get_client(cfg, bounded=True), bucket, key)
            return fetch_bytes_wrangler(bucket, key)
        except FetchError as exc:
            raise RuntimeError(f"Could not read previous {key}: {exc}") from exc

    return reader


def publish_phase(phase: str, bucket: str, *, workers: int = MAX_WORKERS,
                  upload_fn=upload_one, read_fn=None, retain_reader=None) -> list[str]:
    """Upload one phase with bounded concurrency; preserve errors and report keys."""
    items = files_for_phase(phase)
    print(f"R2 transport: {transport_name()} (phase {phase})")
    if phase == "meta" and len(items) != 1:
        raise RuntimeError("board/meta.json is missing; refusing to report a completed publish")
    if phase in ('board', 'meta'):
        board_files = {f'board/{path.name}': path for path in (ROOT / 'board').glob('*.json')}
        if (ROOT / "board" / "meta.json").is_file():
            board_files = retain_unrebuilt_sports(
                board_files,
                _previous_meta(),
                retain_reader or _live_reader(bucket),
                ROOT / "publication-retain",
                floor=r2.CONTENT_FLOOR,
            )
        immutable, pointer = prepare_generation(board_files, ROOT / 'publication')
        if phase == 'board':
            # The sealed generation and the plain board/games_*.json (and board.json)
            # keys must be the same bytes. A direct read of the mutable key then
            # cannot disagree with the generation the site header points at.
            plain = {key: (path, content_type) for key, path, content_type in items}
            for key, path in board_files.items():
                name = Path(key).name
                if name in {"board.json", "games_nfl.json", "games_cfb.json"}:
                    plain[f"board/{name}"] = (Path(path), "application/json")
            items = [(key, path, content_type) for key, (path, content_type) in plain.items()]
            items.extend((key, path, "application/json") for key, path in immutable.items())
        else:
            reader = read_fn or (lambda key: download_one(bucket, key, ROOT / 'publication-check'))
            verify_generation(json.loads(pointer.read_bytes()), reader)
            items = [('board/meta.json', pointer, 'application/json')]
    if not items:
        print(f"No objects in R2 phase {phase}")
        return []
    failures: list[tuple[str, BaseException]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-put") as pool:
        futures = {pool.submit(upload_fn, bucket, key, path, content_type): key
                   for key, path, content_type in items}
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                print(f"Uploaded {key}")
            except Exception as exc:
                failures.append((key, exc))
    if failures:
        for key, exc in failures:
            print(f"::error::R2 put {key} failed: {exc}", file=sys.stderr)
        raise RuntimeError(f"R2 phase {phase} failed for {len(failures)} of {len(items)} objects")
    return [key for key, _, _ in items]


def download_one(bucket: str, key: str, target: Path, *, run=subprocess.run, allow_missing: bool = False,
                 missing_bytes: bytes = b'{"schema_version":1,"bets":{}}', client: Any = None) -> bytes:
    target.mkdir(parents=True, exist_ok=True)
    path = target / key.replace('/', '_')
    if client is None:
        cfg = s3_config()
        if cfg is not None:
            client = get_client(cfg, bounded=True)
    if client is not None:
        try:
            data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if allow_missing and r2.is_no_such_key(exc):
                return missing_bytes
            raise RuntimeError(f'Publication verification fetch failed: {key}') from exc
        data = data if isinstance(data, bytes) else bytes(data)
        path.write_bytes(data)
        return data
    result = run([shutil.which('npx') or 'npx', '--yes', 'wrangler@4', 'r2', 'object', 'get',
                  f'{bucket}/{key}', f'--file={path}', '--remote'], text=True, capture_output=True,
                 check=False, timeout=90)
    if result.returncode != 0:
        import re
        if allow_missing and re.search(r'NoSuchKey|does not exist|not found|\b404\b', (result.stderr or '') + (result.stdout or ''), re.I):
            return missing_bytes
        raise RuntimeError(f'Publication verification fetch failed: {key}')
    return path.read_bytes()


def upload_files(bucket: str, files: dict[str, Path], *, workers: int = MAX_WORKERS,
                 upload_fn=upload_one) -> list[str]:
    """Upload an explicit key->path batch, useful for small post-publish receipts."""
    failures: list[tuple[str, BaseException]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-put") as pool:
        futures = {
            pool.submit(upload_fn, bucket, key, path, _content_type(path)): key
            for key, path in files.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append((key, exc))
    if failures:
        for key, exc in failures:
            print(f"::error::R2 put {key} failed: {exc}", file=sys.stderr)
        raise RuntimeError(f"R2 upload failed for {len(failures)} of {len(files)} objects")
    return list(files)


def _content_type(path: Path) -> str:
    return {
        ".json": "application/json", ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(path.suffix.lower(), "application/octet-stream")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("raw", "snapshots", "legacy", "board", "state", "meta"))
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "football-board"))
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 32:
        parser.error("workers must be between 1 and 32")
    try:
        keys = publish_phase(args.phase, args.bucket, workers=args.workers)
    except Exception as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"Published {len(keys)} object(s) in phase {args.phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
