"""Shared helpers for walking the git history of the legacy output files.

Streams `git log --raw` once per path to get (commit, date, blob) triples,
dedupes on blob hash, and materialises each distinct blob into a scratch
directory via `git cat-file --batch` so callers only parse unique content.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _scratch_root() -> Path:
    env = os.environ.get("CLAUDE_SCRATCHPAD") or os.environ.get("FW_SCRATCH")
    base = Path(env) if env else Path(tempfile.gettempdir()) / "football_weather_recover"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass(frozen=True)
class Snapshot:
    sha: str
    date: datetime
    blob: str
    path: str


def git(*args: str, binary: bool = False) -> bytes | str:
    res = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, check=True,
    )
    return res.stdout if binary else res.stdout.decode("utf-8", "replace")


def ref_exists(ref: str) -> bool:
    res = subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], cwd=REPO, capture_output=True)
    return res.returncode == 0


def path_exists_at(ref: str, path: str) -> bool:
    res = subprocess.run(["git", "cat-file", "-e", f"{ref}:{path}"], cwd=REPO, capture_output=True)
    return res.returncode == 0


def history(path: str) -> list[Snapshot]:
    """All commits touching `path`, oldest first, with the blob hash after each commit."""
    out = str(git("log", "--format=%H %aI", "--raw", "--no-abbrev", "--", path))
    snaps: list[Snapshot] = []
    sha = ""
    date = datetime.min
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith(":"):
            parts = line.split("\t")
            meta = parts[0].split()
            new_blob = meta[3]
            if new_blob.strip("0") == "":
                continue  # deletion
            snaps.append(Snapshot(sha=sha, date=date, blob=new_blob, path=path))
        else:
            sha, iso = line.split(" ", 1)
            date = datetime.fromisoformat(iso.strip())
    snaps.reverse()
    return snaps


def materialise(snaps: list[Snapshot], suffix: str) -> dict[str, Path]:
    """Write every distinct blob once into scratch; return blob -> file path."""
    root = _scratch_root() / "blobs"
    root.mkdir(parents=True, exist_ok=True)
    wanted = {s.blob for s in snaps}
    paths: dict[str, Path] = {}
    missing: list[str] = []
    for blob in sorted(wanted):
        p = root / f"{blob}{suffix}"
        paths[blob] = p
        if not p.exists():
            missing.append(blob)
    if missing:
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=REPO,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        for blob in missing:
            # one request at a time: writing all requests first deadlocks on the stdout pipe
            proc.stdin.write((blob + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().split()
            if len(header) < 3 or header[1] == "missing":
                raise RuntimeError(f"blob {blob} missing")
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
            paths[blob].write_bytes(data)
        proc.stdin.close()
        proc.wait()
    return paths


def iter_snapshots(path: str, suffix: str) -> Iterator[tuple[Snapshot, Path, bool]]:
    """Yield (snapshot, blob_file, is_first_occurrence_of_blob) oldest first."""
    snaps = history(path)
    paths = materialise(snaps, suffix)
    seen: set[str] = set()
    for s in snaps:
        first = s.blob not in seen
        seen.add(s.blob)
        yield s, paths[s.blob], first


def content_hash(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()
