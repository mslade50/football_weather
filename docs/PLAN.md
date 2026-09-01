# football_weather — PLAN (ordered implementation phases)

Each phase lists: goal/ship, deliverables, files to create, golf_scraping files to copy/adapt (absolute source paths), acceptance checks. Phases are sequential; within a phase, tasks marked [P] can run in parallel. Constraints from `C:/Users/McKinley Slade/.claude/CLAUDE.md`: Windows/Git Bash, `python` not `python3`, Write/Edit tools for files (no heredocs), no `pip install` without asking, type hints + pathlib + f-strings. Reference docs: `docs/AUDIT.md`, `docs/ARCHITECTURE.md` (section numbers cited as ARCH §n).

Golf root: `C:/Users/McKinley Slade/dev/golf_scraping`. Target root: `C:/Users/McKinley Slade/dev/football_weather`.

---

## Phase 0 — Recover + scaffold (ship: repo skeleton, tests green, v1 model reproduced)

### Deliverables
1. Recovered static data in `data/raw/`.
2. Golden fixture `tests/fixtures/golden_v1.parquet` (~3000 rows) and `model/impact.py` v1 passing ≥97% exact.
3. Copied-verbatim utilities, contracts, CLI skeleton, requirements, CLAUDE.md, ci.yml.

### Files to create
- `scripts/recover_static.py`: iterates `git log --format=%H -- nfl_weather.csv` / `cfb_weather.xlsx`, `git show <sha>:<file>` into scratchpad, concatenates, takes first-seen static columns per stadium (NFL: stadium, avg_wind, wind_vol, orient, wind_impact, weakest_wind_effect, game_loc, year_built; per team: home_temp) and per CFB home team (orient, wind_impact, weakest_wind_effect, game_loc, year_built, home_temp) → `data/raw/nfl_stadium_curated.csv`, `data/raw/cfb_stadium_curated.csv`. Also `git show 3aa1fa2:cfb_locations_updated.csv > data/raw/cfb_locations_updated.csv`, `git show 25250b0:bol_ncaaf.db`, `fd_cfb.db` → `tests/fixtures/raw/legacy_db/`.
- `scripts/extract_golden.py`: same iteration → rows with inputs (sport, month, temp_fg, wind_fg, rain_fg, travel_alt, home_temp, away_temp) and outputs (gs_fg, away_fg) deduped → `tests/fixtures/golden_v1.parquet`; plus projection rows from commits `d6f4fe6 047f4be fac1c2e 2726b5b 2af9168` → `tests/fixtures/golden_fair_2024.parquet`.
- `pipeline/__init__.py`, `pipeline/contracts.py` (ARCH §4.2), `pipeline/run_context.py`, `pipeline/model/config.py`, `pipeline/model/impact.py` (v1 only), `pipeline/model/signals.py`, `utils/timeutil.py`.
- `utils/telegram.py` ← copy `golf_scraping/utils/telegram.py` verbatim (keep `send_message`; delete golf `format_alert`).
- `utils/state.py` ← copy `golf_scraping/utils/state.py`; key fn `game_key(game_id, market, book)`.
- `pipeline/state.py` ← copy `golf_scraping/board/state.py` (L40-50, 55-116, 213-256, 258-290, 294-391); add `SCHEMA_VERSION=1`, `migrate()`, ALERTS_CAP=500, HISTORY_CAP=120.
- `pipeline/odds/base.py` ← copy `golf_scraping/scrapers/base.py`; add `GameLine` dataclass; keep BaseScraper.
- `main.py` ← copy `golf_scraping/main.py`, args `--book --sport --market --output --headed`.
- `scripts/recon_book.py` ← copy `golf_scraping/recon_betcris.py`, add `--url --keyword --out` args.
- `requirements.txt` (playwright, playwright-stealth, httpx, beautifulsoup4, pandas, openpyxl, pyarrow, python-dotenv, curl_cffi, boto3, pydantic>=2, rapidfuzz, timezonefinder), `requirements-dev.txt` (pytest, ruff, shapely), `pyproject.toml` (ruff + pytest config), `.gitignore` (site/web/data/, data/backtest/, .env, .browser_profile/).
- `CLAUDE.md` (project rules: sport threaded everywhere, raw-first, no data in git, test conventions from golf), `README.md`.
- `.github/workflows/ci.yml`: ruff + pytest on PR/push (stub heavy deps via `sys.modules.setdefault` as in `golf_scraping/tests/test_betcris.py`).
- Tests: `tests/test_impact_v1.py` (golden; log mismatches by boundary bucket; assert ≥0.97 exact within 1e-6 on percent scale), `tests/test_signals.py` (NFL order incl. purple-first, CFB DOW thresholds, combined flags — hand fixtures), `tests/test_state_migrate.py`, `tests/test_contracts.py`.

### Acceptance
- `python scripts/recover_static.py` produces 32+ NFL stadium rows (2024+2025 names) and ≥120 CFB home-team rows; `data/raw/cfb_locations_updated.csv` has 658 rows.
- `pytest` green; `test_impact_v1` reports match rate and lists ≤3% mismatches all in documented ambiguous bands.
- `python main.py --help` runs. No pip installs performed without user approval (flag list in PR/summary).

---

## Phase 1 — Schedule + stadiums + weather + legacy outputs (ship: `nfl_weather.csv` / `cfb_weather.xlsx` regenerated 3×/day by GitHub Actions; existing Streamlit pages work unchanged, odds columns NaN)

### Files to create
- `data/stadiums.csv`, `data/stadiums_overrides.csv`, `data/teams.csv`: seeded by `scripts/seed_stadiums_phase1.py` from `data/raw/nfl_stadium_curated.csv` + `data/raw/cfb_locations_updated.csv` + `data/raw/cfb_stadium_curated.csv` + nflverse `games.csv` distinct `stadium_id` (season ≥2025, incl. international) with columns per ARCH §4.2 Stadium; `orientation_deg` from curated bucket midpoints (N-S→0, NE-SW→45, E-W→90, NW-SE→135), `orientation_src='curated'`; `roof_type` manual list (ARCH audit); `needs_review=1` where lat/lon missing.
- `data/aliases/nfl.json`, `data/aliases/cfb.json`: canonical team_id → aliases (nflverse abbr, full name, city lowercase 'n.y. giants', mascot); CFB from CFBD School/Alt Name1-3/Abbreviation + 'UConn'/'FIU' etc.
- `pipeline/schedule/nfl.py` (nflverse games.csv: season, week, gameday, gametime ET, away/home abbr, stadium_id, roof, surface, location=Neutral → `Game`), `pipeline/schedule/cfb.py` (CFBD `/games?year&week&division=fbs` Bearer; venueId, neutralSite, startDate; `/calendar` for week; ESPN fallback in `schedule/espn.py`), raw capture of both.
- `pipeline/stadiums/loader.py`: load csv + overrides; `resolve(game) -> Stadium`; neutral: compute `travel_alt`/`away_temp` vs both teams, apply larger penalty side (ARCH §7 judge note), legacy columns use schedule home/away.
- `pipeline/weather/openmeteo.py`, `weather/parsers/openmeteo.py`, `weather/nws.py`, `weather/parsers/nws.py`, `weather/merge.py` (ARCH §6; forecast + NWS only in this phase; ensemble fields None).
- `pipeline/outputs/legacy.py`: column-exact writers (AUDIT §4.1/4.2 order, formats: Date 'SUN 11/09', Time '01:00 PM', game_loc 'lat, lon', NFL Game lowercase city 'away vs home', CFB 'Away @ Home', NFL gs_fg fraction, CFB percent, CFB rounding wind 1dp/temp 2dp, Timestamp naive ET ISO, `Other` sheet for FCS/non-FBS).
- `pipeline/outputs/raw_out.py` (local `data/raw_runs/` in this phase; R2 in Phase 3).
- `pipeline/build.py` v0: `--sport --scope weather --print --dry-run`; stages schedule→stadiums→weather→impact v1→signals→legacy; stage timings; Degradation list printed.
- `pipeline/gate_check.py`: httpx-only kickoff horizon check (nflverse/CFBD), prints `run=skip|scrape`, `need_playwright=false`; fail-open.
- `.github/workflows/pipeline.yml` v1: `schedule '17 9,14,20 * * *'` + workflow_dispatch {sport}; gate job; light job (no R2 yet): checkout, setup-python 3.11 pip cache, `pip install -r requirements.txt`, `python -m pipeline.build --sport all --scope weather`, git commit loop (copy from `golf_scraping/.github/workflows/board.yml` commit step: `git pull --rebase --autostash -X theirs origin main` 3 attempts), Telegram `if: failure()` (copy curl step).
- Tests: `tests/test_legacy_columns.py` (exact column lists + dtype/format regexes against `nfl_weather.csv`/`cfb_weather.xlsx` in repo), `tests/test_weather_merge.py` (fixture `tests/fixtures/raw/openmeteo/forecast_multi.json`, `nws/gridpoints.json`; 3-hour mean arithmetic reproduces 0.0207-multiple style), `tests/test_stadium_loader.py` (neutral game, dome, missing stadium → Degradation), `tests/test_schedule_nfl.py`, `tests/test_schedule_cfb.py` (fixtures), `tests/test_pipeline_workflow.py` (contract strings: 'for attempt in 1 2 3', 'if: failure()', '17 9,14,20', no 'continue-on-error' on state steps), `tests/test_gate_check.py`.

