# Football execution evidence, version 1

Each priced exchange row in `games[].total_prices.quotes[]` gains an additive
`execution` object. It describes the exact quote that was priced; it is not an
order, an allocation, an account attestation or approval to trade. Existing
prices, probabilities, thresholds, sizing, `execution_markets`, board rows and
`alerts_feed.json` retain their contracts. `probability_status` remains
`empirically_unvalidated`. The execution service must explicitly adopt this
versioned block; the old top-level execution fields are not populated.

The existing collectors fetch the same catalog/book endpoints. Their parsers
retain evidence on `GameLine.execution_evidence`; canonicalization, normal
archive carry and JSON publication preserve it. No new order-book, account,
trading or rules-document requests are added. New per-page/per-book receipt
clocks are collector metadata beside the raw responses, never publication time.

## Fields

All paths below are relative to `quote.execution`. Decimal prices, quantities,
fee coefficients and binding lines are strings, not American odds or dollar
stakes. Nullable fields use JSON null and an entry in `missing`. Optional
venue-specific fields can be omitted when inapplicable. UTC clocks use ISO 8601.

| Field | Type | Meaning and source | When absent |
|---|---|---|---|
| `schema_version` | integer | `1`; this contract | Never |
| `binding` | object | Canonical association produced after `pipeline.odds.merge.canonicalize` | Never |
| `binding.game_id` | string | Canonical scheduled game | Never |
| `binding.sport` | string | `nfl` or `cfb` | Never |
| `binding.home_team_id`, `away_team_id` | string | Scheduled canonical team IDs, including neutral-site orientation | Never |
| `binding.kickoff_utc` | string | Scheduled kickoff; not a quote clock | Never |
| `binding.market`, `period` | string | `total`, `full-game`; parsers only map full-game totals here | Never |
| `binding.line` | decimal string | Exact priced total line | Never |
| `binding.selected_side` | string | Model side, `under` or `over` | Never |
| `binding.source_id` | string or null | Existing ticker, slug or Novig event:market key from the priced `GameLine` | Legacy line without identity |
| `binding.source` | string | `pipeline.odds.merge.canonicalize` | Never |
| `identity` | object | Native catalog identifiers below; never guessed from a display title | Empty/default-null on legacy carry |
| `identity.event_id` | string or null | Kalshi event ticker, Novig event UUID or US event ID | Catalog omitted it |
| `identity.market_id` | string or null | Kalshi market ticker, Novig market UUID or US market ID | Catalog omitted it |
| `identity.outcome_id` | string or null | Kalshi literal `yes`/`no`, Novig selected UUID, US explicit `marketSides.tokenId` | No explicit ID/token in catalog; no US token invented from long/short |
| `identity.native_side` | string or null | `yes`/`no` on Kalshi/US; `selected_outcome` on Novig, whose UUID is the selected side | Legacy evidence missing |
| `identity.series_id` | string or null | Kalshi `series_ticker` | Other venues; null if omitted by Kalshi |
| `identity.event_slug`, `market_slug` | string or null | US event/market slugs copied from catalog | Other venues; null if omitted by US |
| `identity.source` | string | Catalog parser source, not independent identity certification | Legacy evidence missing |
| `native_quote` | object | Exact selected-side top-of-book evidence, separately from displayed odds | Default-null on legacy carry |
| `native_quote.native_price` | decimal string or null | Kalshi/US YES-book ask for over, YES-book bid for under; Novig selected-outcome ask probability | Raw price missing/invalid; US quote currency not USD |
| `native_quote.selected_side_ask` | decimal string or null | Acquisition cost probability: over YES ask, under `1 - YES bid`, or Novig `1 - opposing bid` | Raw price missing/invalid |
| `native_quote.native_quantity` | decimal string or null | Sum of Novig opposing orders at the best bid only, in native one-cent contracts | Missing size, legacy quote, Kalshi/US catalogs (no depth read) |
| `native_quote.price_convention` | string | `yes_book_dollars` (Kalshi/US) or `selected_outcome_probability` (Novig); Novig probability is a fraction of its one-cent payout | Legacy evidence missing |
| `native_quote.quantity_unit` | string | `one_dollar_contracts` (Kalshi/US) or `one_cent_contracts` (Novig) | Legacy evidence missing |
| `native_quote.observed_at` | UTC string or null | Kalshi event-page receipt; Novig individual book receipt (NBX event-book response); US event-list/detail receipt | Historical capture without receipt; never renewed on carry/serialization |
| `native_quote.provider_updated_at` | UTC string or null | Kalshi `updated_time` / US `updatedAt`, a market metadata clock, not certified book freshness | Provider omitted a timezone-qualified clock; Novig does not supply one here |
| `native_quote.sequence` | integer or null | Novig book `seq` | No sequence; other venues omit |
| `native_quote.source`, `clock_source` | string | Native field/catalog and collector receipt provenance | Legacy evidence missing |
| `fee` | object | Catalog-effective taker rate and existing venue rounding implementation; account-specific overrides are unverified | Default-null on legacy carry |
| `fee.coefficient` | decimal string or null | Kalshi `0.07 * series.fee_multiplier` only for known quadratic types; US `feeCoefficient`; Novig market fee after explicit phase handling | Missing/unsupported fee metadata or unknown Novig phase/schedule |
| `fee.listed_coefficient` | decimal string or null | Novig unmodified catalog rate before phase handling | Missing/invalid rate; other venues omit |
| `fee.charged` | string or null | Kalshi/US `taker`; Novig catalog `ALWAYS` / `WHEN_LIVE` (unknown values remain visible) | Missing Novig schedule; legacy evidence |
| `fee.phase` | string or null | Novig `pregame` only for explicit `OPEN_PREGAME` event status | Status not explicit pregame; other venues omit |
| `fee.source` | string | Exact catalog fee fields | Legacy evidence missing |
| `fee.rounding_rule` | string or null | Kalshi: fee ceiling to 1 microdollar, total debit ceiling to 100 microdollars; Novig: USD 5 decimals half-up; US: USD cent half-even | Legacy evidence missing |
| `fee.rounding_micros` | integer string or null | Conservative rounding allowance in USD microdollars: Kalshi `100`, Novig `10`, US `10000`; use with the full rule, not as a claim all fees round up | Legacy evidence missing |
| `fee.rounding_source` | string | Kalshi `pipeline.odds.kalshi_fees.effective_price`; Novig/US existing `site/worker/execution-preview.js` fee models | Legacy evidence missing |
| `fee.account_verified` | boolean | Always false: collectors do not read account fee overrides | Never true in v1; missing on legacy evidence |
| `fee.included_in_display_odds` | boolean | Novig false: existing parser does not add catalog fees to displayed odds; native fee evidence is separate | Other venues omit |
| `settlement` | object | Catalog evidence only, no inferred venue policy | Default-null on legacy carry |
| `settlement.rules` | string or null | Kalshi primary + secondary rules joined by newline; Novig explicit `settlementRules` or `market_detail.settlementRules`; US market description | Collected catalog omitted rule text |
| `settlement.rules_url` | string or null | Explicit catalog `rules_url` (Kalshi) / `rulesUrl` (Novig/US) | No URL returned; no guessed/static link substituted |
| `settlement.rules_sha256` | string or null | Lowercase SHA-256 of exact published UTF-8 `rules` | No text |
| `settlement.rules_url_sha256` | string or null | SHA-256 of the URL string only; does not certify remote contents | No explicit URL |
| `settlement.source` | string | Native catalog rules location | Legacy evidence missing |
| `settlement.treatments` | object | `push`, `void`, `overtime`, `postponement` evidence, each with fields below | Never; missing treatments get null values |
| `settlement.treatments.<name>.value` | string or null | Verbatim rule clauses mentioning the treatment; Novig void copies explicit enum `FMV` or `PUSH` | No explicit collected clause/enum; half-point geometry does not invent push/void policy |
| `settlement.treatments.<name>.source` | string or null | Catalog rule location or Novig `.voids` field | No evidence |
| `settlement.treatments.<name>.reason` | string or null | `not_explicit_in_collected_rules` when missing | Null when evidence present |
| `settlement.settlement_verified` | boolean | Always false; catalog text/keyword excerpts do not prove complete, equivalent settlement or independent identity certification | Never true in v1 |
| `settlement.reason` | string | Requires independent policy verification, or no collected evidence | Never |
| `eligibility.current_signal` | boolean | True exactly for an under quote when the existing process is eligible and its final discovery state is `signal` | Never |
| `eligibility.reason` | string | `current_process_under_signal`, `model_signal_is_under_only`, or `process_is_not_current_signal` | Never |
| `eligibility.discovery_state` | string or null | Exact existing `GameCard.process.discovery_state` | No published process decision (helper/legacy input) |
| `eligibility.process_reasons` | array of strings | Existing `discovery_reasons`, unchanged | Empty when none |
| `eligibility.evaluated_at` | UTC string or null | Process decision clock, not quote observation | No process evaluation clock |
| `eligibility.source` | string | `GameCard.process` | Never |
| `missing` | object of string reasons | Dot-path to unavailable evidence; special reasons for missing depth, account override attestation and legacy evidence | Empty only if all required evidence available (not expected in v1) |

