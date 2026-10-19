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

from pipeline.outputs import r2

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
    if not failure_alerts_enabled(os.environ.get("FOOTBALL_FAILURE_ALERTS")):
        return {"action": "none", "state": closed_state(), "message": "", "persist": False}
    try:
        raw = read_health_bytes()
    except Exception as exc:  # noqa: BLE001 - a storage outage must not hide a first page
        print(f"::warning::pipeline health state unreadable ({type(exc).__name__}); a failure will page")
        previous, unreadable = closed_state(), True
    else:
        previous, unreadable = interpret_health(raw)
    return decide(
        previous, outcome, unreadable=unreadable,
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        run_url=os.environ.get("RUN_URL", ""),
        sport=os.environ.get("SPORT", ""),
        trigger=os.environ.get("GITHUB_EVENT_NAME", ""),
        jobs=jobs,
    )


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
