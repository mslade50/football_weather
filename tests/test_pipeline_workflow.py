"""String-contract tests for .github/workflows/pipeline.yml v3 (convention: golf_scraping/tests/test_board_workflow.py).

Pins (ARCH §9.2 / §13): R2 state get loop fails on anything but NoSuchKey, put loop
pushes meta.json LAST, d1 execute gated by hashFiles, self-check after publish,
playwright job builds with --merge-into-r2, NO git commit/push step remains (Phase 4:
legacy csv/xlsx go to R2 legacy/), no continue-on-error on state steps."""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pipeline.yml"
STATE_FILES = ("openers", "history", "wx_history", "archive_last", "wx_last", "alerts", "scrape_baseline",
               "telegram_state", "cf_heartbeat", "closings", "status")


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(text: str, name: str) -> str:
    start = text.index(f"      - name: {name}\n")
    try:
        end = text.index("\n      - name:", start + 1)
    except ValueError:
        end = len(text)
    return text[start:end]


def _job(text: str, name: str) -> str:
    start = text.index(f"\n  {name}:\n")
    nxt = [text.find(f"\n  {j}:\n", start + 1) for j in ("gate", "light", "playwright")]
    ends = [i for i in nxt if i > start]
    return text[start:min(ends)] if ends else text[start:]


def test_schedule_backstop_is_off_the_minute(text: str):
    assert "'17 9,14,20 * * *'" in text


def test_schedule_in_season_cadence(text: str):
    # ARCH §9.1 mirrored as GitHub crons (site/worker/SETUP.md §7); all off-the-hour.
    import re
    crons = re.findall(r"- cron: '([^']+)'", text)
    assert len(crons) >= 7
    for c in crons:
        assert c.split()[0] in ("17", "47"), c
    assert "'17 10-23 * * 6'" in text   # Sat CFB hourly
    assert "'17 10-21 * * 0'" in text   # Sun NFL hourly (GitHub DOW 0=Sun)


def test_dispatch_inputs(text: str):
    assert "workflow_dispatch:" in text
    for key in ("sport:", "scope:", "force:"):
        assert key in text
    assert "- nfl" in text and "- cfb" in text and "- all" in text
    assert "- weather" in text and "- light" in text and "- full" in text


def test_concurrency_queues_rather_than_cancels(text: str):
    assert "group: football-refresh" in text
    assert "cancel-in-progress: false" in text


def test_cloudflare_env_and_state_file_list(text: str):
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in text
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CF_ACCOUNT_ID }}" in text
    assert "R2_BUCKET: football-board" in text
    assert "D1_DATABASE: football-odds" in text
    assert "STATE_FILES: " + " ".join(STATE_FILES) in text


def test_gate_job_is_httpx_only_and_fail_open(text: str):
    assert "python -m pipeline.gate_check" in text
    gate = _job(text, "gate")
    step = _step(gate, "Kickoff-horizon gate")
    assert "pip install httpx" in gate
    assert "playwright" not in gate.replace("need_playwright", "").replace("NEED", "")
    assert "wrangler" not in gate
    assert "*) RUN=scrape ;;" in step
    assert 'echo "run=$RUN" >> "$GITHUB_OUTPUT"' in step
    assert 'echo "need_playwright=$NEED" >> "$GITHUB_OUTPUT"' in step
    assert '[ "$SCOPE" = "full" ]' in step
    assert '[ "$BOOK_BETONLINE_ENABLED" != "0" ]' in step


def test_light_job_gated_on_scrape_and_has_no_playwright(text: str):
    light = _job(text, "light")
    assert "needs: gate" in light
    assert "if: needs.gate.outputs.run == 'scrape'" in light
    assert "python-version: '3.11'" in light
    assert "cache: 'pip'" in light
    assert "actions/setup-node@v4" in light and "node-version: '20'" in light
    assert "playwright install" not in light
    assert "grep -viE '^(playwright|playwright-stealth" in light
    build = _step(light, "Build board")
    assert 'python -m pipeline.build --sport "$SPORT" --scope "$LIGHT_SCOPE" --print --run-id "$RUN_ID"' in build
    assert "--legacy-dir" not in build   # legacy files stay in data/ and go to R2, never the repo root
    assert '[ "$LIGHT_SCOPE" = "full" ] && LIGHT_SCOPE=light' in build
    assert "${FORCE:+--force}" in build
    assert 'echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-gh${{ github.run_id }}' in _step(light, "Run id")


@pytest.mark.parametrize("name", ["Fetch board state from R2", "Fetch board state from R2 (playwright)"])
def test_r2_state_get_loop_fails_on_non_nosuchkey(text: str, name: str):
    step = _step(text, name)
    assert "for f in $STATE_FILES meta; do" in step
    assert 'DEST="data/state/prev_meta.json"' in step
    assert 'npx --yes wrangler@4 r2 object get "$R2_BUCKET/board/$f.json" --file="$DEST" --remote 2>&1' in step
    assert "grep -qiE 'NoSuchKey|does not exist|not found|404'" in step
    assert "exit 1" in step
    assert "::error::R2 state fetch failed" in step
    assert "continue-on-error" not in step


