"""Post-publication paper sidecar. Only writes paper/* and board/paper*.json.

Run under the existing football-refresh concurrency group. Captures are written
before the checkpoint; the report and health receipt follow the checkpoint.
NoSuchKey can initialize once, but auth/network/corruption never means empty.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import paper  # noqa: E402
from pipeline.outputs.json_out import dump_json  # noqa: E402
from scripts import fetch_r2, publish_r2  # noqa: E402

STATE_KEY = "paper/ifs-first-observed-v1/checkpoint.json"
REPORT_KEY = "board/paper.json"
HEALTH_KEY = "board/paper_health.json"


def read_remote(bucket: str, key: str) -> dict | None:
    cfg = publish_r2.s3_config()
    data = (fetch_r2.fetch_bytes_boto3(publish_r2.get_client(cfg), bucket, key) if cfg
            else fetch_r2.fetch_bytes_wrangler(bucket, key))
    if data is None:
        return None
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Paper object is not a JSON object")
    return value


def load_checkpoint(read) -> dict:
    state = read(STATE_KEY)
    if state is None:
        # A report with capture history proves this is not the initial rollout.
        if read(REPORT_KEY) is not None:
            raise ValueError("Paper checkpoint is absent but prior report exists; restore, do not reset")
        state = paper.empty()
    paper.validate(state)
    return state


def read_capture(snapshot_dir: Path, run_id: str) -> tuple[list[dict], datetime, list[str]]:
    cards, timestamps, requests = [], set(), set()
    for path in sorted(snapshot_dir.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.get("meta") or {}
        if meta.get("run_id") == run_id:
            if not isinstance(meta.get("books_requested"), list):
                raise ValueError("Snapshot lacks requested-book metadata; cannot freeze paper entry")
            requests.add(tuple(sorted(meta["books_requested"])))
            timestamps.add(meta.get("last_updated"))
            cards.extend(data["games"])
    if len(timestamps) != 1 or len(requests) != 1 or paper.stamp(next(iter(timestamps), None)) is None:
        raise ValueError("No unique timestamped snapshot for the published run")
    return cards, paper.stamp(next(iter(timestamps))), list(next(iter(requests)))


def update_scores(state: dict, backtest: dict, games: list[dict], now: datetime) -> dict:
    paper.validate(state)
    generated = paper.stamp(backtest.get("meta", {}).get("generated_at"))
    if (not isinstance(backtest.get("games"), list) or generated is None
            or not 0 <= (now - generated).total_seconds() <= 86400):
        raise ValueError("Invalid weekly backtest result input")
    for game in games:
        gid = game.get("game_id")
        if gid in state["games"] and paper.stamp(game.get("kickoff_utc")):
            new_kick = paper.iso(paper.stamp(game["kickoff_utc"]))
            if state["games"][gid]["kickoff_utc"] != new_kick:
                state["games"][gid]["rescheduled"] = True
            state["games"][gid]["kickoff_utc"] = new_kick
    for result in backtest["games"]:
        gid = result.get("game_id")
        home, away = paper.number(result.get("home_score")), paper.number(result.get("away_score"))
        if gid in state["games"] and result.get("src_result") and home is not None and away is not None:
            if min(home, away) < 0 or home % 1 or away % 1:
                raise ValueError("Invalid completed score")
            # These fields come from existing completed-score parsers, never a
            # model-derived total or a bet result calculated at another line.
            state["scores"][gid] = {"actual_total": home + away, "src_result": result["src_result"],
                                     "graded_at": paper.iso(now), "kickoff_utc": state["games"][gid]["kickoff_utc"]}
    state["last_grade"] = paper.iso(now)
    return state


def run(mode: str, *, read, put, snapshot_dir: Path, run_id: str, backtest: Path,
        export_dir: Path, output_dir: Path, now: datetime, previous_meta: dict | None = None) -> dict:
    state = load_checkpoint(read)
    event = None
    if mode == "capture":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Unsafe paper run ID")
        cards, observed, requested = read_capture(snapshot_dir, run_id)
        if observed > now or (now - observed).total_seconds() > 3600:
            raise ValueError("Capture snapshot is future-dated or older than one hour; do not backfill as live evidence")
        previous_run = (previous_meta or {}).get("run_id")
        if (state["started_at"] and previous_run and previous_run != run_id and previous_run not in state["runs"]
                and previous_run not in state["gaps"]):
            state["gaps"].append(previous_run)
        state, event = paper.capture(state, cards, run_id, observed, requested_books=requested)
        key = f"paper/{paper.POLICY['id']}/captures/{run_id}.json"
        prior = read(key)
        if prior is not None and prior != event:
            raise ValueError("Immutable paper capture differs; refusing overwrite")
        if prior is None:
            put(key, dump_json(output_dir / "capture.json", event))
    else:
        from pipeline.backtest import load_export_dir
        state = update_scores(state, json.loads(backtest.read_text(encoding="utf-8")),
                              load_export_dir(export_dir).games, now)
    result = paper.report(state, now)
    if event:
        state["last_capture_counts"] = dict(Counter(
            reason for o in event["observations"] for reason in o["reasons"]))
        state["last_capture_games"] = len(event["observations"])
        state["last_quote_rejections"] = dict(Counter(
            f"{book}:{reason}" for o in event["observations"] for book, reason in o["quote_rejections"].items()))
    result["last_capture_rejections"] = state.get("last_capture_counts", {})
    result["last_quote_rejections"] = state.get("last_quote_rejections", {})
    health = {"status": "ok", "updated_at": paper.iso(now), "mode": mode,
              "last_capture": state["last_capture"], "last_grade": state.get("last_grade"),
              "capture_runs": len(state["runs"]), "last_run_id": run_id if event else state.get("last_run_id"),
              "games_in_last_capture": state.get("last_capture_games", 0),
              "rejections": state.get("last_capture_counts", {}), "policy_hash": paper.POLICY_HASH}
    state["last_run_id"] = health["last_run_id"]
    # Sequential writes are intentional. An archive failure cannot advance the
    # checkpoint; report failure leaves the old scorecard and an error receipt.
    put(STATE_KEY, dump_json(output_dir / "checkpoint.json", state))
    put(REPORT_KEY, dump_json(output_dir / "paper.json", result))
    put(HEALTH_KEY, dump_json(output_dir / "paper_health.json", health))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("capture", "grade"))
    p.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "football-board"))
    p.add_argument("--run-id", default=os.environ.get("RUN_ID", ""))
    p.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    p.add_argument("--backtest", type=Path, default=Path("data/board/backtest.json"))
    p.add_argument("--export-dir", type=Path, default=Path("data/d1_export"))
    p.add_argument("--output-dir", type=Path, default=Path("data/paper"))
    p.add_argument("--previous-meta", type=Path, default=Path("data/state/prev_meta.json"))
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    def put(key, path):
        publish_r2.upload_one(args.bucket, key, path, "application/json")
    try:
        previous = json.loads(args.previous_meta.read_text(encoding="utf-8")) if args.previous_meta.is_file() else None
        result = run(args.mode, read=lambda key: read_remote(args.bucket, key), put=put,
                     snapshot_dir=args.snapshot_dir, run_id=args.run_id, backtest=args.backtest,
                     export_dir=args.export_dir, output_dir=args.output_dir, now=now, previous_meta=previous)
        print(f"Paper {args.mode}: {result['capture_runs']} captures, {result['games']} games; paper only")
        return 0
    except Exception as exc:  # noqa: BLE001 - failure is isolated from live board
        print(f"::warning::Paper {args.mode} failed ({type(exc).__name__}); live rules unchanged")
        if isinstance(exc, ValueError):
            print(f"::warning::{exc}")
        try:
            put(HEALTH_KEY, dump_json(args.output_dir / "paper_health.json", {
                "status": "error", "mode": args.mode, "updated_at": paper.iso(now),
                "reason": "Paper update failed; last successful scorecard retained. Inspect workflow logs."}))
        except Exception:  # noqa: BLE001
            print("::warning::Paper health receipt could not be published; timestamp will remain stale")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
