# Resident quotes, explicit bet lifecycle, and immutable publication

This phase is staged for review above `codex/football-ui-contract-integration`
(PR #16, tested head `0a1abd0b01afe1504ae115b4ad1970c9e87c916a`). It does not
install or activate a scheduled task, dispatch a refresh, send a notification,
place a bet, change credentials, or deploy the Worker during implementation.
PR #15 and PR #16 remain separate review and deployment gates. PR #13 and the
existing automation schedules are unchanged. Breakout and the canceled BetCRIS
repair are outside this work.

## What changes

The former fixed board objects could be overwritten independently while readers
still saw an older `meta.json`. A verified content-addressed generation now
contains the exact board bytes and checksums. Publishers upload it first, read
every object back, and write the current meta pointer last. A failed staging
attempt leaves the preceding immutable generation available. Worker requests
pin one generation and check object hashes; initial browser loading pins the
selected generation across both sports. An unavailable payload degrades the
board and removes actionable qualification. Legacy publications are explicitly
unverified until a new generation is published.

The resident collector captures public exchange depth and original response
clocks without rebuilding weather, moving baselines, sending alerts, or writing
orders. The live quote overlay is separate from the weather/model generation.
The browser requires the same full source SHA and board run, a current owner
heartbeat, and unexpired depth evidence. A heartbeat alone cannot make a
provider healthy or a quote executable. An expired or future quote never gains
a newer source timestamp through a refresh or render.

Opener state now distinguishes immutable first observation, provider-attested
original opening price, and the T-minus-six-days reference. Existing T-6 rows
migrate into references rather than masquerading as first observations. A true
opener requires source and original timestamp evidence; conflicting evidence
is retained without replacing the original. CFB's existing encoded opening
spread and T-6 total reference rules are preserved. Previously overwritten
original observations cannot be recovered by this migration. D1 opener writes
use `INSERT OR IGNORE`; reconciliation of already inaccurate historical rows
requires separate evidence and approval.

The alert policy retains existing signal tiers and rules. Production
`Config.from_env()` enables the interview policy; directly constructed
`Config()` keeps legacy caller compatibility. First High/Very High signals and
verified CLEAR invalidations may be immediate. Other first notices and
pre-confirmation reminders enter the 08:00 or 17:00 Eastern routine slot, with
at most two successful routine messages per chat and local day. Failed delivery
does not mark the notice or consume its slot. First notices take priority over
reminders, with compact summaries instead of long price explanations.

Only explicit user confirmation changes bet state. Silence, viewing a quote,
requesting a preview, and seeing $500 depth do not confirm a bet. Once confirmed,
stronger weather, line movement, threshold oscillation, and routine reminders
are suppressed. CLEAR requires a known hard invalidation or complete, current
weather that no longer meets the encoded rules; missing or degraded providers
cannot prove invalidation. The production notification path rereads the
Worker-owned confirmation ledger after depth work and immediately before each
message/group. Read failure stops delivery. A confirmation accepted after that
last read and during the external delivery request remains a narrow transport
race; the two systems cannot be atomically committed together.

A drop greater than roughly 10% is flagged only against a provider-attested
original total. Unknown original prices remain unknown, with first-observed
or T-6 movement labeled separately. Neither weather eligibility nor first
notice depends on liquidity. A weather watch is distinct from the existing
fresh $500 principal-stake preview, which verifies actual taker depth, fees,
native contract units, exact line, and compatible settlement rules. $500 is the
recommendation capacity threshold, not a universal user bet stake.

## Ownership and retention

| Object | Writer | Reader contract |
| --- | --- | --- |
| `board/meta.json` and `board/generations/<sha256>/...` | Serialized pipeline publication | Check manifest, source/run identity, sport counts, and every referenced byte checksum before commit |
| `board/live_quotes.json` | One explicitly selected resident collector | Conditional R2 writes; current owner, full SHA, run identity, heartbeat, depth clocks and expiry required |
| `board/bet_confirmations.json` | Authenticated admin confirmation endpoint | Explicit acknowledgement; immutable bet ID/details; conditional R2 updates; pipeline never uploads this ledger |
| `board/alerts.json`, `board/telegram_state.json` | One production notification sender | Durable receipt checkpoint before proceeding to another message |
| `board/alerts_live_feed.json` | Published notification receipt checkpoint | Live receipts overlay only their matching current run; historical generation reads stay immutable |
| `board/cf_heartbeat.json` | Existing Cloudflare cron handler | Pipeline reads but never overwrites this operational heartbeat |

The collector uses an OS-held local owner lock, and R2 `If-Match`/`If-None-Match`
conditions prevent competing remote writers. It refuses a clean remote owner
with a heartbeat newer than 60 seconds or an unknown/future clock. A failed
conditional write is not blindly retried. Source selection requires a clean
tracked checkout, an explicitly selected full commit SHA, and a published board
with that same SHA.

Each public response is retained before parsing as raw bytes plus a manifest
containing the public URL, receipt time, status, byte count, SHA-256 and Age
header. Captures have no account authorization headers. Individual responses
are limited to 20 MiB. Collection stops when retained raw storage exceeds
512 MiB; it does not silently erase evidence. An archival/retention policy is
required before unattended long-term activation. Existing compact odds/weather
history caps remain; this phase does not replace them with a complete empirical
forecast-vintage and observed-weather archive.

## Local installation is a separate action

`scripts/install_resident.ps1` prints a reviewable plan by default. `-Install`
registers a named **disabled** task, refuses an existing task with that name,
and copies no credentials. `-Activate` is a separate explicit flag, and
`-Publish` is another explicit choice. Do not run the following installation
or activation steps without approval:

1. Review the default plan with the actual Python executable and selected
   40-character SHA.
2. Verify Node is available to the scheduled user and existing R2 SDK credentials
   resolve without printing or copying them. The current host's credential
   availability has not been established by staging this implementation.
3. If approved, register the disabled task with `-Install`; inspect its action,
   repository, selected SHA, ownership and disabled state.
4. Approve publishing and activation separately; `-Install -Activate -Publish`
   would enable and start it. No such command has been run for this phase.

The task runs hidden, with limited privileges and interactive-user ownership,
starts at logon, prevents overlapping task instances, and restarts on failure.
It requires the host to remain logged in and awake. The OS lock provides a
second overlap guard even if another task starts the collector directly.

The default cohort is four games per nominal 10-second cycle, with at most
twelve mapped market references per game and four concurrent games. Cohorts
rotate independently of liquidity and exclude known hard roof/opening-spread
ineligibility. All-game publication verification and provider latency can make
cycles longer. This is not a promise that every game's quote is younger than
15 seconds. Per-game freshness is assessed separately, and on-demand requests
remain necessary outside the current fresh cohort.

## Confirmation endpoint

`GET /api/bet-confirmations` and `POST /api/bet-confirmations` require admin
identity. POST requires same-origin JSON, `confirmed: true`, and the exact
acknowledgement `I placed this bet`, together with `bet_id`, canonical `game_id`,
exchange `book`, `side: under`, exact `line`, and actual cash `stake`.
The stake may be below or above $500. This records an already placed bet and
has no order/account transport. Repeating identical details under the same
ID succeeds; conflicting details return 409. Conditional-write contention
cannot claim success or overwrite another confirmation.

The endpoint is implemented, but a low-effort confirmation control in the
desktop/phone drawer remains to be implemented and reviewed. Users should not
be expected to operate the JSON endpoint as the final product workflow.

## Required activation and acceptance sequence

1. Review and approve PR #15, PR #16, and this stacked phase independently.
   Preserve their tested heads. Confirm the deployment authority before any
   merge, workflow dispatch, or production activation. Main pushes touching
   `site/**` automatically run the existing deploy workflow: canceling the job
   does not prove it failed to activate a Worker version.
2. Deploy the reader only after approval, verify the exact uploaded artifact,
   official Worker version and 100% traffic assignment, then verify served
   behavior. Legacy generation status should remain visibly unverified.
3. After approval, publish a notification-disabled safe refresh for both sports.
   Verify the remote pointer, every immutable object hash, exact sport counts,
   source SHA, run identity and unchanged original source clocks. Do not accept
   green CI or a uploaded artifact as publication proof. Measure immutable
   upload/readback latency against the workflow freshness guard. Consumers must
   expire any quotes that aged during publication; no TTL extension is allowed.
4. Install disabled, inspect, then activate the selected resident owner only
   with separate approval. Demonstrate no overlapping local owner, no remote
   CAS overwrite, correct SHA/run, original raw receipts, and degraded status
   on missing/future/expired quotes, provider failure, lost ownership or quota.
5. On desktop and phone, request one game's fresh executable preview. Verify
   total response time below 30 seconds including R2/publication/provider waits
   (Worker route bound is 28 seconds; provider operation bound is 25 seconds).
   Verify exact line, venue, all-in rounded price, cash principal separately
   from fees, native payout/contracts, settlement policy, depth source clock
   and expiry. Two incompatible $300 offers must not become a $500 result.
   Hard-ineligible games stay absent from table and map; weather-eligible games
   remain discoverable when liquidity is absent. Check one-tap exact stadium
   coordinates and user input across polling/navigation.
6. Complete the explicit confirmation UI and prove it never triggers an order.
   Use test ledgers and fake notification senders for acknowledgement,
   retry/idempotency, post-confirmation suppression and CLEAR-only behavior.
   Then select one durable production notification owner. The quote resident
   does not provide an alert sender. Do not enable a second sender beside the
   pipeline without shared ownership and receipt discipline.
7. Supply an exact 08:00/17:00 America/New_York scheduler, including DST behavior,
   without merging or altering PR #13 or existing automations implicitly. The
   current policy accepts the first successful pass in each named hour;
   existing refresh schedules do not guarantee the exact minute. Resolve the
   first non-massive signal discovered after the last routine slot but before
   kickoff: the current queue cannot guarantee its notice before kickoff.
   Likewise prove delivery under digest overflow; excess notices remain
   unmarked for a subsequent slot. Do not claim these requirements complete.
8. Integrate documented provider-attested opening data. No existing adapter in
   this phase supplies the new attestation fields, so true opener and true
   opener movement may remain unknown. Use separate evidence for historical
   repairs and exclude the canceled BetCRIS repair.
9. Complete the forecast-vintage/truth archive, error estimation by stadium,
   terrain, season, kickoff and lead time, joint signal calibration, outlier
   treatment and fair-price residual validation before displaying calibrated
   likelihood or investment edge. Screening margins and ensemble spread remain
   engineering diagnostics, not empirical probability or accuracy. Current
   full-weather fair adjustments still require double-counting validation.

## Reproducible staged checks

Validated on Windows: 1,318 Python tests passed (one existing xlsxwriter version
warning), 106 Worker/UI tests passed, and Ruff passed. No production operations
were exercised by those tests.

Run locally without installing, activating, publishing or notifying:

```text
ruff check .
python -m pytest tests -q
node --test site/worker/test/*.test.mjs
```

The tests exercise conditional ownership, raw capture before parsing,
source/generation rejection, stale/future quotes, immutable publication
corruption and partial replacement, independent current-run alert receipts,
bounded noncooperative R2/provider reads, explicit confirmation idempotency,
confirmation arriving after planning, Eastern/DST routine caps, failed-send
retry, CLEAR once, degraded weather withholding CLEAR, authoritative opening
spread eligibility, unknown original prices, and compact first-notice priority.
Live provider timing, exact scheduling, host credentials and full browser/phone
acceptance require the separately approved activation work above.
