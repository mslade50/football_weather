# STATUS — football_weather rebuild

Snapshot as of 2026-08-24 (final integration pass). Companion to `PLAN.md`
(phases), `ARCHITECTURE.md` (design) and `AUDIT.md` (legacy reference).
Nothing has been deployed to Cloudflare and nothing has been committed by the
rebuild agents; everything below is verified locally on Windows / Python 3.10.

## 1. Verification summary (this pass)

| Check | Command | Result |
|---|---|---|
| Python tests | `python -m pytest tests -q -o addopts=""` | **729 passed** (19 s; 2026-08-25 after the medium-range + climatology blend pass) |
| Lint | `ruff check .` | All checks passed |
| Worker tests | `node --test "site/worker/test/*.mjs"` | **21 pass / 0 fail** |
| Frontend syntax | `node --check site/web/*.js` | 8 files ok (`alerts app backtest drawer map signals status table`) |
| Backtest CLI | `python -m pipeline.backtest --help` | ok (flags listed in §3) |
| Fixture backtest | `python -m pipeline.backtest --no-network --board-dir … --parquet-dir … --state-dir …` | `board/backtest.json` with **118 grid rows** (ids 1..118, legacy `Signal` order), keys `meta grid stadium_results alerts_clv games`; 4 parquet files (`games grid stadium_results alerts_clv`); 8 snapshot games, 0 graded (season not started) |
| Calibrate | `python -m pipeline.calibrate --dry-run` | "0 usable game(s) … nothing to fit" (expected: no settled data yet) |
| Full build | `python -m pipeline.build --sport all --scope light --no-alerts --run-id final-integration` (scratch out/state/board/snapshot dirs) | exit 0; NFL 0 games in window (272 in 2026 season), CFB week-1 board; pinnacle/betcris/fanduel/kalshi/novig lines live, prophetx 0 (no key), 63 CFB unresolved book names (FCS); wrote legacy csv/xlsx, 8 board JSONs (meta last), 5 state files incl. `closings.json` |

Per-file pytest counts: alert_format 11, alerts_rules 30, backtest_grid 15,
backtest_workflow 18, betcris_parse 26, betonline_parse 14, build_odds 17,
build_stadiums 27, calibrate 15, climatology 10, clv 19, contracts 9, d1_out 11, forecast_blend 13,
deploy_workflow 10, fair 18, fanduel_parse 12, gate_check 17, impact_v1 26,
impact_v2 24, json_out 18, kalshi_parse 19, legacy_columns 9, merge_aliases 57,
novig_parse 12, pinnacle_parse 7, pipeline_workflow 23, prophetx_parse 10,
schedule_cfb 7, schedule_nfl 6, scrape_volume 11, signals 18, site_contract 17,
stadium_loader 16, state_migrate 21, weather_merge 20, weather_stitch 24.

Integration change made in this pass: `pipeline/build.py` now runs a `clv`
stage after `alerts` (`run_clv_stage` → `pipeline/model/clv.run_clv_stage`):
freezes closings from state `history.json` for kicked-off games into
`state/closings.json`, settles EDGE alerts (saves `alerts.json` when anything
settled) and passes the new closing rows to `d1_out.build_statements(closings=…)`
so `d1_inserts.sql` carries `INSERT OR IGNORE INTO closings`. Non-fatal (warn
degradation) like the alert stage; skipped in `--scope weather` (no books).

Leftovers closed in the follow-up pass: `pipeline/alerts.py` formats impact /
components / emoji from the active model's block (`card["impact"][alert_model()]`,
v1 fallback) and labels the version in the impact line (`(wind 6.5 · v1)`);
`pipeline/calibrate.py` scores v1 with the RUN month (`Row.run_month` from
`src_forecast` snapshot stamp / `weather.fetched_at` / kickoff − lead; game month
is the documented fallback); `WeatherForecast.precip_prob_ens` is a contract
field populated by `weather/merge.py` and emitted in the GameCard weather block
(ARCH §5 updated); `backtest.yml` mirrors R2 snapshots with the pipeline.yml
`wrangler r2 object get` loop (keys rebuilt from the D1 `weather_history` /
`odds_history` export, newest `SNAPSHOT_MAX=120`) — no S3 keys anywhere in the
workflows.

