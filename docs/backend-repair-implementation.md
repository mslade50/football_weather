# Backend repair implementation, 2026-10-09

## Venue identity and failed ensemble verification

NFL schedule parsing now resolves an explicit venue name before an upstream stadium ID. A known name conflicting with a known ID is resolved to that named physical stadium, with the original name/ID and resolution method in the game card. A venue different from the designated home team's home ground is neutral for travel calculations. An unregistered explicit name is unresolved: no home-stadium weather substitution is permitted. The PHI–JAX regression covers Tottenham, JAX00, the misleading Home label, London local time, coordinates, and a visible warning.

On failed before/after ensemble metadata checks, previously verified raw members may be retained for the same location and complete requested hour window. They retain their original dataset metadata and per-hour fetch clocks. Retrieval age must be strictly below three hours for near/active games or twelve hours for distant inactive games; known dataset initialization must be at most thirty hours old. Unknown/future initialization, future retrieval timestamps, missing hours, changed parameter signature, and corrupt entries are rejected. No newly retrieved unverified payload is cached. Published status is `retained_members_degraded`, with separate unverified sources and verification errors. Fresh point forecasts keep a separate clock.

This is continuity of previously verified evidence, not a new source verification or a claim about empirical forecast accuracy. Dataset metadata does not establish initialization provenance for every long-range member hour. No v1 impact formula changes.

Validation: full Python suite and `python -m ruff check .` passed locally. No frontend files changed. Production activation and exact-generation validation remain required after integration.

## Exchange prices, fees, capacity, and on-demand depth

Published `total_prices.quotes` and best-price selections now contain supported exchanges only (Kalshi, Polymarket US, Novig). Sportsbook and unsupported-exchange offers remain in `reference_quotes`. Alternate mapped totals are included instead of silently limiting comparison to main rungs. Quote expiry is honored. Legacy alert snapshots retain their historical entry fields; current cards cannot fall back to a sportsbook recommendation.

The probability method is explicitly `empirically_unvalidated`; the current fair adjustment is explicitly `full_weather_adjustment_on_current_market`, and these comparisons remain a weather watch. This flags the possible double counting rather than claiming to have calibrated a replacement model.

Kalshi scraper payloads now include current series fee metadata and a per-series receipt clock. Unknown current fee schedules fail closed for that series. Its pure parser applies the retrieved taker multiplier and canonical golf rounding (six-decimal model fee, four-decimal direct-member debit), without a 0.99 cost cap. Raw metadata accompanies the events before parsing.

Public depth uses current venue fee metadata and venue-specific precision: Kalshi direct-member four-decimal debit; Polymarket US half-even cents for fees; Novig half-up five-decimal fees and native one-cent payout contracts. Each price level is an estimated separate taker fill; actual split-fill rounding may differ. Delayed rebates are excluded.

Alert liquidity reports `cash_stake_capacity`, `fee_capacity`, `debit_capacity`, and `payout_capacity` separately within one exact total and verified settlement group. Actual venue terms are preserved and hashed; cross-venue equivalence is not inferred. `compatible_groups` exposes alternatives separately. `cash_liquidity_verified` requires at least $500 principal cash capacity within that group, compared in integer microdollars. Two $300 offers on different totals or settlement terms cannot qualify together. It is not a $500 budget or a required bet size. A $499.99950 principal example fails even if debit and payout exceed $500; 500 Novig native contracts at a half-dollar probability supply only $2.50 principal. Unknown/empty depth and unfavorable offers do not pad acceptable capacity. The alert bridge has a 25-second overall fetch deadline and a 30-second subprocess limit; retained snapshots expire after 15 seconds.

Allocation ranks and checks actual rounded debit for each affordable order size, recalculating after a fill. Half-even fee discounts can admit a quote whose nominal fee exceeds the ceiling; the one-contract Poly $0.49 / 0.06 case costs $0.50 and passes a $0.502 ceiling. A $0.492 Kalshi contract costs $0.5095 and ranks ahead of a nominally cheaper $0.49 Poly contract costing $0.51. Exact calculations have a bounded work limit and fail closed rather than substitute nominal estimates.

