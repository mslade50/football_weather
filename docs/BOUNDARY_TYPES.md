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
exemptions, shadow coverage, run warning/pruning eligibility and health paging
read codes. `fetch_errors` remains source diagnostic input, never a substring
protocol. Degradations publish additive `outcome_code` and `empty_book` evidence.
Book-health schema 2 migrates known schema-0/1 text once in `state.migrate` and
exports `outcome_code` alongside the existing health fields. Handoff files keep
schema 1 with optional typed evidence; older handoffs still load.

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
