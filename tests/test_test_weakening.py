"""The test-weakening check flags unexplained skips, deletions, and looser asserts."""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_pr_body import is_placeholder, sections
from scripts.check_test_weakening import find_weakening, main

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_test_weakening.py"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
POLICY = ROOT / "docs" / "REVIEW_POLICY.md"
TEMPLATE_SENTENCE = (
    "Optional. Required only when this PR deletes or weakens tests. Say which tests and why."
)


def _dec(kind: str, rest: str = "") -> str:
    """Build a pytest marker without putting the marker on one source line."""
    return "@pytest.mark." + kind + rest


def _call(name: str, args: str) -> str:
    return name + args


def udiff(path: str, old: str, new: str) -> str:
    if old and not old.endswith("\n"):
        old += "\n"
    if new and not new.endswith("\n"):
        new += "\n"
    hunks = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    return f"diff --git a/{path} b/{path}\n{hunks}"


def udiff_delete(path: str, old: str) -> str:
    if not old.endswith("\n"):
        old += "\n"
    lines = old.splitlines()
    body = "".join(f"-{line}\n" for line in lines)
    count = len(lines) or 1
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        f"@@ -1,{count} +0,0 @@\n"
        f"{body}"
    )


def _reasons(diff: str) -> list[str]:
    return [item.reason for item in find_weakening(diff)]


def test_deleted_test_file_is_flagged_at_line_1():
    diff = udiff_delete("tests/test_old.py", "def test_old():\n    assert 1 == 1\n")
    findings = find_weakening(diff)
    assert [(item.path, item.line, item.reason) for item in findings] == [
        ("tests/test_old.py", 1, "deleted test file")
    ]


def test_deleted_js_test_file_is_flagged():
    diff = udiff_delete("site/worker/test/worker.test.mjs", 'test("adds", () => { assert.equal(1, 1); });\n')
    assert _reasons(diff) == ["deleted test file"]


def test_deleted_fixture_and_app_file_are_not_flagged():
    fixture = udiff_delete("tests/fixtures/raw/sample.json", '{"ok": true}\n')
    app = udiff_delete("pipeline/model/impact.py", "def score():\n    assert False\n")
    assert find_weakening(fixture + app) == []


def test_deleted_conftest_is_flagged():
    diff = udiff_delete("tests/conftest.py", "def pytest_configure(config):\n    pass\n")
    assert _reasons(diff) == ["deleted conftest.py"]


def test_removed_test_function_is_flagged_with_its_line():
    diff = """diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1,4 +1,2 @@
 def test_keep():
     assert 1 == 1
-def test_drop():
-    assert 2 == 2
"""
    findings = find_weakening(diff)
    assert len(findings) == 1
    assert findings[0].path == "tests/test_a.py"
    assert findings[0].line == 3
    assert findings[0].reason == "removed test function test_drop"


def test_removed_test_does_not_also_flag_its_asserts():
    diff = udiff(
        "tests/test_a.py",
        "def test_drop():\n    assert one == 1\n    assert two == 2\n",
        "",
    )
    assert _reasons(diff) == ["removed test function test_drop"]


def test_renamed_test_function_is_not_flagged():
    diff = udiff(
        "tests/test_a.py",
        "def test_pricing():\n    assert total == 1\n",
        "def test_pricing_guards():\n    assert total == 1\n",
    )
    assert find_weakening(diff) == []


def test_unrelated_replacement_name_is_flagged():
    diff = udiff(
        "tests/test_a.py",
        "def test_alpha():\n    assert 1 == 1\n",
        "def test_beta():\n    assert 1 == 1\n",
    )
    assert any(reason == "removed test function test_alpha" for reason in _reasons(diff))


def test_moved_test_function_across_files_is_not_flagged():
    removed = udiff("tests/test_a.py", "def test_keep():\n    assert 1 == 1\n", "")
    added = udiff("tests/test_b.py", "", "def test_keep():\n    assert 1 == 1\n")
    assert find_weakening(removed + added) == []


def test_root_level_test_module_is_seen():
    diff = udiff("test_root.py", "def test_keep():\n    assert 1 == 1\n", "")
    assert _reasons(diff) == ["removed test function test_keep"]


def test_adding_a_test_is_not_flagged():
    diff = udiff(
        "tests/test_a.py",
        "def test_old():\n    assert 1 == 1\n",
        "def test_old():\n    assert 1 == 1\n\ndef test_new():\n    assert 2 == 2\n",
    )
    assert find_weakening(diff) == []


