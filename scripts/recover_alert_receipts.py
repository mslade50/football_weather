"""Recover recent sent-alert keys from failed main-branch workflow artifacts.

This reads only ``board/alerts_feed.json`` from bounded ``raw-runs*`` artifacts.
It never downloads logs or raw provider captures and never sends notifications.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

# ``python scripts/recover_alert_receipts.py`` puts scripts/, not the repo root,
# at sys.path[0]. Add the checkout root so pipeline imports work in Actions.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import state  # noqa: E402

API = "https://api.github.com"
LOOKBACK = timedelta(hours=24)
MAX_RUNS = 5
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FEED_BYTES = 4 * 1024 * 1024
CONCLUSIONS = {"failure", "cancelled", "timed_out"}


def _get_json(client: httpx.Client, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.get(url, **kwargs)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError(f"GitHub API returned a non-object for {url}")
    return value


def _recent_failed_runs(client: httpx.Client, repository: str, current_run_id: str,
                        now: datetime) -> list[dict[str, Any]]:
    payload = _get_json(
        client, f"{API}/repos/{repository}/actions/workflows/pipeline.yml/runs",
        params={"branch": "main", "status": "completed", "per_page": 100},
    )
    rows = payload.get("workflow_runs")
    if not isinstance(rows, list):
        return []
    cutoff = now - LOOKBACK
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("id")) == current_run_id:
            continue
        if row.get("head_branch") != "main" or row.get("status") != "completed":
            continue
        if row.get("conclusion") not in CONCLUSIONS:
            continue
        try:
            created = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None or created < cutoff or created > now + timedelta(minutes=5):
            continue
        candidates.append((created, row))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in candidates[:MAX_RUNS]]


def _artifact_feeds(client: httpx.Client, repository: str, run: dict[str, Any],
                    total_downloaded: int) -> tuple[list[dict[str, Any]], int]:
    run_id = str(run.get("id"))
    expected_artifact_names = {f"raw-runs-{run_id}", f"raw-runs-pw-{run_id}"}
    payload = _get_json(client, f"{API}/repos/{repository}/actions/runs/{run_id}/artifacts",
                        params={"per_page": 100})
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return [], total_downloaded
    feeds = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") not in expected_artifact_names:
            continue
        if artifact.get("expired") is True:
            raise RuntimeError(f"candidate receipt artifact {artifact.get('name')} has expired")
        declared_size = artifact.get("size_in_bytes")
        if not isinstance(declared_size, int) or declared_size <= 0:
            continue
        if declared_size > MAX_ARCHIVE_BYTES:
            raise RuntimeError(f"candidate receipt artifact {artifact.get('name')} exceeds the download cap")
        if total_downloaded + declared_size > MAX_TOTAL_BYTES:
            raise RuntimeError("recent receipt artifacts exceed the total download cap; refusing to notify")
        artifact_id = artifact.get("id")
        if artifact_id is None:
            continue
        response = client.get(f"{API}/repos/{repository}/actions/artifacts/{artifact_id}/zip")
        response.raise_for_status()
        archive = response.content
        if len(archive) > MAX_ARCHIVE_BYTES or total_downloaded + len(archive) > MAX_TOTAL_BYTES:
            raise RuntimeError("downloaded receipt artifact exceeds the configured byte cap")
        total_downloaded += len(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for info in zipped.infolist():
                parts = [part for part in info.filename.replace("\\", "/").split("/") if part]
                if len(parts) < 2 or parts[-2:] != ["board", "alerts_feed.json"]:
                    continue
                if info.file_size > MAX_FEED_BYTES:
                    raise RuntimeError(f"receipt feed in {artifact.get('name')} exceeds the feed size cap")
                try:
                    feed = json.loads(zipped.read(info))
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    raise RuntimeError(f"could not read receipt feed in {artifact.get('name')}: {exc}") from exc
                if isinstance(feed, dict):
                    feeds.append(feed)
    return feeds, total_downloaded


def recover_alert_receipts(state_dir: Path, repository: str, token: str, *,
                           current_run_id: str = "", now: datetime | None = None,
                           client: httpx.Client | None = None) -> int:
    """Merge missing recent feed markers into local alerts.json; return added keys."""
    if not repository or "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY must be an owner/repo name")
    if not token:
        raise ValueError("GH_TOKEN is required for read-only Actions artifact access")
    clock = now or datetime.now(timezone.utc)
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
    try:
        runs = _recent_failed_runs(client, repository, current_run_id, clock)
        rows: dict[str, dict[str, Any]] = {}
        total_downloaded = 0
        for run in runs:
            run_id = str(run.get("id"))
            feeds, total_downloaded = _artifact_feeds(client, repository, run, total_downloaded)
            for feed in feeds:
                meta = feed.get("meta") if isinstance(feed.get("meta"), dict) else {}
                feed_run_id = str(meta.get("run_id") or "")
                attempt = int(run.get("run_attempt") or 1)
                if f"-gh{run_id}-{attempt}" not in feed_run_id:
                    continue
                feed_sha = str(meta.get("git_sha") or "")
                head_sha = str(run.get("head_sha") or "")
                if feed_sha and head_sha and not head_sha.startswith(feed_sha):
                    continue
                items = feed.get("alerts") or feed.get("items") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or not item.get("alert_key"):
                        continue
                    sent_at = str(item.get("sent_at") or "")
                    try:
                        sent_time = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if sent_time.tzinfo is None or sent_time < clock - LOOKBACK or sent_time > clock + timedelta(minutes=5):
                        continue
                    key = str(item["alert_key"])
                    prior = rows.get(key)
                    if prior is not None:
                        prior_time = datetime.fromisoformat(prior["last_sent_at"].replace("Z", "+00:00"))
                        if prior_time >= sent_time:
                            continue
                    rows[key] = {**item, "first_sent_at": sent_at, "last_sent_at": sent_at,
                                 "run_id": feed_run_id, "status": "open"}
        if not rows:
            print("No recent sent-alert receipts to recover")
            return 0
        state_dir.mkdir(parents=True, exist_ok=True)
        alerts = state.load_alerts(state_dir)
        added = state.rehydrate_alerts(alerts, rows.values())
        if added:
            state.save_alerts(state_dir, alerts)
        print(f"Recovered {added} missing recent alert receipt(s) from {len(runs)} failed main-branch run(s)")
        return added
    finally:
        if owns_client:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("data/state"))
    args = parser.parse_args()
    try:
        recover_alert_receipts(args.state_dir, os.environ.get("GITHUB_REPOSITORY", ""),
                               os.environ.get("GH_TOKEN", ""),
                               current_run_id=os.environ.get("CURRENT_GITHUB_RUN_ID", ""))
    except Exception as exc:
        print(f"::error::Alert receipt recovery failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
