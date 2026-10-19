"""Fail a PR that weakens tests unless the body explains why.

The diff is the pull request range ``base...head`` (merge-base to head).
PR_BODY and PR_AUTHOR come from the environment and are never printed.
Dependabot and Renovate are skipped, same as scripts/check_pr_body.py.
A ## Test changes section is required only when this script flags something.
Placeholder text (N/A, TODO, an empty fence, the template sentence) does not
count; that screening is is_placeholder() in scripts/check_pr_body.py.

Catches (added or removed diff lines only):

- A deleted test module, or a rename that moves it out of the test tree.
  Test modules are test_*.py, *_test.py, *.test.js/mjs/cjs/jsx/ts/tsx,
  *.spec.* of those suffixes, and other *.py files under a test/ or tests/
  directory. tests/fixtures/ and __init__.py are not test modules.
  Deleting conftest.py is flagged. Root-level test_*.py is included.
- A removed Python ``def test_*`` or JS/TS ``test(`` / ``it(`` / ``specify(``
  whose name is not added again anywhere in the diff. A rename counts as the
  same test when the stems match, one stem contains the other (4+ characters),
  or SequenceMatcher ratio is at least 0.8.
- A removed pytest parametrize list entry in a hunk that mentions parametrize.
- A new or edited pytest/unittest skip, skipif, or xfail, including
  pytest.skip(), pytest.importorskip(), a bare importorskip() call,
  unittest.skip/skipIf/skipUnless, self.skipTest, and add_marker(skip).
  Reason-only edits are ignored. Changing the module passed to
  importorskip is an edit.
- In JS/TS tests: .skip(, .only(, xit/xtest/xdescribe/xcontext, and
  ``skip: true`` / ``only: true``.
- A new or broader ``--ignore``, ``--ignore-glob``, ``--deselect``, or
  ``--test-skip-pattern``; a new or narrower ``-k`` / ``--keyword``; a new or
  narrower ``--test-name-pattern``. Scanned in .github/workflows, pytest.ini,
  pyproject.toml, setup.cfg, tox.ini, conftest.py, and package.json.
- A new collect_ignore or collect_ignore_glob entry in those pytest configs.
- A removed assertion, an equality rewritten to a weaker form (assertTrue,
  assertIn, ordering, inequality, approx, bare truthiness), strictEqual
  rewritten to equal, or a looser rel/abs/delta/tolerance (larger) or places
  (smaller). Combining checks with ``and`` is not weakening. An equivalent
  rewrite (assertEqual to ``assert ==``) in the same hunk is not weakening.

Misses (on purpose, to keep false positives down):

- A weaker check moved into a helper, or a tolerance stored in a variable.
- An expected value that changes while the assert shape stays as strong.
- Inverting assertTrue/assertFalse or assert.ok/assert.notOk.
- Multi-line asserts and pytest.param calls with nested parentheses.
- Fixture and golden-file deletions.
- Exclusions that live only in a Makefile, a shell variable, or a CI system
  other than GitHub Actions workflow files.
- A custom JS runner that does not call test/it or .skip/.only/xit.
- Identical assertions moved across files (a same-file move is fine).

Other repos: root-level test_*.py, unittest assert* / skip*, node:test and
jest/mocha names, and conftest collect_ignore are covered. Suites that live
in spec/ without test_/ *.test./ *.spec. names, or CI outside
.github/workflows and the pytest config files above, need is_test_file()
or is_ci_config() extended.

Usage:
    python3 scripts/check_test_weakening.py --diff change.diff
    BASE_SHA=... HEAD_SHA=... PR_BODY=... python3 scripts/check_test_weakening.py
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_PY_TEST = re.compile(r"(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")
_JS_CASE = re.compile(
    r"""(?:^|[^\w.])"""
    r"""(?:x(?:it|test)|(?:it|test|specify)(?:\s*\.\s*(?:skip|only|todo))?)"""
    r"""\s*\(\s*(?P<q>['"`])(?P<name>(?:\\.|(?!(?P=q)).){1,200})(?P=q)"""
)
_PARAM_ITEM = re.compile(
    r"""^\s*(?:pytest\.param\s*\([^()\n]*\)|'[^'\n]+'|"[^"\n]+"|\([^()\n]+\))\s*,\s*(?:#.*)?$"""
)
_EXCLUDE = re.compile(
    r"(?P<flag>--ignore-glob|--ignore|--deselect|--test-skip-pattern)"
    r"(?:=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|\S+)"
)
_K_FLAG = re.compile(
    r"(?:^|\s)(?:-k|--keyword)(?:=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|\S+)"
)
_NAME_PAT = re.compile(
    r"--test-name-pattern(?:=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|\S+)"
)
_TOL = re.compile(
    r"\b(?P<key>rel|abs|delta|tol|tolerance|places)\s*=\s*"
    r"(?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)
_LIST_ITEM = re.compile(r"""^\s*(['"])[^'"]+\1\s*,?\s*(?:#.*)?$""")

_PY_METHODS = {
    "assertequal": "eq",
    "assertequals": "eq",
    "assertnotequal": "ne",
    "assertnotequals": "ne",
    "asserttrue": "true",
    "assertfalse": "true",
    "assertis": "eq",
    "assertisnone": "eq",
    "assertisnotnone": "not_none",
    "assertin": "in",
    "assertnotin": "in",
    "assertgreater": "order",
    "assertgreaterequal": "order",
    "assertless": "order",
    "assertlessequal": "order",
    "assertalmostequal": "approx",
    "assertalmostequals": "approx",
    "assertnotalmostequal": "approx",
    "assertsequenceequal": "eq",
    "assertlistequal": "eq",
    "assertdictequal": "eq",
    "assertsetequal": "eq",
    "assertcountequal": "eq",
    "asserttupleequal": "eq",
    "assertmultilineequal": "eq",
    "assertisinstance": "in",
    "assertregex": "in",
}
_JS_METHODS = {
    "equal": "eq",
    "strictequal": "eq",
    "deepequal": "eq",
    "deepstrictequal": "eq",
    "notequal": "ne",
    "notstrictequal": "ne",
    "ok": "true",
    "notok": "true",
}
_STRICT_LABELS = frozenset({"strictEqual", "deepStrictEqual"})
_LOOSE_LABELS = frozenset({"equal", "deepEqual"})
_WEAKER_THAN = {
    "eq": frozenset({"approx", "ne", "order", "in", "not_none", "true"}),
    "approx": frozenset({"ne", "order", "in", "not_none", "true"}),
    "ne": frozenset({"in", "not_none", "true"}),
    "order": frozenset({"not_none", "true"}),
    "in": frozenset({"not_none", "true"}),
    "not_none": frozenset({"true"}),
    "true": frozenset(),
    "unknown": frozenset(),
}
_PY_SKIP_RULES = (
    ("pytest.mark.xfail", re.compile(r"(?:@pytest\.mark\.xfail\b|\bpytest\.xfail\s*\(|\bpytest\.mark\.xfail\b)")),
    ("pytest.mark.skipif", re.compile(r"(?:@pytest\.mark\.skipif\b|\bpytest\.mark\.skipif\b)")),
    (
        "pytest.mark.skip",
        re.compile(r"(?:@pytest\.mark\.skip\b|\bpytest\.skip\s*\(|\bpytest\.mark\.skip\b|\.add_marker\(\s*skip\b)"),
    ),
    (
        "pytest.importorskip",
        re.compile(r"(?:\bpytest\.importorskip\s*\(|(?<!\.)\bimportorskip\s*\()"),
    ),
    (
        "unittest.skip",
        re.compile(
            r"(?:@unittest\.skip(?:If|Unless)?\b|\bunittest\.skip(?:If|Unless)?\s*\("
            r"|\bself\.skipTest\s*\(|\braise\s+unittest\.SkipTest\b)"
        ),
    ),
)
_JS_SKIP_RULES = (
    (".only", re.compile(r"\.only\s*\(|\bonly\s*:\s*true\b|['\"]only['\"]\s*:\s*true\b")),
    (".skip", re.compile(r"\.skip\s*\(|\bskip\s*:\s*true\b|['\"]skip['\"]\s*:\s*true\b")),
    ("xit", re.compile(r"\bxit\s*\(")),
    ("xtest", re.compile(r"\bxtest\s*\(")),
    ("xdescribe", re.compile(r"\bxdescribe\s*\(")),
    ("xcontext", re.compile(r"\bxcontext\s*\(")),
)
_TOL_DEFAULTS = {"rel": 1e-6, "abs": 1e-12}
_HIGHER_LOOSER = frozenset({"rel", "abs", "delta", "tol", "tolerance"})
_PYTEST_CONFIGS = frozenset({"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"})
_CONFIG_NAMES = _PYTEST_CONFIGS | {"package.json"}


class DiffError(Exception):
    """The diff could not be loaded. The message is safe to print."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


@dataclass
class HunkLine:
    kind: str
    text: str
    old_ln: int | None
    new_ln: int | None


@dataclass
class Hunk:
    lines: list[HunkLine]


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    deleted: bool
    renamed: bool
    hunks: list[Hunk]


@dataclass(frozen=True)
class Assert:
    kind: str
    label: str
    line_norm: str
    expr: str
    tols: tuple[tuple[str, float], ...]
    line: int
    hunk: int

    def tol_map(self) -> dict[str, float]:
        return dict(self.tols)


def _body_api():
    """Import the PR-body helpers with the repo root on sys.path.

    Running ``python3 scripts/check_test_weakening.py`` puts scripts/ on
    sys.path, not the repo root, so the package import needs the root first.
    """
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.check_pr_body import is_dependency_bot, is_placeholder, sections

    return is_dependency_bot, is_placeholder, sections


def _strip_trailing_comment(text: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _code(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped.startswith(("#", "//", "*", "/*", "'''", '"""')):
        return ""
    return _strip_trailing_comment(stripped).strip()


def _strip_strings(code: str) -> str:
    return re.sub(r"""(?P<q>['"])(?:\\.|(?!(?P=q)).)*(?P=q)""", '""', code)


def _clean_val(raw: str) -> str:
    """Drop quotes and JSON/shell closers that stick to a flag value."""
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val.strip("\"'\\})],")


def _is_js(path: str) -> bool:
    return path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"))


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_test_file(path: str) -> bool:
    """True for a test module. Fixtures, conftest, and __init__ are excluded."""
    if not path or path == "/dev/null":
        return False
    name = _basename(path)
    if name in {"conftest.py", "__init__.py"}:
        return False
    under_fixtures = "/fixtures/" in f"/{path}" or path.startswith("fixtures/")
    named = (
        (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or bool(re.search(r"\.(?:test|spec)\.(?:js|jsx|mjs|cjs|ts|tsx)$", name))
    )
    if named:
        return True
    if under_fixtures or not name.endswith(".py"):
        return False
    parts = path.split("/")
    return any(part in {"test", "tests"} for part in parts[:-1])


def is_ci_config(path: str) -> bool:
    """Workflows and the pytest/JS config files that can exclude tests."""
    if not path or path == "/dev/null":
        return False
    name = _basename(path)
    if name in _CONFIG_NAMES:
        return True
    return path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))


def _renamed_out(file: FileDiff) -> bool:
    return file.renamed and is_test_file(file.old_path) and not is_test_file(file.new_path)


def _live_path(file: FileDiff) -> str:
    if file.deleted or file.new_path == "/dev/null":
        return file.old_path
    return file.new_path


def parse_diff(diff: str) -> list[FileDiff]:
    """Parse a git unified diff into per-file hunks."""
    if not diff or not diff.strip():
        return []
    chunks = re.split(r"(?m)(?=^diff --git )", diff)
    files: list[FileDiff] = []
    for chunk in chunks:
        if chunk.startswith("diff --git "):
            files.append(_parse_file(chunk))
    return files


def _parse_file(chunk: str) -> FileDiff:
    lines = chunk.splitlines()
    header = re.match(r"^diff --git a/(.+) b/(.+)$", lines[0])
    old_path = header.group(1) if header else ""
    new_path = header.group(2) if header else ""
    deleted = False
    renamed = False
    hunks: list[Hunk] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("rename from "):
            renamed = True
            old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            renamed = True
            new_path = line[len("rename to ") :]
        elif line.startswith("@@"):
            hunk, index = _parse_hunk(lines, index)
            hunks.append(hunk)
            continue
        index += 1
    if new_path == "/dev/null":
        deleted = True
    return FileDiff(old_path, new_path, deleted, renamed, hunks)


def _parse_hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    match = _HUNK_RE.match(lines[index])
    if not match:
        return Hunk([]), index + 1
    old_ln = int(match.group(1))
    new_ln = int(match.group(3))
    index += 1
    body: list[HunkLine] = []
    while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("diff --git "):
        line = lines[index]
        index += 1
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            body.append(HunkLine("+", line[1:], None, new_ln))
            new_ln += 1
        elif line.startswith("-"):
            body.append(HunkLine("-", line[1:], old_ln, None))
            old_ln += 1
        elif line.startswith(" "):
            body.append(HunkLine(" ", line[1:], old_ln, new_ln))
            old_ln += 1
            new_ln += 1
    return Hunk(body), index


def _py_name(text: str) -> str | None:
    code = _code(text)
    if not code:
        return None
    match = _PY_TEST.match(code)
    return match.group(1) if match else None


def _js_name(text: str) -> str | None:
    code = _code(text)
    if not code:
        return None
    match = _JS_CASE.search(code)
    if not match:
        return None
    return match.group("name")


def _stem(name: str) -> str:
    text = re.sub(r"^(?:test_|it_|spec_)", "", name.strip().lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_test(name: str, added: set[str]) -> bool:
    if name in added:
        return True
    stem = _stem(name)
    if not stem:
        return False
    for other in added:
        other_stem = _stem(other)
        if not other_stem:
            continue
        if stem == other_stem:
            return True
        if len(stem) >= 4 and len(other_stem) >= 4 and (stem in other_stem or other_stem in stem):
            return True
        if difflib.SequenceMatcher(None, stem, other_stem).ratio() >= 0.8:
            return True
    return False


def _deleted_findings(files: list[FileDiff]) -> list[Finding]:
    found: list[Finding] = []
    for file in files:
        if file.deleted and is_test_file(file.old_path):
            found.append(Finding(file.old_path, 1, "deleted test file"))
        elif _renamed_out(file):
            found.append(Finding(file.old_path, 1, "deleted test file"))
        elif file.deleted and _basename(file.old_path) == "conftest.py":
            found.append(Finding(file.old_path, 1, "deleted conftest.py"))
    return found


def _removed_test_findings(files: list[FileDiff]) -> list[Finding]:
    removed: list[tuple[str, int, str, bool]] = []
    added: set[str] = set()
    for file in files:
        if file.deleted or _renamed_out(file):
            continue
        js = _is_js(file.old_path) or _is_js(file.new_path)
        py = is_test_file(file.old_path) or is_test_file(file.new_path)
        if not js and not py:
            continue
        for hunk in file.hunks:
            for item in hunk.lines:
                if js:
                    name = _js_name(item.text)
                    py_name = None
                else:
                    name = None
                    py_name = _py_name(item.text) if py else None
                label = py_name or name
                if not label:
                    continue
                if item.kind == "+":
                    added.add(label)
                elif item.kind == "-" and item.old_ln:
                    removed.append((file.old_path, item.old_ln, label, bool(py_name)))
    found: list[Finding] = []
    for path, line, name, is_py in removed:
        if _same_test(name, added):
            continue
        kind = "removed test function" if is_py else "removed test case"
        shown = name if is_py else repr(name)
        found.append(Finding(path, line, f"{kind} {shown}"))
    return found


def _skip_kind(text: str, js: bool) -> str | None:
    code = _code(text)
    if not code:
        return None
    stripped = _strip_strings(code)
    if js:
        if re.search(r"""['"]only['"]\s*:\s*true\b""", code) or re.search(r"\bonly\s*:\s*true\b", stripped):
            return ".only"
        if re.search(r"""['"]skip['"]\s*:\s*true\b""", code) or re.search(r"\bskip\s*:\s*true\b", stripped):
            return ".skip"
        # A quoted mention such as note = ".skip(" must not count.
        if re.search(r"\.only\s*\(", stripped):
            return ".only"
        if re.search(r"\.skip\s*\(", stripped):
            return ".skip"
        for kind, pattern in _JS_SKIP_RULES:
            if kind in {".only", ".skip"}:
                continue
            if pattern.search(stripped):
                return kind
        return None
    for kind, pattern in _PY_SKIP_RULES:
        if pattern.search(stripped):
            return kind
    return None


def _blank_reasons(code: str) -> str:
    code = re.sub(
        r"(reason\s*=\s*)(?P<q>['\"])(?:\\.|(?!(?P=q)).)*(?P=q)",
        r'\1""',
        code,
    )
    code = re.sub(
        r"((?:pytest\.skip|pytest\.xfail|unittest\.skip(?:If|Unless)?|self\.skipTest)\s*\(\s*)"
        r"(?P<q>['\"])(?:\\.|(?!(?P=q)).)*(?P=q)",
        r'\1""',
        code,
    )
    return code


def _skip_sig(text: str, js: bool) -> str:
    code = _code(text)
    if not code:
        return ""
    if js:
        code = _strip_strings(code)
    else:
        code = _blank_reasons(code)
    return re.sub(r"\s+", "", code)


def _skip_findings(file: FileDiff) -> list[Finding]:
    path = _live_path(file)
    js = _is_js(file.old_path) or _is_js(file.new_path)
    py_test = is_test_file(file.old_path) or is_test_file(file.new_path)
    conftest = _basename(file.old_path) == "conftest.py" or _basename(file.new_path) == "conftest.py"
    if file.deleted or not (py_test or conftest):
        return []
    removed: list[str] = []
    added: list[tuple[str, str, int]] = []
    for hunk in file.hunks:
        for item in hunk.lines:
            if item.kind not in "+-":
                continue
            kind = _skip_kind(item.text, js)
            if not kind:
                continue
            sig = _skip_sig(item.text, js)
            if item.kind == "-":
                removed.append(sig)
            elif item.new_ln:
                added.append((kind, sig, item.new_ln))
    leftover = list(removed)
    found: list[Finding] = []
    for kind, sig, line in added:
        if sig and sig in leftover:
            leftover.remove(sig)
            continue
        if leftover:
            leftover.pop()
            found.append(Finding(path, line, f"changed {kind} condition"))
        else:
            found.append(Finding(path, line, f"added {kind} marker"))
    return found


def _param_findings(file: FileDiff) -> list[Finding]:
    if file.deleted or _is_js(file.new_path) or not is_test_file(file.new_path):
        return []
    found: list[Finding] = []
    for hunk in file.hunks:
        blob = "\n".join(item.text for item in hunk.lines)
        if "parametrize" not in blob:
            continue
        added = [item.text.strip() for item in hunk.lines if item.kind == "+" and _PARAM_ITEM.match(item.text)]
        for item in hunk.lines:
            if item.kind != "-" or not item.old_ln or not _PARAM_ITEM.match(item.text):
                continue
            key = item.text.strip()
            if key in added:
                added.remove(key)
                continue
            found.append(Finding(file.old_path, item.old_ln, "removed parametrized case"))
    return found


def _top_word(expr: str, word: str) -> bool:
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == "\\" and quote != "`" and i + 1 < len(expr):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and expr.startswith(word, i):
            before = expr[i - 1] if i else " "
            after = expr[i + len(word)] if i + len(word) < len(expr) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
        i += 1
    return False


def _split_top(expr: str, op: str) -> tuple[str, str] | None:
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == "\\" and quote != "`" and i + 1 < len(expr):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and expr.startswith(op, i):
            nxt = expr[i + len(op)] if i + len(op) < len(expr) else ""
            if op in {"<", ">"} and nxt == "=":
                i += 1
                continue
            before = expr[i - 1] if i else " "
            after = nxt or " "
            bounded = True
            if op.isalpha():
                bounded = not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")
            if bounded:
                left, right = expr[:i].strip(), expr[i + len(op) :].strip()
                if left and right:
                    return left, right
        i += 1
    return None


def _classify_expr(expr: str) -> tuple[str, str]:
    if not expr or expr == "(":
        return "unknown", "assert"
    if "pytest.approx" in expr or re.search(r"\bapprox\s*\(", expr):
        return "approx", "approx"
    found = _split_top(expr, "is not")
    if found:
        return ("not_none", "is not None") if found[1] == "None" else ("ne", "is not")
    if _split_top(expr, "not in"):
        return "in", "not in"
    for op, kind, label in (
        ("==", "eq", "=="),
        ("!=", "ne", "!="),
        ("<=", "order", "<="),
        (">=", "order", ">="),
        ("<", "order", "<"),
        (">", "order", ">"),
    ):
        if _split_top(expr, op):
            return kind, label
    found = _split_top(expr, "is")
    if found:
        return ("eq", "is None") if found[1] == "None" else ("eq", "is")
    if _split_top(expr, "in"):
        return "in", "in"
    if re.match(r"not\s+", expr):
        return "true", "assert not"
    return "true", "assert"


def _expression(code: str) -> str:
    match = re.match(r"(?:self\.)?(assert[A-Za-z]+)\s*\((.*)\)\s*$", code)
    if match and match.group(1).lower() in _PY_METHODS:
        return match.group(2)
    match = re.match(r"assert\s*\.\s*([A-Za-z]+)\s*\((.*)\)\s*$", code)
    if match and match.group(1).lower() in _JS_METHODS:
        return match.group(2)
    if code.startswith("assert ") or code == "assert":
        return code[len("assert") :].strip()
    return code


def _tolerances(code: str) -> tuple[tuple[str, float], ...]:
    found: dict[str, float] = {}
    for match in _TOL.finditer(code):
        found[match.group("key")] = float(match.group("num"))
    return tuple(sorted(found.items()))


def parse_assert(text: str, line: int, hunk: int) -> Assert | None:
    code = _code(text).rstrip(";").strip()
    if not code:
        return None
    method = re.match(r"(?:self\.)?(assert[A-Za-z]+)\s*\(.*\)\s*$", code)
    js_method = re.match(r"assert\s*\.\s*([A-Za-z]+)\s*\(.*\)\s*$", code)
    plain = code.startswith("assert ") or code == "assert"
    if method and method.group(1).lower() in _PY_METHODS:
        label = method.group(1)
        kind = _PY_METHODS[label.lower()]
    elif js_method and js_method.group(1).lower() in _JS_METHODS:
        label = js_method.group(1)
        kind = _JS_METHODS[label.lower()]
    elif method or js_method or plain:
        label = (method or js_method).group(1) if (method or js_method) else "assert"
        kind = "unknown"
        if plain and not method and not js_method:
            kind, label = _classify_expr(_expression(code))
    else:
        return None
    expr = _expression(code)
    if kind == "eq" and ("pytest.approx" in code or re.search(r"\bapprox\s*\(", code)):
        kind, label = "approx", "approx"
    return Assert(kind, label, re.sub(r"\s+", "", code), expr, _tolerances(code), line, hunk)


def _fmt(number: float) -> str:
    return format(number, ".6g")


def _looser_tol(old: Assert, new: Assert) -> str | None:
    old_map, new_map = old.tol_map(), new.tol_map()
    notes: list[str] = []
    for key in ("rel", "abs", "delta", "tol", "tolerance", "places"):
        if key in old_map and key in new_map:
            before, after = old_map[key], new_map[key]
            if key == "places" and after < before:
                notes.append(f"places {_fmt(before)} -> {_fmt(after)}")
            elif key in _HIGHER_LOOSER and after > before:
                notes.append(f"{key} {_fmt(before)} -> {_fmt(after)}")
        elif key == "places" and key not in old_map and key in new_map and new_map[key] < 7:
            notes.append(f"places 7 -> {_fmt(new_map[key])}")
        elif (
            key in _TOL_DEFAULTS
            and old.kind == "approx"
            and new.kind == "approx"
            and key not in old_map
            and key in new_map
            and new_map[key] > _TOL_DEFAULTS[key]
        ):
            notes.append(f"{key} {_fmt(_TOL_DEFAULTS[key])} -> {_fmt(new_map[key])}")
    if not notes:
        return None
    return "looser tolerance (" + ", ".join(notes) + ")"


def _added_or(old: Assert, new: Assert) -> bool:
    return _top_word(new.expr, "or") and not _top_word(old.expr, "or")


def _weaker(old: Assert, new: Assert) -> str | None:
    if old.label in _STRICT_LABELS and new.label in _LOOSE_LABELS:
        return f"loosened assertion ({old.label} -> {new.label})"
    if old.kind == "unknown" or new.kind == "unknown":
        return None
    if old.kind == new.kind:
        loosened = _looser_tol(old, new)
        if loosened:
            return loosened
        if _added_or(old, new):
            return f"loosened assertion ({old.label} gained or)"
        return None
    if new.kind in _WEAKER_THAN.get(old.kind, frozenset()):
        return f"loosened assertion ({old.label} -> {new.label})"
    return None


def _norm_space(text: str) -> str:
    return " ".join(text.split())


def _covers(old: Assert, new: Assert) -> bool:
    """True when the old check still appears, including as an ``and`` clause.

    Same arguments with a weaker method (strictEqual to equal) are not a cover.
    """
    if not old.expr or not new.expr or _added_or(old, new) or _weaker(old, new):
        return False
    old_expr = _norm_space(old.expr)
    if _norm_space(new.expr) == old_expr:
        return True
    return any(_norm_space(part) == old_expr for part in re.split(r"\s+and\s+", new.expr))


def _suppressed_assert_lines(file: FileDiff) -> set[int]:
    """Line numbers of asserts that belong to a test this diff deletes."""
    suppressed: set[int] = set()
    js = _is_js(file.old_path) or _is_js(file.new_path)
    removed_rows: list[tuple[int, str]] = []
    for hunk in file.hunks:
        if js:
            removed_cases = [
                item.old_ln for item in hunk.lines if item.kind == "-" and item.old_ln and _js_name(item.text)
            ]
            added_case = any(item.kind == "+" and _js_name(item.text) for item in hunk.lines)
            if removed_cases and not added_case:
                start = min(removed_cases)
                for item in hunk.lines:
                    if item.kind == "-" and item.old_ln and item.old_ln > start:
                        suppressed.add(item.old_ln)
        for item in hunk.lines:
            if item.kind == "-" and item.old_ln:
                removed_rows.append((item.old_ln, item.text))
    if js:
        return suppressed
    owner: int | None = None
    for line_no, text in removed_rows:
        code = _code(text)
        indent = len(text) - len(text.lstrip(" "))
        if code and _PY_TEST.match(code):
            owner = indent
            continue
        if owner is None:
            continue
        if code and indent <= owner and not code.startswith("@"):
            owner = None
            continue
        suppressed.add(line_no)
    return suppressed


def _assert_findings(file: FileDiff) -> list[Finding]:
    if file.deleted or not (is_test_file(file.old_path) or is_test_file(file.new_path)):
        return []
    suppressed = _suppressed_assert_lines(file)
    removed: list[Assert] = []
    added: list[Assert] = []
    for index, hunk in enumerate(file.hunks):
        for item in hunk.lines:
            if item.kind == "-" and item.old_ln and item.old_ln not in suppressed:
                parsed = parse_assert(item.text, item.old_ln, index)
                if parsed:
                    removed.append(parsed)
            elif item.kind == "+" and item.new_ln:
                parsed = parse_assert(item.text, item.new_ln, index)
                if parsed:
                    added.append(parsed)
    used: set[int] = set()
    unmatched: list[Assert] = []
    for old in removed:
        hit = next((i for i, new in enumerate(added) if i not in used and old.line_norm == new.line_norm), None)
        if hit is None:
            unmatched.append(old)
        else:
            used.add(hit)
    still: list[Assert] = []
    for old in unmatched:
        if any(_covers(old, new) for new in added):
            continue
        still.append(old)
    found: list[Finding] = []
    consumed: set[int] = set()
    for old in still:
        candidates = [i for i, new in enumerate(added) if i not in used and i not in consumed and new.hunk == old.hunk]
        if not candidates:
            found.append(Finding(file.old_path, old.line, "removed assertion"))
            continue
        pick = candidates[0]
        consumed.add(pick)
        reason = _weaker(old, added[pick])
        if reason:
            found.append(Finding(file.new_path, added[pick].line, reason))
    return found


def _broader_path(old: str, new: str) -> bool:
    old_path = old.rstrip("/")
    new_path = new.rstrip("/")
    return bool(new_path) and old_path.startswith(new_path + "/")


def _compare_ignores(path: str, flag: str, removed: list[str], added: list[tuple[str, int]]) -> list[Finding]:
    leftover = list(removed)
    found: list[Finding] = []
    for val, line in added:
        if val in leftover:
            leftover.remove(val)
            continue
        widened = next((old for old in leftover if _broader_path(old, val)), None)
        if widened is not None:
            leftover.remove(widened)
            found.append(Finding(path, line, f"widened {flag} from {widened} to {val}"))
        else:
            found.append(Finding(path, line, f"added {flag} {val}"))
    return found


def _k_weaker(old: str, new: str) -> bool:
    before = " ".join(old.split())
    after = " ".join(new.split())
    if before == after:
        return False
    nots_before = set(re.findall(r"\bnot\s+(\S+)", before))
    nots_after = set(re.findall(r"\bnot\s+(\S+)", after))
    if nots_after - nots_before:
        return True
    parts_before = [part.strip() for part in re.split(r"\bor\b", before)]
    parts_after = [part.strip() for part in re.split(r"\bor\b", after)]
    if all(any(part == other for other in parts_after) for part in parts_before) and len(parts_after) >= len(parts_before):
        return False
    return True


def _compare_exprs(
    path: str,
    removed: list[str],
    added: list[tuple[str, int]],
    weaker,
    added_reason: str,
    changed_reason: str,
) -> list[Finding]:
    found: list[Finding] = []
    for index, (expr, line) in enumerate(added):
        if index < len(removed):
            if not weaker(removed[index], expr):
                continue
            found.append(Finding(path, line, changed_reason.format(old=removed[index], new=expr)))
        else:
            found.append(Finding(path, line, added_reason.format(new=expr)))
    return found


def _collect_ignore_findings(file: FileDiff, path: str) -> list[Finding]:
    if _basename(path) not in _PYTEST_CONFIGS and _basename(file.old_path) not in _PYTEST_CONFIGS:
        return []
    found: list[Finding] = []
    for hunk in file.hunks:
        blob = "\n".join(item.text for item in hunk.lines)
        if "collect_ignore" not in blob:
            continue
        removed: list[str] = []
        added: list[tuple[str, int]] = []
        for item in hunk.lines:
            if item.kind not in "+-":
                continue
            interesting = "collect_ignore" in item.text or _LIST_ITEM.match(item.text)
            if not interesting:
                continue
            values = re.findall(r"""['"]([^'"]+)['"]""", item.text)
            if item.kind == "-":
                removed.extend(values)
            elif item.new_ln:
                added.extend((val, item.new_ln) for val in values)
        leftover = list(removed)
        for val, line in added:
            if val in leftover:
                leftover.remove(val)
                continue
            found.append(Finding(path, line, f"added collect_ignore entry {val}"))
    return found


def _config_code(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped.startswith(("#", "//")):
        return ""
    return stripped


def _exclusion_findings(file: FileDiff) -> list[Finding]:
    path = _live_path(file)
    if not is_ci_config(path) and not is_ci_config(file.old_path):
        return []
    removed_flags: dict[str, list[str]] = {}
    added_flags: dict[str, list[tuple[str, int]]] = {}
    removed_k: list[str] = []
    added_k: list[tuple[str, int]] = []
    removed_name: list[str] = []
    added_name: list[tuple[str, int]] = []
    for hunk in file.hunks:
        for item in hunk.lines:
            if item.kind not in "+-":
                continue
            text = _config_code(item.text)
            if not text:
                continue
            for match in _EXCLUDE.finditer(text):
                val = _clean_val(match.group("val"))
                if not val:
                    continue
                flag = match.group("flag")
                if item.kind == "-":
                    removed_flags.setdefault(flag, []).append(val)
                elif item.new_ln:
                    added_flags.setdefault(flag, []).append((val, item.new_ln))
            for match in _K_FLAG.finditer(text):
                expr = _clean_val(match.group("val"))
                if item.kind == "-":
                    removed_k.append(expr)
                elif item.new_ln:
                    added_k.append((expr, item.new_ln))
            for match in _NAME_PAT.finditer(text):
                expr = _clean_val(match.group("val"))
                if item.kind == "-":
                    removed_name.append(expr)
                elif item.new_ln:
                    added_name.append((expr, item.new_ln))
    found: list[Finding] = []
    for flag in sorted(set(removed_flags) | set(added_flags)):
        found.extend(_compare_ignores(path, flag, removed_flags.get(flag, []), added_flags.get(flag, [])))
    found.extend(
        _compare_exprs(
            path,
            removed_k,
            added_k,
            _k_weaker,
            "added -k selection {new}",
            "narrowed -k selection from {old} to {new}",
        )
    )
    found.extend(
        _compare_exprs(
            path,
            removed_name,
            added_name,
            lambda old, new: old != new and not (old in new and len(new) >= len(old)),
            "added --test-name-pattern {new}",
            "narrowed --test-name-pattern from {old} to {new}",
        )
    )
    found.extend(_collect_ignore_findings(file, path))
    return found


def find_weakening(diff: str) -> list[Finding]:
    """Return weakening findings for a unified diff, stable and unique."""
    files = parse_diff(diff)
    found = _deleted_findings(files) + _removed_test_findings(files)
    for file in files:
        if file.deleted or _renamed_out(file):
            continue
        found.extend(_skip_findings(file))
        found.extend(_param_findings(file))
        found.extend(_assert_findings(file))
        found.extend(_exclusion_findings(file))
    return sorted(set(found), key=lambda item: (item.path, item.line, item.reason))


def _explained(body: str | None) -> bool:
    _bots, is_placeholder, sections = _body_api()
    if body is None:
        return False
    text = sections(body).get("test")
    if text is None:
        return False
    return not is_placeholder(text)


def evaluate(diff: str, body: str | None, author: str | None = None) -> tuple[int, list[str]]:
    """Return an exit code and the job summary lines. Never includes the body."""
    is_dependency_bot, _placeholder, _sections = _body_api()
    if is_dependency_bot(author):
        return 0, ["test-weakening: dependency-bot PR, check skipped"]
    findings = find_weakening(diff)
    if not findings:
        return 0, ["test-weakening: no test weakening detected"]
    noun = "weakening" if len(findings) == 1 else "weakenings"
    bullets = [f"- {item.format()}" for item in findings]
    if _explained(body):
        header = f"test-weakening: {len(findings)} {noun} explained in ## Test changes:"
        return 0, [header, *bullets]
    verb = "needs" if len(findings) == 1 else "need"
    header = f"test-weakening: {len(findings)} {noun} {verb} a ## Test changes explanation:"
    footer = "test-weakening: ## Test changes is missing or still placeholder text"
    return 1, [header, *bullets, footer]


def _have_commit(sha: str) -> bool:
    proc = _run_git(["git", "cat-file", "-e", f"{sha}^{{commit}}"])
    return proc.returncode == 0


def _run_git(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False, env=env)


def _ensure_commit(sha: str) -> None:
    if _have_commit(sha):
        return
    _run_git(["git", "fetch", "--no-tags", "origin", sha])
    if _have_commit(sha):
        return
    number = os.environ.get("PR_NUMBER", "")
    if number.isdigit():
        _run_git(["git", "fetch", "--no-tags", "origin", f"+refs/pull/{number}/head"])
    if not _have_commit(sha):
        raise DiffError(f"could not find commit {sha}")


def git_diff_command(base: str, head: str) -> list[str]:
    return ["git", "diff", "--find-renames", "--unified=3", f"{base}...{head}"]


def _git_diff() -> str:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if not _SHA.fullmatch(base) or not _SHA.fullmatch(head):
        raise DiffError("BASE_SHA and HEAD_SHA must be git SHAs")
    _ensure_commit(base)
    _ensure_commit(head)
    proc = _run_git(git_diff_command(base, head))
    if proc.returncode != 0:
        raise DiffError("git diff failed")
    return proc.stdout


def _read_diff(diff_arg: str | None) -> str:
    if diff_arg is None:
        return _git_diff()
    if diff_arg == "-":
        return sys.stdin.read()
    return Path(diff_arg).read_text(encoding="utf-8")


def _emit(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flag unexplained test weakening in a pull request diff.")
    parser.add_argument(
        "--diff",
        help="Unified diff file, or - for stdin. Defaults to git diff BASE_SHA...HEAD_SHA.",
    )
    args = parser.parse_args(argv)
    author = os.environ.get("PR_AUTHOR")
    is_dependency_bot, _placeholder, _sections = _body_api()
    if is_dependency_bot(author):
        _emit(["test-weakening: dependency-bot PR, check skipped"])
        return 0
    try:
        diff = _read_diff(args.diff)
    except DiffError as exc:
        print(f"test-weakening: {exc}", file=sys.stderr)
        return 1
    code, lines = evaluate(diff, os.environ.get("PR_BODY"), author)
    _emit(lines)
    return code


if __name__ == "__main__":
    sys.exit(main())
