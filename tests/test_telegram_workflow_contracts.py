"""Failure-notification contracts shared by the GitHub workflows."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "pipeline.yml",
    "backtest.yml",
    "calibrate.yml",
    "build-stadiums.yml",
    "stadium-climate.yml",
    "deploy.yml",
)


def test_failure_pings_are_clear_encoded_guarded_and_do_not_log_telegram_payloads():
    for name in WORKFLOWS:
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert '--data-urlencode text="$MSG"' in text, name
        assert 'TELEGRAM_BOT_TOKEN" ]' in text and 'TELEGRAM_CHAT_ID" ]' in text, name
        assert ">/dev/null" in text, name
        if name == "pipeline.yml":
            # A rejected publish pages even when the system-alert switch is off.
            # The job itself must be unconditional so a skipped sibling cannot hide it.
            # Playwright/BetOnline failures are decided in pipeline.failure_page.
            module = (ROOT / "pipeline" / "failure_page.py").read_text(encoding="utf-8")
            assert "SYSTEM · Pipeline failed" in module
            assert "SYSTEM · Pipeline recovered" in module
            assert "\n    if: always()\n" in text
            assert "python -m pipeline.failure_page plan" in text
            assert "TELEGRAM_SYSTEM_ALERTS: ${{ vars.TELEGRAM_SYSTEM_ALERTS || '0' }}" in text
            assert "env.FOOTBALL_FAILURE_ALERTS != '0'" in text
            assert "env.FOOTBALL_TELEGRAM_ENABLED == '1'" in text, name
        else:
            assert "SYSTEM ·" in text, name
            assert "vars.TELEGRAM_SYSTEM_ALERTS == '1'" in text, name
            assert "(vars.FOOTBALL_FAILURE_ALERTS || '1') != '0'" in text, name
            assert "FOOTBALL_TELEGRAM_ENABLED" not in text.split("Telegram on failure", 1)[-1], name
            assert ">/dev/null || true" in text, name


def test_pipeline_has_only_one_failure_page_for_the_whole_run():
    text = (ROOT / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
    assert text.count("api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage") == 1
    assert "needs: [gate, light, playwright_scrape, playwright]" in text
    job = text[text.index("\n  notify_failure:\n"):]
    assert "PLAYWRIGHT_RESULT: ${{ needs.playwright.result }}" in job
    assert "LIGHT_RESULT: ${{ needs.light.result }}" in job
    assert "GATE_RESULT: ${{ needs.gate.result }}" in job
    assert "needs.light.result == 'failure' && vars.TELEGRAM_SYSTEM_ALERTS" not in job
    assert "FOOTBALL_TELEGRAM_ENABLED" not in job
