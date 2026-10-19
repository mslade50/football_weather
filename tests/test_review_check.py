"""The review-check workflow parses, stays read-only, and rejects empty PR bodies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "review-check.yml"
SCRIPT = ROOT / "scripts" / "check_pr_body.py"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

FILLED = """## Risk tier
LOW

## Proof
```
python -m pytest tests -q
12 passed
ruff check .
All checks passed!
```

## Rollback steps
Revert this PR.
"""


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_workflow_text())


def _run(body: str | None, author: str = "agent") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PR_AUTHOR"] = author
    if body is None:
        env.pop("PR_BODY", None)
    else:
        env["PR_BODY"] = body
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_workflow_parses_and_is_read_only():
    text = _workflow_text()
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "secrets." not in text
    assert "pull-requests: write" not in text
    assert "contents: write" not in text
    on = wf[True]
    assert on["pull_request"]["types"] == [
        "opened",
        "edited",
        "synchronize",
        "reopened",
        "ready_for_review",
    ]


def test_body_is_passed_by_env_not_interpolated_into_the_script():
    wf = _workflow()
    step = next(s for s in wf["jobs"]["body"]["steps"] if s.get("name") == "Check Proof and Rollback")
    assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
    assert step["env"]["PR_AUTHOR"] == "${{ github.event.pull_request.user.login }}"
    assert step["run"].strip() == "python3 scripts/check_pr_body.py"
    for job in wf["jobs"].values():
        for item in job["steps"]:
            run = item.get("run")
            if run:
                assert "${{" not in run


def test_test_job_runs_the_existing_pytest_and_ruff_commands():
    runs = "\n".join(step.get("run", "") for step in _workflow()["jobs"]["test"]["steps"])
    assert "ruff check ." in runs
    assert "python -m pytest tests -q" in runs
    assert "pip install -r requirements-dev.txt" in runs


def test_empty_body_fails():
    result = _run("")
    assert result.returncode == 1
    assert "PR body is empty" in result.stderr
    assert result.stdout == ""


def test_unfilled_template_fails():
    result = _run(TEMPLATE.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert "Proof section is empty or still placeholder text" in result.stderr
    assert "Rollback section is empty or still placeholder text" in result.stderr


def test_placeholder_words_fail():
    body = "## Proof\nTODO\n\n## Rollback steps\nTBD\n"
    result = _run(body)
    assert result.returncode == 1
    assert "Proof section is empty or still placeholder text" in result.stderr
    assert "Rollback section is empty or still placeholder text" in result.stderr


def test_visible_template_instructions_fail():
    body = (
        "## Proof\n"
        "Paste real pytest and ruff output here. For UI or alert changes, "
        "also attach sample alert text, a board screenshot, or a JSON sample.\n\n"
        "## Rollback steps\n"
        "Plain words: what to revert, which flag to turn off, or what to redeploy.\n"
    )
    result = _run(body)
    assert result.returncode == 1


def test_fenced_heading_does_not_end_the_section():
    body = """## Proof
```
## not a heading
1 passed
```

## Rollback steps
Revert this PR.
"""
    result = _run(body)
    assert result.returncode == 0


def test_missing_rollback_fails():
    body = "## Proof\npython -m pytest tests -q\n1 passed\n"
    result = _run(body)
    assert result.returncode == 1
    assert "Rollback section is missing" in result.stderr
    assert "Proof" not in result.stderr


def test_filled_body_passes_and_does_not_print_the_body():
    secret = "SECRET_SENTINEL_do_not_print"
    body = FILLED.replace("12 passed", f"12 passed {secret}")
    result = _run(body, author="cursor[bot]")
    assert result.returncode == 0
    assert "Proof and Rollback sections are filled" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_dependency_bots_skip_the_template_other_bots_do_not():
    template = TEMPLATE.read_text(encoding="utf-8")
    for author in ("dependabot[bot]", "dependabot", "renovate[bot]"):
        result = _run(template, author=author)
        assert result.returncode == 0, author
        assert "dependency-bot" in result.stdout
        assert "<!--" not in result.stdout
    agent = _run("", author="cursor[bot]")
    assert agent.returncode == 1
    assert "PR body is empty" in agent.stderr