Telegram cleanup (2026-08-31): PLAY alerts now default to Mid+ signals with a real posted price and `edge_pts >= 1.0`; one game-level identity survives best-book churn and model promotion, concurrent signal/fair/line changes collapse to one UPDATE, and notifications stop at kickoff. Quiet-hour messages are rebuilt from current prices, openers are opt-in, and three individual alerts are followed by one bounded SUMMARY. Scrape-volume incidents use one immediate sport-scoped SYSTEM path, NFL/CFB baseline scopes no longer reset each other, and fatal workflow errors have one notification owner (ARCH §10; `pipeline/alerts.py`).

## 2. What is implemented, per phase

### Phase 0 — Recover + scaffold: DONE
- `scripts/recover_static.py`, `scripts/extract_golden.py`, `scripts/_git_history.py`; golden fixtures `tests/fixtures/golden_v1.parquet`, `golden_fair_2024.parquet`; legacy fixtures `tests/fixtures/legacy/{nfl_weather.csv, cfb_weather.xlsx, cfb_weather_backtest.xlsx}`.
- `pipeline/contracts.py` (pydantic GameCard etc.), `pipeline/run_context.py` (stages, degradations, `GITHUB_SHA`), `pipeline/state.py` (schema_version, migrate, caps), `pipeline/model/{config,impact,signals}.py` (v1 model, golden test `test_impact_v1` untouched), `utils/{telegram,state,timeutil}.py`, `pipeline/odds/base.py`, `main.py`, `scripts/recon_book.py`, `ci.yml`.

### Phase 1 — Schedule + stadiums + weather + legacy outputs: DONE
- `pipeline/schedule/*` (nflverse `games.csv`; CFBD `/games` with ESPN scoreboard fallback when `CFBD_API_KEY` is missing).
- `pipeline/stadiums/{loader,climatology,build_stadiums}.py`, `data/stadiums.csv` + `stadiums_overrides.csv`, `data/climatology.csv`, `data/teams.csv`, `data/aliases`; `build-stadiums.yml` (Wikidata / OSM Overpass / EPQS enrichment, dispatch only).
- `pipeline/weather/{openmeteo,nws,merge}.py` + parsers; `pipeline/outputs/legacy.py` writes `nfl_weather.csv` / `cfb_weather.xlsx` with the legacy column contract (`test_legacy_columns`).
- `pipeline.yml` schedule (UTC): `17 9,14,20 * * *` baseline plus Tue/Wed openers, Thu/Fri 2-hourly, Sat/Sun hourly, Sun pre-kickoff `47 16,19,23`, Mon/Thu night; `pipeline/gate_check.py` off-season gate (`PIPELINE_FORCE=1` / `--force` bypass).

### Phase 2 — Odds scrapers: DONE
- httpx books `pipeline/odds/{pinnacle,betcris,fanduel,kalshi,novig,prophetx}.py` + `parsers/`; Playwright book `betonline.py` (own job in `pipeline.yml`, `--scope odds --books betonline --merge-into-r2`; `BETONLINE_CHANNEL` picks an installed Chrome).
- `pipeline/odds/merge.py` + `teams.py` aliases (57 tests), `pipeline/model/fair.py` (fair/edge, golden `golden_fair_2024`), openers persisted in `state/openers.json`, scrape-volume baseline (`test_scrape_volume`), per-book `BOOK_<NAME>_ENABLED` switches.
- `pipeline/odds/oddsapi.py` (Phase 6 add-on): optional The Odds API historical opener seeding, off unless `ODDS_API_KEY` set.

