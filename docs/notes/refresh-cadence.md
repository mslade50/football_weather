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

## Part 2 - Playwright job in parallel (design only, NOT implemented)

Current: `playwright` has `needs: [gate, light]`, measured 150-170s after
light. It runs `pipeline.build --scope odds --books betonline,betcris
--merge-into-r2 --reuse-weather-dir ...`, i.e. a full build (schedule, model,
fair/edges, alerts-less) that first pulls the state light just pushed, then
publishes board/legacy/state/meta itself.

Why it is serialized: BetOnline lines are not an input artifact; they are
scraped inside `build`, merged into `archive_last`/openers/history state, and
published as board JSON. State (`openers`, `history`, `archive_last`, alerts)
round-trips via R2 board/*.json, so the publish must start from light's final
state, and meta.json must be pushed last under `football-refresh`.
`carry_forward_lines` (ODDS_CARRY_MAX_AGE=1h) only carries prior-run lines
for books not scraped; it is not an ingest path for a parallel job.

Proposed split (needs new code, hence not done here):
1. `playwright_scrape` (`needs: gate`): install Chromium, run a new scrape-only
   mode (`main.py --book betonline` / `build --scope scrape-only`) that writes
   verbatim raw capture + parsed `Line` records (with `scraped_at`) to an
   artifact. No R2 writes.
2. `playwright` publish job (`needs: [light, playwright_scrape]`): download the
   artifact, new `build --ingest-lines <dir>` merges those lines instead of
   scraping (raw-first rule: raw payload preserved; parser stays pure), then
   the existing merge-into-R2 + publish sequence unchanged.
Required new work: scrape-only entry point, a lines (de)serializer honoring
`scraped_at` so the 1h freshness/expiry semantics (`current_quotes`) stay
truthful (BetOnline would be ~light-duration older at publish), tests for
staleness, and BetCRIS (also re-scraped in this job for TTL reasons) would
have to stay in the publish job. These touch freshness-critical code, so this
is deferred.

Expected saving if built: the Chromium install/cache restore and the scrape
(roughly 90-120s of the 150-170s) overlap with `light`; the serialized tail
shrinks to fetch state + build + publish (~40-60s). Net wall-clock saving
about 1.5-2 min per full run. Lines would be older by ~light's duration at
publish (several minutes) - acceptable against the 1h TTL but must be
reflected in `scraped_at`.

Rollback for a future implementation: keep the old single-job `playwright`
behind a repo variable and flip back by reverting the YAML + contract tests.
