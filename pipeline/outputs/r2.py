"""Cloudflare R2 publisher + state round-trip + post-publish self-check (ARCH §5, §13).

boto3 path (``push_to_r2`` from golf_scraping/board/build.py L3220-3238) driven by
env ``CF_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY`` (+ optional
``R2_BUCKET``, default ``football-board``). The GitHub workflow uses the wrangler
CLI loops instead (no S3 keys needed there); this module is what ``pipeline.build
--publish`` uses locally / when the S3 keys are present, and it hosts the
``--self-check`` that the workflow runs after its put loop.

Contracts pinned by tests:
* ``get_state``: ``NoSuchKey`` -> skipped (fresh state); any other error raises.
* ``publish``: two-phase — every payload first, ``board/meta.json`` LAST, each put
  retried 3x; a failure before meta leaves old meta over old-or-new data.
* ``self_check``: re-fetches ``board/meta.json``, asserts ``run_id`` and applies a
  content floor (each sport's game count >= 50 % of the previous meta) unless
  ``--force``.

    python -m pipeline.outputs.r2 --self-check --run-id <id> [--prev-meta data/state/prev_meta.json] [--meta-file data/check/meta.json] [--force]
    python -m pipeline.outputs.r2 --get-state --dest data/state
    python -m pipeline.outputs.r2 --publish --manifest data/publish_manifest.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

DEFAULT_BUCKET = "football-board"
BOARD_PREFIX = "board"
RAW_PREFIX = "raw"
META_KEY = f"{BOARD_PREFIX}/meta.json"
CONTENT_FLOOR = 0.5
RETRIES = 3
RETRY_SLEEP_S = 5.0

# State files fetched before a run and pushed after (ARCH §5 state row).
STATE_FILES: tuple[str, ...] = (
    "openers", "history", "wx_history", "archive_last", "wx_last", "alerts",
    "scrape_baseline", "telegram_state", "cf_heartbeat", "closings", "status",
)

_CONTENT_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".sql": "text/plain",
    ".parquet": "application/octet-stream",
}


def content_type_for(name: str) -> str:
    return _CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


# ---- config / client -------------------------------------------------------------------

@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str = DEFAULT_BUCKET
    endpoint: Optional[str] = None

    @property
    def endpoint_url(self) -> str:
        return self.endpoint or f"https://{self.account_id}.r2.cloudflarestorage.com"


def config_from_env(env: Optional[dict[str, str]] = None) -> Optional[R2Config]:
    """None unless all three credentials are present (then R2 is "configured")."""
    env = os.environ if env is None else env
    account = (env.get("CF_ACCOUNT_ID") or env.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    key = (env.get("R2_ACCESS_KEY_ID") or "").strip()
    secret = (env.get("R2_SECRET_ACCESS_KEY") or "").strip()
    if not (account and key and secret):
        return None
    return R2Config(account, key, secret, (env.get("R2_BUCKET") or DEFAULT_BUCKET).strip(), env.get("R2_ENDPOINT") or None)


def configured(env: Optional[dict[str, str]] = None) -> bool:
    return config_from_env(env) is not None


def make_client(cfg: R2Config) -> Any:
    import boto3  # optional dependency: only needed when publishing

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
    )


# ---- low-level ops --------------------------------------------------------------------

def is_no_such_key(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("NoSuchKey", "NotFound"):
        return True
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str(((resp.get("Error") or {}).get("Code")) or "")
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "404", "NotFound") or status == 404:
            return True
    return "NoSuchKey" in str(exc)


def _retry(fn: Callable[[], Any], what: str, attempts: int = RETRIES, sleep: Callable[[float], None] = time.sleep) -> Any:
    last: Optional[BaseException] = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning(f"{what} failed (attempt {i}/{attempts}): {exc}")
            if i < attempts:
                sleep(RETRY_SLEEP_S)
    raise RuntimeError(f"{what} failed after {attempts} attempts: {last}") from last


def get_object(client: Any, bucket: str, key: str) -> Optional[bytes]:
    """Bytes of ``key`` or None when it does not exist. Any other error propagates."""
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        if is_no_such_key(exc):
            return None
        raise
    body = resp["Body"]
    data = body.read()
    return data if isinstance(data, bytes) else bytes(data)


def put_bytes(client: Any, bucket: str, key: str, data: bytes, content_type: Optional[str] = None,
              sleep: Callable[[float], None] = time.sleep) -> None:
    ct = content_type or content_type_for(key)
    _retry(lambda: client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=ct), f"R2 put {key}", sleep=sleep)


def put_file(client: Any, bucket: str, key: str, path: PathLike, sleep: Callable[[float], None] = time.sleep) -> None:
    put_bytes(client, bucket, key, Path(path).read_bytes(), content_type_for(str(path)), sleep=sleep)


# ---- state round-trip ----------------------------------------------------------------

def get_state(client: Any, bucket: str, dest_dir: PathLike, names: Sequence[str] = STATE_FILES,
              prefix: str = BOARD_PREFIX) -> dict[str, Optional[Path]]:
    """Fetch ``{prefix}/{name}.json`` into ``dest_dir/{name}.json``. A missing object
    (NoSuchKey) is skipped — fresh state; any other error raises so the run never
    proceeds on transient failure (it would clobber openers/alerts)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, Optional[Path]] = {}
    for name in names:
        key = f"{prefix}/{name}.json"
        data = get_object(client, bucket, key)
        if data is None:
            logger.info(f"{key} absent (first run) - state starts empty")
            out[name] = None
            continue
        p = dest / f"{name}.json"
        p.write_bytes(data)
        out[name] = p
    return out