### Phase 3 — Cloudflare Worker + R2 + D1 + JSON board: CODE DONE, NOT DEPLOYED
- `site/worker/index.js` (Basic-Auth gate, `/data/*.json` from R2, `/api/{history,wx,alerts,runs,status}` from D1, `/auth/me`, admin `POST /refresh` → workflow_dispatch, `scheduled()` heartbeat + cron→dispatch with ET trimming), `wrangler.toml` (free plan: 2 crons), migrations `0001_init 0002_alerts 0003_runs 0004_v2`, 22 node tests, `SETUP.md`.
- `pipeline/outputs/{json_out,d1_out,r2,raw_out}.py`: board JSONs (`meta.json` written last), per-run snapshots, `d1_inserts.sql`, boto3 R2 publish/merge with content floor + self-check, raw-first archive.
- `site/web/{index.html,app.js,map.js,table.js,drawer.js,styles.css}` + vendored MapLibre / uPlot (`site/web/vendor`, no CDN); `deploy.yml` (tests → `d1 migrations apply --remote` → wrangler deploy on push to `main` touching `site/**`).

### Phase 4 — Telegram alerts, Alerts + Status tabs: DONE (Telegram sends fixture-tested only)
- `pipeline/alerts.py`: PLAY / UPDATE / CLOSED / SYSTEM UX over the EDGE / MOVE / GONE / WX records, stable dedup in `state/alerts.json` (cap 500), chat routing `TELEGRAM_CHAT_ID_NFL` / `_CFB` → `TELEGRAM_CHAT_ID`, `--alerts-stdout` / `--no-alerts`, `--digest clv`.
- `board/alerts_feed.json`, `board/status.json`, `site/web/{alerts,status}.js`; D1 `alerts` + `runs` tables; records retain the originating `ALERT_MODEL`, while a model promotion does not re-page the same play.

### Phase 5 — Better weather + stadiums, v2 model side by side: DONE
- Weather stitching (ARCH §6): HRRR / NBM / GFS / ensemble via Open-Meteo previous-runs + NWS gridpoint, `wind_vol` from ensemble spread, `weather_history` change rows (`test_weather_stitch` 19, `test_weather_merge` 20).
- Stadium orientation from OSM (`build_stadiums.py`, 27 tests), head/cross-wind decomposition.
- `compute_impact_v2` (24 tests) written next to v1; `gs_fg_v2 / away_fg_v2` in cards, D1 `games` (`0004_v2.sql`) and `weather_history`; `site/web/signals.js` Signals view, wind arrows / field axis on the map.
- **Medium-range + climatology blend (2026-08-24, ARCH §6)**: ECMWF AIFS (`ecmwf_aifs025_single`; the bare `ecmwf_aifs025` id returns nulls) added to `CONUS_MODELS` / `INTL_MODELS` and to `model_disagreement`; leads > 7 d (`forecast_blend.medium_range_start_h`) use the weighted mean of {AIFS, IFS, GFS} (`forecast_blend.medium_range_weights`, aifs 0.4 / ifs 0.35 / gfs 0.25; a member missing a field — AIFS has no gusts/PoP — just drops out of that field), 48 h–7 d NBM keeps priority with that blend as the gust/null fallback, `source=medium:aifs+ifs+gfs`. `data/climatology.csv` now also carries stadium × ISO-week × 6-h solar-bin ERA5 cells (mean/P10/P50/P90 wind + temp, gust mean/P90, ≥1 mm rain frequency; 173 stadiums, one hourly 2015–2024 archive request each, cached in the scratch dir; legacy summary row kept, written last per stadium) and `weather/climatology_blend.py` shrinks `wind_fg / gust_fg / temp_fg / precip_prob` + the ensemble P10/P90 band toward the cell with the lead-weighted curves in `calibration.json["forecast_blend"]["weights"]` (w = 1 ≤ 48 h). Raw values ride along as `wind_fg_raw / temp_fg_raw / blend_w / climo_wind / climo_temp` (WeatherForecast, GameCard weather block). `scripts/fit_forecast_blend.py` fitted the curves from Open-Meteo previous-runs (`best_match`) day-1..7 forecasts vs ERA5 truth, 44 sampled stadiums × Sep–Dec 2024+2025 (73 of 88 series fetched, 15 lost to 429s; n ≈ 71k 3-h windows per lead): w* wind 0.70/0.63/0.53/0.43/0.33 and temp 0.84/0.82/0.77/0.72/0.65 at 72/96/120/144/168 h (MAE wind 3.21→2.61 mph, temp 5.50→4.84 °F at 168 h vs forecast-only), rain_prob 0.62→0.48 (fitted on Brier 0.0574→0.0525; wind/temp on MAE); day-1/2 forecasts are also imperfect (w* 0.76/0.74 wind) but the curve keeps w = 1 ≤ 48 h by design. Stats live in `calibration.json["forecast_blend"]["fit"]`. Tests: `test_forecast_blend` (new), `test_weather_stitch`, `test_weather_merge`, `test_climatology`. `pipeline/build.py` was not touched: the cell is found from the Open-Meteo coordinates (nearest stadium ≤ 0.3°) — pass `stadium_id=` to `build_forecast` to make it exact. Open-Meteo's archive quota (hourly request limit, 429) caps the rebuild at ~45 stadiums/hour; the fetch is resume-safe (`--cache-dir`).

