"""String-contract tests for .github/workflows/pipeline.yml v3 (convention: golf_scraping/tests/test_board_workflow.py).

Pins (ARCH §9.2 / §13): R2 state get loop fails on anything but NoSuchKey, put loop
pushes meta.json LAST, d1 execute gated by hashFiles, self-check after publish,
playwright_scrape runs parallel to light and hands lines to the playwright publish
job (--ingest-lines --merge-into-r2), NO git commit/push step remains (Phase 4:
legacy csv/xlsx go to R2 legacy/), no continue-on-error on state steps."""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pipeline.yml"
STATE_FILES = ("openers", "history", "wx_history", "archive_last", "wx_last", "alerts", "scrape_baseline",
               "telegram_state", "cf_heartbeat", "closings", "status", "ensemble_cache", "nws_points")


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
    nxt = [text.find(f"\n  {j}:\n", start + 1) for j in ("gate", "light", "playwright_scrape", "playwright", "notify_failure")]
    ends = [i for i in nxt if i > start]
    return text[start:min(ends)] if ends else text[start:]


def test_schedule_backstop_is_off_the_minute(text: str):
    assert "'17 9 * * *'" in text


def test_schedule_in_season_cadence(text: str):
    # ARCH §9.1 mirrored as GitHub crons (site/worker/SETUP.md §7); all off-the-hour.
    import re
    crons = re.findall(r"- cron: '([^']+)'", text)
    assert len(crons) >= 5
    for c in crons:
        assert c.split()[0] in ("17", "47"), c
    # Sparse backstop only: the Worker owns the hourly game-day cadence.
    assert "'17 15,18,21 * * 6'" in text      # Sat CFB every 3h
    assert "'17 0,3,15,18,21 * * 0'" in text  # Sun NFL every 3h (GitHub DOW 0=Sun)


def test_backstop_defers_to_recent_worker_dispatch(text: str):
    gate = _job(text, "gate")
    step = _step(gate, "Skip backstop tick after a recent dispatch")
    assert "github.event_name == 'schedule'" in step
    assert "event=workflow_dispatch" in step and "100 minutes ago" in step
    assert 'test("sport="' in step and 'test("sport=all( |$)"' in step
    assert "continue-on-error: true" in step     # fail-open
    assert "steps.recent.outputs.skip != 'true'" in _step(gate, "Kickoff-horizon gate")
    assert "cancel-in-progress: false" in text   # overlap queues, never cancels


def test_dispatch_inputs(text: str):
    assert "workflow_dispatch:" in text
    for key in ("sport:", "scope:", "force:"):
        assert key in text
    assert "- nfl" in text and "- cfb" in text and "- all" in text
    assert "- weather" in text and "- light" in text and "- full" in text
    assert "- exchanges" in text
    assert "ecmwf_opendata:" in text
    assert "ECMWF_OPENDATA_FALLBACK: ${{ inputs.ecmwf_opendata || 'auto' }}" in text
    assert "ECMWF_OD_MAX_LEAD_H: ${{ vars.ECMWF_OD_MAX_LEAD_H || '168' }}" in text


def test_safe_refresh_disables_messages_without_force_or_cadence_changes(text: str):
    assert "safe_refresh:" in text
    assert "TELEGRAM_DISABLED: ${{ inputs.safe_refresh == true && '1' || '0' }}" in text
    assert "inputs.safe_refresh != true" in _job(text, "notify_failure")
    assert "FORCE: ${{ inputs.force == true && '1' || '' }}" in text


def test_concurrency_queues_rather_than_cancels(text: str):
    assert "group: football-refresh" in text
    assert "cancel-in-progress: false" in text


def test_cloudflare_env_and_state_file_list(text: str):
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in text
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CF_ACCOUNT_ID }}" in text
    assert "R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}" in text
    assert "R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}" in text
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


