"""Shared test setup.

Heavy/optional runtime deps are stubbed with MagicMock before any pipeline
module is imported (same convention as golf_scraping/tests/test_betcris.py),
so the suite runs on CI without Playwright/curl_cffi/boto3 installed.
"""

from __future__ import annotations

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


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT
