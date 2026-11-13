# Observation log: cached production shadow

`OBSLOG_SHADOW_PATH` enables advisory receipts in the light and Playwright publish
builds. The parallel `playwright_scrape` job has no log or state. Pricing, alerts,
published board files and D1 generation never read this SQLite database. Appends
are atomic; queries are read-only. SQLite files and sidecars are ignored by git.

## Live workflow and cache lifecycle

The exact cached file is `data/cache/obslog/observations.sqlite`. Split
`actions/cache/restore@v4` and `actions/cache/save@v4` use immutable keys
`obslog-v2-<runner.os>-<run_id>-<run_attempt>-<job>`. Light falls back to the
`obslog-v2-<runner.os>-` prefix (newest entry). Playwright waits for light and
first tries the full light key from the **same run and attempt**, then that
prefix. Thus it includes light's rows whenever light's best-effort save succeeds,
independently of any older Playwright cache. Workflow concurrency
`football-refresh` serializes runs. A cache service/save outage can lose evidence;
it cannot affect publication. A rerun may start with already-seen receipts;
identical appends are idempotent.

A cold/evicted cache starts empty. Prepare performs `PRAGMA quick_check`; corrupt
files and sidecars are renamed beside the database to `*.corrupt-<UTC>.sqlite`,
excluded from the cache. Hook recovery records a warn degradation; workflow
prepare emits an Actions warning. Unknown future schema versions are rejected,
not discarded. All evidence/report/cache steps are best effort. Database contents
persist **only in Actions cache**, never a state group, upload manifest, R2 object,
D1 table or migration. JSON parity evidence is in the existing build-log artifact.

After parity, `python -m pipeline.obslog.maintain --path PATH --keep-days 2
--max-mib 192` deletes observations only when both the receipt and latest known
game kickoff are more than two days old. Recent corrections and postponed games
survive. Unreferenced old run receipts are removed, then SQLite runs `VACUUM`.
A two-day tail limits completed-game evidence; it does not bound active games in
the 45-day odds horizon. If the vacuumed file still exceeds **192 MiB**, upkeep
explicitly evicts the entire disposable evidence cache and creates an empty log,
with an Actions warning. Parity runs first. This makes subsequent late-start
classifications honest and prevents unbounded cache growth. Inspect cache bytes
and eviction warnings before extending retention; frequent rotation means the
storage design must change before cutover.

## Schema and identity

`quote_observations` stores every main/alternate `GameLine`: prices, probability,
provider clocks, source ID, opener evidence and full JSON. `withdrawn` events
retain price/`quote_at`, changing availability at `observed_at`. `capture_id`
groups receipts; `pair_at` returns one book snapshot. `Pair.complete` requires
both sides at one canonical number (away spread flipped); `same_capture` exposes
cross-receipt mates. Missing/mismatched sides remain explicit.

Frozen `QuoteKey(game_id, book, market, side)` and `MarketKey(game_id, book,
market)` replace delimited keys; line is a value, retaining alternates separately.
`forecast_observations` retains full forecasts, source/model (`merged`),
fetched/run clocks, lead and hourly valid window. `game_observations` retains
kickoff/sport/season/week and full games. `runs` records stage start/finish,
books/sports and attempted/successful book/sport coverage. `schema_version` is 2.
Opening a v1 cache adds clock-scan indexes with `CREATE INDEX IF NOT EXISTS` and
updates the existing version table in place; all rows survive. Per-key series
use the existing composite quote index instead of scanning the whole log;
CFB T-6 lookup restricts its scan to the individual game.
Fact/receipt primary keys are SHA256 of complete canonical row content:
identical appends are no-ops; corrections at identical clocks remain evidence.
Later unchanged observations survive. Same-clock conflicts use a hash tie-break.

Python timestamps must be aware; SQL uses microsecond UTC ISO text. `as_of`
gates observation AND receipt clocks: later ingestion cannot alter earlier
knowledge. Withdrawals require typed `ScrapeResult` coverage: only `ok` and
`empty`, with `attempted=True`, `coverage_ok=True`, matching book/sport and a
nonempty schedule, can withdraw. `empty` is genuine absence: `scraper_result`
chooses it only after ruling out upstream-stale and fetch-error diagnostics;
ingest failures carry typed outcomes too. `upstream_stale`, `fetch_error`,
`timeout`, `skipped`, `not_requested`, missing results and incomplete coverage
never withdraw. A conservative hook-local check vetoes coverage if raw row count
differs from mapped row count (including resolver failures). This may retain a
quote through partial resolution rather than invent a withdrawal. No degradation
text or severity controls the decision. Actual partial quotes are still appended.
Skipped/not-requested results are not recorded as attempts.

## Query mapping and parity

