"""Cron incidents run through the existing workflow failure-page delivery gate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import failure_page as pages
from pipeline.heartbeat import assess
from pipeline.outputs import json_out

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("hours,status", [(0, "fresh"), (19, "fresh"), (20, "fresh"), (20.001, "stale")])
def test_threshold(hours, status):
    verdict = assess({"ts": (NOW - timedelta(hours=hours)).isoformat()}, NOW)
    assert verdict["status"] == status
    assert verdict["threshold_hours"] == 20


@pytest.mark.parametrize("raw", [None, {}, {"ts": "broken"}, {"ts": 123}, [], {"ts": "99999-01-01"}])
def test_unknown_requires_a_persisted_full_window(raw):
    first = assess(raw, NOW)
    assert first["status"] == "unknown"
    assert assess(raw, NOW + timedelta(hours=20), first)["status"] == "unknown"
    assert assess(raw, NOW + timedelta(hours=20, seconds=1), first)["status"] == "stale"
    assert assess({"ts": (NOW + timedelta(hours=21)).isoformat()}, NOW + timedelta(hours=21), first)["unknown_since"] is None


def test_read_failure_breaks_missing_observation_window():
    first = assess(None, NOW)
    failed = assess(None, NOW + timedelta(days=1), first, readable=False)
    assert failed["status"] == "unknown"
    assert failed["unknown_since"] is None
    assert assess(None, NOW + timedelta(days=2), failed)["status"] == "unknown"


def test_future_clock_is_unknown():
    assert assess({"ts": (NOW + timedelta(hours=1)).isoformat()}, NOW)["status"] == "unknown"


@pytest.fixture
def runtime(monkeypatch):
    state = {"marker": None, "tick": {"ts": (NOW - timedelta(hours=21)).isoformat()}, "now": NOW}
    monkeypatch.setenv("TELEGRAM_DISABLED", "0")
    monkeypatch.setenv("FOOTBALL_TELEGRAM_ENABLED", "0")
    monkeypatch.setenv("TELEGRAM_SYSTEM_ALERTS", "0")
    monkeypatch.delenv("FOOTBALL_FAILURE_ALERTS", raising=False)
    monkeypatch.delenv("SAFE_REFRESH", raising=False)
    monkeypatch.delenv("PIPELINE_CANCELLED", raising=False)
    # The backstop can skip the entire build: the ops entry point still runs.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GATE_RESULT", "success")
    monkeypatch.setenv("LIGHT_RESULT", "skipped")
    monkeypatch.setattr(pages, "now_utc", lambda: state["now"])
    monkeypatch.setattr(pages, "read_health_bytes", lambda: json.dumps(state["marker"]).encode())
    monkeypatch.setattr(pages, "read_heartbeat_bytes", lambda: json.dumps(state["tick"]).encode())
    monkeypatch.setattr(pages, "write_health", lambda value: state.update(marker=json.loads(json.dumps(value))))
    return state


def test_stale_once_then_recovery_once_across_runs(runtime):
    first = pages.plan_from_env()
    assert first["action"] == "failure"
    assert "CF cron heartbeat" in first["message"]
    assert ((runtime["marker"] or {}).get("heartbeat") or {}).get("open") is not True
    pages.write_health(first["state"])  # existing workflow calls commit only on successful send
    for _ in range(3):
        runtime["now"] += timedelta(hours=1)
        assert pages.plan_from_env()["action"] == "none"
    runtime["tick"] = {"ts": runtime["now"].isoformat()}
    recovered = pages.plan_from_env()
    assert recovered["action"] == "recovery"
    assert runtime["marker"]["heartbeat"]["open"] is True
    pages.write_health(recovered["state"])
    assert pages.plan_from_env()["action"] == "none"


def test_failed_send_retries_failure_and_recovery(runtime, monkeypatch):
    from scripts.notify_failure import notify

    def fail(*args, **kwargs):
        raise OSError("simulated delivery failure")

    env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat"}
    page = pages.plan_from_env()
    assert notify(body=page["message"], strict=True, env=env, opener=fail) == 1
    assert ((runtime["marker"] or {}).get("heartbeat") or {}).get("open") is not True
    retry = pages.plan_from_env()
    assert retry["action"] == "failure"
    pages.write_health(retry["state"])
    runtime["tick"] = {"ts": NOW.isoformat()}
    recovery = pages.plan_from_env()
    assert notify(body=recovery["message"], strict=True, env=env, opener=fail) == 1
    assert runtime["marker"]["heartbeat"]["open"] is True
    assert pages.plan_from_env()["action"] == "recovery"


@pytest.mark.parametrize("name,value", [("FOOTBALL_FAILURE_ALERTS", "0"), ("TELEGRAM_DISABLED", "1"),
                                        ("SAFE_REFRESH", "true"), ("PIPELINE_CANCELLED", "1")])
def test_ops_kill_switches_leave_state_untouched(runtime, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(pages, "read_health_bytes", lambda: pytest.fail("quiet run read state"))
    assert pages.plan_from_env()["action"] == "none"
    assert runtime["marker"] is None


@pytest.mark.parametrize("tick", [None, {"ts": "invalid"}])
def test_missing_and_invalid_page_after_observed_window_and_do_not_recover(runtime, tick):
    runtime["tick"] = tick
    assert pages.plan_from_env()["action"] == "none"
    runtime["now"] += timedelta(hours=20)
    assert pages.plan_from_env()["action"] == "none"
    runtime["now"] += timedelta(seconds=1)
    first = pages.plan_from_env()
    assert first["action"] == "failure"
    pages.write_health(first["state"])
    assert pages.plan_from_env()["action"] == "none"
    runtime["tick"] = {"ts": runtime["now"].isoformat()}
    assert pages.plan_from_env()["action"] == "recovery"


def test_pipeline_and_heartbeat_incidents_are_independent(runtime, monkeypatch):
    monkeypatch.setenv("LIGHT_RESULT", "failure")
    first = pages.plan_from_env()
    assert "Pipeline failed" in first["message"] and "CF cron heartbeat" in first["message"]
    pages.write_health(first["state"])
    monkeypatch.setenv("LIGHT_RESULT", "success")
    recovered = pages.plan_from_env()
    assert "Pipeline recovered" in recovered["message"]
    assert "heartbeat recovered" not in recovered["message"]
    pages.write_health(recovered["state"])
    assert pages.plan_from_env()["action"] == "none"
    runtime["tick"] = {"ts": NOW.isoformat()}
    assert pages.plan_from_env()["action"] == "recovery"


def test_publication_uses_the_same_verdict_and_preserves_it_in_status(tmp_path):
    tick = {"ts": (NOW - timedelta(hours=21)).isoformat()}
    (tmp_path / "cf_heartbeat.json").write_text(json.dumps(tick), encoding="utf-8")
    health = json_out.heartbeat_health(tmp_path, NOW)
    assert health == assess(tick, NOW)
    meta = {"heartbeat_health": health}
    status = json_out.build_status(meta, {"run_id": "r1"})
    assert status["heartbeat_health"] == health
    assert status["meta"]["heartbeat_health"] == health
