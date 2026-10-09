# Activation checklist — pending separate approval

PR #17 is a draft. Approval to release PR #15/#16 does not authorize merging or
deploying PR #17, publishing a validation refresh, installing/starting either
resident task, selecting a notification owner, or sending test notifications.
Nothing in this checklist has been activated. Existing automations and PR #13
remain unchanged. Do not make further direct requests to the denied live-board
URL or use another route to bypass that denial. Served behavior remains
unverified; use permitted control-plane and publication receipts until access
is explicitly resolved.

## Current release evidence

| Release | Source and validation | Deployment evidence |
| --- | --- | --- |
| PR #15 | Tested head `c0c29aa5aa736cdf0bbe5a7f2dd95599bafc9708`; merge `2760a412968fef961153c8dd04b7dd619449a9e5` | Deploy [37989342789](https://github.com/mslade50/football_weather/actions/runs/37989342789); Worker `8b222580-640d-4793-b723-7b48e3888ea8`, 100%, activated 2026-10-09 20:48:39.669081 UTC |
| PR #16 | Rebased head `c0eccc09094eaee5f18dd3a738278a98a0f9884a`; source bytes identical to prior tested head; 1,298 Python and 96 Worker/UI tests, Ruff; [CI 37989649822](https://github.com/mslade50/football_weather/actions/runs/37989649822); merge `df1f408b35e267ee5b1d8b598e1a11cb583a3ff2` | Deploy [37989925301](https://github.com/mslade50/football_weather/actions/runs/37989925301); Worker `46743ae1-8f00-4bf0-90ad-8c14ac17bc97`, 100%, activated 2026-10-09 20:54:01.013887 UTC |
| Current data receipt | R2 read at 20:56:18 UTC: run `20261009T182858Z-gh37972654586-1-pw`, last_updated 18:30:17 UTC, older source `2e69a97b5079`; NFL 27/CFB 113, envelopes/cards match meta | No immutable-generation manifest yet. This proves R2 coherence at that read, not served UI freshness or current-source model output. |

The previously approved Worker rollback was superseded by these approved
deployments. Future rollback must select the then-current previous version from
fresh official metadata; never replay an old rollback command blindly.

## Credential and runtime blockers

A read-only presence check of the existing development repository `.env` found
Cloudflare API token, Telegram bot token/shared chat, and board admin password
entries. It found **no R2 access key ID, R2 secret access key, or configured
Cloudflare account-ID entry**. No values were copied, printed, placed in this
document, or verified as available to the scheduled user.

