# Discovery UI integration contract

UI branch: `codex/football-ui-audit`, base main `2e69a97`. Integration and deployment belong to the backend task. This branch modifies frontend files and UI tests only; `current-quotes.mjs` and Worker implementation are unchanged.

## Discovery

Table, map and Signals presets share `filterDiscovery`. Closed roofs, CFB opening spreads beyond ±10, started/cancelled/postponed games never appear. Missing CFB opener or incomplete wind/temperature is an unknown case exposed by **Inspect low likelihood / unknown**. Clear signal and near-screening-margin cases are shown by default. Prices, edge filters and liquidity never gate discovery. Near margins are engineering screens, not calibrated probabilities. An optional `GameCard.discovery.signal_probability` in [0,1] supplies actual signal likelihood; below 5% is hidden by default. Confidence/impact/precipitation probability are never substituted.

The UI uses the published stadium coordinates at their original numeric precision; it does not substitute home-team coordinates. `venue_provenance.resolution`, `weather.ensemble_unverified_sources` and `weather.ensemble_verification_errors` are optional backend evidence shown in the drawer. All source clocks remain intact.

## Verified $500 exchange stake

Existing `total_prices.quotes` are exchange-only **reference quotes** in the detail comparison. They never qualify a $500 recommendation. Updated-at is labeled quote updated when fetched-at is absent; unknown/stale depth or fees stay explicit. Sportsbook odds/history remain available separately.

Table/map qualification reads `GameCard.execution_preview` or the last successful detail check (`GET /api/execution-preview`). Check query sends `game_id`, exact current total `line`, `stake=500`, `budget=500` and `max_price=.99`. `budget=500` preserves compatibility with the old API; an old fee-inclusive-budget result remains unqualified. Backend must recognize principal `stake` mode to provide a qualifying response; it should not treat this request as an order. If stake mode is unavailable, return an explicit unsupported result rather than claiming $500 principal.

Required response fields:

```json
{
  "ok": true,
  "game_id": "cfb:2026:6:AWAY@HOME",
  "side": "under",
  "line": 46.5,
  "stake_mode": "principal",
  "settlement_verified": true,
  "rules_key": "validated-compatible-rules-key",
  "principal": 500,
  "fees": 5,
  "payout_if_win": 1000,
  "average_price": 0.505,
  "fetched_at": "ISO UTC actual quote acquisition clock",
  "depth_fetched_at": "ISO UTC oldest included depth acquisition clock",
  "expires_at": "ISO UTC bounded snapshot expiry",
  "allocations": [{
    "book": "kalshi",
    "side": "under",
    "line": 46.5,
    "rules_key": "validated-compatible-rules-key",
    "principal": 500,
    "fees": 5
  }]
}
```

All allocations must match exact line, under outcome and verified settlement rules. Only `kalshi`, `novig`, `prophetx`, `polymarket_us`, `4cx` can qualify. Principal must total at least $500 **excluding fees**, allocation principal/fee sums must match, and average all-in cost must equal (principal + fees) / payout. Both acquisition clocks must be <=30 seconds old and nonfuture, with unexpired snapshot. No missing field is inferred. Failed publication/generation checks suppress recommendations. Backend should choose and validate the allocation across depth and live taker fees; UI does not sum unmatched reference quotes or claim global best price.

## Refresh and publication

`POST refresh` retains existing selected `sport` and `scope` fields, and adds `request_id` and `requested_at`. Exchange requests have an overall 30-second budget; full runs remain separately labeled background runs. A changed `meta.last_updated` alone cannot complete the request.

Direct response can provide `{ok, request_id, sport, scope, completed_at, meta, games}`. Alternatively, `meta.refresh` can carry the correlated `{request_id, sport, scope, completed_at}` receipt and the UI then fetches the selected sport's games. `completed_at` must follow the request, and all game run IDs must match meta.run_id. Other sports with old generations remain visibly unavailable for current recommendations until reloaded. Completion says **publication loaded**, with per-quote clocks deciding freshness; publication time is never relabeled acquisition time.

The old workflow-only dispatch returns no correlated result: the UI will truthfully time out rather than silently treat an unrelated publish as fresh odds. Backend fast-path/correlation integration is therefore required before deployment. A malformed/failed/mixed-generation load is unavailable, distinct from a valid empty array. Prior quotes retain their original aging clocks on refresh failure.

Integrated backend: exchange scope now returns a direct correlated public-depth result within its own 25-second response deadline; full/light/weather scopes retain background workflow dispatch. Direct results set `delivery_mode: direct_public_depth` and `published: false`, preserve `meta.run_id`, `meta.last_updated`, every game's model generation and weather clock, and carry separate quote/depth acquisition clocks. The UI labels this a quote result with the weather publication unchanged. `GameCard.fresh_odds` records per-game provider results and the nearest verified mapped `selected_line` for an explicit exact-line stake check; this line does not replace market consensus or opener provenance. No R2/D1 writes, workflow dispatch, notification or order occurs on exchange refresh. Unknown/expired mappings remain partial; zero fresh quotes fails unavailable. This path neither discovers unmapped new lines nor establishes a resident collector.

Principal previews are integrated: `stake` targets cash principal excluding fees, selecting one verified exact-line settlement group. Whole native contracts may slightly exceed the requested principal; exact principal, fee, debit and payout are reported separately. Different venue void/postponement policies are never pooled. Public previews do not validate account balances or eligibility. Route responses have a 25-second hard deadline, even when an upstream ignores cancellation; snapshots expire after 15 seconds.

## Validation and limits

Stake-check requests and completed/error states are retained by game/request identity outside the DOM. The 15-second quote tick may replace the price section, but every replacement control is rebound and painted from current state. Navigating to another game, closing, changing the exact under line/kickoff, cancelling, or timing out invalidates and aborts the pending request. Late responses cannot publish an offer or enable a newer request's control, even if the fetch ignores abort. Search keeps the raw focused input and caret/selection; only the matching comparison normalizes whitespace and case.

Run `node --test "test/*.test.mjs"` in `site/worker`, plus `python -m pytest tests/test_site_contract.py -q`. UI fixtures cover hard eligibility, liquidity-independent discovery, quiet/unknown inspection, preset consistency, exact copy and copy failure, exchange stake/fee/clock/rules rejection, mixed generations and unrelated refresh rejection/timeout. `stake-check-race.test.mjs` deterministically exercises delayed responses across a 15-second replacement, navigation, cancellation/restart, close/reopen, changed line and timeout. `search-input.test.mjs` exercises incremental multi-word typing, caret/selection preservation across renders, normalized comparison and unfocused hash-state synchronization.

Safe browser checks use synthetic cards and a blank local MapLibre style under `connect-src 'none'`, with production boot disabled. Live-board browser access was explicitly denied; no alternate access route, production refresh, bets, communications or deployment was attempted. Browser clipboard API returned an empty value even after the UI's successful native clipboard promise; exact copy is verified in the unit fixture, and real system clipboard contents remain unverified.
