"""String-contract tests for .github/workflows/backtest.yml and calibrate.yml (PLAN Phase 6,
ARCH §9.3 / §13; convention: tests/test_pipeline_workflow.py).

backtest.yml: Tue 06:17 ET-ish + Mon digest schedule + dispatch, off-the-minute crons,
read-only R2 state fetch that fails on anything but NoSuchKey, D1 table export,
`python -m pipeline.backtest` -> board/backtest.json pushed to R2 with retries (meta.json
never touched), `python -m pipeline.alerts --digest clv` on the Monday schedule, Telegram on
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


def test_backtest_schedule_tuesday_and_monday_digest_off_the_minute(bt: str):
    crons = re.findall(r"- cron: '([^']+)'", bt)
    assert "17 10 * * 2" in crons      # Tue 06:17 EDT weekly backtest
    assert "17 12 * * 1" in crons      # Mon 08:17 EDT backtest + CLV digest
    for c in crons:
        assert c.split()[0] == "17", c
    assert "workflow_dispatch:" in bt and "digest:" in bt and "dry_run:" in bt
    assert "github.event.schedule == '17 12 * * 1'" in bt
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
    assert "STATE_FILES: alerts closings history wx_history openers" in bt
    assert 'npx --yes wrangler@4 r2 object get "$R2_BUCKET/board/$f.json" --file="$DEST" --remote 2>&1' in step
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in step
    assert "exit 1" in step and "::error::R2 state fetch failed" in step
    assert "continue-on-error" not in step
    # the state files are never pushed back: only backtest.json + parquet leave this job
    push = _step(bt, "Push backtest to R2")
    assert 'put "board/meta.json"' not in push and 'put "board/$f.json"' not in push and "for f in $STATE_FILES" not in push
    assert 'put "board/backtest.json" "data/board/backtest.json" "application/json"' in push
    assert 'put "backtest/$(basename "$p")" "$p" "application/octet-stream"' in push
    assert "for i in 1 2 3; do" in push and "::error::R2 put $1 failed after 3 attempts" in push
    assert "if: env.DRY_RUN == ''" in push


def test_backtest_exports_d1_and_runs_module(bt: str):
    exp = _step(bt, "Export D1 tables")
    assert "D1_TABLES: games odds_history closings alerts stadiums teams weather_history" in bt
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
    # keys rebuilt from the D1 export (game_id + run_id), fetched with the pipeline.yml get loop
    assert 'snapshots/{parts[0]}/{parts[1]}/{parts[2]}/{run_id}.json' in mirror
    assert 'for t in ("weather_history", "odds_history"):' in mirror
    assert 'npx --yes wrangler@4 r2 object get "$R2_BUCKET/$key" --file="$DEST" --remote 2>&1' in mirror
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in mirror
    assert "exit 1" in mirror and "::error::R2 snapshot fetch failed" in mirror
    assert "SNAPSHOT_MAX" in mirror and 'SNAPSHOT_MAX: "120"' in bt
    assert "continue-on-error" not in mirror and "exit 0" not in mirror


def test_backtest_and_calibrate_use_no_s3_keys(bt: str, cal: str):
    """R2 access is wrangler-only (CLOUDFLARE_API_TOKEN + CF_ACCOUNT_ID); the S3 keys do not exist."""
    for text in (bt, cal):
        for token in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "boto3", "r2.cloudflarestorage.com"):
            assert token not in text, token
        assert "npx --yes wrangler@4 r2 object get" in text
    d1 = _step(bt, "Archive backtest rows to D1 (closings / stadium_results)")
    assert "hashFiles('data/d1_backtest.sql') != ''" in d1
    assert 'npx --yes wrangler@4 d1 execute "$D1_DATABASE" --remote --yes --file=data/d1_backtest.sql' in d1


def test_backtest_cli_flags_exist_in_module():
    """The flags the workflow passes must be real pipeline.backtest arguments."""
    src = (WF_DIR.parents[1] / "pipeline" / "backtest.py").read_text(encoding="utf-8")
    for flag in ("--state-dir", "--snapshot-dir", "--export-dir", "--board-dir", "--parquet-dir", "--d1-sql"):
        assert f'"{flag}"' in src, flag


def test_backtest_sends_clv_digest_via_alerts_module(bt: str):
    step = _step(bt, "Weekly CLV digest")
    assert "if: env.SEND_DIGEST == 'true'" in step
    assert "python -m pipeline.alerts --digest clv --state-dir data/state --backtest data/board/backtest.json" in step
    assert "${DRY_RUN:+--dry-run}" in step
    for secret in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_NFL", "TELEGRAM_CHAT_ID_CFB"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in step
    # the digest mode exists in pipeline/alerts.py with that exact spelling
    src = ALERTS_PY.read_text(encoding="utf-8")
    assert 'const="clv"' in src and 'DIGEST_KINDS = ("clv",)' in src and 'args.digest == "clv"' in src


def test_backtest_from_git_dispatch_replays_the_archive(bt: str):
    """docs/HISTORICAL_BACKTEST_SPEC.md §3.5: a manual `from_git` run needs the whole history,
    the CFBD key and the ERA5 window cache; every other run carries the published historical
    groups forward instead of recomputing them."""
    assert "from_git:" in bt and "seasons:" in bt
    assert "FROM_GIT: ${{ inputs.from_git == true && '1' || '' }}" in bt
    assert "SEASONS: ${{ inputs.seasons || '2024,2025' }}" in bt
    assert "fetch-depth: ${{ inputs.from_git == true && 0 || 1 }}" in bt
    run = _step(bt, "Run backtest")
    assert '${FROM_GIT:+--from-git --seasons "$SEASONS"}' in run
    assert "CFBD_API_KEY: ${{ secrets.CFBD_API_KEY }}" in run
    era5 = _step(bt, "Fetch the ERA5 window cache from R2 (read-only)")
    assert "ERA5_WINDOWS: backtest/era5/windows.parquet" in bt
    assert "if: env.FROM_GIT != ''" in era5
    assert 'npx --yes wrangler@4 r2 object get "$R2_BUCKET/$ERA5_WINDOWS" --file=data/backtest/era5/windows.parquet --remote 2>&1' in era5
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in era5 and "exit 1" in era5
    carry = _step(bt, "Fetch the published backtest.json from R2 (read-only)")
    assert 'npx --yes wrangler@4 r2 object get "$R2_BUCKET/board/backtest.json" --file=data/board/backtest.json --remote 2>&1' in carry
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in carry and "exit 1" in carry
    # the merge that keeps the historical groups lives in pipeline.backtest
    src = (WF_DIR.parents[1] / "pipeline" / "backtest.py").read_text(encoding="utf-8")
    assert "def hist_from_previous" in src and '"--from-git"' in src and '"--seasons"' in src
    assert '"--git-cache"' in src and '"--era5-cache"' in src
    # the upload the ERA5 step depends on is documented for the operator
    setup = (WF_DIR.parents[1] / "site" / "worker" / "SETUP.md").read_text(encoding="utf-8")
    assert "backtest/era5/windows.parquet" in setup and "r2 object put" in setup


def test_backtest_step_order_and_failure_ping(bt: str):
    order = ["Fetch board state from R2 (read-only)", "Fetch the published backtest.json from R2 (read-only)",
             "Fetch the ERA5 window cache from R2 (read-only)", "Export D1 tables",
             "Mirror snapshots from R2 (wrangler, read-only)",
             "Run backtest", "Push backtest to R2", "Archive backtest rows to D1 (closings / stadium_results)",
             "Weekly CLV digest", "Upload backtest artifacts", "Telegram on failure"]
    idx = [bt.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)
    tg = _step(bt, "Telegram on failure")
    assert "if: failure()" in tg and "api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" in tg and "--max-time 15" in tg


def test_backtest_no_git_commit_no_continue_on_error_no_playwright(bt: str):
    for token in ("git commit", "git push", "git pull", "continue-on-error", "playwright install"):
        assert token not in bt, token
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
    assert 'get "backtest/games.parquet" data/backtest/games.parquet' in step
    assert 'get "board/backtest.json" data/board/backtest.json' in step
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in step
    assert "exit 1" in step
    assert "r2 object put" not in cal      # calibrate never writes to R2


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
    assert "if: failure()" in tg and "api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" in tg


def test_promotion_rule_documented_in_config_and_calibrate():
    root = WF_DIR.parents[1]
    cfg = (root / "pipeline" / "model" / "config.py").read_text(encoding="utf-8")
    assert "Phase 6 gate: v2 CLV >= v1 over >=4 weeks" in cfg
    calib = (root / "pipeline" / "calibrate.py").read_text(encoding="utf-8")
    assert "ALERT_MODEL = \\\"v2\\\"" in calib or 'ALERT_MODEL = "v2"' in calib
    assert ">= 4 weeks" in calib
