"""Load the repo-root ``.env`` for local runs (no-op in CI where secrets arrive as env)."""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def load_repo_dotenv(path: Path | None = None) -> bool:
    """``python-dotenv`` if installed, ``override=False`` so real env / CI secrets win.
    Returns True when a file was loaded."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return False
    p = path or (_ROOT / ".env")
    return bool(p.is_file() and load_dotenv(p, override=False))
