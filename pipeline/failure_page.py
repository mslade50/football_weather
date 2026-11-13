"""One Telegram page per ongoing pipeline failure, and one when it recovers.

Pick alerts stay behind ``FOOTBALL_TELEGRAM_ENABLED`` (default off). This page
does not. ``FOOTBALL_FAILURE_ALERTS=0`` is the only value that silences it;
a missing value stays on.

The incident marker is ``board/pipeline_health.json``. It is not part of the
board state round-trip, and ``pipeline_health`` is in ``BUILD_PROTECTED_NAMES``
so a publish cannot replace it. The marker is written only after Telegram
accepts the page. A repeat failure while the marker is open does not page.
A later run whose main build succeeded pages once and clears the marker.
Cancelled runs and safe refreshes leave the marker alone.

The same object holds an independent ``heartbeat`` incident and its last
observation. Unknown-window observations can be saved without sending; the
incident's ``open`` receipt changes only after a successful send. This entry
point runs even when the build gate skips, including GitHub schedule backstops.

A gate skip (light did not run) is not a recovery: the board was not rebuilt.
A BetOnline scrape or publish failure pages only when ``TELEGRAM_SYSTEM_ALERTS``
is exactly ``1``, matching the previous step condition.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pipeline import heartbeat
from pipeline.outputs import r2
from utils.timeutil import now_utc

HEALTH_KEY = "board/pipeline_health.json"
SCHEMA_VERSION = 1
_JOBS = ("gate", "light", "playwright_scrape", "playwright")


def failure_alerts_enabled(value: str | None) -> bool:
    """On unless the variable is exactly ``0`` (surrounding space ignored)."""
    if value is None:
        return True
    return value.strip() != "0"


def closed_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "open": False}


def normalize_state(raw: Any) -> dict[str, Any]:
    """A missing or foreign marker is a closed incident, not an error."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return closed_state()
    state: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "open": raw.get("open") is True}
    if isinstance(raw.get("heartbeat"), dict):
        state["heartbeat"] = dict(raw["heartbeat"])
    if not state["open"]:
        return state
    jobs = raw.get("jobs") if isinstance(raw.get("jobs"), dict) else {}
    state.update({
        "run_id": str(raw.get("run_id") or ""),
        "run_url": str(raw.get("run_url") or ""),
        "sport": str(raw.get("sport") or ""),
        "trigger": str(raw.get("trigger") or ""),
        "jobs": {name: str(jobs.get(name) or "") for name in _JOBS},
    })
    return state


def run_outcome(jobs: Mapping[str, str], *, cancelled: bool, safe_refresh: bool,
                system_alerts: bool) -> str:
    """``failure`` pages, ``success`` can recover, ``quiet`` changes nothing."""
    if cancelled or safe_refresh:
        return "quiet"
    gate = jobs.get("gate") or ""
    light = jobs.get("light") or ""
    if gate == "failure" or light == "failure":
        return "failure"
    if system_alerts and (
        jobs.get("playwright") == "failure" or jobs.get("playwright_scrape") == "failure"
    ):
        return "failure"
    if light == "success":
        return "success"
    return "quiet"


def _jobs_line(jobs: Mapping[str, str]) -> str:
    return " · ".join((
        f"gate={jobs.get('gate') or ''}",
        f"light={jobs.get('light') or ''}",
        f"BetOnline scrape={jobs.get('playwright_scrape') or ''}",
        f"BetOnline publish={jobs.get('playwright') or ''}",
    ))


def message_for(action: str, *, jobs: Mapping[str, str], run_url: str, sport: str,
                trigger: str, opened_url: str = "") -> str:
    if action == "failure":
        headline = "🚨 SYSTEM · Pipeline failed"
        tail = "This incident will not page again until the pipeline recovers."
    elif action == "recovery":
        headline = "✅ SYSTEM · Pipeline recovered"
        tail = f"Incident opened by: {opened_url}"
    else:
        return ""
    return "\n".join((
        headline,
        f"Jobs: {_jobs_line(jobs)}",
        f"Trigger: {trigger} · Sport: {sport}",
        f"Open run: {run_url}",
        tail,
    ))


