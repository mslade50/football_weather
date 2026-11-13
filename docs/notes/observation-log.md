# Observation log: cached production shadow

`OBSLOG_SHADOW_PATH` enables advisory receipts in the light and Playwright publish
builds. The parallel `playwright_scrape` job has no log or state. Pricing, alerts,
published board files and D1 generation never read this SQLite database. Appends
are atomic; queries are read-only. SQLite files and sidecars are ignored by git.

## Live workflow and cache lifecycle

The exact cached file is `data/cache/obslog/observations.sqlite`. Split
`actions/cache/restore@v4` and `actions/cache/save@v4` use immutable keys
`obslog-v4-<runner.os>-<run_id>-<run_attempt>-<job>` and restore the
`obslog-v4-<runner.os>-` prefix (newest entry). **One cache entry per run:** when a
Playwright job follows (`need_playwright`), light does not save; it uploads the log
as the one-day artifact `obslog-handoff-<run_id>` (no attempt in the name, uploaded
with `overwrite`, so a re-run of only the Playwright job still finds it). Playwright
restores the older cache, then downloads the handoff over it, so its log has light's
rows, and it saves the run's single cache entry. If Playwright never publishes
(light red before publication), that run's light rows are lost. Without a Playwright
job light saves itself. This keeps GitHub's 10 GB cache from being churned by two
large unique entries per run. Workflow concurrency `football-refresh` serializes
runs. A cache service/save outage can lose evidence; it cannot affect publication.
A rerun may start with already-seen receipts; identical appends are idempotent.
Every shadow step has `timeout-minutes` of 5 or less so it cannot consume the job's
30 minutes. Shadow problems go to the run log only (`::warning::Observation log
shadow: ...`); they are never published degradations, never reach `meta` or D1
`runs`, and never change the Status tab.

A cold/evicted cache starts empty. Prepare performs `PRAGMA quick_check`; corrupt
files and sidecars are renamed beside the database to `*.corrupt-<UTC>.sqlite`,
excluded from the cache. Hook recovery and workflow prepare emit run-log
Actions warnings only. An unknown, newer **or older** schema version (including the one-row-per-run v1/v2
files) is quarantined the same way (bytes kept beside the file, empty log started);
the cache key prefix moved to `obslog-v4-` so such files are not even restored. All evidence/report/cache steps are best effort. Database contents
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

`quote_observations` stores changed **main** `GameLine` values only: line, odds,
probability, observation/receipt clocks, expiry, opener evidence and an integer
`cap` that references `captures` (capture id and run id, the pointer to the raw
capture archived in R2). No provider payload, source id or alternate line is
copied. `withdrawn` events retain price/`quote_at`, changing availability at
`observed_at`. `capture_id`
groups receipts; `pair_at` returns one book snapshot. `Pair.complete` requires
both sides at one canonical number (away spread flipped); `same_capture` exposes
cross-receipt mates. Missing/mismatched sides remain explicit.

Frozen `QuoteKey(game_id, book, market, side)` and `MarketKey(game_id, book,
market)` replace delimited keys; line is a value.
`forecast_observations` retains full forecasts (zlib JSON), source/model (`merged`),
fetched/run clocks, lead and hourly valid window. `game_observations` retains
kickoff/sport/season/week and full games. `runs` records stage start/finish,
books/sports and attempted/successful book/sport coverage. `schema_version` is 4.
Older files are never upgraded in place (see above). Per-key series
use the existing composite quote index instead of scanning the whole log;
CFB T-6 lookup restricts its scan to the individual game.

### Change-only storage (v4)

Version 2 appended a row for every quote on every run; version 3 (change-only)
still held 60.7 MB after two production runs (38,171 rows, 1.6 KB each on disk).
The real file showed why, and version 4 fixes it:

- **Partial groups were never deduplicated.** Only a (book, sport) with full
  coverage was change-only. In production the CFB groups of FanDuel, Kalshi,
  Novig and Polymarket are never "successful" (their raw rows include games the
  schedule drops, so the hook vetoes coverage), so every run re-wrote them. The
  payloads of those repeated rows differed only in `scraped_at` and `run_id`
  (plus `prob_raw` on exchanges, a real value). Now every group is change-only.
- **Alternates were 85% of the rows** (about 19,000 per run: Kalshi and
  Polymarket ladders). Openers, latest, history, closings and weekly totals are
  all main-lines-only, and no query or parity area reads alternates, so they are
  no longer logged; the raw captures keep them. Lost: alternate-line history.
- **Rows were fat.** A row was ~816 bytes of columns (470-byte JSON payload,
  64-char ids, capture/run/source ids repeated) under five indexes. Now a row is
  the semantic columns plus an integer capture pointer, an implicit rowid in place
  of a hash id, and two indexes (`quote_asof`, `quote_head`; each checked with
  EXPLAIN QUERY PLAN; `quote_capture`, `quote_clock` and `forecast_clock` were
  dropped). Re-appending a receipt is idempotent on (key, clocks, kind).

The log stays append-only; no row is ever updated.

- *Unchanged* means equal `line`, `odds`, `prob_raw`, opener attestation fields
  and expiry **offsets**; scrape clocks, run ids and provider metadata
  (`source_id`, `source_updated_at`, `outcome_ids`) never force a row. A rolling
  `expires_at`/`actionable_until` (BetCris: observed + ttl) keeps a fixed offset,
  and queries shift it with the confirmation clock. A head that is a withdrawal
  always differs, so a reappearance, or a revert to an earlier price, is written.
  Forecasts ignore `lead_hours`, `run_time` (it moved on 140 of 152 consecutive
  pairs while values did not) and provider fetch clocks; parity no longer
  compares lead. Game facts are written when the schedule fact changes.
