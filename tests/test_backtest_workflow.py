"""String-contract tests for .github/workflows/backtest.yml and calibrate.yml (PLAN Phase 6,
ARCH §9.3 / §13; convention: tests/test_pipeline_workflow.py).

backtest.yml: Sunday CFB + Tuesday NFL post-mortem schedules and dispatch, off-the-minute crons,
read-only R2 state fetch that fails on anything but NoSuchKey, D1 table export,
`python -m pipeline.backtest` -> board/backtest.json pushed to R2 with retries (meta.json
never touched), `python -m pipeline.alerts --digest postmortem` after the backtest, Telegram on
failure, no continue-on-error, no git commit.

calibrate.yml: dispatch + monthly schedule, R2 backtest inputs, `python -m pipeline.calibrate`
with the 4-week guard, v1 golden + v2 tests gate the PR, PR via peter-evans/create-pull-request
touching only data/calibration.json, config.py diff must be empty, concurrency football-refresh."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WF_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
BACKTEST = WF_DIR / "backtest.yml"
CALIBRATE = WF_DIR / "calibrate.yml"


def test_backtest_dependency_filter_only_excludes_browser_packages():
    text = BACKTEST.read_text(encoding="utf-8")
    assert "grep -viE '^(playwright|playwright-stealth)\\b' requirements.txt" in text


ALERTS_PY = WF_DIR.parents[1] / "pipeline" / "alerts.py"


@pytest.fixture(scope="module")
def bt() -> str:
    return BACKTEST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cal() -> str:
    return CALIBRATE.read_text(encoding="utf-8")


def _step(text: str, name: str) -> str:
    start = text.index(f"      - name: {name}\n")
    try:
        end = text.index("\n      - name:", start + 1)
    except ValueError:
        end = len(text)
    return text[start:end]


def _steps(text: str) -> list[dict]:
    wf = yaml.safe_load(text)
    jobs = wf["jobs"]
    return next(iter(jobs.values()))["steps"]


# ---- backtest.yml ---------------------------------------------------------------------

def test_backtest_parses_and_has_one_job(bt: str):
    wf = yaml.safe_load(bt)
    assert list(wf["jobs"]) == ["backtest"]
    assert wf["jobs"]["backtest"]["timeout-minutes"] <= 30
    assert wf["jobs"]["backtest"]["runs-on"] == "ubuntu-latest"
    assert "self-hosted" not in bt


def test_backtest_schedule_is_sunday_cfb_and_tuesday_nfl_off_the_minute(bt: str):
    crons = re.findall(r"- cron: '([^']+)'", bt)
    assert crons == ["17 13 * * 0", "17 13 * * 2"]
    for c in crons:
        assert c.split()[0] == "17", c
    assert "workflow_dispatch:" in bt and "digest:" in bt and "dry_run:" in bt
    assert "github.event_name == 'schedule'" in bt
    assert "github.event.schedule == '17 13 * * 0'" in bt and "DIGEST_SPORT:" in bt
    assert "digest_sport:" in bt and "options: [all, cfb, nfl]" in bt
    assert "inputs.digest == true" in bt


def test_backtest_concurrency_and_readonly_permissions(bt: str):
    assert "group: football-refresh" in bt
    assert "cancel-in-progress: false" in bt
    assert "contents: read" in bt and "contents: write" not in bt
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in bt
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CF_ACCOUNT_ID }}" in bt
    assert "R2_BUCKET: football-board" in bt and "D1_DATABASE: football-odds" in bt


def test_backtest_state_fetch_is_readonly_and_fails_on_non_nosuchkey(bt: str):
    step = _step(bt, "Fetch board state from R2 (read-only)")
    assert "STATE_FILES:" not in bt
    assert "run: python scripts/fetch_r2.py state --group backtest --dest data/state" in step
    assert "continue-on-error" not in step and "|| true" not in step
    # Shared fetch behavior (missing only, all other errors fatal) has fake-transport
    # coverage in test_fetch_r2.py and test_workflow_r2_groups.py.
    # the state files are never pushed back: only backtest.json + parquet leave this job
    push = _step(bt, "Push backtest to R2")
    assert "run: python scripts/publish_r2.py backtest" in push
    assert "scripts/publish_r2.py state" not in bt and "scripts/publish_r2.py meta" not in bt
    assert "continue-on-error" not in push and "|| true" not in push
    assert "if: env.DRY_RUN == ''" in push


def test_backtest_exports_d1_and_runs_module(bt: str):
    exp = _step(bt, "Export D1 tables")
    assert "D1_TABLES: games odds_history closings alerts stadiums teams weather_history runs" in bt
    # {table}.json in the `d1 execute --json` shape that pipeline.backtest.load_export_dir reads
    assert 'npx --yes wrangler@4 d1 execute "$D1_DATABASE" --remote --json --command "SELECT * FROM $t" > "data/d1_export/$t.json"' in exp
    run = _step(bt, "Run backtest")
    assert ("python -m pipeline.backtest --state-dir data/state --snapshot-dir data/snapshots --export-dir data/d1_export"
            in run)
    assert "--board-dir data/board --parquet-dir data/backtest --d1-sql data/d1_backtest.sql" in run
    cmd = next(ln for ln in run.splitlines() if "python -m pipeline.backtest" in ln)
    assert "--freeze" not in cmd          # state stays read-only; pipeline.yml freezes closings
    assert "CFBD_API_KEY: ${{ secrets.CFBD_API_KEY }}" in run
    mirror = _step(bt, "Mirror snapshots from R2 (wrangler, read-only)")
    # keys rebuilt from the D1 export (game_id + run_id), fetched in parallel by one wrangler
    assert 'from pipeline.backtest import load_export_dir, snapshot_keys' in mirror
    assert 'snapshot_keys(load_export_dir("data/d1_export")' in mirror
    assert "bash scripts/mirror_r2_snapshots.sh data/snapshot_keys.txt data" in mirror
    assert 'MIRROR_PARALLEL: "8"' in mirror
    assert "npx --yes wrangler@4" not in mirror and "set -x" not in mirror
    assert "SNAPSHOT_MAX" in mirror and 'SNAPSHOT_MAX: "120"' in bt
    assert 'BACKTEST_NETWORK_BUDGET_S: "600"' in bt
    script = (WF_DIR.parents[1] / "scripts" / "mirror_r2_snapshots.sh").read_text(encoding="utf-8")
    assert "npm install --no-save --prefix" in script and "wrangler@4" in script
    assert "xargs -d $'\\n' -P \"$PARALLEL\" -n 1" in script
    assert 'r2 object get "${R2_BUCKET}/${key}" --file="${dest}" --remote' in script
    assert "mirrored ${mirrored} snapshot object(s) of ${listed} listed" in script
    assert "does not match listed" in script
    assert "::error::R2 snapshot fetch failed" in script
    assert "npx" not in script and "set -x" not in script
    assert 'echo "$CLOUDFLARE_API_TOKEN"' not in script and "echo $CLOUDFLARE_API_TOKEN" not in script
    assert "continue-on-error" not in mirror and "exit 0" not in mirror


def test_backtest_and_calibrate_use_no_s3_keys(bt: str, cal: str, monkeypatch):
    """R2 access is wrangler-only (CLOUDFLARE_API_TOKEN + CF_ACCOUNT_ID); the S3 keys do not exist."""
    for text in (bt, cal):
        for token in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "boto3", "r2.cloudflarestorage.com"):
            assert token not in text, token
        assert "python scripts/fetch_r2.py" in text
    # With no S3 keys, the shared scripts must use the existing Wrangler fallback.
    from scripts import publish_r2
    for key in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert publish_r2.s3_config() is None
    d1 = _step(bt, "Archive backtest rows to D1 (closings / stadium_results)")
    assert "hashFiles('data/d1_backtest.sql') != ''" in d1
    assert 'npx --yes wrangler@4 d1 execute "$D1_DATABASE" --remote --yes --file=data/d1_backtest.sql' in d1


def test_backtest_cli_flags_exist_in_module():
    """The flags the workflow passes must be real pipeline.backtest arguments."""
    src = (WF_DIR.parents[1] / "pipeline" / "backtest.py").read_text(encoding="utf-8")
    for flag in ("--state-dir", "--snapshot-dir", "--export-dir", "--board-dir", "--parquet-dir", "--d1-sql"):
        assert f'"{flag}"' in src, flag


def test_backtest_sends_postmortem_via_alerts_module(bt: str):
    step = _step(bt, "Weekly post-mortem")
    assert "if: env.SEND_DIGEST == 'true'" in step
    assert "python -m pipeline.alerts --digest postmortem --state-dir data/state --backtest data/board/backtest.json" in step
    assert '--email-fallback "${SPORT_ARGS[@]}"' in step
    assert 'SPORT_ARGS=(--sport "$DIGEST_SPORT")' in step
    assert "${DRY_RUN:+--dry-run}" in step
    for secret in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_NFL", "TELEGRAM_CHAT_ID_CFB"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in step
    for secret in ("POSTMORTEM_EMAIL_TO", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_USE_SSL", "SMTP_STARTTLS"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in step
    # the digest mode exists in pipeline/alerts.py with that exact spelling
    src = ALERTS_PY.read_text(encoding="utf-8")
    assert 'DIGEST_KINDS = ("postmortem",)' in src and 'args.digest == "postmortem"' in src
    assert '"--email-fallback"' in src and "send_postmortem_email" in src


def test_backtest_step_order_and_failure_ping(bt: str):
    order = ["Fetch board state from R2 (read-only)", "Export D1 tables", "Mirror snapshots from R2 (wrangler, read-only)",
             "Run backtest", "Push backtest to R2", "Archive backtest rows to D1 (closings / stadium_results)",
             "Weekly post-mortem", "Upload backtest artifacts", "Telegram on failure"]
    idx = [bt.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)
    tg = _step(bt, "Telegram on failure")
    assert "failure() && vars.TELEGRAM_SYSTEM_ALERTS == '1'" in tg
    assert "python scripts/notify_failure.py" in tg and "--strict" not in tg


def test_backtest_no_git_commit_no_playwright_only_paper_is_isolated(bt: str):
    for token in ("git commit", "git push", "git pull", "playwright install"):
        assert token not in bt, token
    steps = yaml.safe_load(bt)["jobs"]["backtest"]["steps"]
    isolated = [step for step in steps if step.get("continue-on-error")]
    assert len(isolated) == 1
    assert isolated[0]["run"] == "python scripts/paper_run.py grade"
    # Existing fetch/build/publication steps remain fail-closed.
    assert all(not step.get("continue-on-error") for step in steps if step not in isolated)
    assert "grep -viE '^(playwright|playwright-stealth" in bt


# ---- calibrate.yml --------------------------------------------------------------------

def test_calibrate_parses_dispatch_and_monthly_schedule(cal: str):
    wf = yaml.safe_load(cal)
    assert list(wf["jobs"]) == ["calibrate"]
    on = wf[True]
    assert "workflow_dispatch" in on and "schedule" in on
    crons = re.findall(r"- cron: '([^']+)'", cal)
    assert crons == ["17 13 1 * *"]      # monthly, off-the-minute
    assert "force:" in cal and "dry_run:" in cal


def test_calibrate_permissions_and_concurrency(cal: str):
    assert "contents: write" in cal and "pull-requests: write" in cal
    assert "group: football-refresh" in cal and "cancel-in-progress: false" in cal
    assert "continue-on-error" not in cal


def test_calibrate_fetches_backtest_inputs_readonly(cal: str):
    step = _step(cal, "Fetch backtest inputs from R2 (read-only)")
    assert "run: python scripts/fetch_r2.py inputs --group calibrate --dest data" in step
    assert "continue-on-error" not in step and "|| true" not in step
    assert "r2 object put" not in cal      # calibrate never writes to R2
    assert "scripts/publish_r2.py" not in cal


def test_calibrate_runs_module_with_four_week_guard(cal: str):
    step = _step(cal, "Refit v2 coefficients")
    assert "python -m pipeline.calibrate --input data/backtest/games.parquet --out data/calibration.json" in step
    assert "--backtest data/board/backtest.json --min-weeks 4 ${FORCE:+--force} ${DRY_RUN:+--dry-run}" in step
    assert 'echo "refit=false" >> "$GITHUB_OUTPUT"' in step and 'echo "refit=true" >> "$GITHUB_OUTPUT"' in step
    assert "2)" in step   # exit 2 == not enough weeks -> no PR, not a failure


def test_calibrate_gates_pr_on_v1_golden_and_untouched_config(cal: str):
    step = _step(cal, "v1 golden + v2 curve tests against the new calibration")
    assert "if: steps.fit.outputs.refit == 'true'" in step
    assert "git diff --exit-code -- pipeline/model/config.py" in step
    assert "python -m pytest tests/test_impact_v1.py tests/test_impact_v2.py tests/test_calibrate.py -q" in step
    # +/-0.5 is the default-curve spec. The file written by this job is judged
    # by calibration_gate, and a real fit is allowed to miss the band.
    assert "calibration_gate" in step and "A fit may miss +/-0.5" in step
    order = ["Fetch backtest inputs from R2 (read-only)", "Refit v2 coefficients",
             "v1 golden + v2 curve tests against the new calibration", "Open PR", "Telegram on failure"]
    idx = [cal.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)


def test_calibrate_opens_pr_touching_only_calibration_json(cal: str):
    pr = next(s for s in _steps(cal) if "create-pull-request" in s.get("uses", ""))
    assert pr["uses"].startswith("peter-evans/create-pull-request@v")
    assert pr["with"]["add-paths"].strip() == "data/calibration.json"
    assert pr["with"]["branch"] == "chore/calibrate-v2"
    assert "ALERT_MODEL" in pr["with"]["body"] and ">= 4 weeks" in pr["with"]["body"]
    assert "steps.fit.outputs.refit == 'true'" in pr["if"] and "env.DRY_RUN == ''" in pr["if"]
    for token in ("git commit", "git push", "git pull"):
        assert token not in cal, token


def test_calibrate_failure_ping(cal: str):
    tg = _step(cal, "Telegram on failure")
    assert "failure() && vars.TELEGRAM_SYSTEM_ALERTS == '1'" in tg
    assert "python scripts/notify_failure.py" in tg and "--strict" not in tg


def test_promotion_rule_documented_in_config_and_calibrate():
    root = WF_DIR.parents[1]
    cfg = (root / "pipeline" / "model" / "config.py").read_text(encoding="utf-8")
    assert "Phase 6 gate: v2 CLV >= v1 over >=4 weeks" in cfg
    calib = (root / "pipeline" / "calibrate.py").read_text(encoding="utf-8")
    assert "ALERT_MODEL = \\\"v2\\\"" in calib or 'ALERT_MODEL = "v2"' in calib
    assert ">= 4 weeks" in calib