| Query | Existing store/function |
| --- | --- |
| `first_observations`, `first_seen`, `first_attested`, `baseline` | `openers`, `true_openers`, `get_baseline`, pair guards |
| `in_force(at=kickoff-6d)` | `references.t_minus_6d`, `retarget_openers`, per-book CFB rebasing |
| `latest(max_age=1h, honor_expiry=True)` | `archive_last`, `carry_forward_lines` |
| `last_observed` | `last_observed_totals` (unavailable → withdrawn; expired → stale) |
| `series`, `collapse(cap=120)` | `history.json`, `update_history` |
| `collapse` without cap | D1 `odds_deltas` |
| `closing(change_points=True)` | `freeze_from_series` / `freeze_from_rows`, `closings.json` |
| `weekly_total` | first BetOnline total in the Eastern opening window |
| `forecasts`, `game_facts` | forecast fields in `wx_last`/`wx_history`, schedule facts |

300 seeded sequences × 10 runs (150 NFL, 150 CFB) compare per-book baselines,
carry/status, history, D1 deltas and both closings. Cases include partial/missing/
mismatched sides, late/disappearing/returning books, repeats/moves, attestation
conflicts and collection before/after T-6. Dedicated tests cover weekly windows,
age/expiry/kickoff boundaries, transactions, schedule changes, forecasts and
off/on/failing hooks. No xfails.

Six deliberate rules (type ii) are named in the API: (1)
`opener_policy='anchored_first_seen'`: home -3 alone, away +4 rejected, away +3
accepted later; (2) `cfb_t6=True`: CFB reference 45 outranks attested 42;
(3) `late_fallback=True`: collection starts after T-6, so use its first 45;
(4) `historical_quotes=True`: 45 then withdrawal still supplies a historical
baseline; (5) `change_points=True`: unchanged 45 at 00:00 and 01:00, kickoff
02:00, legacy closing clock is 00:00; (6) `max_age`/`honor_expiry`: 45 at 00:00
expires 00:50, usable at expiry and at exactly one hour only if not expired.

Four legacy accidents (type i) are asserted as differences with minimal cases:
(1) successful quote, failed scrape ten minutes later: legacy marks unavailable,
log retains it; (2) quote observed 00:00, history stage 00:10: history stamps
00:10, log retains observation 00:00 and knowledge 00:10; (3) one pre-kickoff
point plus 120 post-kickoff moves: capped history loses the closing, log retains
it; (4) equal quotes before and inside the weekly window, then history-only
backfill: change-only history misses the in-window receipt, log finds it.

## Volume and cutover questions

Eight books (`build.BOOK_ORDER`) × 75–119 games (location proxy in
`refresh-cadence.md`) × 3 markets × 2 sides × 70 runs/week (~10/day) gives
**252,000–399,840 quote rows/week**, before alternates/withdrawals. ~92 dispatches
including game-day extras yields an all-slate ceiling ~526,000. Forecasts add
~5,250–10,948 rows; game facts similarly per stage. Broader odds horizons and
payload/index costs need measurement; locations are not exact game counts.

At roughly 1–2 KiB per quote including payload and indexes, the ceiling is about
75–150 MiB/day before forecasts and longer odds horizons. The two-day completed
game tail and 192 MiB cache budget are deliberately disposable; this estimate
does not promise that an active week's full history fits.

Decide durable host versus D1, retention, concurrent writers, backup/restore,
batching and indexed SQL (prototype policies materialize rows). Backfill D1
`odds_history`/`weather_history`, saved `openers`, attested openers, T-6 references
and frozen `closings` before cutover, preserving provenance, knowledge clocks,
bases and conflicts. Change-only rows cannot recover unchanged receipts,
coverage/withdrawals, discarded history or component forecast windows; raw/JSON
captures may help. Synthetic/frozen consensus and fair/GS/away outputs require
preserved artifacts or versioned model replay; book facts alone do not recover
their frozen provenance. No production read cutover is implemented.

## Reading per-run parity evidence

Both publish jobs always attempt `python -m pipeline.obslog.parity --log PATH
--state-dir data/state --out FILE [--as-of ISO]` before compaction. The module
opens the log read-only and restricts queries to `games_*.json` on the current
local board, including a retained other sport. JSON files are
`data/raw_runs/obslog-parity-light.json` and `obslog-parity-playwright.json` in
`raw-runs-<run_id>` / `raw-runs-pw-<run_id>` (seven-day artifact retention).
Counts also appear as a job-summary table. Internal failures produce
`status: error` and an Actions warning. Findings, CLI errors, unreadable inputs
and output/summary failures always return zero. Workflow report steps also have
`if: always()` and `continue-on-error: true`.

Areas are `baseline` (`get_baseline` versus the named anchored/CFB-T6 options),
`latest` (archive availability, expiry and inclusive one-hour ceiling), `history`
(change-only, last 120 points), `closings` (strictly before kickoff, change-point
clock), `wx_history`, `wx_last`, and `weekly_totals`. Weather compares forecast
lead, wind, gust, temperature, rain and precipitation probability; duplicate raw
projections collapse when only an impact moved. Consensus, model-fair series and
weather impacts `gs_fg`/`gs_fg_v2` are explicitly excluded: they require preserved
model outputs or versioned replay. This report cannot justify removing them.

Each compared key gets one classification:

- `match`: equal projected facts and clocks.
- `log_started_late`: a stored fact predates the first quote/forecast for that
  key, or the key has no logged observation. Uses baseline/closing/latest clocks
  and the oldest retained history point. An attestation can prove an old baseline
  directly. A same-price coincidence still cannot prove an older fact.
- `known_i1_failed_scrape`: archive is unavailable, log retains a usable quote,
  and a later attempted-but-unsuccessful coverage receipt proves the difference.
- `known_i2_history_clock`: matching values but the legacy history-stage clock
  follows quote/receipt time; also covers forecast history and history closings.
- `known_i3_history_cap`: missing closing and a full 120-point legacy history
  containing only post-kickoff moves, while the log retains the close.
- `known_i4_weekly_backfill`: a missing weekly opener with a logged in-window
  repeat equal to an earlier observation, lost by change-only history.
- `mismatch`: any remaining difference; **only these are findings**.

`counts[area][classification]` includes zeros; `findings` totals mismatches.
Up to `--samples` (default 20) mismatches contain both values and up to six
relevant observation rows, including source/receipt clocks. `status: error` is
missing evidence, never a clean result. Many late-start keys in the first run
are expected; cold starts and size evictions reset that evidence.

## Promotion gates and recommended cutover order

These are proposed gates, not automatic switches. Count only contiguous runs
with successful cache handoff, `status: ok`, no eviction and no unexplained
mismatches for the store. Review every known difference as an explicit product
decision; it is not exact parity. Zero-key/all-late reports cannot qualify.

| Order / store | Real-run evidence required before a separate cutover |
| --- | --- |
| 1. `archive_last` carry-forward reads | 30 publish jobs over 7 days; both jobs/sports, every enabled book, 100 judgeable keys per sport. Exercise one-hour boundary, withdrawal/return and failed coverage retention. Zero unexplained latest mismatches. Keep the D1 checkpoint separate. |
| 2. `history.json` book series | 50 jobs over 7 days; 500 judgeable keys, 100 actual moves, a real Playwright handoff. Review all clock differences; zero unexplained series mismatches. Preserve fair/consensus series separately. |
| 3. Forecast parts of `wx_history` / `wx_last` | 50 jobs over 7 days; 30 games per sport, 100 changed forecasts, full/reused-weather paths. Zero raw-field mismatches. Preserve/replay derived impacts and their change triggers before removing either whole store. |
| 4. `closings.json` | Two completed football weekends, 30 judgeable games per sport observed from pre-kickoff through freeze. Zero unexplained price/clock mismatches; decide clock and capped-history policies explicitly. |
| 5. `openers.json`, T-6, weekly totals | 100 jobs spanning 14 days and two opening windows; 30 games per sport with first quotes and CFB T-6 targets actually covered. Zero unexplained baseline mismatches. Verify attestations, anchored pairs, T-6 retargeting and weekly repeats. Backfill pre-shadow facts or preserve their frozen values. |

The cache may rotate before these gates can be met. That is evidence to choose
durable storage and backfill before moving reads, not to lower the gates. Keep
fallbacks for unjudgeable games; never replace a saved opener with a late log's
first quote.

## D1 ordering audit

The audit claim was true: both jobs uploaded `archive_last`/`wx_last` before
applying `d1_inserts.sql`. `stage_odds` computes deltas then advances the marker;
`weather_deltas` advances its marker too. No pending SQL, retry or failure-page/
receipt recovery covered the resulting lost-delta risk.

The local `pipeline.obslog.archive` workflow adapter prepares exact pending-plus-
fresh SQL, attempts the existing D1 execution, acknowledges success, then publishes
the board/state/meta in their existing order. D1 failure is reported as a failed
job **after** publication/self-check, so failure/recovery paging remains intact.
On failure, `pending_d1_sql` inside the **existing** `archive_last.json` upload
carries the complete attempted SQL. Ordinary state load/save preserves this extra
field. The next run prepends it even with no fresh deltas. Existing insert-ignore/
upsert statements tolerate partial application/retry; fresh upserts follow older
ones. Success removes the field before the same existing state upload.

No new object, table, upload call, migration or database persistence route is
introduced. The adapter is independent of shadow logging. A failed publication
keeps the earlier replayable state, or causes harmless replay of acknowledged
rows. Interruption before any state publication retains the original run-loss
boundary; this is not a transaction across GitHub, D1 and R2.