### Phase 6 — Backtest + calibration + CLV: DONE (fixture-verified; no settled season yet)
- `pipeline/model/clv.py` (19 tests): closing freeze from `history.json` / D1 `odds_history`, side-relative `clv_pts`, legacy `clv_status`, `closings.json` store, `settle_alerts`; now wired into `pipeline.build` (see §1).
- `pipeline/backtest.py` (15 grid tests incl. pandas oracle of the legacy `pages/cfb_weather.py` lookup): 118 legacy buckets from the xlsx fixture, Wins/Losses/Push/Sample/Margin/ROI/+CLV/CLV%, stadium results, alerts CLV by tier/league/book/model/market; inputs from snapshots, D1 export (`--export-dir`) or SQLite replay (`--sqlite`), HRRR actuals from Open-Meteo historical-forecast, results from CFBD / ESPN / nflverse; outputs `board/backtest.json`, `data/backtest/*.parquet`, optional `--d1-sql`.
- `pipeline/calibrate.py` (14 tests): bounded coordinate-descent refit of the v2 block into `data/calibration.json` (never edits `config.py`; sha256 guard), refuses on <4 distinct weeks unless `--force`; promotion rule + v1-vs-v2 CLV gate.
- `backtest.yml` (Tue `17 10 * * 2` weekly, Mon `17 12 * * 1` + CLV digest), `calibrate.yml` (monthly `17 13 1 * *`, PR `chore/calibrate-v2` touching only `data/calibration.json`); 17 workflow-contract tests.
- `site/web/backtest.js` Backtest tab (grid / stadium results / matched games / CLV summary + promotion pill); Record/ROI hover lines in map/table/drawer; CLV columns in Alerts tab.

## 3. Live-verified vs fixture-only

Live-verified during the rebuild (real network calls succeeded from this machine):
- Open-Meteo forecast, previous-runs (leads 1/3/5) and historical-forecast HRRR (Gillette 2025-11-09 18Z → 59.0 F / 8.1 mph); NWS gridpoints.
- nflverse `games.csv`; ESPN CFB scoreboard (CFBD path only exercised without a key → fallback).
- Wikidata / OSM Overpass / EPQS stadium enrichment (`build_stadiums.py`).
- Odds: pinnacle, betcris, fanduel, kalshi, novig return lines in today's build (counts in §1). prophetx returns 0 without `PROPHETX_*` keys. betonline Playwright scrape not run in this pass (Chromium job; fixture-tested parsers).
- `python -m pipeline.build` end-to-end (weather + light scopes), alerts stage in stdout mode, clv stage, board/legacy/D1-SQL outputs.