The quote and notification runners use R2's S3 SDK. Before activation an
approved credential path must supply `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
and the correct nonsecret `CF_ACCOUNT_ID`/`CLOUDFLARE_ACCOUNT_ID` to the selected
runtime. Do not assume that the existing Wrangler API token provides S3 SDK
credentials. Do not provision, copy or store credentials implicitly. The
notification runner also needs the existing `TELEGRAM_BOT_TOKEN`, one shared
`TELEGRAM_CHAT_ID`, and Wrangler/D1 archive permissions for its durable receipt
archive. Bot/chat presence in one `.env` does not establish delivery permission
or scheduled-process access.

A bounded names-only desktop preflight found the existing `GolfRichShotCollector`
uses `DESKTOP-2KI41V6\McKinley Slade` with Interactive/Limited task identity and
`C:\Users\McKinley Slade\AppData\Local\Programs\Python\Python310\python.exe`.
Its hidden launcher runs `shot_collector.rich_service` in the isolated
`golf_scraping-collector-prod` checkout. This identity/executable/task pattern
can be reused without changing golf. No football task was present. The actual
user/machine registry environment exposed no football R2 SDK provider names;
the user scope declared only the relevant golf `IMG_SHOT_INGEST_TOKEN`.
The inspected golf wrappers inject no relevant credentials, the collector
checkout has no `.env`, the golf odds `.env` declares no R2 provider names, and
this user's `.aws` directory is absent. These are provider-name/path findings,
not a claim that no key exists anywhere or that declared values are valid.
Football's SDK implementation has no Wrangler, keyring or AWS-profile fallback.

The existing GitHub/Cloudflare **Wrangler** path can publish the first immutable
generation without new S3 credentials: after separately approved PR #17
merge/deploy, explicitly request one `pipeline.yml` run on released `main` with
`sport=all`, `scope=exchanges`, `safe_refresh=true`, `force=false`. This makes
schedule/weather/exchange reads and writes `football-board` R2 raw, snapshot,
legacy and generation objects/meta plus `football-odds` D1 history, with no
Telegram sends or bets. Merging/deploying alone does not refresh data. Keep
the gates enabled and verify the actual receipt/source/hash/counts afterward.
For the current local SDK runners, securely supplying an existing authorized
R2 key pair to the selected runtime (or separately authorizing a new scoped
pair) remains necessary. Do not paste keys into chat or put them in task
arguments. The golf ingest token is not a football storage credential.

The exact planned disabled task names are `Football Weather Resident Quotes`
and `Football Weather Notification Clock`. Both start at user logon if later
activated. Quotes targets 10-second cycles with four-game rotating cohorts;
long reads may take up to 28 seconds. Notification Clock polls at most every
10 seconds and targets 08:00/17:00 America/New_York. `--verify-only` is one-shot
no-send/no-remote-write inspection; `--run` is capable of real Telegram delivery.
No continuously running no-send notification mode is claimed. Keep installation
disabled and activation/owner handoff separately approved.

Select one clean checkout of the approved full SHA, a known Python executable,
Node in the scheduled user's PATH, writable canonical state roots, and a host
that remains logged in and awake. Resolve the 512 MiB raw-capture archival policy
before unattended operation. Retain originals and checksums; reaching the limit
must degrade/stop collection rather than erase evidence.

## Reviewable sequence

1. Review the final PR #17 diff and exact-head CI. Obtain explicit merge/deploy
   approval. Record the previous active Worker version and traffic, then verify
   the new uploaded version and official 100% traffic assignment after release.
   A canceled or green workflow alone is not deployment proof.
2. Obtain approval for a notification-disabled validation publication of both
   sports. Check the allowed remote meta pointer, full source SHA, every manifest
   object hash/byte count, exact sport counts and all card/envelope run IDs.
   Confirm original quote and weather clocks were not renewed by publication.
   Current legacy data cannot activate either resident runner.
3. Resolve the approved runtime credentials without disclosing values. Run the
   quote collector once **without `--publish`** to validate ownership lock,
   raw-before-parse captures, actual source timestamps and local expiry. This
   writes local evidence only and has no alert or order transport.
4. Run `python -m pipeline.notification_scheduler --expected-sha <full-sha>
   --root <canonical-root> --verify-only`. This reads and validates publication,
   alerts and confirmations; it sends zero notifications and writes zero remote
   receipts. It reports whether an existing owner selection matches. Keep
   configuration absent/unselected until the handoff is separately approved.
   Default invocation without `--run`/`--verify-only` merely prints the next
   target. Do not combine verification with transport activation.
5. Review the default `scripts/install_resident.ps1` plan with `-PythonPath` and
   `-ExpectedSha`. `-Mode Quotes` and `-Mode Notifications` select separate task
   names and roots. After installation approval, `-Install` registers a disabled
   task; inspect executable, arguments, source, principal, root and disabled
   state. Do not pass `-Activate` in this step. No credentials belong in task
   arguments.
6. Separately approve quote publishing/activation. In quote mode,
   `-Install -Activate -Publish` would install, enable and start it. For an already
   installed disabled task, inspect it first and use an explicitly approved
   enable/start action rather than replacing it. Capture owner UUID/PID/SHA,
   first conditional R2 write, heartbeat, actual raw quote clock and expiry.
7. Separately approve notification handoff and activation. Drain existing
   notification workflows before selecting one remote owner. The configuration
   contract is `board/notification_owner.json`, schema 1, `kind: local`, full
   `git_sha`, exact `hostname`, and canonical `root` using normalized forward
   slashes. The matching pipeline version must abstain from delivery and the
   local OS lock must reject a second runner using that root. Keep root/host
   selection exact; do not activate two independently selected roots. Recheck
   ownership before every send and receipt write. Then explicitly enable/start
   the disabled notification task. No real notification tests are authorized
   by staging this code; use fixtures until separate permission exists.

## Acceptance evidence

- Healthy liveness requires a current owner/SHA/run, actual raw receipt clocks,
  non-future unexpired depth, successful conditional writes and verified source
  outcomes. A heartbeat alone is insufficient. Stop/kill one collector and
  verify stale/future/missing/failed quotes degrade instead of staying green.
  Prove another local owner and competing fresh remote owner cannot take over.
- Measure exact 08:00 and 17:00 America/New_York clock targets, observed start and
  transport completion separately. Validate both DST transitions, five-minute
  retry recovery, at most two successful shared routine messages/day, failed
  sends consuming no slot, durable restart deduplication, and immediate first
  notices before kickoff when no routine slot remains. Failed transport or a
  sleeping host prevents a delivery guarantee and must be visible as degraded.
- With fake ledgers/senders, validate editing, acknowledgement, saving,
  uncertain outcome, same-ID retry/reload reconciliation, confirmed, conflict,
  and recording a separately acknowledged second bet. A $125 stake is valid;
  $500 is the depth recommendation threshold. Confirm stronger weather, line
  movement, threshold oscillation and reminders stop after acknowledgement,
  while complete current invalidation emits one CLEAR and degraded weather
  emits none. No quote check or drawer polling submits the form.
- Once live access is explicitly resolved, separately validate desktop/phone
  form layout, focus and navigation, exact lat/lon copy, table/map eligibility,
  near-signal screening, fee-inclusive $500 principal depth and response time
  below 30 seconds, including publication/provider waits. Keep these served
  tests marked unverified while the URL denial remains in force.

## Rollback and stop procedure — also requires authorization

1. Disable and stop only the named task(s) activated for this release; do not
   delete state, raw captures, alert receipts, opener provenance or confirmations.
   Quote overlay expiry should remove stale qualification automatically. Capture
   final heartbeat/status, task state and preserved raw evidence.
2. For notification handoff, stop/drain the local sender before explicitly
   selecting the pipeline owner (`kind: pipeline`) or returning to the prior
   approved ownership setting. Verify a single sender. Do not delete delivery
   markers; do not assume transport failure proves a message was never sent.
3. If Worker rollback is approved, restore the freshly identified previous
   version through the documented deployment API, then verify version and
   traffic in official metadata. Preserve the receipt and matching source CI.
4. If publication rollback is approved, restore the previously verified meta
   pointer to its retained immutable generation. Do not overwrite original
   quote/weather clocks or manufacture a fresh timestamp. Keep partial failed
   staging objects for investigation; do not erase historical evidence.

Provider-attested original openers, historical overwritten-baseline recovery,
empirical forecast-error/joint-signal calibration and fair-adjustment
double-counting validation remain data/evidence work. Missing evidence must
remain explicit. The canceled BetCRIS repair and Breakout are outside this phase.
