# Activation checklist - pending separate approval

PR #17 is a draft. PR #15/#16 release approval does not authorize PR #17 release,
validation publication, task registration/start, ownership selection, outbox
publishing, ChatGPT delivery, or changes to automations/PR #13. No task has been
installed or activated. The owner-review outbox replaces the proposed resident
Telegram sender; it must never be described as connected ChatGPT delivery.
Existing Telegram remains unchanged pending its separate PR #13 retirement gate.
Do not request the denied live-board URL again or use another route to bypass
the denial. Served desktop/phone behavior remains unverified.

## Current release and runtime evidence

| Evidence | Verified condition |
| --- | --- |
| PR #15 | Merge `2760a412968fef961153c8dd04b7dd619449a9e5`; deploy [37989342789](https://github.com/mslade50/football_weather/actions/runs/37989342789); Worker `8b222580-640d-4793-b723-7b48e3888ea8`, 100%, activated 2026-10-09 20:48:39.669081 UTC |
| PR #16 / current released main | Tested head `c0eccc09094eaee5f18dd3a738278a98a0f9884a`; [CI 37989649822](https://github.com/mslade50/football_weather/actions/runs/37989649822); merge `df1f408b35e267ee5b1d8b598e1a11cb583a3ff2`; deploy [37989925301](https://github.com/mslade50/football_weather/actions/runs/37989925301); Worker `46743ae1-8f00-4bf0-90ad-8c14ac17bc97`, 100%, activated 2026-10-09 20:54:01.013887 UTC |
| Latest permitted R2 read | 2026-10-09 21:18:22 UTC; run `20261009T182858Z-gh37972654586-1-pw`, updated 18:30:17 UTC, older SHA `2e69a97b5079`; NFL 27/CFB 113, envelope/card/meta identities match; no immutable manifest. This is storage coherence, not served UI/current-source model proof. |
| Existing task pattern | `GolfRichShotCollector`, `DESKTOP-2KI41V6\McKinley Slade`, Interactive/Limited, hidden launcher, Python310 executable; isolated golf collector checkout. LastTaskResult 0 is task metadata, not cloud/provider health. No football tasks found. Do not modify golf. |