- *Live and unchanged at T* is derived from `coverage`. A successful (book, sport)
  writes one row per market with `keys` NULL: it confirms every live quote of the
  scope and is the only thing that withdraws. A failed or partial scrape writes
  nothing for a failure, and for a partial scrape one row per market listing only
  the (game, side) pairs it saw unchanged (zlib JSON): it confirms those and
  nothing else, and never withdraws. A quote is confirmed at an event when it is
  the key's latest logged row (not a withdrawal), the event confirms it, and it
  was recorded after that row and before the key's next logged row.
- The coverage clock is the minimum `scraped_at` of the market's (unchanged)
  lines, so it never overstates freshness.
- Per-run write cost: one indexed window query per (book, sport) in the run over
  the covering `quote_head` index (4 ms on the real file), plus point lookups for
  games and forecasts; no full-table load. Coverage rows older than 21 days are
  pruned.

Query meaning: `latest` lifts the head to its newest confirmation, so `max_age`
judges when the quote was last confirmed live, and withdrawn/unscraped quotes age
out. `series(expand=True)` re-derives the one-row-per-run view. `in_force`
(without `change_points`), natural `closing` and `weekly_total` use confirmations
(bounded by `at`, strictly before kickoff, or the weekly window); `change_points`
modes already want the first row of a price spell, which is the logged row.
`first_seen` carries live unchanged sides a capture's coverage confirms into the
batch (with the capture's clock), and treats each partial scrape's listed
composition as an event, so a mismatched pair completed by one side changing, or
a lone side listed after a mismatched pair, is judged at that run; `pair_at` does the same for one snapshot.
Known edge: a provider attestation whose `opened_at` is later than the scrape
clock that first logged it is not re-validated by a later unchanged confirmation.
Fact/receipt primary keys are SHA256 of complete canonical row content:
identical appends are no-ops; corrections at identical clocks remain evidence.
Later unchanged observations are derived from coverage, not stored. Re-appending a receipt is idempotent on key, clocks and kind; a differing same-clock correction is not retained (v4 dropped the content hash).

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

`anchored_first_seen` follows `state._record_pair_openers`: per capture the first
main line of each side counts (build.py passes the store main lines only), a
mismatched pair records nothing, a lone side anchors, and with one side known the
other joins only at that number. Production parity showed two thin Novig spreads
with no stored opener where the log had anchored a lone side. Cause: the store had
anchored the mate side the day before the log started, and the missing side never
returned to that number, so the store correctly withholds it. The log cannot know
an anchor that predates it, so parity classifies this as `log_started_late`.

Four legacy accidents (type i) are asserted as differences with minimal cases:
(1) successful quote, failed scrape ten minutes later: legacy marks unavailable,
log retains it; (2) quote observed 00:00, history stage 00:10: history stamps
00:10, log retains observation 00:00 and knowledge 00:10; (3) one pre-kickoff
point plus 120 post-kickoff moves: capped history loses the closing, log retains
it; (4) equal quotes before and inside the weekly window, then history-only
backfill: change-only history misses the in-window receipt, log finds it.

## Volume and cutover questions

**Measured (v4), replaying the real production file** (3 captures, two runs 15
minutes apart on a Friday): 38,171 v3 rows / 60.7 MB become 5,045 rows /
2.09 MB, **415 bytes per row all-in** (rows, indexes, coverage, captures) against
1.6 KB. Real churn: about 3,460 main keys; run 2 wrote 177 main quotes (5.1%) of
which Kalshi 67, Polymarket 40, Novig 27, BetCris 36, Pinnacle 7, FanDuel 0
(408 of 408 FanDuel rows had been rewritten unchanged by v3); forecasts changed
23 of 155 (+3 new, 17%) at ~1 KB zlib each; about 11 KB of partial-coverage key
lists per two runs. Projection per run: ~190 quotes x 415 B = 80 KB, forecasts
~30 KB, coverage and run rows ~10 KB, about **120 KB per run**; a 120-run
(8-day) week is **about 17 MiB** including a ~2 MiB first run, roughly 6x under the
96 MiB week target and 11x under the 192 MiB bound, and still ~30 MiB if churn
tripled around Sunday kickoffs. `tests/test_obslog_change_only.py` simulates 120
runs with the measured shape (6% churn, partial groups, 600 alternates a run,
forecasts) and asserts < 48 MiB; it measures 11.8 MiB (28,089 rows, 441 B/row all-in), below the 17 MiB real-payload projection because its forecasts are smaller.
Uncertain: Sunday-morning churn and forecast payload size are extrapolated from
one Friday-afternoon pair.

The paragraphs below are the pre-change-only estimate (one row per quote per run) and
describe the upper bound if every quote changed on every run.

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
bases and conflicts. Pre-shadow history cannot be recovered from change-only
legacy state; raw/JSON captures may help. Synthetic/frozen consensus and fair/GS/away outputs require
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

The local `pipeline.outputs.d1_pending` workflow adapter (not part of the
observation log; the queue is bounded at 48 run chunks / 1 MiB and drops the oldest
chunks with a loud warning rather than failing forever on a rejected statement) prepares exact pending-plus-
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
