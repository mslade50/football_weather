"""Regression: on 2026-08-24 the alert tests sent ~70 real Telegram messages about the
fixture game because ``build.main()`` loaded ``.env`` (bot token) and the alert stage
used the real sender. Three independent guards now make that impossible."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import utils.telegram as tg
from utils import env as envmod


def test_pytest_marks_environment():
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert os.environ.get("TELEGRAM_DISABLED") == "1"
    assert os.environ.get("FOOTBALL_WEATHER_NO_DOTENV") == "1"
    assert not any(k.startswith("TELEGRAM_") and k != "TELEGRAM_DISABLED" for k in os.environ)


def test_dotenv_never_loads_under_pytest(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("TELEGRAM_BOT_TOKEN=should-not-load\n", encoding="utf-8")
    assert envmod.dotenv_blocked()
    assert envmod.load_repo_dotenv(f) is False
    assert "TELEGRAM_BOT_TOKEN" not in os.environ


def test_send_message_is_suppressed_even_with_explicit_token():
    # conftest replaces httpx.AsyncClient with a class that raises if instantiated;
    # the guard must return before ever getting there.
    ok = asyncio.run(tg.send_message("fixture message", bot_token="123:abc", chat_id="1"))
    assert ok is False


def test_cli_entry_points_share_the_guarded_loader():
    from pipeline import alerts, backtest, build, calibrate, gate_check

    for mod in (alerts, backtest, build, calibrate, gate_check):
        assert mod.load_repo_dotenv is envmod.load_repo_dotenv
    assert envmod.load_repo_dotenv() is False   # repo .env exists locally but must not load here