def test_ecmwf_sample_cache_is_restored_across_runs(text: str):
    light = _job(text, "light")
    playwright = _job(text, "playwright")
    light_step = _step(light, "Restore ECMWF open-data field cache")
    pw_step = _step(playwright, "Restore ECMWF open-data field cache (playwright)")
    for step in (light_step, pw_step):
        assert "path: data/cache/ecmwf/samples" in step
        assert "actions/cache@v4" in step
        assert "continue-on-error: true" in step
        assert "ecmwf-samples-${{ runner.os }}-" in step
    assert "key: ecmwf-samples-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}" in light_step
    assert "key: ecmwf-samples-pw-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}" in pw_step
    assert light.index("Restore ECMWF open-data field cache") < light.index("name: Build board")
    assert playwright.index("Restore ECMWF open-data field cache (playwright)") < playwright.index("Build board (BetOnline)")


def test_light_job_gated_on_scrape_and_has_no_playwright(text: str):
    light = _job(text, "light")
    assert "needs: gate" in light
    assert "if: needs.gate.outputs.run == 'scrape'" in light
    assert "python-version: '3.11'" in light
    assert "cache: 'pip'" in light
    assert "path: ~/.npm" in light and "npm-wrangler-4" in light
    assert "actions/setup-node@v4" in light and "node-version: '20'" in light
    assert "playwright install" not in light
    assert "grep -viE '^(playwright|playwright-stealth" in light
    build = _step(light, "Build board")
    assert 'python -m pipeline.build --sport "$SPORT" --scope "$LIGHT_SCOPE" --print --no-alerts --run-id "$RUN_ID"' in build
    assert "--legacy-dir" not in build   # legacy files stay in data/ and go to R2, never the repo root
    assert '[ "$LIGHT_SCOPE" = "full" ] && LIGHT_SCOPE=light' in build
    assert "${FORCE:+--force}" in build
    assert 'echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-gh${{ github.run_id }}' in _step(light, "Run id")


@pytest.mark.parametrize("name", ["Fetch board state from R2", "Fetch board state from R2 (playwright)"])
def test_r2_state_fetch_fails_on_non_nosuchkey(text: str, name: str):
    step = _step(text, name)
    # The fail-closed loop (NoSuchKey -> empty, anything else -> exit 1, ::error:: message) lives in
    # scripts/fetch_r2.py and is pinned by tests/test_fetch_r2.py; the step must be a bare call.
    assert "run: python scripts/fetch_r2.py state --dest data/state" in step
    assert "|| true" not in step and "continue-on-error" not in step
    assert "wrangler" not in step


@pytest.mark.parametrize("name", ["Fetch wind-signal history", "Fetch wind-signal history (playwright)"])
def test_alert_history_is_read_only_optional_context(text: str, name: str):
    step = _step(text, name)
    assert "python scripts/fetch_r2.py object board/backtest.json data/state/backtest.json" in step
    assert "--warn-message 'Wind history unavailable; alerts will label it unavailable'" in step
    assert "python scripts/fetch_r2.py object backtest/wind-history-v1.json data/state/wind-history-v1.json" in step
    assert "--warn-message 'Historical wind archive unavailable; alerts will label the coverage gap'" in step
    assert step.index("wind-history-v1.json") < step.index("board/backtest.json")
    assert "r2 object put" not in step and "wrangler" not in step


@pytest.mark.parametrize("name", ["Push to R2", "Push to R2 (playwright)"])
def test_r2_publisher_is_bounded_and_meta_remains_last(text: str, name: str):
    step = _step(text, name)
    phases = ["python scripts/publish_r2.py raw", "python scripts/publish_r2.py snapshots",
              "python scripts/publish_r2.py legacy", "python scripts/publish_r2.py board",
              "python scripts/publish_r2.py state", "python scripts/publish_r2.py meta"]
    assert all(phase in step for phase in phases)
    assert [step.index(phase) for phase in phases] == sorted(step.index(phase) for phase in phases)
    assert "python -m pipeline.current_quotes" in step
    assert "--meta-file data/board/meta.json --prev-meta data/state/prev_meta.json" in step
    assert step.index("python -m pipeline.current_quotes") < step.index("python scripts/publish_r2.py board")
    assert step.index("python scripts/publish_r2.py state") < step.index("python scripts/publish_r2.py meta")
    assert "PUBLISH_GUARD_ELAPSED" in step and "-gt 80" in step
    assert "refusing to write the commit marker" in step
    assert "continue-on-error" not in step