def decide(previous: Any, outcome: str, *, run_id: str, run_url: str, sport: str,
           trigger: str, jobs: Mapping[str, str], unreadable: bool = False) -> dict[str, Any]:
    """Return ``action``, the state to store after a successful send, and ``message``.

    ``action`` is ``failure``, ``recovery``, or ``none``. ``persist`` is true only
    when a page should be recorded after Telegram accepts it.
    """
    if unreadable and outcome != "failure":
        return {"action": "none", "state": closed_state(), "message": "", "persist": False}
    state = closed_state() if unreadable else normalize_state(previous)
    if outcome == "quiet" or outcome not in ("failure", "success"):
        return {"action": "none", "state": state, "message": "", "persist": False}
    if outcome == "failure":
        if state.get("open"):
            return {"action": "none", "state": state, "message": "", "persist": False}
        opened = {
            "schema_version": SCHEMA_VERSION,
            "open": True,
            "run_id": run_id,
            "run_url": run_url,
            "sport": sport,
            "trigger": trigger,
            "jobs": {name: str(jobs.get(name) or "") for name in _JOBS},
        }
        return {
            "action": "failure",
            "state": opened,
            "message": message_for("failure", jobs=jobs, run_url=run_url, sport=sport, trigger=trigger),
            "persist": True,
        }
    if not state.get("open"):
        return {"action": "none", "state": state, "message": "", "persist": False}
    return {
        "action": "recovery",
        "state": closed_state(),
        "message": message_for(
            "recovery", jobs=jobs, run_url=run_url, sport=sport, trigger=trigger,
            opened_url=str(state.get("run_url") or ""),
        ),
        "persist": True,
    }


def interpret_health(raw: bytes | None) -> tuple[dict[str, Any], bool]:
    """``(state, unreadable)``. Invalid JSON is a closed marker, not unreadable."""
    if raw is None:
        return closed_state(), False
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("::warning::pipeline health state was not valid JSON; treating the incident as closed")
        return closed_state(), False
    return normalize_state(parsed), False


def read_health_bytes() -> bytes | None:
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError("R2 is not configured")
    return r2.get_object(r2.make_client(cfg, bounded=True), cfg.bucket, HEALTH_KEY)


def read_heartbeat_bytes() -> bytes | None:
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError("R2 is not configured")
    return r2.get_object(r2.make_client(cfg, bounded=True), cfg.bucket, "board/cf_heartbeat.json")


def with_heartbeat(decision: dict[str, Any], previous: dict[str, Any],
                   health: dict[str, Any], *, unreadable: bool = False) -> dict[str, Any]:
    """Combine independent pipeline/cron incidents in the existing delivery gate.

    When the marker could not be read, ``previous`` is a made-up closed state.
    Persisting anything derived from it would overwrite a possibly open incident
    (losing its recovery page), and paging without a persisted receipt would
    repeat on every run while reads keep failing. So the heartbeat is NOT paged
    and nothing is attached while the marker is unreadable; a stale heartbeat is
    paged on the first run whose marker reads fine.
    """
    if unreadable:
        return decision
    old = previous.get("heartbeat") or {}
    opened = old.get("open") is True
    action = "none"
    if health["status"] == "stale" and not opened:
        action = "failure"
        text = ("SYSTEM · Refresh stale · CF cron heartbeat\n"
                f"{health['reason']} (> {health['threshold_hours']} h)\n"
                "This incident will not page again until the Worker heartbeat recovers.")
    elif health["status"] == "fresh" and opened:
        action = "recovery"
        text = f"SYSTEM · CF cron heartbeat recovered\n{health['reason']}"
    observation = {**health, "open": opened}
    decision["state"]["heartbeat"] = observation
    if action != "none":
        decision["message"] = "\n\n".join(filter(None, (decision["message"], text)))
        if decision["action"] == "none":
            decision["action"] = action
        decision["persist"] = True
        decision["state"]["heartbeat"]["open"] = action == "failure"
    return decision