Names-only preflight found no football R2 SDK provider names in the inspected
interactive user/machine environment, golf wrappers/configuration or AWS path.
Football `.env` declares Cloudflare API/CFBD/Telegram names but no SDK key pair or
account-ID declaration. No secret values were copied or validated. Other private
storage remains unknown. The golf ingest token is not a football credential.
Current runners load only selected-repository `.env` plus inherited environment;
there is no Wrangler, keyring or AWS-profile fallback. R2 input reads require
`CF_ACCOUNT_ID`/`CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and
`R2_SECRET_ACCESS_KEY`. Securely configure an existing authorized pair, or separately
approve creation of a scoped pair. Never put keys in chat or task arguments.
The outbox requires **no Telegram token/chat or D1 receipt-archive permission**.

The approved GitHub/Cloudflare Wrangler path can publish an immutable generation
without new S3 credentials after separately approved PR #17 merge/deploy: request
one main `pipeline.yml` run, `sport=all`, `scope=exchanges`, `safe_refresh=true`,
`force=false`. This reads schedule/weather/public exchange providers and writes
football-board raw/snapshot/legacy/generation/meta objects and football-odds D1
history, with no notifications or bets. Existing schedules remain unchanged;
safe_refresh applies only to that run. Merge/deploy alone does not refresh data.

## Reviewable sequence

1. Finish PR #17 review and exact-head CI. Separately approve merge/deploy. Record
   the current previous Worker from official metadata before release; verify the
   new exact source/version and 100% traffic afterward. A green/canceled job alone
   is insufficient deployment proof.
2. Separately approve the safe validation publication described above. Read actual
   meta/manifest/objects and verify every hash/byte count, full source SHA, run ID,
   exact sport counts and card/envelope identities. Original quote/weather clocks
   must remain original. Legacy data cannot activate either runner.
3. Resolve secure SDK input access for the selected interactive runtime. After
   approval, run quote capture once without `--publish`; verify local lock, raw
   receipts, real provider timestamps and expiry. It has no alert/order transport.
4. After permission for source reads, run `python -m pipeline.notification_scheduler
   --expected-sha <full-sha> --root <canonical-root> --verify-only`. This validates
   publication and explicit confirmations in memory with zero transports/remote
   writes. It does not require/select Telegram ownership. Default invocation
   merely prints a plan without credential loading or I/O.
5. Review installer plans for `-Mode Quotes` and `-Mode ReviewOutbox`. If approved,
   `-Install` registers `Football Weather Resident Quotes` and `Football Weather
   Owner Review Outbox` **disabled**, with the existing Python310 executable,
   clean released full SHA, exact repository/root and Interactive/Limited user.
   Do not pass `-Activate`. No task or process is started by disabled registration.
6. Separately approve quote publication/start. Inspect an existing disabled task
   before enabling/starting it; do not replace it blindly. Capture owner UUID/PID,
   SHA/run, first conditional R2 write, provider/raw receipt clock and expiry.
   Review the 512 MiB raw archival policy before unattended operation.
7. Separately approve local outbox evaluation/start. `--run` persists review items
   locally and sends nothing. `--read-outbox` reads fresh committed material with
   no network or writes. Resolve its 4 MiB observation-retention limit. Installation
   defaults to local-only production; it does not connect any assistant automation.
   An optional remote outbox publication requires its own approval, `--publish`,
   and independently selected `board/review_outbox_owner.json` (schema 1,
   `kind: review_outbox`, exact full SHA, hostname and canonical normalized root).
   No outbox action changes `board/notification_owner.json` or retires Telegram.
8. Connect ChatGPT delivery only through a separately authorized supported assistant
   automation after resolving authenticated outbox access, actual message delivery
   acknowledgment and uncertain-outcome reconciliation. See the [outbox contract](owner-review-outbox.md).
   The available tools expose no acknowledgment callback this Python runner can
   safely wire; claims/receipts are currently unwired. Creating/requesting an
   automation run or generating a file must never mark delivered. Verify actual
   delivered first notices, pre-confirmation reminders, Eastern routine cap and
   CLEAR dedupe before calling ChatGPT notifications operational. Telegram
   retirement remains separate PR #13 approval.

## Acceptance evidence

- Quote liveness needs matching owner/SHA/run, raw source clocks, unexpired depth,
  CAS success and verified provider outcomes. Killing a collector or supplying
  future/stale/missing/failed evidence must degrade; a heartbeat alone is insufficient.
- Outbox modes must run with no Telegram token and zero sends. Verify stable
  IDs/durable observations across restart; combined NFL/CFB 08:00/17:00 Eastern
  review readiness across DST; immediate massive/late-first/CLEAR readiness;
  read-only consumption; stale/future/degraded/hash failures; source/confirmation
  changes before commit; competing remote writes; and zero receipt mutations.
  Outbox readiness is not delivery and cannot establish the actual daily cap.
- Use fixtures for explicit bet acknowledgment, uncertain save/retry/reload,
  idempotency/conflict and a separately confirmed second bet. Actual stake may be
  $125; $500 is verified cash-capacity recommendation threshold. Confirmation
  withdraws stronger-weather/movement/reminder watches. Positive current
  invalidation permits CLEAR; unknown/degraded evidence permits none and preserves
  a later CLEAR. Generation never consumes a CLEAR marker.
- After live access is explicitly resolved, verify desktop/phone form/focus,
  navigation, one-tap stadium coordinates, hard eligibility in table/map, hidden
  low-likelihood default view, fee-inclusive $500 principal depth and complete
  fresh-preview response below 30 seconds. Keep served tests unverified meanwhile.

## Stop and rollback - separate authorization

Disable/stop only tasks activated for this release; retain state, raw captures,
outbox observations, receipts, opener provenance and bet confirmations. Quote
expiry and 30-second outbox validity must make a stopped process unusable. Capture
the final heartbeat/task state. Stop a remote outbox writer before reselecting its
independent owner; do not alter Telegram ownership as its rollback.

If approved, restore a freshly identified previous Worker and verify official
traffic/version. Restore only a previously verified immutable meta pointer for
publication rollback; preserve original source clocks and failed staging evidence.
Do not replay the superseded earlier Worker rollback.

True-opener attestation/historical recovery, empirical forecast errors and joint
signal calibration, and fair-weather double-counting remain evidence work. No
calibrated accuracy is invented. Breakout and canceled BetCRIS remain outside scope.
