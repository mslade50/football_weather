"""Shared test setup.

Heavy/optional runtime deps are stubbed with MagicMock before any pipeline
module is imported (same convention as golf_scraping/tests/test_betcris.py),
so the suite runs on CI without Playwright/curl_cffi/boto3 installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"

_STUBBED = (
    "curl_cffi",
    "curl_cffi.requests",
    "playwright",
    "playwright.async_api",
    "playwright_stealth",
    "boto3",
    "botocore",
    "botocore.config",
    "streamlit",
    "plotly",
    "plotly.express",
    "timezonefinder",
    "shapely",
    "shapely.geometry",
    "rapidfuzz",
    "rapidfuzz.fuzz",
    "rapidfuzz.process",
)
for _name in _STUBBED:
    sys.modules.setdefault(_name, MagicMock())

# Tests must never reach Telegram or load the repo .env. Set at import time (before any
# pipeline module runs) and enforced again inside utils.telegram / utils.env.
for _k in [k for k in os.environ if k.startswith("TELEGRAM_")]:
    os.environ.pop(_k, None)
os.environ["TELEGRAM_DISABLED"] = "1"
os.environ["FOOTBALL_WEATHER_NO_DOTENV"] = "1"


@pytest.fixture(autouse=True)
def _no_real_sends(monkeypatch):
    """Belt and braces: any test that still reaches the HTTP layer of the sender fails loudly."""
    import utils.telegram as _tg

    def _forbidden(*a, **k):
        raise AssertionError("utils.telegram tried to open an HTTP client inside pytest")

    monkeypatch.setattr(_tg, "_client", _forbidden)
    yield


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT
