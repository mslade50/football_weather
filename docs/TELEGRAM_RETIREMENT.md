# Football Telegram retirement

Football Telegram delivery is retired by default behind the non-secret
`FOOTBALL_TELEGRAM_ENABLED` master switch. Only the exact value `1` enables it.
Missing, empty or unrecognized values fail closed. Existing `TELEGRAM_DISABLED`,
pytest protection, safe-refresh and system-alert opt-in checks remain independent.

The replacement is the enabled ChatGPT **Football weather opportunities** monitor:
hourly 07:00-23:00 America/New_York. It dynamically reads the existing GitHub
published-board archive, verifies publication receipts and generation, checks real
quote ages and kickoff, and reports WATCH/candidates for review. It does not place
bets or infer stakes. Two initial scheduled reads retrieved the same stale generation
and suppressed unchanged notifications. Numerical entry and stake criteria remain
unconfirmed. The scheduled agent is instruction-driven; the credential-free fixture
acceptance harness is separate and is not represented as its production runtime.

## Sender coverage

- Python `utils.telegram.send_message`: transport-level master gate.
- `pipeline.published_alerts`: stops before remote preflight or delivery-state writes.
- `pipeline.alerts`: disabled planner skips liquidity/dispatch/persistence; digest and
  flush CLI delivery exits successfully without initiating SMTP fallback. Explicit
  dry-run rendering remains available.
- `pipeline.yml`: both post-publication notifications and its direct failure job.
- `backtest.yml`: weekly delivery and direct failure message. Backtest generation,
  upload, archives and grading continue; deliberate retirement does not cause email.
- `deploy.yml`, `calibrate.yml`, `build-stadiums.yml`: direct Telegram failure messages.
- Worker `notifyTelegram`: token/dispatch-failure messages gated before HTTP.

The GitHub repository variable defaults to `0`; Worker `wrangler.toml` sets `0`.
Authenticated `/api/status` reports the effective Worker master flag without exposing
credentials. Other repositories or apps' notifications are not affected.

## Preserved behavior

No model, provider, market, preview/order logic, security/access rule, refresh cadence,
cron, grading, odds/weather history, opener, archive or secret is removed or modified.
Historical Telegram receipts and queues remain stored. They are not successful
delivery evidence for the new monitor. Future monitor observations belong to its own
history; the retired Telegram first-entry cohort stops acquiring new sent entries.
No message or synthetic live opportunity is sent to Telegram to verify this change.

## Verification

Credential-free replacement cases: fresh forecast watch; stale odds; missing depth;
degraded ensemble; unchanged duplicate; material weather/price change; started game;
publisher-generation mismatch. Additional cases cover expired/future quotes, closed
roofs, unchanged stale sources, and accumulated history with an older header run ID.
History row clocks and the publication receipt matter; only current meta/sport feeds
must share current generation markers.

Retirement tests forbid HTTP, remote preflight, new delivery receipts, SMTP fallback
and liquidity retrieval while off. Worker tests retain heartbeat and collection
dispatch on failures. Workflow contracts enumerate and gate each Telegram sender.

## Rollback (only after an explicit request)

1. Pause retirement reversal until queue items are checked against current data and
   kickoff. Do not manually flush or replay retained stale snapshots.
2. Set the GitHub repository variable `FOOTBALL_TELEGRAM_ENABLED=1` to restore future
   workflow delivery. For a local CLI, set the same process environment value.
   `safe_refresh` and `TELEGRAM_DISABLED=1` still suppress delivery.
3. Set Worker `FOOTBALL_TELEGRAM_ENABLED` to `1` in `wrangler.toml` and use the existing
   deploy workflow. Worker system notices additionally require the separate existing
   `TELEGRAM_SYSTEM_ALERTS=1`; do not change that opt-in without a request.
4. Verify non-secret effective status and a normal published run. Keep collection
   crons intact. To retire again, restore both master settings to `0`.

Reverting this notification-only commit is an alternative rollback, but must still
avoid replaying stale queue entries and must preserve any later unrelated changes.