def put_state(client: Any, bucket: str, state_dir: PathLike, names: Sequence[str] = STATE_FILES,
              prefix: str = BOARD_PREFIX, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    pushed = []
    for name in names:
        p = Path(state_dir) / f"{name}.json"
        if not p.is_file():
            continue
        key = f"{prefix}/{name}.json"
        put_file(client, bucket, key, p, sleep=sleep)
        pushed.append(key)
    return pushed


# ---- two-phase publish -------------------------------------------------------------

def order_keys(keys: Iterable[str]) -> list[str]:
    """Publish order: raw/ + snapshots/ first, then board payloads, state, meta LAST."""
    ks = [k for k in keys if k != META_KEY]
    has_meta = META_KEY in set(keys)

    def rank(k: str) -> tuple[int, str]:
        if k.startswith(f"{RAW_PREFIX}/"):
            return (0, k)
        if k.startswith("snapshots/"):
            return (1, k)
        if k.startswith(f"{BOARD_PREFIX}/") and Path(k).stem in STATE_FILES:
            return (3, k)
        return (2, k)

    ks.sort(key=rank)
    if has_meta:
        ks.append(META_KEY)
    return ks


def publish(client: Any, bucket: str, files: dict[str, PathLike], sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Upload ``{r2_key: local path}``; ``board/meta.json`` goes last so a failure
    mid-loop leaves the old meta (readers see the previous consistent run).
    Raises on the first key that fails all retries."""
    pushed: list[str] = []
    for key in order_keys(files.keys()):
        put_file(client, bucket, key, files[key], sleep=sleep)
        pushed.append(key)
    logger.info(f"Pushed {len(pushed)} objects to r2://{bucket}/ (meta last={pushed[-1] == META_KEY if pushed else False})")
    return pushed


def push_to_r2(files: dict[str, PathLike], cfg: Optional[R2Config] = None) -> list[str]:
    cfg = cfg or config_from_env()
    if cfg is None:
        raise RuntimeError("R2 not configured: set CF_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY")
    return publish(make_client(cfg), cfg.bucket, files)


def raw_dir_files(raw_base: PathLike) -> dict[str, Path]:
    """``raw/{sport}/{run_id}/...`` keys for everything under a local raw_runs dir."""
    base = Path(raw_base)
    if not base.is_dir():
        return {}
    return {f"{RAW_PREFIX}/{p.relative_to(base).as_posix()}": p for p in base.rglob("*") if p.is_file()}


def raw_mirror(client: Any, bucket: str) -> Callable[[str, bytes, str], None]:
    """Callback for ``RawStore.mirror``: uploads one capture immediately."""
    def _mirror(key: str, data: bytes, content_type: str) -> None:
        put_bytes(client, bucket, key, data, content_type)
    return _mirror


# ---- self-check --------------------------------------------------------------------

def check_meta(meta: dict[str, Any], run_id: str, prev_meta: Optional[dict[str, Any]] = None,
               floor: float = CONTENT_FLOOR, force: bool = False) -> list[str]:
    """Problems found (empty = OK). ``run_id`` must match; each sport count must be
    >= floor x previous count when the previous meta is known (skipped on ``force``)."""
    problems: list[str] = []
    got = meta.get("run_id")
    if got != run_id:
        problems.append(f"meta.run_id={got!r} != expected {run_id!r}")
    counts = meta.get("sport_counts") or {}
    if not force and prev_meta:
        prev = prev_meta.get("sport_counts") or {}
        for sport, before in prev.items():
            try:
                before_n = int(before or 0)
            except (TypeError, ValueError):
                continue
            if before_n <= 0:
                continue
            now_n = int(counts.get(sport) or 0)
            if now_n < floor * before_n:
                problems.append(f"content floor: {sport} games {now_n} < {floor:.0%} of previous {before_n}")
    return problems


def self_check(run_id: str, *, meta_file: Optional[PathLike] = None, prev_meta_file: Optional[PathLike] = None,
               floor: float = CONTENT_FLOOR, force: bool = False, cfg: Optional[R2Config] = None) -> list[str]:
    if meta_file is not None:
        meta = json.loads(Path(meta_file).read_text(encoding="utf-8"))
    else:
        cfg = cfg or config_from_env()
        if cfg is None:
            return ["self-check: no --meta-file and R2 not configured"]
        data = get_object(make_client(cfg), cfg.bucket, META_KEY)
        if data is None:
            return [f"self-check: {META_KEY} missing after publish"]
        meta = json.loads(data.decode("utf-8"))
    prev = None
    if prev_meta_file is not None and Path(prev_meta_file).is_file():
        try:
            prev = json.loads(Path(prev_meta_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
    return check_meta(meta, run_id, prev, floor=floor, force=force)


# ---- CLI -----------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.outputs.r2")
    p.add_argument("--self-check", action="store_true")
    p.add_argument("--run-id", default=None)
    p.add_argument("--meta-file", type=Path, default=None, help="local copy of board/meta.json (fetched by wrangler)")
    p.add_argument("--prev-meta", type=Path, default=None, help="meta.json as fetched BEFORE this run")
    p.add_argument("--floor", type=float, default=CONTENT_FLOOR)
    p.add_argument("--force", action="store_true", help="skip the content floor")
    p.add_argument("--get-state", action="store_true")
    p.add_argument("--dest", type=Path, default=Path("data/state"))
    p.add_argument("--publish", action="store_true")
    p.add_argument("--manifest", type=Path, default=Path("data/publish_manifest.json"), help='{"r2/key": "local path"}')
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.self_check:
        if not args.run_id:
            print("::error::--self-check requires --run-id")
            return 2
        problems = self_check(args.run_id, meta_file=args.meta_file, prev_meta_file=args.prev_meta, floor=args.floor, force=args.force)
        for pr in problems:
            print(f"::error::self-check: {pr}")
        if not problems:
            print(f"self-check OK: meta.run_id={args.run_id}")
        return 1 if problems else 0
    cfg = config_from_env()
    if cfg is None:
        print("::error::R2 not configured (CF_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
        return 2
    client = make_client(cfg)
    if args.get_state:
        got = get_state(client, cfg.bucket, args.dest)
        for name, path in got.items():
            print(f"{name}: {'fetched' if path else 'absent'}")
        return 0
    if args.publish:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        pushed = publish(client, cfg.bucket, {k: Path(v) for k, v in manifest.items()})
        print(f"pushed {len(pushed)} objects; last={pushed[-1] if pushed else '-'}")
        return 0
    print("nothing to do (see --help)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_BUCKET", "BOARD_PREFIX", "RAW_PREFIX", "META_KEY", "CONTENT_FLOOR", "STATE_FILES",
    "R2Config", "config_from_env", "configured", "make_client", "is_no_such_key",
    "get_object", "put_bytes", "put_file", "get_state", "put_state", "order_keys", "publish", "push_to_r2",
    "raw_dir_files", "raw_mirror", "check_meta", "self_check", "content_type_for", "main",
]