### Frontend integration contract

`GET /api/fresh-odds?game_id=<canonical-id>[&line=<half-point-total>]` is an authenticated read-only endpoint available to current viewers. It fetches public taker depth directly, with no workflow dispatch, order endpoint, database write, or R2 write. It preserves `board_run_id`, `model_observed_at`, original `quote_observed_at`, and separate `depth_fetched_at`. A supplied line checks only that exact total; without it, up to twelve mapped totals nearest the current consensus are checked, and `markets_not_checked` makes the limit explicit. Unknown probability remains null and cannot qualify capacity.

Response includes the liquidity fields above, `quotes`, `fresh_quote_count`, `partial`, `elapsed_ms`, and `can_execute: false`. Errors and zero verified quotes must remain unavailable/partial in the UI. Responses use no-store. The route owns a 25-second hard response deadline, including an unresponsive fetch implementation. A depth snapshot taking fifteen seconds expires and fails instead of being presented as fresh. This endpoint does not establish a resident local worker or refresh unmapped/new provider lines.

Fee references checked 2026-10-09:
- https://docs.kalshi.com/getting_started/fee_rounding
- https://kalshi.com/docs/kalshi-fee-schedule.pdf
- https://docs.polymarket.us/fees
- https://docs.novig.com/api/concepts/money
- https://docs.novig.com/fees (v3 straight markets defer to each market's fee object)

Validation: 1,297 Python tests passed (one existing xlsxwriter-version warning); Ruff passed. Worker regressions cover native units, principal/debit boundary, negative offers, current fee multipliers, provider failures, expired quotes, authenticated GET-only depth, separate clocks, and no mutation/dispatch on the fast quote path. Actual deployed desktop/phone latency remains unverified until integration and authorized browser access.

## Remaining integration work

Resident local collection/supervision and incremental publication; opener/spread provenance and late movement; explicit bet confirmation and CLEAR lifecycle; daily alert policy; weather-confidence/degradation lifecycle; immutable publication generations; production activation and exact-generation acceptance. PR #13 remains untouched and separately gated. No external alerts, bets/orders, credentials, or unrelated Breakout changes were made.

## UI and principal/refresh contract integration

Reviewed UI commits `278c981` and `abe2f8d` are integrated in the isolated integration branch. They share table/map eligibility and search rules, hide low likelihood/unknown cases by default, preserve exact stadium coordinates, display explicit reference/fee/depth evidence, retain pending stake checks across quote refreshes, and preserve focused raw search text/caret.

The backend now accepts `stake` as principal cash, excludes fees from that target, preserves actual settlement terms in each allocation and selects one exact-line compatible group. Whole contracts may exceed the requested stake by less than one contract's cash cost. A fixture's principal is $500 while debit exceeds $500; the real response passes the discovery UI's verifier. The two incompatible $300 venues remain insufficient for that request.

Exchange refresh no longer waits on a GitHub workflow. It returns a direct request-correlated public-depth snapshot with a 25-second hard deadline, no production storage writes and no dispatch. Original weather/publication generation and acquisition clocks are retained. Per-game `fresh_odds` records provider status and nearest mapped exact line for an explicit stake check; this is not a replacement consensus or true opener. Missing maps/failures stay unavailable/partial and no fresh quote result cannot complete successfully. This does not discover unmapped new lines or establish a resident collector, and actual provider/board latency remains unverified.

Incident: canceled deploy run `37978198965` still activated Worker `32cfca67-880c-47bd-b870-c19651d50aa2` at 19:09:41 UTC. Official Cloudflare deployment metadata confirms this. Automatic approval review rejected a restore to prior version `3056b9b5-b4cd-490a-ba61-f26892ecb503` because its available authorization required read-only auditing. No rollback occurred; production operations are held pending explicit approval. PR #15 hotfix commit `c0c29aa` passed exact-SHA CI and independent focused review. UI integration is separate and has not been deployed.
