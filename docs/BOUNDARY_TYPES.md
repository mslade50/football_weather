# Boundary types

## Scrapes

`pipeline/scrape_outcomes.py` owns `ScrapeOutcome` and frozen `ScrapeResult`.
Codes: `ok`, `empty`, `upstream_stale`, `fetch_error`, `timeout`, `skipped`,
`not_requested`. Results retain row counts, diagnostics, optional stale age,
attempt/coverage evidence and the historical health diagnostic. Health summaries
still treat partial rows as recovery; upstream staleness resets the empty streak
without updating the last successful observation. Outer gather failures preserve
the previous health summary. No numeric, alert, severity or text behavior changes
are intended by this refactor.

The live scrape and handoff boundaries create results. Book health, volume
exemptions, shadow coverage, run warning status and health paging
read codes. `fetch_errors` remains source diagnostic input, never a substring
protocol. Degradations publish additive `outcome_code` and `empty_book` evidence.
Book-health schema 2 migrates known schema-0/1 text once in `state.migrate` and
exports `outcome_code` alongside the existing health fields. Handoff files keep
schema 1 with optional typed evidence; older handoffs still load.
The audited sentence check in `d1_out._warning_status` was a run-status selector,
not a D1 history-pruning guard; its existing role is preserved.

## Game status

`utils/game_status.py` owns `GameStatus`: `scheduled`, `tbd`, `live`, `final`,
`postponed`, `cancelled`, `suspended`, `unknown`, `unspecified`. The schedule sources currently
emit scheduled/TBD/final (nflverse and CFBD) and scheduled/TBD/live/final (ESPN).
Frozen `Game` construction derives the code, including after dataclass replace.
GameCards/snapshots add `status_code`; raw `status` is preserved. Existing D1
columns and stored snapshots are unchanged. Legacy cards normalize through the
single Python helper and one `game-status.js` helper in each JS runtime. The
web helper is a classic script; the Worker helper is ESM, matching their existing
loaders without changing the bundler or asset service.

Audit evidence: commit `1f52499` introduced both default-week selection and the
table game-state badge. The badge intentionally answers a display question:
Final/Live labels, suppression for cancelled/postponed/suspended/TBD/explicit
unknown, and a kickoff-clock Live fallback for other old cards. This policy stays
in `table.js` using codes. `alert_policy.clear_reason` intentionally requires
scheduled status plus its existing positive-evidence checks; it is not broadened
to TBD/unknown. CFBD's include-final filter still excludes only final games.

The shorter week selectors have no documented/tested exception for live or
suspended games. They predate the full filter and are treated as drift. Changes:

- Python `json_out.current_week` excludes live, in-progress, suspended and final
  variants (for example Final/OT), matching the general pre-kickoff policy.
- Browser `app.defaultWeek` excludes live, in-progress and suspended games.
- All general eligibility filters recognize `in_play` and `half`/halftime as
  live, matching the established badge aliases. This affects Python alert policy,
  process discovery, resident collection, current-week selection, browser default
  week/discovery/execution picker, and Worker liquidity/refresh/execution/fresh odds.

TBD/unknown eligibility and the strict future-kickoff boundary are preserved.
Explicit unknown values suppress the badge. Unrecognized/missing raw text uses
`unspecified`, preserving the historical kickoff-clock display fallback without
claiming the positive scheduled-status evidence required for CLEAR.

Converted consumers: Python `alert_policy.collect`, `alert_policy.clear_reason`,
`model.process.evaluate_card`, `resident`, `outputs.json_out.current_week`,
`schedule.cfb` include-final; web `app`, `discovery`, `execution-preview`, `table`;
Worker `alert-liquidity`, `exchange-refresh`, `execution-preview`, `fresh-odds`.

## Scalar coercion

`utils/numbers.finite_number` is the low-level finite float coercion. The old
`odds.parsers.common` name re-exports it, preserving all provider imports.
Native bools are rejected by default; `allow_bool=True` preserves wind-history
and existing provider exceptions. Float conversion accepts signed/whitespace
strings and numpy/pandas numeric scalars; commas, empty text, NaN and infinity
are missing. Overflow still raises, and negative zero retains its sign.

Replaced identical number bodies: `backtest._num`, `calibrate._num`,
`model.process._f`, `model.wind_history._number` (bool-enabled partial), and
`odds.parsers.common.finite_number`. No numeric behavior changes.

`utils/timeutil.parse_iso_utc` owns nullable ISO/datetime-to-UTC parsing. Numbers
are never epochs. Its explicit boundary options preserve the old differences:

| Consumer | Shared policy |
| --- | --- |
| `state.parse_utc` | Alias; no trimming, naive treated as UTC, any Z translated |
| `alerts._dt`, `backtest._dt`, `model.clv._dt` | Partials; trim, naive UTC, terminal Z only |
| `odds.parsers.common.parse_utc` | Thin string-only adapter; no trim; retains `naive_utc` option |

Before replacement, the original functions were compared offline on 22 numeric
and 20 timestamp cases, including None, native/numpy bool, whitespace, nonfinite
values, commas/signs, overflow, numpy/pandas scalars, naive/aware datetimes, Z,
offsets and epoch numbers: zero differences for every replaced body. Golden
tables in `tests/test_scalar_helpers.py` pin these policies and the retained
differences, including pandas' existing NaT datetime-subclass behavior.

Retained special policies (bodies unchanged; descriptive names where permitted):

| Module/helper | Why it is distinct |
| --- | --- |
| `alerts._strict_number` | Actual int/float only, no strings/native bool; rejects nonfinite |
| `model.fair._numeric_nan_to_none` | Numeric-only, bool/infinity allowed, NaN missing |
| `model.clv._number_allow_infinity` | Converts strings, no native bool, infinity allowed |
| `weather.climatology_blend._number_allow_infinity` | Same historical infinity policy, preserved for compatibility |
| `model.signals._number_allow_bool_and_infinity` | Native bool/infinity allowed, NaN missing |
| `backtest_git._number_allow_bool_and_infinity` | Legacy sheet bool/infinity behavior |
| `odds.parsers.betonline._num` | Invalid/missing becomes zero; bool/nonfinite allowed |
| `odds.parsers.novig._num` | Two-decimal rounding; bool/infinity allowed; NaN missing |
| `weather.parsers.ensemble._num`, `weather.parsers.openmeteo._num` | Float conversion retains bool/NaN/infinity |
| `weather.parsers.ensemble_mean._nums` | List mapper with the same permissive float conversion |
| `weather.ecmwf_ranges._member_number` | Integer GRIB member ID from a row, not scalar float coercion |
| `obslog.queries._canonical_line` | Home-side spread normalization and rounding, not coercion |
| `calibrate._offset_preserving_dt` | Stringifies sheet scalars, retains offsets for run-month selection |
| `alert_policy.evidence_time` | Rejects timezone assumptions; retains evidence's source offset |
| `timeutil.parse_iso` | Raises on invalid input, configurable default timezone, retains offsets |
| `timeutil.ensure_utc` | Datetime-only timezone operation, not a nullable string parser |

Provider-specific parser files were intentionally not renamed/edited. Their
special behavior cannot be replaced by finite coercion without changing values.
`weather/openmeteo.py` has no private `_num` body; the permissive helper is in
its parser. No new persisted shape or JSON fields are introduced by scalar
consolidation; part 1's migration and part 2's legacy-card fallback still apply.