### golf files to copy/adapt
`golf_scraping/.github/workflows/board.yml` (job skeleton, commit loop, Telegram step), `golf_scraping/board/gate_check.py` (shape), `golf_scraping/board/build.py` L911 `_retry`, L3196-3204 write loop.

### Acceptance
- Local: `python -m pipeline.build --sport all --scope weather --print` writes `data/nfl_weather.csv` and `data/cfb_weather.xlsx`; `streamlit run app.py` (user-run) renders both maps from the new files with no code change (copy files to repo root paths for the check).
- Every current-season home team resolves to a stadium; unresolved list empty or flagged `needs_review`.
- pipeline.yml runs green on dispatch; three scheduled runs/day commit files with message `Update <file> with Timestamp <iso>`.
- User must supply `CFBD_API_KEY` and Telegram secrets before this phase's CI run.

---

## Phase 2 — Odds scrapers (ship: legacy files carry Spread/Total open/now; openers persisted; fair/edge columns filled)

### Recon prerequisite (once, user-run locally with Playwright; outputs committed as scrubbed fixtures)
`python scripts/recon_book.py --url https://www.betonline.ag/sportsbook/football/nfl --keyword offering --out tests/fixtures/raw/betonline/` (also college-football, nfl-preseason) → lock `League` slugs and `AwayLine/HomeLine` spread/total key names. `python scripts/recon_book.py` for FanDuel ncaaf/nfl, Novig, Kalshi, ProphetX, bookmaker.eu HTML saved via httpx. `scripts/fixtures_scrub.py` trims to ≤20 games per fixture.

### Files to create (order of ease) [P after recon]
- `pipeline/odds/teams.py` (normalize_team with aliases + rapidfuzz ≥92 + unresolved log), `pipeline/odds/merge.py` (game_key match ±36 h, neutral flip, pivot, main-line selection, openers via `pipeline/state.py`).
- `pipeline/odds/betcris.py` + `parsers/betcris.py` ← adapt `golf_scraping/scrapers/betcris.py` (pages nfl/nfl-preseason/college-football; cells vS_/hS_/vT_/hT_/vM_/hM_; ½; oddsSubTitle venue; START time PT).
- `pipeline/odds/fanduel.py` + parser ← adapt `golf_scraping/scrapers/fanduel.py` (customPageId nfl|ncaaf; MONEY_LINE / MATCH_HANDICAP_(2-WAY) / TOTAL_POINTS_(OVER/UNDER); result.type).
- `pipeline/odds/kalshi.py` + parser ← adapt `golf_scraping/scrapers/kalshi.py` (+ `kalshi_fill.py` optional): `/events?series_ticker=...&with_nested_markets=true`; ladder → main; `*_dollars`.
- `pipeline/odds/novig.py` + parser ← adapt `golf_scraping/scrapers/novig.py`.
- `pipeline/odds/pinnacle.py` + parser ← adapt `golf_scraping/scrapers/pinnacle.py` (sport 15, spread/total/moneyline).
- `pipeline/odds/prophetx.py` + parser ← adapt `golf_scraping/scrapers/prophetx.py` (sport filter, market_types).
- `pipeline/odds/betonline.py` + parser ← adapt `golf_scraping/scrapers/betonline.py` (Playwright transport unchanged; `Sport:'football'`).
- `pipeline/odds/draftkings.py` (optional) ← adapt `golf_scraping/scrapers/draftkings.py`.
- `pipeline/model/fair.py`: devig helpers copied from `golf_scraping/board/build.py` L150-170/L891-910; consensus (Pinnacle-weighted), pts→prob tables, fair lines, edges, confidence, tiers (ARCH §7.3); legacy My_total/Edge/My_spread/Edge_s.
- `pipeline/build.py`: add `--scope full|light|odds`, `--books`, asyncio.gather scrapers with `return_exceptions=True`, per-book counts, `_check_scrape_volume` copied from `golf_scraping/board/build.py` L2255-2357 (keys book|market).
- `pipeline.yml`: add `scope` input; light job installs no Playwright; playwright job (`needs: light`, `if: need_playwright`) runs `--scope odds --books betonline` and re-writes legacy files; state (`openers.json`, `archive_last.json`, `scrape_baseline.json`) temporarily committed under `data/state/` (moved to R2 in Phase 3) — NOTE: acceptable only because the Phase 1–2 commit loop already exists.
- Tests: `tests/test_betcris_parse.py`, `test_fanduel_parse.py`, `test_kalshi_ladder.py`, `test_novig_parse.py`, `test_pinnacle_parse.py`, `test_prophetx_parse.py`, `test_betonline_parse.py` (each from fixture → expected GameLine rows incl. neutral game and a plus-price), `test_merge_aliases.py` (Miami FL/OH, 'St' vs 'State', Kalshi abbrs, neutral flip), `test_fair.py` (golden_fair_2024 rows reproduce My_total/Edge given ref values; devig; key-number table monotone), `test_scrape_volume.py`.

### Acceptance
- `python main.py --book betcris --sport cfb` prints ≥40 games in-season with spread/total/ml; each book's parser test green from fixtures.
- Legacy NFL file has Spread_now/Total_now from BetOnline (fallback consensus), CFB has Fd_open/FD_now/Open/Current from FanDuel; `Spread`/`Total_proj` = consensus ref with `ref_book` logged; openers stable across two consecutive runs (Fd_open unchanged while FD_now moves).
- Unresolved team names per run ≤2% of rows, listed in build output.
- Telegram receives scrape-volume alert when a book returns 0 rows while peers report.

---

## Phase 3 — Cloudflare Worker + R2 + D1 + JSON board (ship: private URL with Table + NFL/CFB maps; httpx books live on the board from day one; CF crons primary)

### One-time infra (user runs; commands documented in `site/worker/SETUP.md`)
`wrangler r2 bucket create football-board`; `wrangler d1 create football-odds`; `wrangler secret put BOARD_PASSWORD | BOARD_ADMIN_USERNAME | BOARD_ADMIN_PASSWORD | GH_DISPATCH_TOKEN | TELEGRAM_BOT_TOKEN | TELEGRAM_CHAT_ID`; add GitHub secrets `CLOUDFLARE_API_TOKEN`, `CF_ACCOUNT_ID`; decide Workers Paid.

