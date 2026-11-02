"""scripts/mirror_r2_snapshots.sh: parallel wrangler gets, fail closed, no token in logs."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mirror_r2_snapshots.sh"
TOKEN = "cf-token-do-not-log-9f3a7c"


def _fake_wrangler(tmp: Path) -> Path:
    path = tmp / "wrangler"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  echo "wrangler 4.test"
  exit 0
fi
if [[ "${1:-}" != "r2" || "${2:-}" != "object" || "${3:-}" != "get" || "${6:-}" != "--remote" ]]; then
  echo "bad argv: $*" >&2
  exit 2
fi
obj="${4}"
file_arg="${5}"
[[ "$file_arg" == --file=* ]] || { echo "missing --file" >&2; exit 2; }
dest="${file_arg#--file=}"
key="${obj#${EXPECTED_BUCKET}/}"
printf '%s\\n' "$*" > "$CALLS_DIR/${key//\\//_}-$$"
mode="ok"
[[ -f "$CONTROL/$key" ]] && mode="$(cat "$CONTROL/$key")"
if [[ "$mode" == "fail" ]]; then
  printf 'partial' > "$dest"
  echo "NoSuchKey ${CLOUDFLARE_API_TOKEN:-} Authorization: Bearer ${CLOUDFLARE_API_TOKEN:-}" >&2
  exit 1
fi
if [[ "$mode" == "flake" ]]; then
  nfile="$CONTROL/attempts-${key//\\//_}"
  n=0
  [[ -f "$nfile" ]] && n="$(cat "$nfile")"
  n=$((n + 1))
  printf '%s' "$n" > "$nfile"
  if [[ "$n" -lt 2 ]]; then
    printf 'partial' > "$dest"
    echo "temporary ${CLOUDFLARE_API_TOKEN:-}" >&2
    exit 1
  fi
fi
if [[ "${SLEEP_S:-}" != "" ]]; then
  start="$(date +%s%N)"
  sleep "$SLEEP_S"
  end="$(date +%s%N)"
  printf '%s %s\\n' "$start" "$end" > "$SPANS_DIR/${key//\\//_}-$$"
fi
mkdir -p "$(dirname "$dest")"
printf 'bytes:%s' "$key" > "$dest"
echo "ok ${CLOUDFLARE_API_TOKEN:-}" >&2
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run(tmp: Path, keys: str, *, extra_env: dict[str, str] | None = None, wrangler: Path | None = None):
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / "data"
    dest.mkdir()
    keys_file = tmp / "keys.txt"
    keys_file.write_text(keys, encoding="utf-8")
    calls = tmp / "calls"
    spans = tmp / "spans"
    control = tmp / "control"
    calls.mkdir()
    spans.mkdir()
    control.mkdir()
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text("#!/usr/bin/env bash\necho npm-was-called > \"$NPM_MARKER\"\nexit 1\n", encoding="utf-8")
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update({
        "R2_BUCKET": "football-board",
        "EXPECTED_BUCKET": "football-board",
        "CLOUDFLARE_API_TOKEN": TOKEN,
        "CALLS_DIR": str(calls),
        "SPANS_DIR": str(spans),
        "CONTROL": str(control),
        "MIRROR_PARALLEL": "8",
        "MIRROR_ATTEMPTS": "1",
        "MIRROR_RETRY_SLEEP": "0",
        "NPM_MARKER": str(tmp / "npm-marker"),
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
    })
    if wrangler is not None:
        env["WRANGLER"] = str(wrangler)
    else:
        env.pop("WRANGLER", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(keys_file), str(dest)],
        capture_output=True, text=True, env=env, check=False,
    )
    return proc, dest, calls, spans, control


def test_parallel_fetch_writes_exact_bytes_at_the_listed_paths(tmp_path: Path):
    wrangler = _fake_wrangler(tmp_path)
    keys = [
        "snapshots/cfb/2026/1/20261101T000000Z.json",
        "snapshots/nfl/2026/8/20261101T010000Z.json",
        "snapshots/cfb/2026/9/20261101T020000Z-bt.json",
    ]
    proc, dest, calls, spans, _ = _run(
        tmp_path, "\n".join(keys) + "\n\n",
        wrangler=wrangler,
        extra_env={"MIRROR_PARALLEL": "4", "SLEEP_S": "0.4"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "mirrored 3 snapshot object(s) of 3 listed" in proc.stdout
    for key in keys:
        assert (dest / key).read_text(encoding="utf-8") == f"bytes:{key}"
    argv = sorted(p.read_text(encoding="utf-8").strip() for p in calls.iterdir())
    assert argv == sorted(
        f"r2 object get football-board/{key} --file={dest / key} --remote" for key in keys
    )
    intervals = []
    for path in spans.iterdir():
        start, end = path.read_text(encoding="utf-8").split()
        intervals.append((int(start), int(end)))
    assert len(intervals) == 3
    assert any(a < d and c < b for i, (a, b) in enumerate(intervals) for c, d in intervals[i + 1:])
    blob = proc.stdout + proc.stderr
    assert TOKEN not in blob
    assert not (tmp_path / "npm-marker").exists()


def test_failed_fetch_removes_the_file_and_redacts_the_token(tmp_path: Path):
    wrangler = _fake_wrangler(tmp_path)
    proc, dest, _, _, _ = _run_with_modes(
        tmp_path, wrangler,
        {
            "snapshots/cfb/2026/1/ok.json": "ok",
            "snapshots/nfl/2026/2/missing.json": "fail",
        },
    )
    assert proc.returncode != 0
    assert (dest / "snapshots/cfb/2026/1/ok.json").read_text(encoding="utf-8") == "bytes:snapshots/cfb/2026/1/ok.json"
    assert not (dest / "snapshots/nfl/2026/2/missing.json").exists()
    blob = proc.stdout + proc.stderr
    assert TOKEN not in blob
    assert "Authorization: Bearer ***" in blob
    assert "mirrored 1 snapshot object(s) of 2 listed" in blob
    assert "does not match listed" in blob
    assert "::error::R2 snapshot fetch failed for snapshots/nfl/2026/2/missing.json" in blob


def test_retry_keeps_the_successful_bytes(tmp_path: Path):
    wrangler = _fake_wrangler(tmp_path)
    key = "snapshots/cfb/2026/3/flake.json"
    proc, dest, _, _, _ = _run_with_modes(
        tmp_path, wrangler, {key: "flake"},
        extra_env={"MIRROR_ATTEMPTS": "2"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (dest / key).read_text(encoding="utf-8") == f"bytes:{key}"
    assert TOKEN not in proc.stdout + proc.stderr
    assert "attempt 1/2" in proc.stdout + proc.stderr


def test_empty_key_list_fails(tmp_path: Path):
    wrangler = _fake_wrangler(tmp_path)
    proc, dest, _, _, _ = _run(tmp_path, "", wrangler=wrangler)
    assert proc.returncode != 0
    assert "snapshot key list is empty" in proc.stdout + proc.stderr
    proc2, _, _, _, _ = _run(tmp_path / "blank", " \n\t\n", wrangler=wrangler)
    assert proc2.returncode != 0
    assert "snapshot key list is empty" in proc2.stdout + proc2.stderr
    assert list(dest.rglob("*")) == [] or all(p.is_dir() for p in dest.rglob("*"))


def test_missing_wrangler_binary_fails_before_npm_when_path_is_set(tmp_path: Path):
    missing = tmp_path / "no-such-wrangler"
    proc, _, _, _, _ = _run(tmp_path, "snapshots/cfb/2026/1/a.json\n", wrangler=missing)
    assert proc.returncode != 0
    assert "not executable" in proc.stdout + proc.stderr
    assert not (tmp_path / "npm-marker").exists()


def _run_with_modes(tmp: Path, wrangler: Path, modes: dict[str, str], *, extra_env: dict[str, str] | None = None):
    """Same as ``_run``, with per-key fake-wrangler modes planted before the script starts."""
    # Reimplement the small setup so mode files exist before exec.
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / "data"
    dest.mkdir()
    keys_file = tmp / "keys.txt"
    keys_file.write_text("".join(f"{key}\n" for key in modes), encoding="utf-8")
    calls = tmp / "calls"
    spans = tmp / "spans"
    control = tmp / "control"
    for path in (calls, spans, control):
        path.mkdir()
    for key, mode in modes.items():
        target = control / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mode, encoding="utf-8")
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text("#!/usr/bin/env bash\necho npm-was-called > \"$NPM_MARKER\"\nexit 1\n", encoding="utf-8")
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update({
        "R2_BUCKET": "football-board",
        "EXPECTED_BUCKET": "football-board",
        "CLOUDFLARE_API_TOKEN": TOKEN,
        "CALLS_DIR": str(calls),
        "SPANS_DIR": str(spans),
        "CONTROL": str(control),
        "WRANGLER": str(wrangler),
        "MIRROR_PARALLEL": "4",
        "MIRROR_ATTEMPTS": "1",
        "MIRROR_RETRY_SLEEP": "0",
        "NPM_MARKER": str(tmp / "npm-marker"),
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
    })
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(keys_file), str(dest)],
        capture_output=True, text=True, env=env, check=False,
    )
    return proc, dest, calls, spans, control
