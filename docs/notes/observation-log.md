# Observation log: local shadow prototype

Local shadow only: `OBSLOG_SHADOW_PATH` enables receipts; its parent must exist.
No workflow, uploader, migration or production reader changes. SQLite files and
sidecars are ignored. Appends are atomic; queries are read-only.

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
books/sports and attempted/successful book/sport coverage. `schema_version` is 1.
Fact/receipt primary keys are SHA256 of complete canonical row content:
identical appends are no-ops; corrections at identical clocks remain evidence.
Later unchanged observations survive. Same-clock conflicts use a hash tie-break.

Python timestamps must be aware; SQL uses microsecond UTC ISO text. `as_of`
gates observation AND receipt clocks: later ingestion cannot alter earlier
knowledge. Withdrawals require successful coverage; shadow coverage vetoes
scrape/ingest/merge failures.

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

Decide durable host versus D1, retention, concurrent writers, backup/restore,
batching and indexed SQL (prototype policies materialize rows). Backfill D1
`odds_history`/`weather_history`, saved `openers`, attested openers, T-6 references
and frozen `closings` before cutover, preserving provenance, knowledge clocks,
bases and conflicts. Change-only rows cannot recover unchanged receipts,
coverage/withdrawals, discarded history or component forecast windows; raw/JSON
captures may help. Synthetic/frozen consensus and fair/GS/away outputs require
preserved artifacts or versioned model replay; book facts alone do not recover
their frozen provenance. No cutover is implemented.
