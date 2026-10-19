"""One page when a pipeline failure starts, none while it continues, one on recovery."""

from __future__ import annotations

import json

import pytest

from pipeline.failure_page import (
    closed_state,
    decide,
    failure_alerts_enabled,
    interpret_health,
    main,
    run_outcome,
)
from pipeline.publication import BUILD_PROTECTED_NAMES

OCT17_JOBS = {
    "gate": "success",
    "light": "failure",
    "playwright_scrape": "skipped",
    "playwright": "skipped",
}


def _page(state, outcome, jobs, **kwargs):
    return decide(
        state, outcome, jobs=jobs,
        run_id=kwargs.get("run_id", "1"),
        run_url=kwargs.get("run_url", "https://example/1"),
        sport=kwargs.get("sport", "cfb"),
        trigger=kwargs.get("trigger", "workflow_dispatch"),
        unreadable=kwargs.get("unreadable", False),
    )


def test_failure_switch_defaults_on_and_only_exact_zero_disables():
    assert failure_alerts_enabled(None) is True
    assert failure_alerts_enabled("") is True
    assert failure_alerts_enabled("1") is True
    assert failure_alerts_enabled(" 0 ") is False
    assert failure_alerts_enabled("0") is False
    assert failure_alerts_enabled("false") is True


def test_publish_cannot_replace_the_incident_marker():
    assert "pipeline_health" in BUILD_PROTECTED_NAMES


def test_seven_hourly_failures_page_once_and_recovery_pages_once():
    state = closed_state()
    sent = []
    for hour in range(7):
        page = _page(
            state, "failure", OCT17_JOBS,
            run_id=str(39043184946 + hour),
            run_url=f"https://github.com/example/actions/runs/{hour}",
        )
        if page["action"] != "none":
            sent.append(page)
            assert page["persist"] is True
            state = page["state"]
        else:
            assert page["persist"] is False
            assert page["state"]["run_url"] == sent[0]["state"]["run_url"]
    assert [item["action"] for item in sent] == ["failure"]
    assert "will not page again" in sent[0]["message"]
    assert "gate=success · light=failure · BetOnline scrape=skipped · BetOnline publish=skipped" in sent[0]["message"]
    assert state["open"] is True
    assert state["run_url"].endswith("/0")

    recovered = _page(
        state, "success",
        {"gate": "success", "light": "success", "playwright_scrape": "skipped", "playwright": "success"},
        run_id="8", run_url="https://github.com/example/actions/runs/8", sport="all", trigger="schedule",
    )
    assert recovered["action"] == "recovery"
    assert recovered["persist"] is True
    assert recovered["state"] == {"schema_version": 1, "open": False}
    assert "Pipeline recovered" in recovered["message"]
    assert "Incident opened by: https://github.com/example/actions/runs/0" in recovered["message"]
    assert "Open run: https://github.com/example/actions/runs/8" in recovered["message"]

    quiet = _page(recovered["state"], "success", {"gate": "success", "light": "success",
                                                   "playwright_scrape": "skipped", "playwright": "success"})
    assert quiet["action"] == "none"
    assert quiet["persist"] is False


def test_gate_skip_does_not_clear_an_open_incident():
    opened = _page(closed_state(), "failure", OCT17_JOBS)
    skipped = {
        "gate": "success", "light": "skipped",
        "playwright_scrape": "skipped", "playwright": "skipped",
    }
    assert run_outcome(skipped, cancelled=False, safe_refresh=False, system_alerts=False) == "quiet"
    page = _page(opened["state"], "quiet", skipped)
    assert page["action"] == "none"
    assert page["persist"] is False
    assert page["state"]["open"] is True
    assert page["state"]["run_url"] == opened["state"]["run_url"]


@pytest.mark.parametrize("cancelled,safe_refresh", [(True, False), (False, True), (True, True)])
def test_cancelled_and_safe_refresh_stay_quiet_even_when_light_failed(cancelled, safe_refresh):
    assert run_outcome(OCT17_JOBS, cancelled=cancelled, safe_refresh=safe_refresh, system_alerts=True) == "quiet"


def test_playwright_failure_pages_only_when_system_alerts_are_on():
    jobs = {"gate": "success", "light": "success", "playwright_scrape": "failure", "playwright": "failure"}
    assert run_outcome(jobs, cancelled=False, safe_refresh=False, system_alerts=False) == "success"
    assert run_outcome(jobs, cancelled=False, safe_refresh=False, system_alerts=True) == "failure"
    light_failed = dict(OCT17_JOBS)
    assert run_outcome(light_failed, cancelled=False, safe_refresh=False, system_alerts=False) == "failure"


