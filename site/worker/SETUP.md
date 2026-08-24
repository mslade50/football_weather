# football-board — Cloudflare setup

Private football weather/odds board: Cloudflare Worker (`site/worker`) serving
the static frontend (`site/web`), proxying `/data/*.json` from R2 and small
read-only `/api/*` routes from D1. Data is produced by GitHub Actions
(`.github/workflows/pipeline.yml`). Adapted from `golf_scraping/board`.

- **Account:** `ba4875f01f2bc46dd48e1e26d2ec9080` (mckinleyslade@gmail.com)
- **Worker:** `football-board` → `https://football-board.mckinleyslade.workers.dev`
- **R2 bucket:** `football-board` (`board/` JSON + state, `raw/`, `snapshots/`, `legacy/`)
- **D1 database:** `football-odds` (binding `DB`)
- **Plan decisions:** Workers **Free** (2 cron triggers), **Basic Auth** (not
  Cloudflare Access), OpenFreeMap tiles (no key).

```
GitHub Actions pipeline.yml  ->  python -m pipeline.build  ->  wrangler r2 object put --remote (meta.json LAST)
                             ->  wrangler d1 execute --remote --file data/d1_inserts.sql
Cloudflare Worker (site/worker)
   Basic-Auth gate -> /data/*.json from R2 | /api/* from D1 | /refresh -> workflow_dispatch | else static site/web
   scheduled(): heartbeat -> board/cf_heartbeat.json, cron -> {sport, scope} -> dispatch pipeline.yml
```

Nothing below has been run yet — every command is for you to execute once,
from `site/worker`, after `npx wrangler login`.

## 1. R2 bucket

```bash
cd site/worker
npx wrangler r2 bucket create football-board
```

## 2. D1 database + migrations

```bash
npx wrangler d1 create football-odds
```

Copy the printed `database_id` into `wrangler.toml` (`[[d1_databases]]`,
replacing `REPLACE_WITH_D1_DATABASE_ID`), then apply the migrations
(`migrations/0001_init.sql`, `0002_alerts.sql`, `0003_runs.sql`):

```bash
npx wrangler d1 migrations apply football-odds --remote
npx wrangler d1 execute football-odds --remote --command "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
```

Expected tables: `alerts, closings, games, odds_history, openers, runs,
stadium_results, stadiums, teams, weather_history` (+ `d1_migrations`).

> Gotcha (same as golf): `wrangler d1 ...` and `wrangler r2 object put` act on a
> LOCAL store unless you pass `--remote`. The deployed Worker reads remote
> resources, so always use `--remote`.

## 3. Worker secrets

```bash
npx wrangler secret put BOARD_PASSWORD          # shared viewer password (any username)
npx wrangler secret put BOARD_ADMIN_USERNAME    # optional override of the `mslade` var in wrangler.toml
npx wrangler secret put BOARD_ADMIN_PASSWORD    # distinct admin password: unlocks POST /refresh
npx wrangler secret put GH_DISPATCH_TOKEN       # fine-grained GitHub PAT, Actions: write on mslade50/football_weather
npx wrangler secret put TELEGRAM_BOT_TOKEN      # cron/dispatch failure pings
npx wrangler secret put TELEGRAM_CHAT_ID
```

Notes:
- `BOARD_ADMIN_PASSWORD` must differ from `BOARD_PASSWORD`; if equal, admin is
  never granted (fails closed, see `boardIdentity`).
- If neither `BOARD_PASSWORD` nor `BOARD_ADMIN_PASSWORD` is set the Worker is
  OPEN (dev convenience) — set `BOARD_PASSWORD` before the first deploy.
- `GH_DISPATCH_TOKEN`: GitHub → Settings → Developer settings → Fine-grained
  tokens → repository `mslade50/football_weather` → Permissions → Actions:
  Read and write. Note the expiry in your calendar; the Worker Telegram-pings
  when a dispatch returns 401/403.

## 4. First deploy (manual)

```bash
node --test "test/*.test.mjs"      # 21 worker unit tests
npx wrangler deploy
```

Then open `https://football-board.mckinleyslade.workers.dev` — it must prompt
Basic Auth (realm `football-board`). `/auth/me` returns `{role}`;
`/api/status` returns runs + heartbeat (empty until the first pipeline run and
the first cron fire).

Seed data manually if you want to see the board before `pipeline.yml` runs:

```bash
python -m pipeline.build --sport all --scope light --run-id local
for f in meta games_nfl games_cfb board history wx_history; do   # note: --remote!
  npx wrangler r2 object put "football-board/board/$f.json" \
    --file="../../data/board/$f.json" --content-type=application/json --remote
done
```

## 5. GitHub secrets (repo `mslade50/football_weather`)

| Secret | Used by | Scope |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | deploy.yml, pipeline.yml | Workers Scripts: Edit, Workers R2 Storage: Edit, D1: Edit (dash.cloudflare.com/profile/api-tokens) |
| `CF_ACCOUNT_ID` | deploy.yml, pipeline.yml | `ba4875f01f2bc46dd48e1e26d2ec9080` (exported as `CLOUDFLARE_ACCOUNT_ID`) |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | OPTIONAL — local `pipeline.build --publish` / `--merge-into-r2` boto3 path only; pipeline.yml uses the wrangler put loop and does not read them | R2 → Manage R2 API tokens → Object Read & Write on `football-board` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | all workflows `if: failure()` + alerts | already set for Phases 1–2 |
| `CFBD_API_KEY`, `PROPHETX_*` | pipeline.yml | already set |

```bash
gh secret set CLOUDFLARE_API_TOKEN
gh secret set CF_ACCOUNT_ID --body ba4875f01f2bc46dd48e1e26d2ec9080
gh secret set R2_ACCESS_KEY_ID
gh secret set R2_SECRET_ACCESS_KEY
```

R2 S3 endpoint for boto3: `https://ba4875f01f2bc46dd48e1e26d2ec9080.r2.cloudflarestorage.com`.

## 6. Continuous deploy

`.github/workflows/deploy.yml` runs on every push to `main` touching `site/**`:
worker unit tests → `wrangler d1 migrations apply football-odds --remote` →
`cloudflare/wrangler-action@v3` deploy. New migrations go in
`site/worker/migrations/NNNN_name.sql` (wrangler tracks applied files in the
`d1_migrations` table; never edit an applied file).

## 7. Scheduling (free plan: 2 cron triggers)

`wrangler.toml` `[triggers] crons`:

| Cron (UTC) | Purpose | scheduled() behaviour |
|---|---|---|
| `*/30 * * * *` | heartbeat | writes `board/cf_heartbeat.json` `{ts, cron}`; never dispatches. `pipeline.build` alerts when it is >20 h stale |
| `15 17 * * *` | mid-day dispatch (13:15 EDT / 12:15 EST) | `sport=all scope=full` Aug–Jan, `nfl` in Feb, trimmed Mar–Jul |

Every fire writes the heartbeat first, then `resolveCron(cron, time)` maps the
expression to `{sport, scope}` using **ET** parts (`Intl`
`America/New_York`, so DST needs no cron edits) or `null` (trimmed, zero GitHub
minutes). The **full** scrape cadence is therefore the `schedule:` block in
`.github/workflows/pipeline.yml` (GitHub cron, DOW `0=Sun`; use off-the-hour
minutes, GitHub drops on-the-hour schedules). Suggested GitHub schedule to
mirror ARCH §9.1 (UTC, EDT-based; pipeline gate skips off-season):

```yaml
schedule:
  - cron: '17 14,20 * * 2,3'     # Tue/Wed 10:17, 16:17 ET  (openers, light)
  - cron: '17 12-23/2 * * 4,5'   # Thu/Fri every 2h 08:17-19:17 ET
  - cron: '17 10-23 * * 6'       # Sat hourly 06:17-19:17 ET (CFB)
  - cron: '17 10-21 * * 0'       # Sun hourly 06:17-17:17 ET (NFL)
  - cron: '47 16,19,23 * * 0'    # Sun pre-kickoff 12:47/15:47/19:47 ET
  - cron: '17 22,23 * * 1,4'     # Mon/Thu night 18:17, 19:17 ET
```

**DAY-OF-WEEK in `wrangler.toml` is Quartz: 1=Sun … 7=Sat** (verified on the
golf board 2026-07-19 — `0` is rejected and `4,5,6,7` fired Wed–Sat, never Sun).
GitHub Actions cron is standard `0=Sun`. The unit test
`Quartz DOW` pins every CRON_PLAN key to `1-7`.

### Expanding on Workers Paid ($5/mo)

1. Upgrade the account plan (Workers & Pages → Plans).
2. Replace `crons` in `wrangler.toml` with the `PAID_CRONS` keys from
   `index.js` (`CRON_PLAN` already implements their ET trimming):
   ```toml
   crons = [
     "*/30 * * * *",                                  # heartbeat (optional on paid; keep for liveness)
     "15 17 * * *",                                   # mid-day full
     "0 14,20 * * 3,4",                               # Tue/Wed openers, light
     "0 0,2,12,14,16,18,20,22 * * 5,6,7",             # Thu/Fri every 2h 08-22 ET, full at 12/18 ET
     "0 0,1,2,10,11,12,13,14,15,16,17,18,19,20,21,22,23 * * 7,1",  # Sat hourly 06-21 ET (CFB), full at 10/14 ET
     "0 10,11,12,13,14,15,16,17,18,19,20,21 * * 1",   # Sun hourly 06-17 ET (NFL)
     "30 16,19,23 * * 1",                             # Sun 12:30/15:30/19:30 ET full
     "0 22,23 * * 2,5",                               # Mon/Thu night 18/19 ET
   ]
   ```
3. `npx wrangler deploy`, then thin `pipeline.yml` `schedule:` down to a sparse
   backstop (e.g. `'17 9,14,20 * * *'`) so CF is primary and GitHub is fallback.
4. Update the `every wrangler.toml cron has a CRON_PLAN entry` test's
   `crons.length` assertion (it pins 2 on the free plan).

## 8. Routes (`index.js`)

| Route | Auth | Source |
|---|---|---|
| `GET /` → `/index.html`, other paths | viewer | `env.ASSETS` (`site/web`) |
| `GET /data/<name>.json` | viewer | R2 `board/<name>` (`[a-zA-Z0-9._-]`, `.json` required), `cache-control: no-store` |
| `GET /api/history?game_id&market&book` | viewer | D1 `odds_history` (≤2000 rows) |
| `GET /api/wx?game_id` | viewer | D1 `weather_history` |
| `GET /api/alerts?sport&season&week` | viewer | D1 `alerts` (≤500) |
| `GET /api/runs?limit=20` | viewer | D1 `runs` (limit ≤100) |
| `GET /api/status` | viewer | D1 `runs` (20) + R2 heartbeat + meta summary |
| `GET /auth/me` | viewer | `{username, role, can_refresh}` |
| `POST /refresh {sport, scope, force}` | **admin** | `workflow_dispatch` pipeline.yml (`sport` nfl/cfb/all, `scope` weather/light/full); body must be `content-type: application/json` (CSRF guard, 415 otherwise); skipped when a run is already active unless `force` |

## 9. Optional: upgrade to Cloudflare Access (email OTP) instead of the password

Zero Trust → Access → Applications → Add → Self-hosted → domain
`football-board.mckinleyslade.workers.dev`; policy Allow → Emails → your
address; login method One-time PIN. Keep BOTH Worker secrets in place:
`boardIdentity` only opens up when neither `BOARD_PASSWORD` nor
`BOARD_ADMIN_PASSWORD` is set, so deleting just `BOARD_PASSWORD` would 401
every viewer behind Access (and dropping both would disable admin `/refresh`).
Dropping the Basic Auth layer entirely means editing `boardIdentity` to trust
the `Cf-Access-Authenticated-User-Email` header instead. Needs an API token
with **Access: Apps and Policies: Edit** if done via API. Access gates the
whole route (UI + data + api).

## 10. Frontend vendor files (`site/web/vendor/`)

MapLibre GL JS + CSS and uPlot are vendored (no CDN at runtime);
`site/web/vendor/VENDOR.md` records the versions and the exact re-download
URLs. If `maplibre-gl.js` / `uPlot.iife.min.js` are missing, run the `curl`
lines in VENDOR.md (`tests/test_site_contract.py` skips the vendor check until
they exist). Basemap style: `https://tiles.openfreemap.org/styles/liberty`.
