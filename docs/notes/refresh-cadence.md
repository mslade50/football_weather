# Refresh cadence: Worker-primary dispatch and Playwright parallelism

## Part 1 - Worker cron as primary trigger (implemented)

Problem: GitHub drops `schedule:` ticks (Sat 2026-10-10: 5 runs vs an hourly
schedule). The Worker already had a free-plan pair of cron triggers.

Design
- No new trigger (free plan allows 2 here). The existing `*/30 * * * *`
  heartbeat tick now also calls `shouldDispatch(date)` (pure, in
  `site/worker/index.js`): only exact `:00` ticks inside fixed UTC windows
  dispatch `pipeline.yml` with `sport/scope=full`:
  - CFB: Sat 15:00Z .. Sun 04:00Z (`all` in Dec/Jan, none in Feb)
  - NFL: Sun 15:00Z .. Mon 01:00Z; Thu night 22Z..Fri 02Z; Mon night 22Z..Tue 02Z
  - Season by ET month (`sportForMonth`); offseason (Mar-Jul) never dispatches.
  Windows are fixed UTC, so DST needs no handling (kickoffs shift 1h in ET;
  `gate_check` trims windows with no kickoff within the horizon).
- The 17:15Z mid-day cron is suppressed on days where the 17:00Z tick already
  dispatched.
- Failures: existing path (`dispatchBoard` never throws, logs status, writes
  `board/cf_dispatch.json` receipt, optional Telegram SYSTEM notice).

Double-run avoidance
1. GitHub `schedule:` thinned to a backstop (~every 3-4h on game days, ~45
   ticks/week vs ~100). It is no longer a competing hourly source.
2. New gate step: on `schedule` events only, if a `workflow_dispatch` run
   (Worker or manual) was created in the last 50 min and did not fail/cancel,
   the gate is skipped (`run` empty -> light/playwright skip). Fail-open on any
   API error.
3. `concurrency: football-refresh` (queue, no cancel) still serializes writers.
`pipeline/outputs/json_out.py` `next_run_eta` mirrors both sources (pinned by
tests, including a JS-window mirror test).

Requires: `GH_DISPATCH_TOKEN` Worker secret (already needed for the 17:15
dispatch). Redeploy the Worker (`npx wrangler deploy`) for the cron logic to go
live; merging alone changes only the GitHub schedule.

Rollback: revert the commit (restores the GitHub schedule and the Worker plan
returns `null` for heartbeats). If only the Worker misbehaves, unset
`GH_DISPATCH_TOKEN`; the thin backstop then still runs.
Caveat: if the Worker secret is missing, game-day cadence drops to the 3h
backstop (a SYSTEM Telegram notice fires when TELEGRAM_SYSTEM_ALERTS=1).

## Part 2 - Playwright job in parallel (implemented)

Before: `playwright` had `needs: [gate, light]` and re-ran a full build that
launched Chromium to scrape BetOnline (+ BetCRIS), adding 151-184s after light
(8 production runs, 2026-10-15/16).

Now (`.github/workflows/pipeline.yml`):
1. `playwright_scrape` (`needs: gate`, parallel to `light`): installs Chromium
   and runs `python -m pipeline.build --scrape-only data/pw-lines --books
   betonline --run-id <...>-pwscrape`. Raw captures go to `data/raw_runs/` as
   usual (raw-first, parser untouched); parsed lines go to
   `data/pw-lines/{sport}/betonline.json` (`pipeline/odds/handoff.py`, schema v1:
   every `GameLine` field incl. the original `scraped_at`, the scraper's raw
   games for merge, and its `fetch_errors`). Uploaded as the
   `pw-lines-<run>-<attempt>` artifact (1-day retention). No state, no secrets,
   no R2/D1 writes.
2. `playwright` publish job (`needs: [gate, light, playwright_scrape]`, runs
   when light succeeded and the scrape job succeeded OR failed): no Chromium
   (light deps), downloads the artifact into `data/`, then the unchanged
   sequence: fetch R2 state -> `build --scope odds --books betonline,betcris
   --merge-into-r2 --ingest-lines data/pw-lines` -> push raw (incl. the scrape
   job's captures) / snapshots / legacy / board / state -> meta.json last ->
   D1 -> self-check -> notify. Still under `concurrency: football-refresh`.

Freshness: `--ingest-lines` reads BetOnline lines with their original
`scraped_at` (archive_last `ts`, `ODDS_CARRY_MAX_AGE`, `current_quotes` and the
`scraped_at + 1h` expiry all see the true age). At ingest, lines older than
`ODDS_CARRY_MAX_AGE`, already expired, undated, or more than 2 min in the future
are dropped with an `odds.ingest` warn degradation. A missing/corrupt handoff
reads exactly like a failed in-build scrape: BetOnline 0 lines, its previous
archive_last quotes withdrawn, book_health records the error. BetCRIS stays a
live scrape in the publish job (short college TTL). Ingested BetOnline lines
are ~one light job old at publish (2.5-9 min observed light duration), well
inside the 1h window.

Saving (measured components, same 8 runs): Chromium/deps install ~36s and the
BetOnline+BetCRIS scrape stages 18-42s leave the serialized tail; BetCRIS alone
(httpx) and the artifact download (~2-3s) stay. The scrape job itself (~70-100s
incl. setup) is shorter than every observed light job (146-549s), so it is
never on the critical path. Estimated net: ~50-75s per full run (tail
~150-185s -> ~100-120s). The earlier 1.5-2 min estimate assumed a slower
Chromium install than the cached path shows.

Rollback: revert the commit (restores the single serialized `playwright` job
and its contract tests). `--scrape-only` / `--ingest-lines` are inert unless
passed, so the build-side code can stay.
