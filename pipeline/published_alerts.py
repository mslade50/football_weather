"""Notify only a verified published board; save delivery receipts independently."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pipeline import alerts, state
from pipeline.current_quotes import expire_card
from pipeline.outputs import d1_out, json_out
from pipeline.run_context import RunContext
from utils.telegram import football_telegram_enabled
from utils.timeutil import now_utc


def verify_published(bucket: str, run_id: str) -> None:
    with tempfile.TemporaryDirectory(prefix="football-published-") as directory:
        target = Path(directory) / "meta.json"
        subprocess.run([shutil.which("npx") or "npx", "--yes", "wrangler@4", "r2", "object", "get",
                        f"{bucket}/board/meta.json", f"--file={target}", "--remote"], check=True, timeout=90,
                       capture_output=True)
        if json.loads(target.read_text(encoding="utf-8")).get("run_id") != run_id:
            raise RuntimeError("Published run changed; refusing to send notifications for a different board")


def archive_receipts(path: Path) -> None:
    subprocess.run([shutil.which("npx") or "npx", "--yes", "wrangler@4", "d1", "execute",
                    os.environ.get("D1_DATABASE", "football-odds"), "--remote", "--yes", f"--file={path}"],
                   check=True, timeout=90, capture_output=True)


def notify_published(board_dir: Path, state_dir: Path, run_id: str, bucket: str, *,
                     verifier: Callable[[str, str], None] = verify_published,
                     uploader: Callable | None = None, archiver: Callable = archive_receipts,
                     sender: alerts.Sender | None = None, now: datetime | None = None) -> alerts.AlertsRun | None:
    # Retirement and safe_refresh must not modify delivery markers or queue state.
    if not football_telegram_enabled() or os.environ.get("TELEGRAM_DISABLED") == "1":
        return None
    meta = json.loads((board_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("run_id") != run_id:
        raise RuntimeError("Local board run does not match the requested notification run")
    verifier(bucket, run_id)  # No message or delivery-state write until this succeeds.
    if uploader is None:
        from scripts.publish_r2 import upload_one
        uploader = upload_one
    clock = now or now_utc()
    cards = {}
    for sport in ("nfl", "cfb"):
        path = board_dir / f"games_{sport}.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("meta", {}).get("run_id") != run_id:
            raise RuntimeError(f"{sport} cards belong to a different publication")
        cards[sport] = [expire_card(c, clock) for c in (payload if isinstance(payload, list) else payload["games"])]
    ctx = RunContext(sport="all", scope="published-alerts", run_id=run_id, git_sha=meta.get("git_sha"))

    def checkpoint(alert_state: dict, telegram_state: dict) -> None:
        state.save_alerts(state_dir, alert_state)
        state.save_telegram_state(state_dir, telegram_state)
        # The dedup receipt is first. If any write fails, stop before another send.
        uploader(bucket, "board/alerts.json", state_dir / "alerts.json", "application/json")
        uploader(bucket, "board/telegram_state.json", state_dir / "telegram_state.json", "application/json")
        feed_path = board_dir / "alerts_feed.json"
        json_out.dump_json(feed_path, json_out.build_alerts_feed(alert_state, meta))
        uploader(bucket, "board/alerts_feed.json", feed_path, "application/json")

    checkpoint(state.load_alerts(state_dir), state.load_telegram_state(state_dir))  # Preflight receipt storage.
    inputs_path = board_dir.parent / "alert_inputs.json"
    inputs = json.loads(inputs_path.read_text(encoding="utf-8")) if inputs_path.is_file() else {}
    if inputs and inputs.get("run_id") != run_id:
        raise RuntimeError("Alert inputs belong to a different build")
    # Keep production time live: liquidity retrieval may cross kickoff. Only a
    # caller-supplied test/replay clock should freeze run_alerts' time refresh.
    result = alerts.run_alerts(ctx, cards, state_dir, sender=sender, now=now,
                              new_keys_by_sport=inputs.get("new_keys_by_sport"), receipt_checkpoint=checkpoint)
    records = list(result.alerts.get("records", {}).values())
    # A previous attempt may have saved R2 receipts but failed the D1 write.
    # Re-upsert retained receipts, including those deduped on this attempt.
    statements = d1_out.alert_upsert_sql(d1_out.alert_rows(records))
    sent_keys = result.alerts.get("sent", {})
    n_alerts = sum(record.get("run_id") == run_id and record.get("alert_key") in sent_keys
                   for record in records)
    statements.append(f"UPDATE runs SET n_alerts = {n_alerts} WHERE run_id = {d1_out.d1_sql_value(run_id)};")
    sql_path = state_dir / "published_alerts.sql"
    d1_out.write_sql(sql_path, statements)
    archiver(sql_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    result = notify_published(args.board_dir, args.state_dir, args.run_id, args.bucket)
    return 1 if result is not None and result.outcome.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