Fixture / unit-test only (never exercised against the real service):
- Telegram sends (`utils/telegram.py`, `pipeline/alerts.py` real sender), Worker Telegram pings.
- Cloudflare: Worker deploy, R2 bucket, D1 database/migrations, `wrangler r2 object put` / `d1 execute` loops, boto3 `--publish` / `--merge-into-r2`, `/refresh` → GitHub dispatch, cron `scheduled()`. Worker logic is covered by the 21 node tests only.
- All GitHub Actions workflows (`pipeline deploy backtest calibrate build-stadiums ci`) — contract-tested by `test_*_workflow.py`, never run on GitHub.
- The Odds API historical client (`oddsapi.py`), CFBD `/games` with a real key.
- Backtest grading, CLV settlement and calibration on real settled games (season starts 2026-08-29; all runs so far produce empty samples).
- Frontend rendered only via `node --check` + contract tests; no browser session against the Worker.

## 4. Secrets and environment variables

Where: **.env** = local `python -m pipeline.*` runs (python-dotenv); **GH** = repository secret (`gh secret set NAME`) or repository variable (`gh variable set`); **wrangler** = `npx wrangler secret put NAME` from `site/worker` (Worker runtime).

| Name | Used by | Set in | Required? |
|---|---|---|---|
| `CFBD_API_KEY` | `pipeline/schedule` (CFB schedule/results), `pipeline/backtest.py` results | .env, GH | optional — ESPN scoreboard fallback (warn degradation) |
| `PROPHETX_API_KEY`, `PROPHETX_SECRET_KEY`, `PROPHETX_ACCESS_KEY`, `PROPHETX_API_BASE_URL` | `pipeline/odds/prophetx.py` | .env, GH (`_API_BASE_URL` optional, defaults in code) | optional — book returns 0 lines without them |
| `BOOK_PINNACLE_ENABLED`, `BOOK_BETCRIS_ENABLED`, `BOOK_FANDUEL_ENABLED`, `BOOK_KALSHI_ENABLED`, `BOOK_NOVIG_ENABLED`, `BOOK_PROPHETX_ENABLED`, `BOOK_BETONLINE_ENABLED` | each `pipeline/odds/<book>.py` (`"0"` disables the book); `BOOK_BETONLINE_ENABLED` also read as a GH **variable** by `pipeline.yml` to skip the Playwright job | .env, GH variable | optional (default enabled) |
| `BETONLINE_CHANNEL` | `pipeline/odds/betonline.py` Playwright channel (e.g. `chrome`) | .env | optional |
| `ODDS_API_KEY`, `ODDS_API_ENABLED` | `pipeline/odds/oddsapi.py` opener seeding (`ODDS_API_ENABLED=0` disables) | .env, GH (not yet referenced by any workflow) | optional |
| `ALERT_MODEL` | `pipeline/model/config.py` — `v1` (default) or `v2` picks which impact drives alerts | .env / workflow env | optional |
| `PIPELINE_FORCE` | `pipeline/gate_check.py` (`1` bypasses the off-season gate) | GH dispatch input / .env | optional |
| `GITHUB_SHA`, `GITHUB_OUTPUT` | `run_context.py` meta sha, `gate_check.py` outputs | provided by Actions | — |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `utils/telegram.py`, `pipeline/alerts.py`, every workflow `if: failure()`, Worker cron/dispatch failure pings | .env, GH, wrangler | required for alerts / failure pings |
| `TELEGRAM_CHAT_ID_NFL`, `TELEGRAM_CHAT_ID_CFB` | `pipeline/alerts.py` per-sport routing (fallback `TELEGRAM_CHAT_ID`) | .env, GH (pipeline.yml) | optional |
| `TELEGRAM_MIN_TIER`, `TELEGRAM_MIN_EDGE_PTS`, `TELEGRAM_MAX_PER_RUN`, `TELEGRAM_INCLUDE_OPENERS` | Telegram policy; defaults `mid`, `1.0`, `4`, `0` | .env / workflow env | optional |
| `CLOUDFLARE_API_TOKEN` | `pipeline.yml`, `deploy.yml`, `backtest.yml`, `calibrate.yml`, `build-stadiums.yml` (wrangler r2/d1/deploy) — needs Workers Scripts: Edit, R2 Storage: Edit, D1: Edit | GH | required for any Cloudflare workflow |
| `CF_ACCOUNT_ID` | same workflows (exported as `CLOUDFLARE_ACCOUNT_ID`); `pipeline/outputs/r2.py` boto3 endpoint | GH, .env | required; value `ba4875f01f2bc46dd48e1e26d2ec9080` |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (+ optional `R2_BUCKET`, `R2_ENDPOINT`) | `pipeline/outputs/r2.py` boto3 path (`--publish`, `--merge-into-r2`) | .env | optional, local only — no workflow references them (pipeline / backtest / calibrate use wrangler get/put); the secrets do not exist on GitHub |
| `GITHUB_TOKEN` | `calibrate.yml` PR creation, `deploy.yml` | provided by Actions | — |
| `BOARD_PASSWORD` | Worker viewer Basic Auth | wrangler | required before first deploy (Worker is OPEN without it) |
| `BOARD_ADMIN_PASSWORD`, `BOARD_ADMIN_USERNAME` | Worker admin (`POST /refresh`); username defaults to `mslade` var in `wrangler.toml` | wrangler | admin password required for `/refresh`; must differ from `BOARD_PASSWORD` |
| `GH_DISPATCH_TOKEN` | Worker → `workflow_dispatch pipeline.yml` (fine-grained PAT, Actions: write on `mslade50/football_weather`) | wrangler | required for CF-cron-driven scrapes and `/refresh` |
| `GH_REPO`, `GH_WORKFLOW`, `GH_REF` | Worker dispatch target | `wrangler.toml [vars]` (already set) | — |
| `DB`, `ODDS`, `ASSETS` | Worker bindings (D1 `football-odds`, R2 `football-board`, static `site/web`) | `wrangler.toml` | `database_id` placeholder must be filled |