def test_changing_failure_reason_does_not_open_a_second_incident():
    first = _page(closed_state(), "failure", OCT17_JOBS, run_url="https://example/first")
    other = {"gate": "failure", "light": "skipped", "playwright_scrape": "skipped", "playwright": "skipped"}
    second = _page(first["state"], "failure", other, run_url="https://example/second")
    assert second["action"] == "none"
    assert second["state"]["run_url"] == "https://example/first"


def test_unreadable_marker_pages_a_failure_and_does_not_invent_a_recovery():
    failed = _page(None, "failure", OCT17_JOBS, unreadable=True)
    assert failed["action"] == "failure"
    assert failed["persist"] is True
    healthy = {"gate": "success", "light": "success", "playwright_scrape": "success", "playwright": "success"}
    assert _page(None, "success", healthy, unreadable=True)["action"] == "none"


def test_invalid_marker_is_closed_rather_than_unreadable():
    state, unreadable = interpret_health(b"not-json")
    assert unreadable is False
    assert state == closed_state()
    missing, missing_bad = interpret_health(None)
    assert missing_bad is False
    assert missing == closed_state()
    foreign, _ = interpret_health(json.dumps({"schema_version": 99, "open": True}).encode())
    assert foreign == closed_state()


def test_plan_cli_records_nothing_until_commit(monkeypatch, tmp_path, capsys):
    stored = {}

    def reader():
        return None

    def writer(state):
        stored.update(state)

    monkeypatch.setattr("pipeline.failure_page.read_health_bytes", reader)
    monkeypatch.setattr("pipeline.failure_page.write_health", writer)
    monkeypatch.setenv("GATE_RESULT", "success")
    monkeypatch.setenv("LIGHT_RESULT", "failure")
    monkeypatch.setenv("PLAYWRIGHT_SCRAPE_RESULT", "skipped")
    monkeypatch.setenv("PLAYWRIGHT_RESULT", "skipped")
    monkeypatch.setenv("TELEGRAM_SYSTEM_ALERTS", "0")
    monkeypatch.setenv("FOOTBALL_FAILURE_ALERTS", "1")
    monkeypatch.setenv("SPORT", "cfb")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_RUN_ID", "39043184946")
    monkeypatch.setenv("RUN_URL", "https://github.com/example/actions/runs/39043184946")
    action = tmp_path / "action"
    message = tmp_path / "message"
    state = tmp_path / "state.json"
    assert main(["plan", "--action-out", str(action), "--message-out", str(message), "--state-out", str(state)]) == 0
    assert action.read_text(encoding="utf-8") == "failure\n"
    assert "39043184946" in message.read_text(encoding="utf-8")
    assert stored == {}
    assert "bot" not in capsys.readouterr().out
    assert main(["commit", "--state-file", str(state)]) == 0
    assert stored["open"] is True
    assert stored["run_id"] == "39043184946"


def test_plan_cli_repeat_failure_does_not_ask_for_a_send(monkeypatch, tmp_path):
    marker = json.dumps({
        "schema_version": 1, "open": True, "run_id": "1", "run_url": "https://example/1",
        "sport": "cfb", "trigger": "workflow_dispatch", "jobs": OCT17_JOBS,
    }).encode()
    monkeypatch.setattr("pipeline.failure_page.read_health_bytes", lambda: marker)
    monkeypatch.setenv("GATE_RESULT", "success")
    monkeypatch.setenv("LIGHT_RESULT", "failure")
    monkeypatch.setenv("PLAYWRIGHT_SCRAPE_RESULT", "skipped")
    monkeypatch.setenv("PLAYWRIGHT_RESULT", "skipped")
    monkeypatch.delenv("FOOTBALL_FAILURE_ALERTS", raising=False)
    action = tmp_path / "action"
    assert main(["plan", "--action-out", str(action), "--message-out", str(tmp_path / "m"),
                 "--state-out", str(tmp_path / "s")]) == 0
    assert action.read_text(encoding="utf-8") == "none\n"
    assert not (tmp_path / "s").exists()


def test_plan_cli_switch_off_does_not_page(monkeypatch, tmp_path):
    monkeypatch.setattr("pipeline.failure_page.read_health_bytes", lambda: pytest.fail("switch off must not read state"))
    monkeypatch.setenv("FOOTBALL_FAILURE_ALERTS", "0")
    monkeypatch.setenv("LIGHT_RESULT", "failure")
    monkeypatch.setenv("GATE_RESULT", "success")
    action = tmp_path / "action"
    assert main(["plan", "--action-out", str(action), "--message-out", str(tmp_path / "m"),
                 "--state-out", str(tmp_path / "s")]) == 0
    assert action.read_text(encoding="utf-8") == "none\n"