def test_removed_js_case_is_flagged_and_added_case_is_not():
    diff = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'test("adds more", () => {\n  assert.equal(1, 1);\n});\n',
    )
    # Similar titles share a stem, so a small rename is not a removal.
    assert find_weakening(diff) == []
    gone = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'test("subtracts", () => {\n  assert.equal(1, 1);\n});\n',
    )
    assert any(reason == "removed test case 'adds'" for reason in _reasons(gone))


def test_removed_parametrize_case_is_flagged():
    diff = udiff(
        "tests/test_a.py",
        '@pytest.mark.parametrize("name", [\n    "alpha",\n    "beta",\n])\ndef test_name(name):\n    assert name\n',
        '@pytest.mark.parametrize("name", [\n    "alpha",\n])\ndef test_name(name):\n    assert name\n',
    )
    assert _reasons(diff) == ["removed parametrized case"]


def test_removing_a_string_outside_parametrize_is_not_a_case():
    diff = udiff(
        "tests/test_a.py",
        "def test_name():\n    names = [\n        'alpha',\n        'beta',\n    ]\n    assert names\n",
        "def test_name():\n    names = [\n        'alpha',\n    ]\n    assert names\n",
    )
    assert find_weakening(diff) == []


def test_pytest_skip_marker_and_call_and_unittest_skip_are_flagged():
    marker = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        _dec("skip", "(reason='later')") + "\ndef test_a():\n    assert 1 == 1\n",
    )
    skipif = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        _dec("skipif", "(True, reason='later')") + "\ndef test_a():\n    assert 1 == 1\n",
    )
    xfail = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        _dec("xfail", "(reason='later')") + "\ndef test_a():\n    assert 1 == 1\n",
    )
    call = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        "def test_a():\n    " + _call("pytest.skip", '("later")') + "\n",
    )
    unit = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        _call("@unittest.skip", '("later")') + "\ndef test_a():\n    assert 1 == 1\n",
    )
    assert any("pytest.mark.skip marker" in reason for reason in _reasons(marker))
    assert any("pytest.mark.skipif marker" in reason for reason in _reasons(skipif))
    assert any("pytest.mark.xfail marker" in reason for reason in _reasons(xfail))
    assert any("pytest.mark.skip marker" in reason for reason in _reasons(call))
    assert any("unittest.skip marker" in reason for reason in _reasons(unit))


def test_new_importorskip_is_flagged_and_an_unchanged_one_is_not():
    added = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        "def test_a():\n    " + _call("pytest.importorskip", '("pandas")') + "\n    assert 1 == 1\n",
    )
    bare = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        "def test_a():\n    " + _call("importorskip", '("pandas")') + "\n    assert 1 == 1\n",
    )
    unchanged = udiff(
        "tests/test_a.py",
        "def test_a():\n    " + _call("pytest.importorskip", '("pandas")') + "\n    assert total == 1\n",
        "def test_a():\n    " + _call("pytest.importorskip", '("pandas")') + "\n    assert total == 2\n",
    )
    edited = udiff(
        "tests/test_a.py",
        "def test_a():\n    " + _call("pytest.importorskip", '("pandas")') + "\n    assert 1 == 1\n",
        "def test_a():\n    " + _call("pytest.importorskip", '("pyarrow")') + "\n    assert 1 == 1\n",
    )
    assert any("added pytest.importorskip marker" in reason for reason in _reasons(added))
    assert any("added pytest.importorskip marker" in reason for reason in _reasons(bare))
    assert find_weakening(unchanged) == []
    assert any("changed pytest.importorskip condition" in reason for reason in _reasons(edited))


def test_reason_only_skip_edit_is_not_flagged_but_condition_change_is():
    same = udiff(
        "tests/test_a.py",
        _dec("skipif", "(SYS, reason='old')") + "\ndef test_a():\n    assert 1 == 1\n",
        _dec("skipif", "(SYS, reason='new')") + "\ndef test_a():\n    assert 1 == 1\n",
    )
    changed = udiff(
        "tests/test_a.py",
        _dec("skipif", "(False, reason='later')") + "\ndef test_a():\n    assert 1 == 1\n",
        _dec("skipif", "(True, reason='later')") + "\ndef test_a():\n    assert 1 == 1\n",
    )
    assert find_weakening(same) == []
    assert any("changed pytest.mark.skipif condition" in reason for reason in _reasons(changed))


def test_skip_mentioned_in_a_string_is_not_a_marker():
    diff = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert 1 == 1\n",
        "def test_a():\n    note = '@pytest.mark.skip'\n    assert 1 == 1\n",
    )
    assert find_weakening(diff) == []