## Venue limits and consumer obligations

Kalshi and US nested event catalogs supply native BBOs but no executable size;
no order-book call is added. Novig already fetches books and supplies the selected
outcome's opposing best-bid quantity. Its `WHEN_LIVE` coefficient is zero only
when `OPEN_PREGAME` and a valid listed rate are explicitly present. Unknown live
phase remains null; the execution service must recheck phase and fees at send time.

Rounding sources are existing code, not a new account or exchange attestation.
The Kalshi parser already uses direct-member rounding. Novig and US rounding
rules document the existing Worker preview implementations; this change does
not alter their parser prices. Account fees, settlement equivalence, freshness,
available balance and approvals still require downstream checks. An explicit
current signal does not imply positive fee-adjusted edge or executable depth.

Treatments are verbatim excerpts (or an explicit Novig void enum), not a policy
interpreter. A cancellation clause does not establish every postponement case.
A half-point total's model push probability of zero is not venue push evidence.
No cancellation, refund, overtime or participation rule is filled from memory.
The rules URL is never fetched here. Independent identity/settlement registry
verification remains necessary before a builder can certify an instruction.

## Immutable generation

`board/meta.json` adds `execution_fields_schema_version: 1` for a new pipeline
build. The existing immutable generation manifest retains `schema_version`,
`run_id`, `git_sha` and every object's byte length/SHA-256. It adds:

| Manifest field | Type | Meaning |
|---|---|---|
| `execution_fields.schema_version` | integer | `1` |
| `execution_fields.path` | string | `games[].total_prices.quotes[].execution` |
| `execution_fields.coverage.<nfl|cfb>.quotes` | integer | Priced exchange quotes in those sealed game bytes |
| `execution_fields.coverage.<nfl|cfb>.with_execution_v1` | integer | Actual quote count with the v1 block; not a certified-bet count |

Read meta, pin its generation, verify the manifest and game byte hashes, then
read each quote's block. Partial-sport publications may carry a prior sport with
no block. Coverage reports that honestly; neither the generation clock nor its
commit re-observes a retained quote. Old generations are not rewritten. The
existing scheduled publish job seals and uploads these fields normally; local
dry verification never calls the publisher.
