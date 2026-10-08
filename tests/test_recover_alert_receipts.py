"""Recent failed-run receipts prevent resend after a pre-state-write timeout."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from pipeline import state
from scripts.recover_alert_receipts import recover_alert_receipts

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)


def test_script_path_invocation_can_import_pipeline():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts" / "recover_alert_receipts.py"), "--help"],
                            cwd=root, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--state-dir" in result.stdout


def _archive(feed: dict) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("board/alerts_feed.json", json.dumps(feed))
    return content.getvalue()


def test_recovery_merges_only_recent_keys_and_preserves_existing_records(tmp_path):
    feed = {
        "meta": {"run_id": "20261008T190000Z-gh42-1", "git_sha": "a" * 12},
        "alerts": [
            {"alert_key": "sent|recent", "family": "wx", "game_id": "cfb:42", "sport": "cfb",
             "sent_at": "2026-10-08T19:30:00Z"},
            {"alert_key": "sent|old", "sent_at": "2026-10-07T19:00:00Z"},
            {"alert_key": "sent|future", "sent_at": "2026-10-08T20:10:00Z"},
            {"alert_key": "sent|already", "family": "edge", "sent_at": "2026-10-08T19:40:00Z"},
        ],
    }
    routes = httpx.MockTransport(lambda request: _response(request, _archive(feed)))
    client = httpx.Client(transport=routes)
    existing = state.load_alerts(tmp_path)
    existing.setdefault("records", {})
    existing["sent"]["sent|already"] = "2026-10-08T18:00:00Z"
    existing["records"]["sent|already"] = {"alert_key": "sent|already", "status": "closed", "sends": 9}
    state.save_alerts(tmp_path, existing)

    added = recover_alert_receipts(tmp_path, "owner/repo", "token", current_run_id="77",
                                   now=NOW, client=client)
    recovered = state.load_alerts(tmp_path)
    assert added == 1
    assert set(recovered["sent"]) == {"sent|already", "sent|recent"}
    assert recovered["sent"]["sent|already"] == "2026-10-08T18:00:00Z"
    assert recovered["records"]["sent|already"] == {"alert_key": "sent|already", "status": "closed", "sends": 9}
    assert recovered["records"]["sent|recent"]["last_sent_at"] == "2026-10-08T19:30:00Z"
    client.close()


def _response(request: httpx.Request, archive: bytes) -> httpx.Response:
    if request.url.path.endswith("/actions/workflows/pipeline.yml/runs"):
        return httpx.Response(200, json={"workflow_runs": [
            {"id": 42, "run_attempt": 1, "head_branch": "main", "status": "completed", "conclusion": "failure",
             "created_at": "2026-10-08T19:00:00Z", "head_sha": "a" * 40},
            {"id": 43, "run_attempt": 1, "head_branch": "feature", "status": "completed", "conclusion": "failure",
             "created_at": "2026-10-08T19:00:00Z", "head_sha": "b" * 40},
        ]})
    if request.url.path.endswith("/actions/runs/42/artifacts"):
        return httpx.Response(200, json={"artifacts": [
            {"id": 7, "name": "raw-runs-42", "size_in_bytes": len(archive), "expired": False},
        ]})
    if request.url.path == "/repos/owner/repo/actions/artifacts/7/zip":
        return httpx.Response(200, content=archive)
    raise AssertionError(f"unexpected GitHub API request: {request.url}")


def test_recovery_fails_closed_when_candidate_artifact_download_fails(tmp_path):
    def response(request):
        if request.url.path.endswith("/actions/workflows/pipeline.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 42, "run_attempt": 1, "head_branch": "main", "status": "completed", "conclusion": "failure",
                 "created_at": "2026-10-08T19:00:00Z", "head_sha": "a" * 40},
            ]})
        if request.url.path.endswith("/actions/runs/42/artifacts"):
            return httpx.Response(200, json={"artifacts": [
                {"id": 7, "name": "raw-runs-42", "size_in_bytes": 20, "expired": False},
            ]})
        if request.url.path == "/repos/owner/repo/actions/artifacts/7/zip":
            return httpx.Response(503)
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            recover_alert_receipts(tmp_path, "owner/repo", "token", now=NOW, client=client)
    finally:
        client.close()