@pytest.mark.parametrize("name", ["Notify published board", "Notify published board (playwright)"])
def test_notifications_follow_published_self_check(text: str, name: str):
    job_name = "playwright" if name.endswith("(playwright)") else "light"
    job = _job(text, job_name)
    assert job.index("Self-check published board") < job.index(name)
    assert "pipeline.published_alerts" in _step(job, name)
    assert "TELEGRAM_MAX_LEAD_DAYS" in _step(job, name)
    assert "inputs.scope || 'full') != 'weather'" in _step(job, name)


def test_failed_run_receipts_recover_before_build_only_in_light_job(text: str):
    light = _job(text, "light")
    recovery = _step(light, "Recover receipts from recent failed runs")
    assert "actions: read" in light
    assert "GH_TOKEN: ${{ github.token }}" in recovery
    assert light.index("Fetch wind-signal history") < light.index("Recover receipts from recent failed runs")
    assert light.index("Recover receipts from recent failed runs") < light.index("Build board")
    assert "Recover receipts from recent failed runs" not in _job(text, "playwright")


@pytest.mark.parametrize("name", ["Archive to D1 (change-only)", "Archive to D1 (change-only, playwright)"])
def test_d1_execute_gated_by_hashfiles(text: str, name: str):
    step = _step(text, name)
    assert "if: hashFiles('data/d1_inserts.sql') != ''" in step
    assert 'npx --yes wrangler@4 d1 execute "$D1_DATABASE" --remote --yes --file=data/d1_inserts.sql' in step
    assert "continue-on-error" not in step


@pytest.mark.parametrize("name", ["Self-check published board", "Self-check published board (playwright)"])
def test_self_check_after_publish(text: str, name: str):
    step = _step(text, name)
    assert "python scripts/fetch_r2.py object board/meta.json data/check/meta.json\n" in step
    assert "--warn-message" not in step   # the self-check fetch is strict: any failure fails the step
    assert 'python -m pipeline.outputs.r2 --self-check --run-id "$RUN_ID"' in step
    assert "--meta-file data/check/meta.json --verify-generation --prev-meta data/state/prev_meta.json ${FORCE:+--force}" in step
    assert "continue-on-error" not in step


def test_light_step_order(text: str):
    light = _job(text, "light")
    order = ["Fetch board state from R2", "Build board", "Push to R2",
             "Archive to D1 (change-only)", "Self-check published board", "Upload build logs"]
    idx = [light.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)


def test_playwright_scrape_runs_parallel_to_light_without_state_or_r2(text: str):
    job = _job(text, "playwright_scrape")
    assert "    needs: gate\n" in job          # NOT light: the scrape overlaps the light job
    assert "needs.gate.outputs.need_playwright == 'true'" in job
    assert "python -m playwright install --with-deps chromium" in job
    assert "path: ~/.cache/ms-playwright" in job
    assert "key: ${{ runner.os }}-ms-playwright-${{ steps.pw-version.outputs.version }}" in job
    assert "python -m playwright install-deps chromium" in job
    assert "steps.pw-cache.outputs.cache-hit != 'true'" in job
    scrape = _step(job, "Scrape BetOnline (scrape-only)")
    assert 'python -m pipeline.build --sport "$SPORT" --scrape-only data/pw-lines --books betonline --run-id "$SCRAPE_RUN_ID"' in scrape
    assert "-pwscrape" in scrape   # raw dir distinct from the publish job's -pw run
    upload = _step(job, "Upload BetOnline handoff")
    assert "name: pw-lines-${{ github.run_id }}-${{ github.run_attempt }}" in upload
    assert "data/pw-lines/" in upload and "data/raw_runs/" in upload   # raw-first: captures travel too
    assert "retention-days: 1" in upload
    # Scrape-only: never touches state, R2, D1 or Telegram, and holds no secrets.
    for token in ("fetch_r2.py", "publish_r2.py", "wrangler", "--merge-into-r2", "secrets.", "TELEGRAM", "contents: write"):
        assert token not in job, token