def test_js_skip_only_and_xit_are_flagged():
    skip = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'test.skip("adds", () => {\n  assert.equal(1, 1);\n});\n',
    )
    only = udiff(
        "site/worker/test/worker.test.mjs",
        'it("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'it.only("adds", () => {\n  assert.equal(1, 1);\n});\n',
    )
    xit = udiff(
        "site/worker/test/worker.test.mjs",
        'it("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'xit("adds", () => {\n  assert.equal(1, 1);\n});\n',
    )
    option = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.equal(1, 1);\n});\n',
        'test("adds", { skip: true }, () => {\n  assert.equal(1, 1);\n});\n',
    )
    assert any("added .skip marker" in reason for reason in _reasons(skip))
    assert not any("removed test case" in reason for reason in _reasons(skip))
    assert any("added .only marker" in reason for reason in _reasons(only))
    assert any("added xit marker" in reason for reason in _reasons(xit))
    assert any("added .skip marker" in reason for reason in _reasons(option))


def test_added_ignore_deselect_and_widened_ignore_are_flagged():
    added = udiff(
        ".github/workflows/ci.yml",
        "        run: python -m pytest tests -q\n",
        "        run: python -m pytest tests -q --ignore=tests/slow --deselect=tests/test_a.py::test_one\n",
    )
    widened = udiff(
        ".github/workflows/ci.yml",
        "        run: python -m pytest tests -q --ignore=tests/slow\n",
        "        run: python -m pytest tests -q --ignore=tests\n",
    )
    reasons = _reasons(added)
    assert "added --ignore tests/slow" in reasons
    assert "added --deselect tests/test_a.py::test_one" in reasons
    assert _reasons(widened) == ["widened --ignore from tests/slow to tests"]


def test_dropping_an_ignore_is_not_flagged_and_comments_are_ignored():
    dropped = udiff(
        ".github/workflows/ci.yml",
        "        run: python -m pytest tests -q --ignore=tests/slow\n",
        "        run: python -m pytest tests -q\n",
    )
    comment = udiff(
        ".github/workflows/ci.yml",
        "        run: python -m pytest tests -q\n",
        "        # keep --ignore out of this command\n        run: python -m pytest tests -q\n",
    )
    assert find_weakening(dropped) == []
    assert find_weakening(comment) == []


def test_narrowed_k_is_flagged_and_a_broader_k_is_not():
    narrowed = udiff(
        ".github/workflows/ci.yml",
        '        run: python -m pytest tests -q -k "foo or bar"\n',
        '        run: python -m pytest tests -q -k "foo"\n',
    )
    broader = udiff(
        ".github/workflows/ci.yml",
        '        run: python -m pytest tests -q -k "foo"\n',
        '        run: python -m pytest tests -q -k "foo or bar"\n',
    )
    extra_not = udiff(
        "pyproject.toml",
        'addopts = "-q -k \'not slow\'"\n',
        'addopts = "-q -k \'not slow and not net\'"\n',
    )
    assert any(reason.startswith("narrowed -k selection") for reason in _reasons(narrowed))
    assert find_weakening(broader) == []
    assert any("narrowed -k selection" in reason for reason in _reasons(extra_not))


def test_node_skip_pattern_and_test_name_pattern():
    skip = udiff(
        "package.json",
        '{"scripts": {"test": "node --test site/worker/test/*.test.mjs"}}\n',
        '{"scripts": {"test": "node --test --test-skip-pattern=worker site/worker/test/*.test.mjs"}}\n',
    )
    broader = udiff(
        "package.json",
        '{"scripts": {"test": "node --test --test-name-pattern=foo"}}\n',
        '{"scripts": {"test": "node --test --test-name-pattern=foo|bar"}}\n',
    )
    assert any("--test-skip-pattern worker" in reason for reason in _reasons(skip))
    assert find_weakening(broader) == []


def test_conftest_collect_ignore_entry_is_flagged():
    diff = udiff(
        "tests/conftest.py",
        'collect_ignore = [\n    "test_old.py",\n]\n',
        'collect_ignore = [\n    "test_old.py",\n    "test_network.py",\n]\n',
    )
    assert _reasons(diff) == ["added collect_ignore entry test_network.py"]


def test_conftest_blanket_skip_is_flagged():
    old = "def pytest_configure(config):\n    pass\n"
    new = "def pytest_configure(config):\n    " + _call("pytest.skip", '("off")') + "\n"
    assert any("pytest.mark.skip marker" in reason for reason in _reasons(udiff("tests/conftest.py", old, new)))