### Files to create
- `site/worker/wrangler.toml` ← adapt `golf_scraping/board/worker/wrangler.toml` (name football-board, `[assets] directory="../web" run_worker_first=["/*"] html_handling="none"`, R2 `ODDS`=football-board, D1 `DB`=football-odds, `[triggers] crons` per ARCH §9.1).
- `site/worker/index.js` ← adapt `golf_scraping/board/worker/index.js` (auth L1-30/154-250, `/data` proxy, `boardIdentity`, `dispatchBoard/boardRunActive/ghHeaders/jsonResponse/notifyTelegram` ~L1060-1156; strip Kalshi/ProphetX order routes and pre-wednesday); add `/api/history`, `/api/wx`, `/api/alerts`, `/api/runs`, `scheduled()` cron→{sport,scope} map with ET trimming + heartbeat.
- `site/worker/migrations/0001_init.sql`, `0003_runs.sql` (ARCH §4.3; 0002 in Phase 4), `site/worker/package.json`, `site/worker/test/worker.test.mjs` (auth, sanitize, cron→scope map, Quartz DOW).
- `pipeline/outputs/json_out.py` (GameCard, board, meta with books status, snapshots), `pipeline/outputs/d1_out.py` (copy `_d1_sql_value/_write_d1_deltas/_archive_rows` from `golf_scraping/board/build.py` L1887-1983; tables games/stadiums/teams upserts, odds_history/weather_history change-only, openers, runs), `pipeline/outputs/r2.py` (boto3 `push_to_r2` from build.py L3220-3238 + `--self-check`), `pipeline/outputs/raw_out.py` → R2 `raw/`.
- `pipeline.yml` v3: R2 state get loop (fail on non-NoSuchKey), R2 put loop (raw, snapshots, payloads, state, meta last), `d1 execute --file` gated by hashFiles, self-check step, playwright job merges into R2 (`--merge-into-r2`). Remove `data/state/` commit; keep legacy commit loop only until Phase 4.
- `deploy.yml` (push paths `site/**` → migrations apply + wrangler-action@v3), `.github/workflows/ci.yml` add `node --test site/worker/test`.
- `site/web/index.html`, `styles.css` ← adapt `golf_scraping/board/web/index.html` shell; `app.js` ← scaffolding from `golf_scraping/board/web/app.js` (fetch/poll/sort/filter/hover/auth/refresh); `table.js`, `map.js` (MapLibre, OpenFreeMap style, marker spec ARCH §11 minus arrows/axis which need Phase 5 data), `drawer.js` (three old tables + line history from history.json), `status.js`; `vendor/` MapLibre GL + uPlot (user downloads; sizes noted).
- Tests: `tests/test_json_out.py` (allow_nan, GameCard keys, date/time labels), `tests/test_d1_out.py` (INSERT OR IGNORE, chunking ≤100 rows, change-only), `tests/test_pipeline_workflow.py` extended (state get loop 'NoSuchKey', 'meta.json' last, 'self-check'), `tests/test_deploy_workflow.py`.

### Acceptance
- `https://football-board.<acct>.workers.dev` prompts Basic Auth; Table and both maps render from `/data/games_*.json`; header shows Updated/next run and book chips; `/api/history` returns rows.
- Two consecutive runs: `odds_history` grows only for moved lines; `openers` unchanged; `runs` has both run_ids; `meta.json.run_id` equals latest.
- Killing R2 mid-put (simulated) leaves old meta over old data (self-check fails loudly, Telegram fired).
- CF cron fires visible in `cf_heartbeat.json`; GitHub backstop still present.
- Off-season dispatch → gate `skip` → no Chromium install.

---

## Phase 4 — Telegram edge + line-move alerts; retire Streamlit (ship: alerts live, Alerts tab, Status tab)

### Files to create
- `pipeline/alerts.py` (ARCH §10: PLAY/UPDATE/CLOSED/SYSTEM UX over the persisted alert families, keys, quiet hours + `telegram_state.json` queue, three individual messages + one bounded summary by default, per-sport chat routing, HTML formatters, `--digest`), `_alert_once` closure from `golf_scraping/board/build.py` L2494-2502.
- `site/worker/migrations/0002_alerts.sql`; `d1_out.py` alerts upsert; `json_out.py` `alerts_feed.json`, `status.json`.
- `pipeline/state.py`: alerts rehydrate from D1 export when R2 `alerts.json` missing (wrangler `d1 export --table alerts` step in workflow, or `/api/alerts` fetch).
- `site/web/`: Alerts tab, Status tab, degradation banners, deep links from alert messages, uPlot alert markers on line history.
- Workflow: BetOnline/FanDuel-Playwright/ProphetX behind `BOOK_*_ENABLED` (already), legacy csv/xlsx now written to R2 `legacy/` and commit loop removed; delete `app.py`, `pages/`, Streamlit lines in requirements; README updated.
- Tests: `tests/test_alerts_rules.py` (thresholds per sport/market, weather-driven gate, confidence/lead bypass, move buckets, edge-gone, quiet-hours queue/flush, cap/digest, mark-only-after-send), `tests/test_alert_format.py` (HTML sample matches spec), workflow contract: no `git commit` step remains.

