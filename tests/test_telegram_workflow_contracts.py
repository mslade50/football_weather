"""Failure-notification contracts shared by the GitHub workflows."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "pipeline.yml",
    "backtest.yml",
    "calibrate.yml",
    "build-stadiums.yml",
    "deploy.yml",
)


def test_failure_pings_are_clear_encoded_guarded_and_do_not_log_telegram_payloads():
    for name in WORKFLOWS:
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "SYSTEM ·" in text, name
        assert '--data-urlencode text="$MSG"' in text, name
        assert 'TELEGRAM_BOT_TOKEN" ]' in text and 'TELEGRAM_CHAT_ID" ]' in text, name
        assert ">/dev/null || true" in text, name