def test_playwright_publish_job_ingests_handoff_after_light(text: str):
    pw = _job(text, "playwright")
    assert "needs: [gate, light, playwright_scrape]" in pw
    assert "needs.gate.outputs.need_playwright == 'true'" in pw
    assert "needs.light.result == 'success'" in pw
    # A failed scrape still publishes (BetOnline 0 lines + degradation); a skipped/cancelled one does not.
    assert "needs.playwright_scrape.result == 'success' || needs.playwright_scrape.result == 'failure'" in pw
    assert "always()" in pw
    assert "ref: main" not in pw
    # No Chromium in the publish job any more.
    assert "playwright install" not in pw
    assert "ms-playwright" not in pw
    assert "grep -viE '^(playwright|playwright-stealth" in pw
    download = _step(pw, "Download BetOnline handoff")
    assert "name: pw-lines-${{ github.run_id }}-${{ github.run_attempt }}" in download
    assert "path: data/" in download
    assert "continue-on-error: true" in download
    build = _step(pw, "Build board (BetOnline)")
    assert ('python -m pipeline.build --sport "$SPORT" --scope odds --books betonline,betcris --print --no-alerts '
            '--run-id "$RUN_ID" --merge-into-r2 --ingest-lines data/pw-lines') in build
    assert 'ECMWF_OD_CACHE_ONLY: "1"' in build
    assert "ECMWF_OD_CACHE_ONLY" not in _step(_job(text, "light"), "Build board")
    assert "contents: write" not in pw
    order = ["Download BetOnline handoff", "Fetch board state from R2 (playwright)", "Build board (BetOnline)",
             "Push to R2 (playwright)", "Archive to D1 (change-only, playwright)", "Self-check published board (playwright)"]
    idx = [pw.index(f"- name: {n}\n") for n in order]
    assert idx == sorted(idx)
    assert "Telegram on failure" not in pw


def test_publish_job_pushes_handoff_raw_captures_before_meta(text: str):
    push = _step(_job(text, "playwright"), "Push to R2 (playwright)")
    # The downloaded data/raw_runs/ (scrape job captures) ride the raw phase; meta stays last.
    assert push.index("python scripts/publish_r2.py raw") < push.index("python scripts/publish_r2.py meta")
    assert push.rstrip().endswith("python scripts/publish_r2.py meta")


def test_no_git_commit_step_remains(text: str):
    # Phase 4: nothing is committed back to the repo. Legacy csv/xlsx ride R2 legacy/.
    for token in ("git commit", "git push", "git pull", "git add", "git config", "git rebase", "Commit legacy files"):
        assert token not in text, token
    assert "--legacy-dir ." not in text
    assert "contents: write" not in text


def test_legacy_files_uploaded_to_r2_legacy_prefix(text: str):
    for name in ("Push to R2", "Push to R2 (playwright)"):
        step = _step(text, name)
        assert "python scripts/publish_r2.py legacy" in step, name
        # legacy/ lands before board payloads so a mid-loop failure never leaves new meta over old legacy
        assert step.index("python scripts/publish_r2.py legacy") < step.index("python scripts/publish_r2.py board"), name


def test_state_steps_never_continue_on_error(text: str):
    for name in ("Fetch board state from R2", "Fetch board state from R2 (playwright)", "Push to R2", "Push to R2 (playwright)"):
        assert "continue-on-error" not in _step(text, name)


