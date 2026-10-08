"""Bounded, retryable R2 uploads through the workflow's Wrangler API-token auth.

Run one phase at a time so board/meta.json remains the final commit marker.
Each object is idempotently overwritten on retry; a phase fails only after all
attempts for an object fail. Concurrency is bounded to avoid overwhelming R2 or
the GitHub runner while removing per-process latency from the serial upload loop.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path("data")
MAX_WORKERS = 8
ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
STATE_FILES = (
    "openers", "history", "wx_history", "archive_last", "wx_last", "alerts",
    "scrape_baseline", "telegram_state", "cf_heartbeat", "closings", "status",
    "ensemble_cache", "nws_points",
)


def upload_one(bucket: str, key: str, path: Path, content_type: str, *, run=subprocess.run,
               sleep=time.sleep, attempts: int = ATTEMPTS, timeout_seconds: int = 90) -> None:
    """Upload a single R2 object, retrying transient CLI/API failures."""
    command = [
        "npx", "--yes", "wrangler@4", "r2", "object", "put", f"{bucket}/{key}",
        f"--file={path}", f"--content-type={content_type}", "--remote",
    ]
    # The commit marker is tiny and must not hang beyond the quotes' reserved
    # freshness budget. Leave enough retries to ride out one transient response.
    if key == "board/meta.json":
        attempts = min(attempts, 2)
        timeout_seconds = min(timeout_seconds, 15)
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
            sleep(RETRY_DELAY_SECONDS)
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
                for p in sorted(base.glob("*.json")) if p.is_file() and p.name != "meta.json"] if base.is_dir() else []
    if phase == "state":
        excluded = {"cf_heartbeat"}  # Worker-owned; do not roll back a newer tick.
        names = os.environ.get("STATE_FILES", " ".join(STATE_FILES)).split()
        return [(f"board/{name}.json", ROOT / "state" / f"{name}.json", "application/json")
                for name in names if name not in excluded and (ROOT / "state" / f"{name}.json").is_file()]
    if phase == "meta":
        path = ROOT / "board" / "meta.json"
        return [("board/meta.json", path, "application/json")] if path.is_file() else []
    raise ValueError(f"unknown publishing phase: {phase}")


def publish_phase(phase: str, bucket: str, *, workers: int = MAX_WORKERS,
                  upload_fn=upload_one) -> list[str]:
    """Upload one phase with bounded concurrency; preserve errors and report keys."""
    items = files_for_phase(phase)
    if phase == "meta" and len(items) != 1:
        raise RuntimeError("board/meta.json is missing; refusing to report a completed publish")
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
