"""Default-off retirement cannot send, mutate delivery state or trigger SMTP."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pipeline import alerts, published_alerts, state
from utils import telegram


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "1 ", "invalid"])
def test_master_switch_is_fail_closed(value):
    env = {} if value is None else {"FOOTBALL_TELEGRAM_ENABLED": value}
    assert not telegram.football_telegram_enabled(env)


def test_explicit_rollback_opt_in():
    assert telegram.football_telegram_enabled({"FOOTBALL_TELEGRAM_ENABLED": "1"})


def test_retired_transport_stops_before_http_even_without_pytest_guard(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.delenv("FOOTBALL_TELEGRAM_ENABLED", raising=False)
    assert not asyncio.run(telegram.send_message("fixture", bot_token="fixture", chat_id="fixture"))


def test_rollback_still_respects_legacy_disabled_guard(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FOOTBALL_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    assert not asyncio.run(telegram.send_message("fixture", bot_token="fixture", chat_id="fixture"))


def test_requested_rollback_can_use_fake_transport(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setenv("FOOTBALL_TELEGRAM_ENABLED", "1")
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            calls.append((url, json))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(telegram, "_client", FakeClient)
    assert asyncio.run(telegram.send_message("fixture", bot_token="fixture", chat_id="fixture"))
    assert len(calls) == 1 and calls[0][1]["text"] == "fixture"


def test_retired_published_sender_does_not_read_or_write(monkeypatch, tmp_path):
    monkeypatch.delenv("FOOTBALL_TELEGRAM_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("retired delivery must not use remote storage, send or checkpoint")

    assert published_alerts.notify_published(tmp_path / "missing-board", tmp_path / "state", "fixture", "fixture",
                                            verifier=forbidden, uploader=forbidden, archiver=forbidden,
                                            sender=forbidden) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("args", [["--digest", "postmortem", "--email-fallback"], ["--digest"], ["--flush"]])
def test_retired_cli_does_not_send_or_trigger_email_fallback(monkeypatch, args):
    monkeypatch.delenv("FOOTBALL_TELEGRAM_ENABLED", raising=False)
    monkeypatch.setattr(alerts, "default_sender", lambda: pytest.fail("sender must not be created"))
    monkeypatch.setattr(alerts, "send_postmortem_email", lambda *a, **kw: pytest.fail("no retirement-triggered SMTP"))
    monkeypatch.setattr(state, "load_alerts_rehydrated", lambda *a, **kw: pytest.fail("delivery state must not load"))
    assert alerts.main(args) == 0


def test_retired_planner_preserves_delivery_state_and_skips_liquidity(monkeypatch, tmp_path):
    monkeypatch.delenv("FOOTBALL_TELEGRAM_ENABLED", raising=False)
    state.save_alerts(tmp_path, state.migrate(None, "alerts"))
    state.save_telegram_state(tmp_path, state.migrate(None, "telegram_state"))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(alerts, "enrich_liquidity", lambda *a: pytest.fail("retirement must not request depth"))
    result = alerts.run_alerts(None, {"nfl": [], "cfb": []}, tmp_path,
                              sender=lambda *a: pytest.fail("retired planner must not send"))
    assert result.outcome.n_messages == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_every_workflow_telegram_http_sender_has_master_gate():
    directory = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for path in directory.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                if "api.telegram.org" in step.get("run", ""):
                    condition = str(step.get("if", "")) + str(job.get("if", ""))
                    assert "vars.FOOTBALL_TELEGRAM_ENABLED == '1'" in condition, path.name


def test_pipeline_and_weekly_delivery_steps_are_default_off():
    root = Path(__file__).resolve().parents[1]
    for name in ("pipeline.yml", "backtest.yml"):
        wf = yaml.safe_load((root / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        assert wf["env"]["FOOTBALL_TELEGRAM_ENABLED"] == "${{ vars.FOOTBALL_TELEGRAM_ENABLED || '0' }}"
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                if "pipeline.published_alerts" in step.get("run", "") or "--digest postmortem" in step.get("run", ""):
                    assert "env.FOOTBALL_TELEGRAM_ENABLED == '1'" in step["if"]