@pytest.mark.parametrize("name", ["Push to R2", "Push to R2 (playwright)"])
def test_r2_put_loop_pushes_meta_last_with_retries(text: str, name: str):
    step = _step(text, name)
    assert "for i in 1 2 3; do" in step
    assert 'npx --yes wrangler@4 r2 object put "$R2_BUCKET/$1" --file="$2"' in step
    assert "--remote && return 0" in step
    assert "::error::R2 put $1 failed after 3 attempts" in step
    assert 'put "raw/${p#data/raw_runs/}"' in step
    assert 'put "snapshots/${p#data/snapshots/}"' in step
    assert 'put "legacy/nfl_weather.csv" data/nfl_weather.csv "text/csv"' in step
    assert 'put "legacy/cfb_weather.xlsx" data/cfb_weather.xlsx' in step
    assert 'put "board/$f.json" "data/state/$f.json"' in step
    assert '[ "$f" = "meta.json" ] && continue' in step
    body = step.rstrip()
    assert body.endswith('put "board/meta.json" "data/board/meta.json" "application/json"')
    assert (step.index("data/raw_runs") < step.index("data/snapshots") < step.index("legacy/nfl_weather.csv")
            < step.index("data/board/*.json") < step.index("$STATE_FILES") < step.rindex("board/meta.json"))
    assert "continue-on-error" not in step


@pytest.mark.parametrize("name", ["Archive to D1 (change-only)", "Archive to D1 (change-only, playwright)"])
def test_d1_execute_gated_by_hashfiles(text: str, name: str):
    step = _step(text, name)
    assert "if: hashFiles('data/d1_inserts.sql') != ''" in step
    assert 'npx --yes wrangler@4 d1 execute "$D1_DATABASE" --remote --yes --file=data/d1_inserts.sql' in step
    assert "continue-on-error" not in step


@pytest.mark.parametrize("name", ["Self-check published board", "Self-check published board (playwright)"])
def test_self_check_after_publish(text: str, name: str):
    step = _step(text, name)
    assert 'r2 object get "$R2_BUCKET/board/meta.json" --file=data/check/meta.json --remote' in step
    assert 'python -m pipeline.outputs.r2 --self-check --run-id "$RUN_ID"' in step
    assert "--meta-file data/check/meta.json --prev-meta data/state/prev_meta.json ${FORCE:+--force}" in step
    assert "continue-on-error" not in step


def test_light_step_order(text: str):
    light = _job(text, "light")
    order = ["Fetch board state from R2", "Build board", "Push to R2",
             "Archive to D1 (change-only)", "Self-check published board", "Upload build logs", "Telegram on failure"]
    idx = [light.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)


def test_playwright_job_runs_betonline_odds_scope_and_merges_into_r2(text: str):
    pw = _job(text, "playwright")
    assert "needs: [gate, light]" in pw
    assert "needs.gate.outputs.need_playwright == 'true'" in pw
    assert "ref: main" not in pw   # no light-job commit to pick up any more
    assert "python -m playwright install --with-deps chromium" in pw
    build = _step(pw, "Build board (BetOnline)")
    assert 'python -m pipeline.build --sport "$SPORT" --scope odds --books betonline --print --run-id "$RUN_ID" --merge-into-r2' in build
    assert "contents: write" not in pw
    order = ["Fetch board state from R2 (playwright)", "Build board (BetOnline)",
             "Push to R2 (playwright)", "Archive to D1 (change-only, playwright)", "Self-check published board (playwright)"]
    idx = [pw.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)
    assert "if: failure()" in pw


def test_no_git_commit_step_remains(text: str):
    # Phase 4: nothing is committed back to the repo. Legacy csv/xlsx ride R2 legacy/.
    for token in ("git commit", "git push", "git pull", "git add", "git config", "git rebase", "Commit legacy files"):
        assert token not in text, token
    assert "--legacy-dir ." not in text
    assert "contents: write" not in text


def test_legacy_files_uploaded_to_r2_legacy_prefix(text: str):
    for name in ("Push to R2", "Push to R2 (playwright)"):
        step = _step(text, name)
        assert 'put "legacy/nfl_weather.csv" data/nfl_weather.csv "text/csv"' in step, name
        assert ('put "legacy/cfb_weather.xlsx" data/cfb_weather.xlsx '
                '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"') in step, name
        # legacy/ lands before board payloads so a mid-loop failure never leaves new meta over old legacy
        assert step.index("legacy/nfl_weather.csv") < step.index("data/board/*.json"), name


def test_state_steps_never_continue_on_error(text: str):
    assert "continue-on-error" not in text


def test_telegram_on_failure(text: str):
    for name in ("Telegram on failure", "Telegram on failure (playwright)"):
        step = _step(text, name)
        assert "if: failure()" in step
        assert "api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" in step
        assert "--max-time 15" in step


def test_read_only_contents_permission(text: str):
    for job in ("light", "playwright"):
        assert "contents: read" in _job(text, job), job
    assert "contents: write" not in text
    assert "CFBD_API_KEY: ${{ secrets.CFBD_API_KEY }}" in text


def test_no_legacy_files_at_repo_root():
    root = WORKFLOW.parents[2]
    for name in ("nfl_weather.csv", "cfb_weather.xlsx", "cfb_weather_backtest.xlsx", "app.py"):
        assert not (root / name).exists(), f"{name} must not live at the repo root (fixtures: tests/fixtures/legacy/)"
    assert not (root / "pages").exists()
    for name in ("nfl_weather.csv", "cfb_weather.xlsx"):
        assert (root / "tests" / "fixtures" / "legacy" / name).is_file()
