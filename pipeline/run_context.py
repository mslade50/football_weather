"""Run identity, clocks and stage timers shared by every build stage."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.contracts import Degradation
from utils.timeutil import ET, ensure_utc, now_utc, run_id_for

REPO_ROOT = Path(__file__).resolve().parent.parent


def detect_git_sha(root: Path = REPO_ROOT) -> Optional[str]:
    env_sha = os.environ.get("GITHUB_SHA")
    if env_sha:
        return env_sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha or None


@dataclass
class RunContext:
    sport: str
    scope: str = "full"
    started_at: datetime = field(default_factory=now_utc)
    run_id: str = ""
    git_sha: Optional[str] = None
    dry_run: bool = False
    stage_timings: dict[str, float] = field(default_factory=dict)
    degradations: list[Degradation] = field(default_factory=list)
    unresolved_names: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.started_at = ensure_utc(self.started_at)
        if not self.run_id:
            self.run_id = f"{run_id_for(self.started_at)}-{self.sport}"
        if self.git_sha is None:
            self.git_sha = detect_git_sha()

    @property
    def started_et(self) -> datetime:
        return self.started_at.astimezone(ET)

    @property
    def now_utc(self) -> datetime:
        return now_utc()

    @property
    def now_et(self) -> datetime:
        return now_utc().astimezone(ET)

    def degrade(self, component: str, reason: str, severity: str = "warn") -> Degradation:
        d = Degradation(component=component, reason=reason, severity=severity, run_id=self.run_id, ts=now_utc())
        self.degradations.append(d)
        return d

    def count(self, book: str, market: str, n: int) -> None:
        self.counts.setdefault(book, {})[market] = self.counts.get(book, {}).get(market, 0) + n

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stage_timings[name] = round(time.perf_counter() - t0, 3)

    def summary_lines(self) -> list[str]:
        lines = [f"run_id={self.run_id} sport={self.sport} scope={self.scope} sha={self.git_sha or '-'}"]
        for k, v in self.stage_timings.items():
            lines.append(f"  stage {k:<14} {v:8.3f}s")
        for d in self.degradations:
            lines.append(f"  DEGRADED [{d.severity}] {d.component}: {d.reason}")
        return lines


__all__ = ["RunContext", "detect_git_sha", "REPO_ROOT"]