def test_removed_assert_line_is_flagged_with_the_old_line_number():
    diff = """diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1,3 +1,2 @@
 def test_a():
-    assert total == 1
     assert other == 2
"""
    findings = find_weakening(diff)
    assert [(item.line, item.reason) for item in findings] == [(2, "removed assertion")]


def test_assert_equal_rewritten_to_assert_true_or_assert_in():
    trueish = udiff(
        "tests/test_a.py",
        "def test_a():\n    self.assertEqual(total, 3)\n",
        "def test_a():\n    self.assertTrue(total)\n",
    )
    member = udiff(
        "tests/test_a.py",
        "def test_a():\n    self.assertEqual(items, ['a'])\n",
        "def test_a():\n    self.assertIn('a', items)\n",
    )
    bare = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert status == 'ok'\n",
        "def test_a():\n    assert status\n",
    )
    assert _reasons(trueish) == ["loosened assertion (assertEqual -> assertTrue)"]
    assert _reasons(member) == ["loosened assertion (assertEqual -> assertIn)"]
    assert _reasons(bare) == ["loosened assertion (== -> assert)"]


def test_equivalent_assert_refactor_and_same_strength_value_change_are_not_flagged():
    refactor = udiff(
        "tests/test_a.py",
        "def test_a():\n    self.assertEqual(total, 3)\n",
        "def test_a():\n    assert total == 3\n",
    )
    retarget = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert total == 1\n",
        "def test_a():\n    assert total == 2\n",
    )
    combined = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert a == 1\n    assert b == 2\n",
        "def test_a():\n    assert a == 1 and b == 2\n",
    )
    assert find_weakening(refactor) == []
    assert find_weakening(retarget) == []
    assert find_weakening(combined) == []


def test_looser_and_tighter_tolerances():
    looser = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert value == pytest.approx(1.0, rel=1e-6)\n",
        "def test_a():\n    assert value == pytest.approx(1.0, rel=1e-2)\n",
    )
    places = udiff(
        "tests/test_a.py",
        "def test_a():\n    self.assertAlmostEqual(a, b, places=5)\n",
        "def test_a():\n    self.assertAlmostEqual(a, b, places=2)\n",
    )
    tighter = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert value == pytest.approx(1.0, rel=1e-2)\n",
        "def test_a():\n    assert value == pytest.approx(1.0, rel=1e-6)\n",
    )
    disjunct = udiff(
        "tests/test_a.py",
        "def test_a():\n    assert value == 1\n",
        "def test_a():\n    assert value == 1 or True\n",
    )
    assert any(reason.startswith("looser tolerance (rel ") for reason in _reasons(looser))
    assert any("places 5 -> 2" in reason for reason in _reasons(places))
    assert find_weakening(tighter) == []
    assert any("gained or" in reason for reason in _reasons(disjunct))


def test_js_assert_equal_to_ok_and_strict_to_loose():
    weaker = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.equal(total, 3);\n});\n',
        'test("adds", () => {\n  assert.ok(total);\n});\n',
    )
    strict = udiff(
        "site/worker/test/worker.test.mjs",
        'test("adds", () => {\n  assert.strictEqual(total, 3);\n});\n',
        'test("adds", () => {\n  assert.equal(total, 3);\n});\n',
    )
    assert any("equal -> ok" in reason for reason in _reasons(weaker))
    assert any("strictEqual -> equal" in reason for reason in _reasons(strict))


def test_application_assert_removal_is_ignored():
    diff = udiff(
        "pipeline/model/impact.py",
        "def score():\n    assert total == 1\n",
        "def score():\n    return total\n",
    )
    assert find_weakening(diff) == []


def test_pure_rename_of_a_test_file_is_not_a_deletion():
    diff = (
        "diff --git a/tests/test_old.py b/tests/test_new.py\n"
        "similarity index 100%\n"
        "rename from tests/test_old.py\n"
        "rename to tests/test_new.py\n"
    )
    assert find_weakening(diff) == []


def test_rename_out_of_the_test_tree_is_a_deletion():
    diff = (
        "diff --git a/tests/test_old.py b/pipeline/model/impact.py\n"
        "similarity index 90%\n"
        "rename from tests/test_old.py\n"
        "rename to pipeline/model/impact.py\n"
    )
    assert _reasons(diff) == ["deleted test file"]