def test_same_workflow_raw_weather_handoff(text: str):
    upload = _step(text, "Upload same-workflow weather inputs")
    download = _step(text, "Download same-workflow weather inputs")
    assert "weather-points-${{ github.run_id }}-${{ github.run_attempt }}" in upload
    assert "weather-points-${{ github.run_id }}-${{ github.run_attempt }}" in download
    assert "--weather-handoff-dir data/weather-points" in _step(text, "Build board")
    assert "--reuse-weather-dir data/weather-reuse" in _step(text, "Build board (BetOnline)")
    assert "retention-days: 1" in upload
    assert "continue-on-error: true" in download


def test_cron_backstop_has_no_duplicate_ticks_and_is_thinner(text: str):
    import re
    from collections import Counter

    def numbers(field, last):
        values = set()
        for part in field.split(","):
            span, _, step = part.partition("/")
            bounds = (0, last) if span == "*" else tuple(map(int, span.split("-"))) if "-" in span else (int(span), int(span))
            values.update(range(bounds[0], bounds[1] + 1, int(step or "1")))
        return values

    result = Counter()
    for cron in re.findall(r"- cron: '([^']+)'", text):
        minute, hour, _, _, day = cron.split()
        for d in numbers(day, 6):
            for h in numbers(hour, 23):
                for m in numbers(minute, 59):
                    result[d, h, m] += 1
    assert max(result.values()) == 1
    # Never hourly: no two scheduled ticks closer than 2h apart on game days (Sat/Sun).
    for d in (6, 0):
        hours = sorted(h for (dd, h, _m) in result if dd == d)
        assert all(b - a >= 2 or b - a == 0 for a, b in zip(hours, hours[1:], strict=False)), (d, hours)
    assert len(result) <= 45  # was 100+ ticks/week before the Worker became primary


def test_telegram_on_failure(text: str):
    job = _job(text, "notify_failure")
    step = _step(job, "Telegram on failure or recovery")
    assert "needs: [gate, light, playwright_scrape, playwright]" in job
    # Unconditional at the job: a skipped Playwright job must not skip the page.
    assert "\n    if: always()\n" in job
    assert "!cancelled()" in step
    assert "inputs.safe_refresh != true" in step
    assert "env.FOOTBALL_FAILURE_ALERTS != '0'" in step
    assert "FOOTBALL_TELEGRAM_ENABLED" not in job
    assert "python -m pipeline.failure_page plan" in step
    assert "python -m pipeline.failure_page commit" in step
    assert 'GATE_RESULT: ${{ needs.gate.result }}' in step
    assert 'LIGHT_RESULT: ${{ needs.light.result }}' in step
    assert 'PLAYWRIGHT_RESULT: ${{ needs.playwright.result }}' in step
    assert 'PLAYWRIGHT_SCRAPE_RESULT: ${{ needs.playwright_scrape.result }}' in step
    assert "TELEGRAM_SYSTEM_ALERTS: ${{ vars.TELEGRAM_SYSTEM_ALERTS || '0' }}" in step
    # The marker is stored only after the send succeeds.
    assert step.index('sendMessage') < step.index('pipeline.failure_page commit')
    assert "api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" in step
    assert "--max-time 15" in step
    assert "FOOTBALL_FAILURE_ALERTS: ${{ vars.FOOTBALL_FAILURE_ALERTS || '1' }}" in text


def test_novig_nbx_secrets_reach_only_the_light_build(text: str):
    job = _job(text, "light")
    assert "NOVIG_CLIENT_ID: ${{ secrets.NOVIG_CLIENT_ID }}" in job
    assert "NOVIG_CLIENT_SECRET: ${{ secrets.NOVIG_CLIENT_SECRET }}" in job
    assert "NOVIG_CLIENT" not in _job(text, "playwright_scrape")


def test_read_only_contents_permission(text: str):
    for job in ("light", "playwright_scrape", "playwright"):
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
