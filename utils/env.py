"""Load the repo-root ``.env`` for local runs (no-op in CI where secrets arrive as env)."""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def dotenv_blocked() -> bool:
    """Never load ``.env`` inside pytest or when explicitly disabled: a loaded bot token let
    the alert tests send real Telegram messages once (2026-08-24)."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("FOOTBALL_WEATHER_NO_DOTENV") == "1"


def load_repo_dotenv(path: Path | None = None) -> bool:
    """``python-dotenv`` if installed, ``override=False`` so real env / CI secrets win.
    Returns True when a file was loaded."""
    if dotenv_blocked():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return False
    p = path or (_ROOT / ".env")
    return bool(p.is_file() and load_dotenv(p, override=False))