def test_policy_documents_the_check():
    text = POLICY.read_text(encoding="utf-8")
    assert "scripts/check_test_weakening.py" in text
    assert "## Test changes" in text
    assert "collect_ignore" in text


def test_unfilled_test_changes_section_is_placeholder():
    found = sections(TEMPLATE.read_text(encoding="utf-8"))
    assert "test" in found
    assert is_placeholder(found["test"])
    assert is_placeholder(TEMPLATE_SENTENCE)
    assert is_placeholder("N/A")
    assert is_placeholder("```\n```")


def _run_cli(diff: str, body: str | None, author: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("BASE_SHA", None)
    env.pop("HEAD_SHA", None)
    env.pop("GITHUB_STEP_SUMMARY", None)
    if author is None:
        env.pop("PR_AUTHOR", None)
    else:
        env["PR_AUTHOR"] = author
    if body is None:
        env.pop("PR_BODY", None)
    else:
        env["PR_BODY"] = body
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--diff", "-"],
        input=diff,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_fails_without_an_explanation_and_passes_with_one():
    diff = udiff_delete("tests/test_old.py", "def test_old():\n    assert 1 == 1\n")
    missing = _run_cli(diff, None)
    assert missing.returncode == 1
    assert "tests/test_old.py:1: deleted test file" in missing.stdout
    assert "needs a ## Test changes explanation" in missing.stdout
    assert "placeholder" in missing.stdout

    placeholder = _run_cli(diff, "## Test changes\nN/A\n")
    assert placeholder.returncode == 1

    empty_fence = _run_cli(diff, "## Test changes\n```\n```\n")
    assert empty_fence.returncode == 1

    sentence = _run_cli(diff, f"## Test changes\n{TEMPLATE_SENTENCE}\n")
    assert sentence.returncode == 1

    explained = _run_cli(
        diff,
        "## Test changes\nDeleted test_old because the behavior moved to test_new.\n",
    )
    assert explained.returncode == 0
    assert "explained in ## Test changes" in explained.stdout
    assert "tests/test_old.py:1: deleted test file" in explained.stdout


def test_cli_passes_when_nothing_weakens_and_does_not_print_the_body():
    diff = udiff(
        "tests/test_a.py",
        "def test_old():\n    assert 1 == 1\n",
        "def test_old():\n    assert 1 == 1\n\ndef test_new():\n    assert 2 == 2\n",
    )
    secret = "SECRET_SENTINEL_do_not_print"
    result = _run_cli(diff, f"## Test changes\nkept {secret}\n", author="cursor[bot]")
    assert result.returncode == 0
    assert "no test weakening detected" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_dependency_bots_skip_without_a_diff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PR_AUTHOR", "dependabot[bot]")
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.delenv("PR_BODY", raising=False)
    called: list[object] = []
    monkeypatch.setattr("scripts.check_test_weakening.subprocess.run", lambda *args, **kwargs: called.append(args))
    code = main([])
    assert code == 0
    assert called == []


def test_bad_sha_fails_without_calling_git(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.delenv("PR_AUTHOR", raising=False)
    monkeypatch.setenv("BASE_SHA", "not-a-sha")
    monkeypatch.setenv("HEAD_SHA", "b" * 40)
    called: list[object] = []
    monkeypatch.setattr("scripts.check_test_weakening.subprocess.run", lambda *args, **kwargs: called.append(args))
    assert main([]) == 1
    assert called == []
    assert "BASE_SHA and HEAD_SHA must be git SHAs" in capsys.readouterr().err


def test_git_diff_uses_three_dot_and_does_not_use_a_shell(monkeypatch: pytest.MonkeyPatch):
    base = "a" * 40
    head = "b" * 40
    monkeypatch.delenv("PR_AUTHOR", raising=False)
    monkeypatch.delenv("PR_BODY", raising=False)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        assert kwargs.get("shell") is not True

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if cmd[1] == "cat-file":
            Result.returncode = 0
        return Result()

    monkeypatch.setattr("scripts.check_test_weakening.subprocess.run", fake_run)
    assert main([]) == 0
    assert ["git", "diff", "--find-renames", "--unified=3", f"{base}...{head}"] in commands


def test_summary_file_lists_the_finding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("PR_AUTHOR", raising=False)
    monkeypatch.delenv("PR_BODY", raising=False)
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(udiff_delete("tests/test_old.py", "def test_old():\n    assert 1 == 1\n"), encoding="utf-8")
    assert main(["--diff", str(diff_path)]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "tests/test_old.py:1: deleted test file" in text
    assert text == capsys.readouterr().out
