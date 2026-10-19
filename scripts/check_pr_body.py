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
# A fence with no body, including one that only has a language tag.
EMPTY_FENCE = re.compile(
    r"(?m)^[ \t]*(?P<mark>`{3,}|~{3,})[^\n]*\n(?:[ \t]*\n)*[ \t]*(?P=mark)[ \t]*\n?"
)

# Phrases that are not proof or a rollback step. Matching happens after
# normalization (lowercase, punctuation removed, whitespace collapsed), so
# "N/A." and "n/a" are the same entry. Add a line here to reject it when it
# is the whole Proof or Rollback section.
REJECTED_PHRASES = (
    "todo",
    "tbd",
    "fixme",
    "xxx",
    "placeholder",
    "n/a",
    "none",
    "todo fill this in",
    "tests passed",
    "revert if needed",
    "Paste real pytest and ruff output here. For UI or alert changes, also attach sample alert text, a board screenshot, or a JSON sample.",
    "Plain words: what to revert, which flag to turn off, or what to redeploy.",
)
DEPENDENCY_BOTS = {"dependabot", "dependabot-preview", "renovate"}
REQUIRED = (("proof", "Proof"), ("rollback", "Rollback"))


def is_dependency_bot(author: str | None) -> bool:
    """True for Dependabot-style dependency bots. Other bots still get checked."""
    if not author or not author.strip():
        return False
    name = author.strip().lower().split("[", 1)[0]
    return name in DEPENDENCY_BOTS or name.startswith("dependabot")


def _fold(text: str) -> str:
    """Lowercase, drop punctuation, and collapse whitespace."""
    kept = (ch.lower() for ch in text if ch.isalnum() or ch.isspace())
    return " ".join("".join(kept).split())


_REJECTED = frozenset(phrase for phrase in (_fold(item) for item in REJECTED_PHRASES) if phrase)


def _prepare(text: str) -> str:
    visible = COMMENT.sub("", text)
    previous = None
    while previous != visible:
        previous = visible
        visible = EMPTY_FENCE.sub("\n", visible)
    return visible


def is_placeholder(text: str) -> bool:
    visible = _prepare(text)
    folded = _fold(visible)
    if not folded or folded in _REJECTED:
        return True
    lines = [line for line in (_fold(line) for line in visible.splitlines()) if line]
    return bool(lines) and all(line in _REJECTED for line in lines)


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
