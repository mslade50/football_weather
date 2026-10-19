"""Fail a pull request whose Proof or Rollback section is missing or still a placeholder.

The body is read from the PR_BODY environment variable and is never printed.
Dependabot and Renovate do not fill the repo template, so their PRs pass.
Every other author, including other bots and coding agents, must fill both
sections.

Run from the repo root: python scripts/check_pr_body.py
"""

from __future__ import annotations

import os
import re
import sys

FENCE = re.compile(r"^(?P<mark>`{3,}|~{3,})")
HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Whole-section text that counts as unfilled. Comments are stripped first, so
# the HTML notes in the template already fail as empty; these catch the same
# notes left behind as visible text, plus one-line stand-ins.
PLACEHOLDER_TEXT = {
    "todo",
    "tbd",
    "fixme",
    "xxx",
    "placeholder",
    "n/a",
    "na",
    "none",
    "...",
    "high or low. high = alerts, pricing, edge, or fair price, r2 or worker publish, workflows, or secrets. low = display, copy, or docs.",
    "paste real pytest and ruff output here. for ui or alert changes, also attach sample alert text, a board screenshot, or a json sample.",
    "plain words: what to revert, which flag to turn off, or what to redeploy.",
}
PLACEHOLDER_LINES = {"todo", "tbd", "fixme", "xxx", "placeholder", "n/a", "na", "none", "..."}
DEPENDENCY_BOTS = {"dependabot", "dependabot-preview", "renovate"}
REQUIRED = (("proof", "Proof"), ("rollback", "Rollback"))


def is_dependency_bot(author: str | None) -> bool:
    """True for Dependabot-style dependency bots. Other bots still get checked."""
    if not author or not author.strip():
        return False
    name = author.strip().lower().split("[", 1)[0]
    return name in DEPENDENCY_BOTS or name.startswith("dependabot")


def _normalize(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _visible(text: str) -> str:
    return COMMENT.sub("", text).strip()


def is_placeholder(text: str) -> bool:
    visible = _visible(text)
    if not visible:
        return True
    folded = _normalize(visible)
    if folded in PLACEHOLDER_TEXT:
        return True
    if folded.startswith("<") and folded.endswith(">"):
        return True
    lines = [_normalize(line) for line in COMMENT.sub("", text).splitlines() if line.strip()]
    return bool(lines) and all(line in PLACEHOLDER_LINES for line in lines)


def sections(body: str) -> dict[str, str]:
    """Map the first Proof and Rollback headings to the text under each.

    Headings inside fenced code blocks are part of the section body, so pasted
    command output cannot close a section early.
    """
    found: dict[str, str] = {}
    title: str | None = None
    buf: list[str] = []
    fence: str | None = None

    def flush() -> None:
        nonlocal title, buf
        if title is None:
            return
        parts = title.split()
        key = parts[0].lower().rstrip(":") if parts else ""
        if key in {"proof", "rollback"} and key not in found:
            found[key] = "\n".join(buf)
        title = None
        buf = []

    for line in body.splitlines():
        marker = FENCE.match(line.strip())
        if marker:
            mark = marker.group("mark")[0]
            fence = None if fence == mark else (fence or mark)
            if title is not None:
                buf.append(line)
            continue
        if fence is None:
            heading = HEADING.match(line)
            if heading:
                flush()
                title = heading.group("title").strip()
                buf = []
                continue
        if title is not None:
            buf.append(line)
    flush()
    return found


def problems(body: str | None, author: str | None = None) -> list[str]:
    if is_dependency_bot(author):
        return []
    if body is None or not body.strip():
        return ["PR body is empty; add Proof and Rollback sections"]
    found = sections(body)
    issues: list[str] = []
    for key, label in REQUIRED:
        if key not in found:
            issues.append(f"{label} section is missing")
        elif is_placeholder(found[key]):
            issues.append(f"{label} section is empty or still placeholder text")
    return issues


def main() -> int:
    author = os.environ.get("PR_AUTHOR")
    if is_dependency_bot(author):
        print("review-check: dependency-bot PR, template not required")
        return 0
    issues = problems(os.environ.get("PR_BODY"), author)
    if issues:
        for issue in issues:
            print(f"review-check: {issue}", file=sys.stderr)
        return 1
    print("review-check: Proof and Rollback sections are filled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
