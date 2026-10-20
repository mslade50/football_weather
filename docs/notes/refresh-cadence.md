# Refresh cadence: Worker-primary dispatch and Playwright parallelism

## Part 3 - All-week 2h ET cadence (implemented)

Problem: GitHub dropped the Mon 2026-10-19 09:17Z and 14:17Z `schedule` ticks
entirely (no workflow run). The 00:17Z Monday backstop was delayed until
04:36Z and did publish. The Worker heartbeat only dispatched inside game-day
windows, so Monday daytime had no reliable trigger. McKinley needs fresh odds
and forecasts about every 2 hours from 7 AM through midnight ET, all week,
especially early in the week.

Design (no new cron; free plan still has the same two triggers):
- `refreshSlotPlan` in `site/worker/index.js`: odd America/New_York hours
  7, 9, 11, 13, 15, 17, 19, 21, 23, in season (`sportForMonth`), `scope=full`.
  The existing `*/30` heartbeat dispatches those `:00` ticks as
  `sportForMonth` (both leagues, Aug–Jan) even when `shouldDispatch` would
  have sent only `nfl` or `cfb`. Non-slot `:00` ticks inside a game window
  stay single-sport.
- Catch-up: a `:30` tick inside 7 AM–11 PM ET dispatches when the last
  accepted `board/cf_dispatch.json` receipt is at least 150 minutes old, when
  a failed attempt is at least 90 minutes old, or when that receipt names a
  single sport and is at least 90 minutes old. A missing receipt does
  not catch up, so a failed R2 read cannot dispatch every half hour.
- Dedupe: an accepted dispatch newer than 90 minutes suppresses a slot only
  when it covers that slot's sport (`all` covers both; `nfl` does not). A
  receipt with no sport still suppresses, so an older Worker does not
  double-fire during the deploy. The 17:15Z midday duplicate uses the same
  90-minute gap. An in-progress pipeline run suppresses the slot and the
  catch-up (`boardRunActive`). Game-day hourly ticks on non-slot hours are
  unchanged. The GitHub backstop gate skips when a `workflow_dispatch` whose
  title is `sport=all`, or whose title does not name a sport, started within
  100 minutes. `sport=nfl` and `sport=cfb` do not skip the all-sport backstop.
- `next_run_eta` advertises the sooner of the next game-day hour and the next
  ET slot. It does not advertise the GitHub backstop during the season.
- `/api/status` `refresh` and `meta.refresh` are `ok`, `overdue` (ETA + 90
  min, no newer publish), or `missed` (dispatch failed, or accepted and still
  unpublished after 30 min). The header and the status pill show that state.

Rollback: revert the commit. The two wrangler crons are unchanged. Unset
`GH_DISPATCH_TOKEN` to stop Worker dispatches; the GitHub backstop remains.

### Open-Meteo weight at this cadence

Published free-tier limits are 10,000 weighted calls/day, 5,000/hour, and
600/minute. A call with many variables, models, or ensemble members counts as
more than one call. `pipeline/weather/rate_limit.py` `query_weight` is that
formula: locations × max(1, variables × models-or-members / 10), and a window
under 14 days does not multiply further. The weather window is 10 days, so
measured requests stayed in the flat band. `static/meta.json` is not forecast
quota. The publish job reuses the light-job point handoff, so a pipeline run
is billed once, on light.

Weights below were computed from the forecast and ensemble URLs in successful
light-job logs. GEFS member HTTP was missing on the both-league runs because
verification failed (dataset stale, or still inside the provider's 10-minute
settling window). The GEFS column is those same locations at 31/51 of the IFS
weight, which is the cost when both models succeed.

| Run | Board | Points | IFS members | GEFS | All models |
| --- | --- | ---: | ---: | ---: | ---: |
| [39200298913](https://github.com/mslade50/football_weather/actions/runs/39200298913) Mon 2026-10-19 04:36Z | NFL+CFB, 15+60 locations | 60.5 | 137.7 (9 locations) | 83.7 | 282 |
| [39055629976](https://github.com/mslade50/football_weather/actions/runs/39055629976) Sat 2026-10-17 17:52Z | NFL+CFB, 23+96 locations | 152.0 | 290.7 (19 locations) | 176.7 | 619 |
| [39143062710](https://github.com/mslade50/football_weather/actions/runs/39143062710) Sun 2026-10-18 16:00Z | NFL only | 38.5 | 76.5 | 9.3 (one location fetched) | 124 |
| [39147611746](https://github.com/mslade50/football_weather/actions/runs/39147611746) Sun 2026-10-18 17:00Z | NFL only, next hour | 41.0 | 0 | 0 | 41 |

Steady-state dispatches that spend quota, October EDT. Odd ET hours are
both-league even when they sit inside a game window. The other game-day `:00`
ticks stay single-sport. The 17:15Z midday cron is inside the 90-minute
dedupe. The daily 09:17Z GitHub backstop is more than 100 minutes after the
11 PM ET slot, so it does run (`sport=all`).

Slot hours are both-league even inside a game window, so a Monday-night or
Saturday-afternoon hourly tick cannot leave the other league stale. Counted
in October EDT, with the 09:17Z backstop included when it is more than 100
minutes after the last both-league run:

- Tue/Wed/Fri: 10 both-league runs (9 slots + the backstop).
- Mon/Thu: 10 both-league + 3 NFL (6 PM, 8 PM, 10 PM ET stay NFL-only).
- Sat: 10 both-league + 7 CFB (even ET hours inside the Saturday window).
- Sun: 10 both-league + 5 NFL + the 04:00Z CFB tail.

Member pulls are already cached by verified dataset version
(`ensemble_cache.json` in the R2 state set). The Sunday 17:00Z run paid 0
member weight one hour after 16:00Z paid 85.8. ECMWF and GFS update about
every 6 hours, so a day pays about four member cycles. Point forecasts are
fetched on every run; NWS covers most CONUS games inside seven days, which is
why the Monday both-league point bill was 60.5 rather than a full-stadium pull.

Projected weighted calls per ET day when that cache holds and both ensembles
succeed:

- Early-week board (Monday's slate), 10 both-league runs: 10 × 60.5 points + 4 × 221 member cycles ≈ **1,500**.
- Heavy 10-day slate (Saturday's board) on a 10-run weekday: 10 × 152 + 4 × 467 ≈ **3,400**.
- Heavy Saturday with the cache holding: 10 both-league point pulls at the measured 152, plus about four member cycles, 10 × 152 + 4 × 467 ≈ **3,400**. The 7 CFB-only hours add point weight and no extra member cycle while the cache hits.
- Same heavy Saturday if the member cache misses on every run: 10 × 619 for the both-league hours + 7 × 350 for the CFB-only hours ≈ **8,600**. 350 is the earlier CFB-only miss estimate, not a new measurement.

The largest measured run is 619, under 5,000/hour and 600/minute. Local
ceilings stay 4,500/hour and 500/minute, and the concurrency group runs one
pipeline at a time. Making the odd ET hours both-league adds the other
league's locations on those hours; it does not add a second run, and it does
not change the per-run member cache. The cache-miss Saturday above is about
8,600 weighted calls, under the 10,000/day published cap, with about 1,400
left. A normal early-week day stays near the 10 both-league projection
(about 1,500) plus three short NFL-only hours. Odds stay on the 2-hour
cadence for both leagues. Member forecasts stay on the model-run cache that
is already shipping. The published non-commercial term of the free tier is
unchanged.

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