### Acceptance
- Dry-run (`--no-alerts --print`) lists candidate alerts with keys; a live run defaults to no more than four messages per destination chat; re-run sends none (dedup); a material total move of 1.5 points triggers one UPDATE before kickoff.
- Alerts tab shows the same keys as Telegram; `/api/alerts` returns D1 rows with status.
- Repo has no generated data files tracked (`git ls-files data/*.csv data/*.xlsx` empty except `data/stadiums*.csv`, `teams.csv`, `raw/`).

---

## Phase 5 — Better weather + stadium data; v2 model side by side (ship: OSM orientation, ensemble wind_vol, HRRR/NBM stitching, wind arrows/field axis, Signals view)

### Files to create
- `pipeline/stadiums/build_stadiums.py` (CFBD /venues + /teams fbs, nflverse stadium ids, Wikidata SPARQL P118/P115 with User-Agent, OSM Overpass `leisure=pitch sport=american_football around:400` one batched union query with 2–5 s throttle, shapely `minimum_rotated_rectangle` → orientation_deg 0–180 + bucket, elevation EPQS (US) / Open-Meteo elevation (intl, batched 100), timezonefinder, overrides, validation: every home team mapped, lat/lon within 300 m of OSM centroid, needs_review flags; provenance columns) and `pipeline/stadiums/climatology.py` (ERA5 archive → avg_wind_sep..jan, avg_temp_f per stadium).
- `.github/workflows/build-stadiums.yml` (dispatch → PR via peter-evans/create-pull-request).
- `pipeline/weather/openmeteo.py`: ensemble client; stitching per ARCH §6 with `source`/`lead_hours`/`model_disagreement`; roof_state from nflverse then heuristic; cross/head components; hourly strip.
- `pipeline/model/impact.py` v2 (ARCH §7.5), `data/calibration.json` defaults; `fair.py` v2 fair lines + confidence from `wind_vol_fc`; GameCard `impact.v2`; D1 `gs_fg_v2/away_fg_v2`.
- `site/web/map.js`: wind arrow (rotation wind_dir_deg, length ∝ wind_fg), field-axis line (orient_deg), hollow dome markers, opacity by confidence with static toggle, clustering <zoom 4, ring colors; `drawer.js`: hourly strip with P10–P90 band, forecast-drift sparkline (`/api/wx`), stadium compass card; Signals view presets (CFB Wind/NFL Wind/Heat/Alt+Heat).
- Tests: `tests/test_impact_v2.py` (continuous curve calibrated to v1 tier midpoints ±0.5; dir_mult parse of 'x N','E/W','all'; roof closed zeroes), `tests/test_weather_stitch.py` (lead bands pick correct source; NWS fill; disagreement), `tests/test_build_stadiums.py` (MRR bearing on fixture polygons: Gillette≈158, AT&T≈69, Ohio≈7; pitch selection inside stadium polygon), `tests/test_climatology.py`.

### Acceptance
- `build-stadiums.yml` PR: ≥95% of FBS+NFL stadiums have `orientation_src in {osm_pitch, osm_stadium_mrr}`, remainder `curated`/`manual` with needs_review; international NFL venues present.
- GameCard has `wind_vol_fc`, `wind_p10/p90`, `cross_mph`, `source`, `lead_hours`, `impact.v2`; map shows arrows/axes; alerts still keyed to v1.
- Degradation emitted when ensemble missing (falls back to static vol).
- pip approvals needed: shapely (dev-only for build_stadiums), timezonefinder (already installed locally).

---

## Phase 6 — Backtest + calibration + CLV (ship: `backtest.json` replaces `cfb_weather_backtest.xlsx`; weekly backtest/calibrate/CLV digest; v2 promotion gate)