def write_health(state: Mapping[str, Any]) -> None:
    cfg = r2.config_from_env()
    if cfg is None:
        raise RuntimeError("R2 is not configured")
    data = json.dumps(dict(state), sort_keys=True, allow_nan=False).encode("utf-8")
    r2.put_bytes(r2.make_client(cfg, bounded=True), cfg.bucket, HEALTH_KEY, data, "application/json")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def plan_from_env() -> dict[str, Any]:
    jobs = {name: os.environ.get(f"{name.upper()}_RESULT", "") for name in _JOBS}
    outcome = run_outcome(
        jobs,
        cancelled=_env_flag("PIPELINE_CANCELLED"),
        safe_refresh=_env_flag("SAFE_REFRESH"),
        system_alerts=os.environ.get("TELEGRAM_SYSTEM_ALERTS", "").strip() == "1",
    )
    if (not failure_alerts_enabled(os.environ.get("FOOTBALL_FAILURE_ALERTS"))
            or _env_flag("TELEGRAM_DISABLED") or _env_flag("SAFE_REFRESH") or _env_flag("PIPELINE_CANCELLED")):
        return {"action": "none", "state": closed_state(), "message": "", "persist": False}
    try:
        raw = read_health_bytes()
    except Exception as exc:  # noqa: BLE001 - a storage outage must not hide a first page
        print(f"::warning::pipeline health state unreadable ({type(exc).__name__}); a failure will page")
        previous, unreadable = closed_state(), True
    else:
        previous, unreadable = interpret_health(raw)
    decision = decide(
        previous, outcome, unreadable=unreadable,
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        run_url=os.environ.get("RUN_URL", ""),
        sport=os.environ.get("SPORT", ""),
        trigger=os.environ.get("GITHUB_EVENT_NAME", ""),
        jobs=jobs,
    )
    try:
        raw_tick = read_heartbeat_bytes()
    except Exception:  # noqa: BLE001 - storage errors are unknown, never a missing tick
        tick, readable = None, False
    else:
        readable = True
        try:
            tick = json.loads(raw_tick) if raw_tick is not None else None
        except (ValueError, UnicodeDecodeError):
            tick = None
    old_heartbeat = previous.get("heartbeat") or {}
    health = heartbeat.assess(tick, now_utc(), old_heartbeat, readable=readable)
    # Save observations separately from receipts. Never acknowledge a proposed
    # page here, or overwrite an unreadable marker with observational state.
    observed = {**previous, "heartbeat": {**health, "open": old_heartbeat.get("open") is True}}
    # Only the unknown-observation window needs to survive between runs; a
    # normal fresh tick writes nothing.
    if not unreadable and health.get("unknown_since") != old_heartbeat.get("unknown_since"):
        try:
            write_health(observed)
        except Exception:  # noqa: BLE001 - a first stale page can still be delivered
            print("::warning::heartbeat observation was not saved")
    decision = with_heartbeat(decision, previous, health, unreadable=unreadable)
    if decision["action"] != "none" and "Open run:" not in decision["message"]:
        decision["message"] += f"\nOpen run: {os.environ.get('RUN_URL', '')}"
    return decision


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan", help="decide the page; do not send or store it")
    plan.add_argument("--action-out", type=Path, required=True)
    plan.add_argument("--message-out", type=Path, required=True)
    plan.add_argument("--state-out", type=Path, required=True)
    commit = sub.add_parser("commit", help="store the marker after Telegram accepts the page")
    commit.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "commit":
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
        write_health(state)
        print(f"pipeline health stored open={str(bool(state.get('open'))).lower()}")
        return 0
    decision = plan_from_env()
    _write_text(args.action_out, decision["action"] + "\n")
    _write_text(args.message_out, decision["message"])
    if decision["persist"]:
        _write_text(args.state_out, json.dumps(decision["state"], sort_keys=True, allow_nan=False))
    print(f"failure-page action={decision['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
