# Owner-review outbox and ChatGPT integration boundary

`pipeline.notification_scheduler` produces review material only. It has no
sender, dispatch, receipt checkpoint, Telegram token/chat requirement, D1 receipt
archive, or bet/order transport. Selecting an outbox publisher never selects
`board/notification_owner.json` or disables the existing Telegram pipeline. PR #13
and existing assistant automations remain unchanged. Nothing is activated here.

## Producer and read contract

The default CLI prints a plan without loading credentials or doing I/O.
`--verify-only` checks exact source, immutable generation and explicit confirmations
in memory; it creates no outbox and writes nothing remotely. `--run` evaluates
under an OS-held root lock into atomic `data/review-outbox/outbox.json`, with a
matching committed heartbeat. It uses existing R2 SDK credentials for input reads;
it needs no Telegram credential. The installer uses this local-only mode and
registers `Football Weather Owner Review Outbox` disabled if separately approved.

`--read-outbox --expected-sha <full-sha> --root <canonical-root>` reads local
material only and never claims or acknowledges anything. It refuses mismatched
SHA, future/stale batches, degraded heartbeat or a heartbeat that does not match
the exact payload hash. Every batch expires after 30 seconds. A consumer must
recheck current publication identity and explicit confirmations before actual
delivery; a file read is no permission to deliver later from stale evidence.

An optional, separately approved `--run --publish` writes only
`board/owner_review_outbox.json` using `If-Match`/`If-None-Match`, and requires
`board/review_outbox_owner.json`: schema 1, `kind: review_outbox`, exact full
`git_sha`, `hostname`, and canonical normalized `root`. Neither owner nor credentials
are created by this code. A competing conditional write fails without blind retry.
Remote consumers must check the batch's validity/source/confirmation hashes; the
local heartbeat is not an R2 delivery receipt. No public outbox route was added.

Each item has a stable `outbox_id`, exact `revision_sha256`, source run/full SHA/
immutable generation/confirmation hash, candidate notice IDs, and `status:
generated`, `delivery_state: unacknowledged`. Source changes can change the revision
without inventing a new first-notice identity. Current review items are rebuilt
from current evidence; previously queued items disappear when no longer eligible
or after explicit bet confirmation. Durable first observations survive restarts.
The atomic file retains observations and current candidates, not a full historical
outbox archive; it stops at 4 MiB without erasing observations. Archive/retention
review is required before unattended long-term use.

Existing encoded signal rules select weather watches regardless of liquidity.
Every row explicitly says `verified_cash_recommendation: false`; no fresh $500
cash-capacity recommendation is manufactured from board prices. Massive and late
first notices and positive-evidence post-bet CLEAR can be review-ready immediately.
Other first notices form one combined NFL/CFB review batch at 08:00/17:00 Eastern.
Unknown/degraded invalidation cannot create CLEAR. After explicit bet confirmation,
stronger-weather, movement and threshold/reminder candidates are withheld.

Because no delivery acknowledgment is wired, generation does not transition a
first notice into a reminder, mark CLEAR delivered, consume a routine slot, or
prove a two-message daily cap. Unacknowledged notices remain reviewable; the outbox
is not a functioning notification service. Legacy pipeline sender tests retain
their own channel receipts and cannot establish ChatGPT delivery.

## Receipt and acknowledgment semantics for the future adapter

These are an integration contract, not an implemented acknowledgment endpoint:

- **Generated:** durable review content exists. No delivery marker is written.
- **Claimed:** an independently authenticated delivery adapter reserves the exact
  item/revision with a bounded lease. Claiming is not delivery, and a read-only
  consumer never writes a claim. Lease expiry does not prove no send occurred.
- **Confirmed delivered:** the supported delivery path returns durable evidence of
  the actual ChatGPT message publication (message/conversation identity and delivery
  time), bound to the exact outbox ID, revision, candidate IDs and routine slot.
  Only that authenticated acknowledgment may consume first/CLEAR markers and the
  shared Eastern daily slot. A user opening/reading the message is not required.
- **Unknown:** a request was made but completion is uncertain. Reconcile against
  platform evidence before resending; do not record success or blindly retry.
- **Superseded:** newer evidence/confirmation makes a prior revision inappropriate.
  Revalidate immediately before send; confirmed bets permit verified CLEAR only.

The adapter must persist claims/receipts in independently owned storage with
conditional writes, reject forged/stale/mismatched acknowledgments, combine routine
NFL/CFB into at most two delivered messages/day, dedupe notice/CLEAR IDs across
restarts and revisions, and permit reminders only before explicit bet confirmation.
The reserved `board/owner_review_receipts.json` name is protected from generic
build publication. The producer neither reads nor writes receipts today.

## Exact unsupported boundary

The supported assistant automation tools available in this session can create,
update, or request runs; a successful run request is not a message-delivery receipt.
They expose no callable message-publication acknowledgment or reconciliation
callback that this Python runner can safely bind to its item/revision. No adapter,
polling automation, webhook, fabricated ChatGPT API, credentials, or receipt writer
was created. Cloud automation also has no established authenticated read path to
this local file/private R2 object. Those access and acknowledgment boundaries must
be resolved with the supported platform before actual ChatGPT delivery is claimed.

User approval for disabled task registration alone does not authorize starting
it, publishing outbox data, creating/changing an assistant automation, or retiring
Telegram. Telegram retirement remains the independent PR #13 approval gate.