## 5. One-time setup, in order (from `site/worker/SETUP.md`)

```bash
# 0. local
cp .env.example .env   # (create .env with the .env rows above; never committed)
python -m pytest tests -q -o addopts=""        # 656
cd site/worker && node --test "test/*.mjs"     # 21
npx wrangler login

# 1. R2 bucket
npx wrangler r2 bucket create football-board

# 2. D1 database + migrations (0001_init 0002_alerts 0003_runs 0004_v2)
npx wrangler d1 create football-odds
#    -> paste database_id into wrangler.toml [[d1_databases]] (REPLACE_WITH_D1_DATABASE_ID)
npx wrangler d1 migrations apply football-odds --remote
npx wrangler d1 execute football-odds --remote --command "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
#    expect: alerts closings games odds_history openers runs stadium_results stadiums teams weather_history (+ d1_migrations)

# 3. Worker secrets
npx wrangler secret put BOARD_PASSWORD
npx wrangler secret put BOARD_ADMIN_PASSWORD      # != BOARD_PASSWORD
npx wrangler secret put BOARD_ADMIN_USERNAME      # optional
npx wrangler secret put GH_DISPATCH_TOKEN
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID

# 4. First deploy + smoke
npx wrangler deploy
#    open https://football-board.mckinleyslade.workers.dev -> Basic Auth prompt; /auth/me ; /api/status
#    optional seed before the first Actions run:
python -m pipeline.build --sport all --scope light --run-id local
for f in meta games_nfl games_cfb board history wx_history alerts_feed status; do
  npx wrangler r2 object put "football-board/board/$f.json" --file="../../data/board/$f.json" --content-type=application/json --remote
done

# 5. GitHub secrets (repo mslade50/football_weather)
gh secret set CLOUDFLARE_API_TOKEN
gh secret set CF_ACCOUNT_ID --body ba4875f01f2bc46dd48e1e26d2ec9080
#   (no R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY: every workflow goes through wrangler)
gh secret set TELEGRAM_BOT_TOKEN ; gh secret set TELEGRAM_CHAT_ID         # (+ TELEGRAM_CHAT_ID_NFL / _CFB optional)
gh secret set CFBD_API_KEY ; gh secret set PROPHETX_API_KEY ; gh secret set PROPHETX_SECRET_KEY ; gh secret set PROPHETX_ACCESS_KEY
gh variable set BOOK_BETONLINE_ENABLED --body 1

# 6. Push to main -> deploy.yml (worker tests, migrations apply, wrangler deploy); pipeline.yml runs on its schedule
#    or: gh workflow run pipeline.yml -f sport=all -f scope=light
# 7. Later: gh workflow run backtest.yml ; gh workflow run calibrate.yml (needs >=4 settled weeks)
```

