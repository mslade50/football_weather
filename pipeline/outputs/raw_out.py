"""Raw-first capture store (ARCH §1.3 / §5).

Every external fetch is written verbatim BEFORE parsing to
``<base>/{sport}/{run_id}/{source}.<ext>`` with a ``manifest.json`` alongside
(``{source: {sha256, bytes, fetched_at, url, path}}``).

Phase 1 base dir is local ``data/raw_runs/`` (gitignored); Phase 3 mirrors the
same layout under the R2 ``raw/`` prefix: set ``store.mirror`` to a callback
``(key, data, content_type)`` (``pipeline.outputs.r2.raw_mirror``) and every
capture + the manifest is uploaded to ``raw/{sport}/{run_id}/...`` as it is
written. A mirror failure is logged and never fails the run (the workflow's
wrangler put loop re-uploads the local dir anyway).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from pipeline.run_context import REPO_ROOT
from utils.timeutil import utc_iso

logger = logging.getLogger(__name__)

DEFAULT_BASE = REPO_ROOT / "data" / "raw_runs"
MANIFEST = "manifest.json"
R2_RAW_PREFIX = "raw"

PathLike = Union[str, Path]
Mirror = Callable[[str, bytes, str], None]
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CONTENT_TYPES = {"json": "application/json", "txt": "text/plain", "bin": "application/octet-stream",
                  "html": "text/html", "csv": "text/csv", "xml": "application/xml"}


def safe_name(source: str) -> str:
    s = _SAFE.sub("_", source.strip()).strip("._")
    return s or "capture"


def _to_bytes(payload: Any) -> tuple[bytes, str]:
    """Return (bytes, extension) for str / bytes / JSON-able objects."""
    if isinstance(payload, bytes):
        return payload, "bin"
    if isinstance(payload, str):
        return payload.encode("utf-8"), "txt"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"), "json"


@dataclass
class RawStore:
    sport: str
    run_id: str
    base_dir: Path = field(default_factory=lambda: DEFAULT_BASE)
    enabled: bool = True
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    mirror: Optional[Mirror] = None
    mirror_errors: int = 0

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)

    @property
    def run_dir(self) -> Path:
        return self.base_dir / self.sport / self.run_id

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / MANIFEST

    def r2_key(self, name: str) -> str:
        return f"{R2_RAW_PREFIX}/{self.sport}/{self.run_id}/{name}"

    def _mirror(self, name: str, data: bytes, ext: str) -> None:
        if self.mirror is None:
            return
        try:
            self.mirror(self.r2_key(name), data, _CONTENT_TYPES.get(ext, "application/octet-stream"))
        except Exception as exc:  # noqa: BLE001 - raw mirror must never fail the run
            self.mirror_errors += 1
            logger.warning(f"raw mirror failed for {self.r2_key(name)}: {exc}")

    def put(self, source: str, payload: Any, url: str | None = None, ext: str | None = None) -> Path | None:
        """Write one capture; returns the path (None when disabled / dry-run)."""
        data, guessed = _to_bytes(payload)
        ext = ext or guessed
        name = f"{safe_name(source)}.{ext}"
        entry = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "fetched_at": utc_iso(),
            "url": url,
            "path": name,
        }
        self.manifest[source] = entry
        if not self.enabled:
            return None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / name
        path.write_bytes(data)
        self._write_manifest()
        self._mirror(name, data, ext)
        return path

    def _manifest_bytes(self) -> bytes:
        return json.dumps(self.manifest, indent=2, sort_keys=True).encode("utf-8")

    def _write_manifest(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_bytes(self._manifest_bytes())

    def finalize(self) -> Path | None:
        if not self.enabled:
            return None
        self._write_manifest()
        self._mirror(MANIFEST, self._manifest_bytes(), "json")
        return self.manifest_path

    def r2_files(self) -> dict[str, Path]:
        """``{r2 key: local path}`` for everything captured this run (workflow put loop)."""
        if not self.enabled or not self.run_dir.is_dir():
            return {}
        return {self.r2_key(p.name): p for p in self.run_dir.iterdir() if p.is_file()}


class NullRawStore(RawStore):
    """Drop-in for --dry-run: records the manifest in memory, writes nothing."""

    def __init__(self, sport: str = "all", run_id: str = "dry") -> None:
        super().__init__(sport=sport, run_id=run_id, enabled=False)


__all__ = ["DEFAULT_BASE", "MANIFEST", "R2_RAW_PREFIX", "Mirror", "RawStore", "NullRawStore", "safe_name"]
