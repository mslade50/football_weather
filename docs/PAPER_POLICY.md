# Prospective weather paper test, version 1

This is a new first-observed test, starting with deployment. It does not validate
or reproduce the retrospective daily-vintage study. It has no trading, sizing,
alert, fair-price or live-screen authority. There is no established betting edge.

## Frozen selection

`pipeline/paper.py:POLICY` and its SHA-256 hash identify the policy. Changing a
definition requires a new policy/version and a separate ledger; the loader
refuses a mismatched checkpoint. NFL and CFB are always separate.

- Primary: CFB, IFS sustained wind at least 12 mph, fixed FanDuel total under.
- Secondary: 15 mph and NFL (fixed BetOnline), each separately reported. These
  are overlapping tests, not independent bets or confirmatory discoveries.
- Entry horizon: 24 through 144 hours before scheduled kickoff, inclusive.
  This is the first **observed** qualifying poll within that horizon, not the
  first public forecast, model release time or a daily historical vintage.
  Only runs that explicitly requested that sport's fixed book can freeze an
  entry. CFB normally freezes in the light/FanDuel job; NFL in the dependent
  BetOnline job. A weather-only run or a run not requesting the reference book
  archives observations but cannot consume the first entry. Both fixed and
  secondary best-line variants share this sport-specific observation clock.
  If the requested book actually fails or has no fresh quote, that first
  eligible crossing remains a miss; a later retry cannot select another price.
- Forecast: four complete, unblended IFS hourly winds, from the UTC hour
  containing kickoff through +3 hours. No AIFS, stitch, gust or missing-hour
  substitution. This extra field leaves the production three-hour window alone.
- Known exposed roof, scheduled game, point receipt no more than six hours old,
  and no aged-point marker. When known, Open Data model initialization must be
  no more than 24 hours old. The forecast receipt is not a publication timestamp.
  Unavailable initialization/publication clocks stay null. Open-Meteo model
  release freshness cannot be independently certified by its receipt timestamp.
- Freeze the first weather crossing, including missing-quote opportunities.
  A later quote does not turn a missed crossing into a successful entry.
  Forecast fades/re-crossings cannot replace the original crossing.
- Quotes must be paired over/under totals at one line and one scrape timestamp,
  at most 60 minutes old, with finite American odds and no expired provider
  clock. Source timestamps, when supplied, must also be recent and not future.
  These are recorded observations, not claims of an executed or fillable wager.
- Allowed books: FanDuel, BetOnline, DraftKings, Pinnacle. No consensus, BetCris,
  exchanges, fabricated prices or assumed -110. Fixed-book prices have no
  additional juice filter. Secondary “best” uses only odds at least -120,
  highest under total then highest decimal payout, with a deterministic book
  tie-break. This ranks lines; it does not estimate an economic value for buying
  points or claim the optimum across differently priced totals.
- No rain, temperature, stadium-orientation, team-style or opening-spread gate
  is added to this new test. No overs strategy is promoted. Captures include
  all observed cards and rejection reasons, so those can be preregistered later.

## Controls and grading

At entry, freeze every valid below-threshold control in the same capture, sport,
season, week, 24-hour lead bin and selected book. Exclude the target game itself.
Do not choose controls using outcomes, closes or later availability. Report
raw under CLV as entry total minus the same-book closing total. A closing quote
must have been observed strictly before kickoff and within its last 60 minutes;
a change-only historical price is not evidence of a fresh close. The regular
polling cadence may miss this window. Missing closes stay missing.

Excess CLV subtracts the equal-weight control CLV. It is reported only when every
frozen control has a valid close; the frozen cohort is not silently reduced.
No controls means no excess CLV. A changed authoritative kickoff excludes the
game's bets or controls from grading permanently, including a later restoration
of its original time. There is no inferred replacement entry.
Current schedule changes come from subsequent captures and weekly D1 exports.

The weekly existing backtest fetches completed results. The paper sidecar reads
its sourced home/away scores and grades **the original entry line and odds**,
risking one unit per paper entry. Wins return the quoted net payout, losses -1,
pushes zero. ROI includes pushes in the denominator. Scores can be corrected on
subsequent weekly runs; no model-derived total is accepted as a result. Missing
result sources remain pending. No conversion of CLV into expected ROI is used.

The report includes weekly and cumulative counts, missed entries, pending and
excluded statuses, closing/control coverage, record, CLV and realized paper ROI.
Deterministic 1,000-draw percentile bootstrap intervals resample whole weeks
within each test, preserving games/controls that share a week. Intervals appear
only after eight weeks with that metric; they are descriptive, unadjusted for
multiple testing and are not a promotion gate. Do not tune thresholds weekly.
Review collection quality weekly; judge the hypothesis at a preregistered season
review with adequate independent weeks and a separate confirmation period.

## Storage, ownership and operation

The existing `football-refresh` workflow concurrency group is the sole writer.
`scripts/paper_run.py capture` runs after successful publication, self-check and
notification in each light/BetOnline job. It consumes that job's local snapshots,
not the backtest's limited 120-snapshot mirror. It refuses old/future snapshots,
duplicate games, conflicting run IDs, out-of-order observations, corrupt state
or non-missing storage failures. Identical retries are idempotent.

R2 objects:

| Object | Purpose |
|---|---|
| `paper/ifs-first-observed-v1/captures/<run_id>.json` | Permanent normalized observations for every card, weather samples, paired quotes, provenance and rejection reasons |
| `paper/ifs-first-observed-v1/checkpoint.json` | First crossings and frozen control cohorts, all games' closing candidates, run fingerprints, scores and known missing runs |
| `board/paper.json` | Current cumulative and weekly scorecard with auditable entry rows |
| `board/paper_health.json` | Latest capture/grading success or failure receipt |

Capture is archived first, checkpoint second, report third, health last. An
archive failure cannot advance the checkpoint. A report failure can leave the
checkpoint ahead; the next successful run regenerates the report. No generic
board publisher owns these objects. Paper failures do not fail or delay the
live publication/notification path; their isolated step records a workflow
warning and attempts a health error receipt. A failed health write remains
detectable through its old timestamp/run ID. Logs include the generated paper
files as short-lived GitHub artifacts; R2 captures are the permanent record.

Known skipped published runs are recorded when the previous board run is absent
from the paper index. This detects gaps in successful board publications, not
every possible missing scheduler tick. The Status panel shows last capture,
grade, missing closes, gaps, and gate/quote rejections. It flags capture age over
12 hours or a mismatch with the current board run. An eight-day-old weekly grade
is overdue. The board's existing scheduler health remains the source for missed
refreshes. Paper health does not change trading availability.

No extra provider fetch, score fetch, subscription, database migration or
schedule is introduced. The process adds a few small reads/writes to existing
jobs, a growing checkpoint and immutable capture storage. It uses existing
transport retries and weekly result enrichment. It cannot recover an unobserved
quote or guarantee a fresh close when polling misses kickoff. Review the
checkpoint size/job duration at season end before scaling to many seasons.

## Maintenance and recovery

1. Weekly: inspect capture/grade age, gaps, rejections, missing quotes and fresh
   close coverage before interpreting performance. Inspect the named workflow
   step for an error; do not change the policy to improve the scorecard.
2. Retry an identical failed capture with the same snapshot/run ID within one
   hour. Do not label delayed historical reconstruction as prospective capture.
   Later runs retain known gaps. Archive replay/recovery is an explicit offline
   operation with its original timestamps, not automatic backfilling.
3. If the checkpoint is absent while a report exists, the sidecar refuses to
   reset. Restore a known checkpoint from the generated workflow artifact or
   reconstruct from the complete ordered capture archive under independent
   review. Never delete/reset the permanent ledger to make a check pass.
4. Rollback: set repository variable `FOOTBALL_PAPER_ENABLED=0` to stop both
   capture and weekly grading without touching the ledger or live model.
   The old scorecard remains and becomes visibly stale. To remove this version
   fully, revert the implementation commit and let the existing deploy workflow
   publish the previous UI. Retain the R2 archive for audit.