Note: `SETUP.md` §2 lists migrations 0001–0003; `0004_v2.sql` (v2 columns on
`games`) is also applied by the same `migrations apply` command.

## 6. Known gaps

- ~~v1 golden reproduction stuck at 0.9706 with ~2.2% unexplained mismatches~~ — resolved 2026-08-24: the legacy rules were partially mis-reverse-engineered. Corrections (now in `pipeline/model/{config,impact}.py`, AUDIT §5): rain tiers `>1 / ≥6 / >12` keyed on the RUN month; heat_away = `home_temp − away_temp ≥ 10` (NFL every era; CFB until 2024-09-26, then `away_temp < 54`); CFB 2.0 altitude tier (`travel_alt ≥ 700` and home elevation ≥ 1100 m); CFB away components SUM (NFL keeps max); NFL 3.5-alt threshold 1283; NFL cold_away floor 60 from Jan 2026 (65 before); NFL test tolerance = the csv's 5-dp storage quantum. Era switches live only in the golden replay (`era_date`). Rate now ≈0.9964; the remaining ~150 rows are CFB `wind_fg` stored at 1 dp exactly on a tier threshold (12.0/15.0/17.0) — irreducible from the stored files.
- Nothing deployed: no Cloudflare resources exist, `wrangler.toml` still has `REPLACE_WITH_D1_DATABASE_ID`, no workflow has run on GitHub, no commit made by the rebuild.
- Backtest / calibration / CLV grids are structurally complete but empty until games settle (first kickoff 2026-08-29); the v2 promotion gate cannot fire before ≥4 distinct weeks. `calibrate.py` needs `backtest/games.parquet` rows carrying `total_open/total_close`.
- `backtest.yml` D1 export does `SELECT *` on `odds_history`; add a season filter / LIMIT once the table grows. The snapshot mirror fetches only the newest `SNAPSHOT_MAX` (120) run snapshots referenced by that export, one `wrangler r2 object get` each; older weeks come from the D1 tables.
- The build's CLV freeze only uses state `history.json` (cap 120 points per key); the D1 `odds_history` path is used by `pipeline.backtest` from the weekly export, which is authoritative for closings.
- NFL: 0 games inside the current window today (season 2026 schedule loaded, 272 games) — expected pre-season; CFB week 1 has 63 unresolved FCS book names (aliases only cover FBS + common FCS).
- `CFBD_API_KEY` set (.env + GitHub secret, 2026-08-25): CFBD drives the full-season FBS schedule, neutral-site venues and backtest results; ESPN scoreboard remains the fallback.
- ProphetX sandbox credentials still pending (`PROPHETX_API_KEY` / `_SECRET_KEY` / `_ACCESS_KEY`); the book returns 0 lines until they are set.
- FanDuel and Novig answer 403 to requests from GitHub Actions IPs; a `curl_cffi` (browser-impersonating TLS) fallback for those httpx books is in progress by another agent.
- betonline Playwright job requires `playwright install chromium` (or `BETONLINE_CHANNEL=chrome`) and was not run in this pass.
- Telegram sending, Worker `/refresh` dispatch and CF crons are unit-tested only. Free plan limits the Worker to 2 cron triggers; the full scrape cadence lives in `pipeline.yml`.
- Legacy backtest sheet row 110 has an inconsistent `+ CLV` value (183 > Sample 87); carried through as a `legacy` reference only.
- `ODDS_API_KEY` opener seeding is not called from any workflow (manual use).
- `.env.example` does not exist yet; the table in §4 is the reference.
