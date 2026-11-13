# Workflow notification and R2 contracts

Workflow failure/recovery messages use `scripts/notify_failure.py`. It uses only
the standard library, posts form-encoded text once, skips missing credentials,
and logs no request, response body, token or exception text. Ordinary calls are
best effort. Pipeline retains its credential guards and uses `--strict` inside
the existing send-before-marker condition. `pipeline.failure_page` still owns
incident policy, message formatting and marker persistence. HTTP success remains
the delivery boundary, as with the former `curl -f`; there is no new Telegram
JSON-body acceptance gate. `utils/telegram.py` is for football bet alerts, imports
httpx and enforces their separate master switch, so system pages cannot reuse it.
Redirects remain unfollowed and retain curl's status acceptance, ensuring one
request without forwarding credentials to a redirect target.

The five ordinary failure steps retain their original conditions and text.
Pipeline retains failure/recovery planning, safe-refresh/cancellation gates and
`FOOTBALL_FAILURE_ALERTS`. Missing credentials now produce a safe one-line notice
in ordinary workflows. Send failures produce a generic warning, with no payload
or raw curl error. Socket timeouts are 15 seconds, or 30 for deploy. The two
stadium workflows previously had no curl timeout. urllib's socket timeout is not
curl's whole-transfer `--max-time` deadline. No send is retried by the transport.

## State membership

`pipeline/outputs/r2.py` owns state names and ordered `STATE_GROUPS`. The shared
scripts accept `--group`; workflows contain no literal `STATE_FILES` environment
list. Explicit groups take precedence over the legacy environment override,
which remains available to existing standalone callers. Fetching a state group
also reads `board/meta.json` into `data/state/prev_meta.json`. Publishing state
still skips absent local files and `BUILD_PROTECTED_NAMES` (including alerts,
telegram_state, cf_heartbeat and bet_confirmations).

The first twelve historical state names, in order, are openers, history,
wx_history, archive_last, wx_last, alerts, scrape_baseline, telegram_state,
cf_heartbeat, closings, status and ensemble_cache. The former lists are preserved
as these named groups:

| Former caller/list | Group | Ordered membership |
| --- | --- | --- |
| `pipeline.outputs.r2.STATE_FILES` | module | First twelve, bet_confirmations, book_health |
| `scripts.publish_r2.STATE_FILES` (also fetch default) | default | First twelve, nws_points |
| `pipeline.yml` environment | pipeline | First twelve, nws_points, bet_confirmations, book_health |
| `backtest.yml` environment | backtest | alerts, closings, history, wx_history, openers |

`STATE_FILES` in the module retains its original module-default tuple, including
the existing publisher key ranking. The script's compatibility alias points to
the centralized default group. No default gains a read or a write.

Local history audit:

- `f01b955` added nws_points to pipeline.yml. `1117a54` carried it into the new
  standalone publisher list. The module list never received it: historical drift,
  now explicitly represented by module versus pipeline/default groups.
- `6b374b9` added bet_confirmations to pipeline.yml and the module list, and
  excluded it from build publication because another writer owns confirmations.
  `51257b0` centralized protected-writer exclusions. Exclusion from writes is
  deliberate. Omission from standalone default reads is historical drift; the
  default group preserves it rather than silently expanding reads.
- `be12392` added book_health to pipeline.yml and the module list for persistent
  book health, without adding it to the standalone publisher's default list.
  That omission is historical drift, preserved explicitly in the default group.
  Pipeline still reads and writes book_health as before.
- Backtest's five-file subset dates to `aea8274`, where it was introduced as
  read-only backtest input. Its smaller membership and different order are
  deliberate. Backtest never writes mutable state or meta.
- Calibrate has never declared `STATE_FILES`. Its `INPUT_GROUPS["calibrate"]`
  reads `backtest/games.parquet`, then `board/backtest.json`, to the same relative
  paths under data/. Missing products are removed locally and tolerated; all
  other fetch errors fail the job. It never publishes to R2.

## Shared modes and failure boundaries

`fetch_r2.py state --group backtest` uses the existing fail-closed state reader.
With the workflow's existing API-token credentials, Wrangler reads serially in
the same order and stops on the first nonmissing failure. The shared reader
stages successful bytes until every fetch succeeds; unlike the old shell loop,
it leaves no partial local state after a failure. The job still fails rather
than running a backtest on incomplete state. Missing notices and fatal-error
wording now use the common reader's text.

`fetch_r2.py inputs --group calibrate` fetches the same two products serially,
tolerates only missing keys, and stops on the first other error. Its common
transport has a 90-second subprocess timeout; the former inline get did not.
Backtest gets also inherit that existing shared timeout. Neither workflow adds
S3 credentials; both retain Wrangler transport in production.

`publish_r2.py backtest` uploads the same sorted top-level `data/backtest/*.parquet`
files to `backtest/<basename>` as application/octet-stream, followed by the
required `data/board/backtest.json` to `board/backtest.json` as application/json.
It is serial, stops on the first exhausted upload, and retains the shell loop's
three attempts and five-second sleep after each failure (including the last).
The shared upload transport adds its existing 90-second timeout and common
logging. This phase bypasses generation/meta publication entirely and does not
write state. Dry-run conditions are unchanged. Pipeline's separate raw,
snapshots, board, state and meta phases are unchanged, with meta last.

`tests/test_workflow_r2_groups.py` pins the historical lists literally and checks
ordered reads/writes, ownership filtering, missing keys and fatal failures using
fake transports. Notification tests also use fake openers. Actual credentials,
Telegram delivery, R2 availability and GitHub skip/condition evaluation require
a real workflow run and are not exercised by these offline checks.

## Observation evidence and D1 checkpoint ordering

Light and Playwright publish builds set `OBSLOG_SHADOW_PATH` to the exact
`data/cache/obslog/observations.sqlite` Actions-cache file. Split v4 restore/save
keys include OS, run, attempt and job; Playwright tries this run's light key
before the shared latest-cache prefix. Best-effort parity, maintenance and save
run even after build failure. The database is never in a state group, upload
manifest, artifact path or remote upload. Parity JSON is under `data/raw_runs/`
in each existing build-log artifact; the job summary contains its counts.
See `docs/notes/observation-log.md` for retention and evidence gates.

The D1 step now runs before the existing R2 publish sequence. Before it, the
local `pipeline.obslog.archive prepare` adapter prepends `pending_d1_sql` from
`archive_last.json` to fresh SQL and retains the exact combined text in that
same existing state object. Success acknowledges it locally before the ordinary
state upload. Failure leaves it queued, still publishes board/state/meta (meta
last), then fails the job after self-check to retain failure/recovery paging.
Replay uses existing idempotent inserts/upserts, including after partial D1
application. There are no extra R2 upload calls, objects, tables or migrations.
The original claim that upload-before-D1 could silently lose deltas was valid;
the alert-receipt recovery step did not recover SQL. This fix changes failure
ordering and the optional pending field, not healthy board or SQL generation.