### Files to create
- `pipeline/model/clv.py` (closing freeze = last odds_history row before kickoff per key; `clv_pts`; alerts update; `closings` table + `closings.json`).
- `pipeline/backtest.py`: inputs D1 export (odds_history, closings, games) + `snapshots/` + historical-forecast HRRR actuals + previous-runs `_previous_dayN` (lead 1/3/5) → per-game rows (actual wind/temp/rain at kickoff window, forecast at lead N, closing total/spread, result from CFBD `/games` scores and nflverse `result/total`); regenerate the Backtesting grid (NCAAF wind [8,15]/[15,∞) × temp (,50]/[50,60]/[60,75]/[75,100] × spread [0,10]/[10,20]/[0,20] × CLV all/+/−; NFL bands incl. [32,45]) with Wins/Losses/Push/Sample/Margin/ROI/+CLV/CLV%; Stadiums sheet equivalent → `stadium_results`; CLV per alert by tier/league/book, v1 vs v2 → `board/backtest.json`, `data/backtest/*.parquet` (R2).
- `pipeline/calibrate.py`: refit v2 coefficients (wind curve, gust blend, rain prob threshold, alt slope, heat-away delta) minimizing closing-total error / maximizing under ROI on ≥4 weeks → `data/calibration.json` PR; promotion rule: set `ALERT_MODEL=v2` in `model/config.py` only when v2 CLV ≥ v1 over ≥4 weeks (manual merge).
- `.github/workflows/backtest.yml` (Tue 06:00 ET + dispatch; Monday CLV digest sent via `pipeline.alerts --digest`), `calibrate.yml`.
- `site/web/`: Backtest tab (grid, stadium results, matched games list = old bottom table), CLV columns in Alerts tab, drawer CLV timeline; hover Record/ROI lookup by first-match.
- Optional: `pipeline/odds/oddsapi.py` (The Odds API historical seeding of true openers, `ODDS_API_KEY`), `draftkings.py` enablement.
- Tests: `tests/test_clv.py` (freeze picks last pre-kickoff row; sign conventions per side), `tests/test_backtest_grid.py` (bucket assignment reproduces xlsx rows from fixture games; first-match semantics), `tests/test_calibrate.py` (writes valid calibration.json schema; never touches v1 constants), workflow contract tests for backtest/calibrate.

### Acceptance
- `backtest.json` grid has the 118 legacy buckets (ids preserved) populated from ≥1 season of D1 + historical data; UI hover shows Record/ROI for matched CFB games as before.
- Weekly digest arrives Monday with CLV by tier/league/book and v1 vs v2.
- `calibrate.yml` opens a PR; v1 golden test still green after merge.
- `cfb_weather_backtest.xlsx` retained only as a test fixture.

---

## Cross-phase checklists

**Every PR**: ruff clean; pytest green with dep stubs; workflow-contract tests updated when YAML changes; no generated data committed (after Phase 4); secrets never in code; `sport` threaded through every function that touches games.

**Copy-verbatim manifest** (source → target):
- `golf_scraping/utils/telegram.py` → `utils/telegram.py`
- `golf_scraping/utils/state.py` → `utils/state.py`
- `golf_scraping/board/state.py` → `pipeline/state.py`
- `golf_scraping/scrapers/base.py` → `pipeline/odds/base.py`
- `golf_scraping/scrapers/{betonline,betcris,fanduel,kalshi,kalshi_fill,novig,pinnacle,prophetx,draftkings}.py` → `pipeline/odds/*.py` (transport) + `pipeline/odds/parsers/*.py` (new)
- `golf_scraping/board/build.py` (helper line ranges in AUDIT §7.1) → `pipeline/build.py`, `pipeline/model/fair.py`, `pipeline/outputs/{d1_out,r2}.py`
- `golf_scraping/board/gate_check.py` → `pipeline/gate_check.py` (shape)
- `golf_scraping/main.py` → `main.py`
- `golf_scraping/recon_betcris.py` → `scripts/recon_book.py`
- `golf_scraping/board/worker/{index.js,wrangler.toml}` → `site/worker/`
- `golf_scraping/board/web/{index.html,app.js}` → `site/web/`
- `golf_scraping/.github/workflows/{board.yml,betonline.yml}` → `.github/workflows/{pipeline.yml,deploy.yml}`
- `golf_scraping/tests/{test_betcris.py,test_novig.py,test_board_workflow.py}` → test conventions
- `golf_scraping/board/SETUP.md` → `site/worker/SETUP.md` (account id, `--remote` gotcha, Access upgrade)

**User decisions required before Phase 3**: Workers Paid vs 2 free crons; Basic Auth vs Cloudflare Access; OpenFreeMap vs PMTiles; pip approvals (pydantic, rapidfuzz, timezonefinder, shapely, pyarrow, boto3).
