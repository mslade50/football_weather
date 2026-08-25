# football_weather — ARCHITECTURE (final design)

Winner design ("reuse-first, lift golf_scraping, ship in phases") merged with judge graft notes. This document is the source of truth for implementation agents. Sections: 1 principles · 2 tiers · 3 repo layout · 4 data model + D1 schema · 5 R2 payloads/state · 6 weather stack · 7 edge model spec (v1 exact + v2) · 8 odds scrapers · 9 scheduling/GitHub Actions · 10 alerts · 11 frontend · 12 Worker/API · 13 robustness contracts · 14 secrets · 15 risks.

---

## 1. Principles

1. **golf_scraping is the template.** Copy files by path (see AUDIT §7), rename golf→game concepts. Same three tiers: CF Worker scheduler → GitHub Actions Python pipeline → CF Worker site over R2 + D1.
2. **Nothing computed in the Worker.** Python on GitHub Actions does schedule → stadiums → weather → impact model → odds → fair/edges → alerts → outputs. Worker only serves assets, proxies R2 JSON, runs small D1 reads, and dispatches workflows.
3. **Raw-first capture.** Every scraper and weather fetch persists the verbatim response to R2 `raw/{sport}/{run_id}/{source}.json` (+ manifest of sha256s) BEFORE parsing. Parsers are pure functions in `pipeline/odds/parsers/` and `pipeline/weather/parsers/` tested from scrubbed fixtures in `tests/fixtures/raw/{source}/`. Book breakage = fixture refresh; backtests are replayable. (The old pipeline died because nothing raw survived.)
4. **Degradation records, not silent fallbacks.** Every fallback emits `Degradation{component, reason, severity in info|warn|error, run_id}` into `meta.json.degradations[]`; site renders banners; Telegram at warn+.
5. **Idempotent, identity-stamped.** `run_id = f"{sport}-{utc_ts:%Y%m%dT%H%M%SZ}-{sha7}"` threaded through every row and state file. D1 writes are `INSERT OR IGNORE` on natural keys. Every R2 state JSON carries `schema_version` and passes through `migrate()` on load.
6. **Two-phase publish + self-check.** Data payloads → state files → `meta.json` last; then re-fetch `meta.json`, assert `run_id`, assert row count ≥ 50% of previous snapshot for the same (sport, week) unless `--force`.
7. **No generated data in git** after Phase 2. Legacy csv/xlsx commit loop exists only in Phases 1–2 (Streamlit compatibility); afterwards legacy files go to R2 `legacy/`.
8. **v1 model is the alert model** until backtest/CLV over ≥4 weeks shows v2 ≥ v1. v2 is computed and shown side by side from Phase 5.
9. **Private site.** Basic Auth from day one; Cloudflare Access upgrade path. Never republish raw Kalshi/FanDuel prices publicly.
10. **Tooling weight for a solo repo:** pydantic frozen contracts + parser fixture tests + ruff + pytest + workflow-contract tests = yes. mypy --strict, pandera, structlog, hypothesis = optional, never gating Phases 2–3.

## 2. Tiers

### 2.1 Scheduler — Cloudflare Worker `football-board` `scheduled()`
Copied from `golf_scraping/board/worker/index.js` (`dispatchBoard`, `boardRunActive`, `notifyTelegram`, `ghHeaders`). On each cron fire: (1) write `board/cf_heartbeat.json {ts, cron}` to R2 first; (2) trim in-handler by ET clock (see §9); (3) POST `https://api.github.com/repos/{owner}/football_weather/actions/workflows/pipeline.yml/dispatches` `{ref:'main', inputs:{sport, scope, force}}` using `GH_DISPATCH_TOKEN`; (4) `notifyTelegram` on missing token or non-204. GitHub `schedule:` kept as sparse backstop.

### 2.2 Pipeline — GitHub Actions `pipeline.yml`, `python -m pipeline.build`
Two jobs: **A `light`** (httpx books + weather + model + edges + alerts + publish, ~2–3 min, no Chromium) and **B `playwright`** (BetOnline + optional FanDuel-Playwright fallback, ~60–90 s scrape, merges into A's output by game_key, gated by `needs.gate.outputs.need_playwright` and `inputs.scope == 'full'`). See §9.

### 2.3 Site — single Worker with `[assets] directory="../web"`
`run_worker_first=["/*"]`, `html_handling="none"`, Basic Auth copied from golf `index.js`. Routes in §12. Frontend vanilla JS + vendored MapLibre GL + uPlot (§11).

### 2.4 Data stores
- **R2 bucket `football-board`**: `board/` served JSON + state; `raw/` captures; `snapshots/{sport}/{season}/{week}/{run_id}.json`; `legacy/` csv/xlsx (Phase 3+).
- **D1 `football-odds`**: dimensions + change-only history + alerts + runs (§4).
- **Git**: code, `data/stadiums.csv`, `data/stadiums_overrides.csv`, `data/teams.csv`, `data/aliases/*.json`, `data/calibration.json`, `data/raw/` cached API JSON per season, `tests/fixtures/`.

## 3. Repo layout

```
football_weather/
  pipeline/
    __init__.py
    build.py                 # orchestrator; CLI --sport nfl|cfb|all --scope full|light|odds|weather --print --no-alerts --dry-run --force
    contracts.py             # pydantic frozen models: Game, Stadium, Team, WeatherForecast, OddsQuote(GameLine), Edge, Degradation, RunMeta
    gate_check.py            # httpx-only: prints skip|scrape + need_playwright; fail-open
    run_context.py           # run_id, git_sha, clocks (utc/ET), stage timers
    schedule/
      __init__.py
      nfl.py                 # nflverse games.csv
      cfb.py                 # CFBD /games (Bearer CFBD_API_KEY); ESPN scoreboard fallback
      espn.py                # scoreboard client (venue id, neutralSite) shared fallback
    stadiums/
      __init__.py
      loader.py              # stadiums.csv + teams.csv + overrides -> Stadium/Team maps; resolve game->stadium; neutral handling
      build_stadiums.py      # preseason: CFBD /venues + nflverse + Wikidata + OSM MRR + elevation + timezonefinder -> stadiums.csv (PR)
      climatology.py         # month-specific avg_wind (sep..jan) + avg_temp from Open-Meteo archive (ERA5) per stadium
    weather/
      __init__.py
      openmeteo.py           # forecast / ensemble / historical-forecast / previous-runs clients; batching <=50 points; unit params
      nws.py                 # /points cache + gridpoints ISO8601 duration expansion
      parsers/openmeteo.py   # pure: raw json -> hourly rows
      parsers/nws.py
      merge.py               # per-game window aggregation + stitching -> WeatherForecast
    model/
      __init__.py
      config.py              # ALL constants (tiers, cutoffs, RAIN_SUPPRESS_MONTHS, key numbers, weights) in one place
      impact.py              # gs_fg / away_fg v1 exact + v2
      signals.py             # NFL/CFB/Combined dot rules ported from pages/*.py
      fair.py                # devig, consensus (Pinnacle-weighted), fair lines, pts->prob, edges, confidence
      clv.py                 # closing-line freeze + clv_pts
    odds/
      __init__.py
      base.py                # copied BaseScraper + GameLine dataclass
      teams.py               # normalize_team(sport, raw) via data/aliases + rapidfuzz + unresolved log
      merge.py               # canonicalize, game_key, pivot by book, main-line selection, openers
      betonline.py  betcris.py  fanduel.py  kalshi.py  novig.py  prophetx.py  pinnacle.py  draftkings.py
      parsers/               # pure parse functions per book (input: raw payload, output: list[GameLine])
        betonline.py betcris.py fanduel.py kalshi.py novig.py prophetx.py pinnacle.py draftkings.py
    alerts.py                # edge / move / opener / ops rules, dedup, quiet hours, digest, format_* HTML
    state.py                 # copied golf board/state.py + schema_version/migrate + D1 rehydrate
    outputs/
      __init__.py
      json_out.py            # site/web/data/*.json + snapshots (allow_nan=False)
      d1_out.py              # data/d1_inserts.sql (INSERT OR IGNORE, chunked 100 rows)
      legacy.py              # nfl_weather.csv / cfb_weather.xlsx column-exact
      raw_out.py             # raw/{sport}/{run_id}/... + manifest.json
      r2.py                  # boto3 push (alternative to wrangler in CI); self-check
    backtest.py              # weekly: closings + HRRR actuals + previous-runs -> data/backtest/*.parquet + board/backtest.json
    calibrate.py             # weekly: refit v2 coefficients -> data/calibration.json (PR)
  utils/
    __init__.py
    telegram.py              # copied verbatim (send_message); format helpers moved to pipeline/alerts.py
    state.py                 # copied; matchup_key -> game_id|market|book
    timeutil.py              # ET/UTC helpers, date_label 'SUN 11/09', time_label '01:00 PM'
  data/
    stadiums.csv  stadiums_overrides.csv  teams.csv  calibration.json
    aliases/nfl.json  aliases/cfb.json
    raw/cfbd_venues_2026.json  raw/wikidata_2026.csv  raw/osm_pitches_2026.json
    raw/cfb_locations_updated.csv         # git show 3aa1fa2
    raw/nfl_stadium_curated.csv           # recovered from csv history
    raw/cfb_stadium_curated.csv           # recovered from xlsx history
    backtest/                             # parquet (gitignored after Phase 6 -> R2)
    nfl_weather.csv  cfb_weather.xlsx     # committed only Phases 1-2
  site/
    worker/
      wrangler.toml  index.js  package.json
      migrations/0001_init.sql  0002_alerts.sql  0003_runs.sql
      test/worker.test.mjs
    web/
      index.html  app.js  map.js  table.js  drawer.js  charts.js  status.js  styles.css
      vendor/maplibre-gl.js  maplibre-gl.css  uplot.iife.min.js  uplot.min.css
      data/                                # gitignored build output
  scripts/
    recon_book.py            # generic Playwright response logger (copied recon_betcris.py)
    recover_static.py        # git history -> data/raw/*_curated.csv
    extract_golden.py        # git history -> tests/fixtures/golden_v1.parquet (~3000 rows)
    fixtures_scrub.py        # raw capture -> scrubbed test fixture
  tests/
    fixtures/raw/{betonline,betcris,fanduel,kalshi,novig,prophetx,pinnacle,openmeteo,nws,nflverse,cfbd}/
    fixtures/golden_v1.parquet
    test_impact_v1.py  test_impact_v2.py  test_signals.py  test_fair.py  test_clv.py
    test_betonline_parse.py  test_betcris_parse.py  test_fanduel_parse.py  test_kalshi_ladder.py
    test_novig_parse.py  test_prophetx_parse.py  test_pinnacle_parse.py
    test_merge_aliases.py  test_teams.py  test_weather_merge.py  test_stadium_loader.py
    test_alerts_rules.py  test_state_migrate.py  test_legacy_columns.py  test_json_out.py  test_d1_out.py
    test_pipeline_workflow.py  test_deploy_workflow.py  test_gate_check.py
  .github/workflows/
    pipeline.yml  deploy.yml  ci.yml  build-stadiums.yml  backtest.yml  calibrate.yml
  main.py                    # copied golf CLI: --book --sport --market --output --headed
  requirements.txt  requirements-dev.txt  pyproject.toml (ruff+pytest config only)
  README.md  CLAUDE.md
  docs/AUDIT.md  ARCHITECTURE.md  PLAN.md
Removed in Phase 4: app.py, pages/, streamlit requirements.
```

## 4. Data model

### 4.1 Canonical keys
- `sport` ∈ {`nfl`,`cfb`}.
- `team_id`: NFL = lowercase nflverse abbr (`sea`,`ne`,`lar`,`lac`,`lv`,`jax`); CFB = slug of CFBD School (`miami-fl`,`texas-am`,`ole-miss`).
- `stadium_id`: own slug (`gillette-stadium`).
- `game_id = f"{sport}:{season}:{week}:{away_id}@{home_id}"`; neutral games keep schedule home/away, `neutral=1`. CFB bowl/playoff week = 16..20 as CFBD reports; `week` for postseason NFL = 19..22.
- Odds key: `f"{game_id}|{market}|{side}|{book}"`; markets `ml|spread|total`; sides `home|away|over|under`.

### 4.2 pydantic contracts (`pipeline/contracts.py`, `frozen=True`)
```
Game(game_id, sport, season, week, kickoff_utc, kickoff_local, tz, home_id, away_id, stadium_id, neutral, roof_state, status, source)
Stadium(stadium_id, name, aliases[], city, state, country, lat, lon, elevation_m, timezone, orientation_deg, orientation_bucket, orientation_src, roof_type, surface, capacity, year_built, avg_wind_static, wind_vol_static, wind_impact_static, weakest_wind_effect, avg_wind_by_month{sep..jan}, avg_temp_f, wikidata_qid, osm_way_id, cfbd_venue_id, espn_venue_id, nflverse_stadium_id, needs_review)
Team(team_id, sport, name, short, home_stadium_id, avg_temp_f, conference, aliases[])
WeatherForecast(game_id, source, run_time, lead_hours, temp_fg, wind_fg, gust_fg, wind_dir_1h, wind_dir_2h, wind_dir_fg, wind_dir_deg, rain_fg_mm, precip_prob, wind_vol_fc, wind_p10, wind_p50, wind_p90, cross_mph, head_mph, model_disagreement, roof_state, hourly[] {t, temp, wind, gust, dir, precip, pop, p10, p90})
GameLine(sport, game_id, book, market, side, line: float|None, odds: int, prob_raw: float|None, is_main: bool, source_id, scraped_at, run_id)
Edge(game_id, book, market, side, line, odds, fair_line, fair_prob, vigfree_prob, edge_pts, edge_prob, confidence, tier in strong|edge|watch|none, model_version, ref_book, n_books)
Degradation(component, reason, severity, run_id, ts)
RunMeta(run_id, sport, season, week, git_sha, started_at, finished_at, stage_timings{}, counts{book:{market:n}}, baseline{}, degradations[], unresolved_names[], next_run_eta)
```

### 4.3 D1 schema — `site/worker/migrations/0001_init.sql`
```sql
CREATE TABLE IF NOT EXISTS stadiums (
  stadium_id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, state TEXT, country TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL, elevation_m REAL, timezone TEXT,
  orientation_deg REAL, orientation_bucket TEXT, orientation_src TEXT,
  roof_type TEXT CHECK(roof_type IN ('open','dome','retractable')), surface TEXT,
  capacity INTEGER, year_built INTEGER,
  avg_wind_static REAL, wind_vol_static TEXT, wind_impact_static TEXT, weakest_wind_effect TEXT,
  avg_wind_sep REAL, avg_wind_oct REAL, avg_wind_nov REAL, avg_wind_dec REAL, avg_wind_jan REAL,
  avg_temp_f REAL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
  team_id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL, short TEXT,
  home_stadium_id TEXT REFERENCES stadiums(stadium_id), avg_temp_f REAL, conference TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY, sport TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL,
  kickoff_utc TEXT NOT NULL, kickoff_local TEXT, tz TEXT,
  home_id TEXT NOT NULL, away_id TEXT NOT NULL, stadium_id TEXT, neutral INTEGER DEFAULT 0,
  roof_state TEXT, status TEXT, source TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_week ON games(sport, season, week);
CREATE INDEX IF NOT EXISTS idx_games_kick ON games(kickoff_utc);

-- change-only: insert when any of wind/gust/temp/precip/pop/gs_fg moved vs last row for (game_id, source)
CREATE TABLE IF NOT EXISTS weather_history (
  game_id TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL, run_id TEXT NOT NULL,
  lead_hours REAL, temp_f REAL, wind_mph REAL, gust_mph REAL, wind_dir_deg REAL, wind_dir TEXT,
  precip_mm REAL, precip_prob REAL, wind_vol REAL, wind_p10 REAL, wind_p90 REAL, cross_mph REAL, head_mph REAL,
  gs_fg REAL, away_fg REAL, gs_fg_v2 REAL, away_fg_v2 REAL, model_version TEXT,
  PRIMARY KEY (game_id, source, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_wx_game ON weather_history(game_id, fetched_at);

-- change-only: first row per (game_id, book, market, side) = opener
CREATE TABLE IF NOT EXISTS odds_history (
  scraped_at TEXT NOT NULL, game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, prob REAL, fair_line REAL, fair_prob REAL, edge_pts REAL, edge_prob REAL,
  is_main INTEGER DEFAULT 1, run_id TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side, scraped_at)
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_history(game_id, market, scraped_at);
CREATE INDEX IF NOT EXISTS idx_odds_time ON odds_history(scraped_at);
CREATE INDEX IF NOT EXISTS idx_odds_book ON odds_history(book, scraped_at);

CREATE TABLE IF NOT EXISTS openers (
  game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, seen_at TEXT NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side)
);
CREATE TABLE IF NOT EXISTS closings (
  game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, scraped_at TEXT NOT NULL, kickoff_utc TEXT NOT NULL, frozen_at TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side)
);
```
`0002_alerts.sql`:
```sql
CREATE TABLE IF NOT EXISTS alerts (
  alert_key TEXT PRIMARY KEY, family TEXT NOT NULL, game_id TEXT, sport TEXT, season INTEGER, week INTEGER,
  market TEXT, side TEXT, book TEXT, tier TEXT, model_version TEXT,
  first_sent_at TEXT NOT NULL, last_sent_at TEXT NOT NULL, sends INTEGER DEFAULT 1,
  first_line REAL, first_odds INTEGER, first_fair REAL, first_edge REAL,
  last_line REAL, last_odds INTEGER, last_fair REAL, last_edge REAL,
  closing_line REAL, clv_pts REAL, status TEXT CHECK(status IN ('open','closed','settled')), run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_week ON alerts(sport, season, week);
CREATE INDEX IF NOT EXISTS idx_alerts_game ON alerts(game_id);
```
`0003_runs.sql`:
```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, sport TEXT, season INTEGER, week INTEGER, git_sha TEXT, scope TEXT,
  started_at TEXT, finished_at TEXT, duration_s REAL, status TEXT,
  stage_timings_json TEXT, counts_json TEXT, degradations_json TEXT, unresolved_json TEXT,
  n_games INTEGER, n_lines INTEGER, n_alerts INTEGER
);
CREATE TABLE IF NOT EXISTS stadium_results (
  stadium_id TEXT NOT NULL, sport TEXT NOT NULL, season INTEGER NOT NULL,
  under_w INTEGER, under_l INTEGER, under_p INTEGER, roi REAL, n INTEGER, updated_at TEXT,
  PRIMARY KEY (stadium_id, sport, season)
);
```
All pipeline writes: `INSERT OR IGNORE` (history/openers/alerts-first-send) or `INSERT ... ON CONFLICT(pk) DO UPDATE` (games, stadiums, teams, alerts updates, runs). Statements chunked ≤100 rows, ≤100 KB each.

## 5. R2 layout and JSON payloads

Prefix `board/` (served via Worker `/data/<name>.json`, `cache-control: no-store`). Each payload embeds `meta` `{run_id, last_updated, season, week, sport_counts, git_sha, model_version, next_run_eta, degradations[]}`.

| Object | Content |
|---|---|
| `board/meta.json` | RunMeta + `books: {book: {count, baseline, status: green|amber|red, last_ok}}`; pushed LAST |
| `board/games_nfl.json`, `board/games_cfb.json` | `[GameCard]` (below) |
| `board/board.json` | combined slim table rows for the Table view |
| `board/history.json` | `{"<game_id>|<market>|<side>|<book>": [[ts, line, odds], ...]}` change-only, HISTORY_CAP=120 |
| `board/wx_history.json` | `{"<game_id>": [[ts, lead_h, wind, gust, temp, precip, pop, gs_fg], ...]}` change-only |
| `board/alerts_feed.json` | last 200 sent alerts `{alert_key, family, tier, game_id, text_html, sent_at, clv_pts}` — shared keys with Telegram |
| `board/backtest.json` | bucket grid + stadium results + matched games (Phase 6) |
| `board/status.json` | last 20 runs (from D1 runs) + current degradations + unresolved names + counts vs baseline |
| state: `board/openers.json`, `board/history.json`, `board/wx_history.json`, `board/archive_last.json`, `board/wx_last.json`, `board/alerts.json`, `board/scrape_baseline.json`, `board/telegram_state.json`, `board/cf_heartbeat.json`, `board/closings.json` | every state file: `{"schema_version": N, "run_id": ..., ...}` |
| `raw/{sport}/{run_id}/{source}.json` + `raw/{sport}/{run_id}/manifest.json` | verbatim captures; manifest `{source: {sha256, bytes, fetched_at, url}}` |
| `snapshots/{sport}/{season}/{week}/{run_id}.json` | full GameCard list per run (backtest input) |
| `legacy/nfl_weather.csv`, `legacy/cfb_weather.xlsx` | Phase 3+ |

**GameCard**:
```
{game_id, sport, season, week, kickoff_utc, kickoff_local, tz, date_label 'SUN 11/09', time_label '01:00 PM',
 home {team_id, name, short}, away {...}, neutral, status,
 stadium {stadium_id, name, lat, lon, orient_deg, orient, roof_type, roof_state, elevation_m, year_built,
          wind_vol_static, wind_impact_static, weakest_wind_effect, avg_wind, avg_wind_month},
 travel_alt, home_temp, away_temp,
 weather {temp_fg, wind_fg, gust_fg, wind_dir_1h, wind_dir_2h, wind_dir_fg, wind_dir_deg, rain_fg, precip_prob,
          precip_prob_ens, wind_vol_fc, wind_p10, wind_p90, wind_diff, cross_mph, head_mph, source, lead_hours, fetched_at,
          hourly [{t, temp, wind, gust, dir, precip, pop, p10, p90}]  (kickoff-1h .. kickoff+4h)},
 impact {v1 {gs_fg_pct, away_fg_pct, components {wind, cold, heat, rain, alt, heat_away, cold_away}},
         v2 {...same + w_eff, dir_mult, conf}, model_version},
 signal {nfl|cfb label, color, size, flags [CFB Wind|NFL Wind|Heat|Alt+Heat], dow_base},
 odds {<book>: {spread {home_line, home_odds, away_odds, open_line, open_odds, updated_at},
                total  {line, over, under, open_line, open_under, updated_at},
                ml     {home, away, open_home, open_away}}},
 consensus {spread_open, spread_now, total_open, total_now, move_s, move_t, ref_book, n_books, thin},
 fair {my_total, my_spread, fair_total_v2, fair_spread_v2, edges [Edge...], best_total, best_spread},
 alerts [alert_key...], run_id}
```

## 6. Weather stack

Clients in `pipeline/weather/openmeteo.py` and `nws.py`; all raw responses captured (§1.3).

- **Forecast** `https://api.open-meteo.com/v1/forecast` — `models=ncep_nbm_conus,ncep_hrrr_conus,ncep_gfs_seamless,ecmwf_ifs025`, `hourly=temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,wind_direction_10m`, `wind_speed_unit=mph&temperature_unit=fahrenheit&precipitation_unit=mm`, `timezone=UTC`, `start_hour/end_hour` = kickoff−1h .. kickoff+4h UTC, batched `latitude=a,b,..&longitude=..` ≤50 points/call. International venues: models default (`best_match` + `ecmwf_ifs025`) since CONUS models return null.
- **Ensemble** `https://ensemble-api.open-meteo.com/v1/ensemble` — `models=ecmwf_ifs025,gfs_seamless`, `hourly=wind_speed_10m,wind_gusts_10m,precipitation`. `wind_vol_fc = P90−P10` of pooled member wind over kickoff..+3h; `wind_p10/p50/p90`; `precip_prob_ens` = fraction of members with precip >0.1 mm in window.
- **NWS** `https://api.weather.gov/points/{lat},{lon}` (cached in `data/raw/nws_points.json`) → `/gridpoints/{wfo}/{x},{y}` raw grid; expand `validTime` ISO8601 durations; read `uom` per field; User-Agent `football_weather (mckinleyslade@gmail.com)`; retry 3× on 5xx; horizon ≤7.5 d. Used as fallback and as independent gust/PoP second opinion.
- **Backtest sources**: `historical-forecast-api.open-meteo.com` (HRRR from 2018, NBM from 2024-10) as actuals; `previous-runs-api.open-meteo.com` `_previous_day1..7` as lead-N forecasts; `archive-api` ERA5 for climatology (`stadiums/climatology.py`).
- **Stitching** (`weather/merge.py`), stamped per row as `source` + `lead_hours`:
  - lead ≤18 h (≤48 h when a synoptic HRRR run covers the window): wind/gust/temp/precip from HRRR; PoP from NBM.
  - 18 h < lead ≤ 11 d: wind mean/temp/PoP from NBM; gusts from GFS (NBM lacks gusts); precip mm from NBM.
  - lead > 11 d: `gfs_seamless` with `ecmwf_ifs025` averaged; flag `low_confidence`.
  - NWS replaces any null field ≤7 d; if Open-Meteo fails entirely → NWS-only + Degradation(warn).
  - `model_disagreement = max−min` of deterministic model wind means.
- **Aggregation window**: v1 legacy fields = mean of the 3 hourly samples at kickoff hour, +1h, +2h (matches old wind_fg arithmetic); `wind_dir_1h/2h` = 16-pt compass of hour 1/2; `wind_dir_fg` = compass of vector-mean direction; `rain_fg` = sum of mm over the same 3 hours. Legacy NFL writer keeps unrounded floats; CFB writer rounds wind 1dp, temp 2dp.
- **Roof**: `roof_state` per game = nflverse `roof` field when present (`outdoors|dome|closed|open`), else stadium `roof_type` (`dome`→closed; `retractable`→ heuristic closed if temp<40 or precip_prob>0.6 or wind_fg>20, else open; `open`→outdoors). `closed`/`dome` ⇒ weather fields kept for display but all impact components = 0 and `avg_wind=0`.
- **Components**: `cross_mph = |wind_fg · sin(wind_dir_deg − orientation_deg)|`, `head_mph = |wind_fg · cos(...)|`.

## 7. Edge model spec (`pipeline/model/`)

### 7.1 v1 — exact reproduction (alert model). Constants in `model/config.py`
```
WIND_TIERS = [(25.0, 10.0), (17.0, 6.5), (15.0, 3.5), (12.0, 2.0)]   # first match, descending
COLD_BASE_F = 30.0; COLD_PER_F = 0.125
HEAT_BASE_F = 80.0; HEAT_PER_F = 0.125
RAIN_TIERS_MM = [(12.0, 6.5), (6.0, 3.0), (1.0, 1.5)]; RAIN_TIER_STRICT_MM = {12.0, 1.0}
RAIN_SUPPRESS_MONTHS = {9}          # keyed on the RUN month (generator clock), not the game month
HEAT_AWAY_DELTA_F = 10.0; HEAT_AWAY_CUTOFF_F = {"cfb": 54.0}
COLD_AWAY_BASE_F = 32.0; COLD_AWAY_AWAY_TEMP_MIN_F = {"nfl": 60.0, "cfb": 65.0}
ALT_TIERS_M = {"nfl": [(1283, 3.5), (900, 2.0)], "cfb": [(1000, 3.5)]}
CFB_ALT2_C = 2.0; CFB_ALT2_TRAVEL_MIN_M = 700.0; CFB_ALT2_HOME_ELEV_MIN_M = 1100.0
```
```
wind_c  = tier(wind_fg)                       cold_c = max(0, 30 - temp_fg) * 0.125
heat_c  = max(0, temp_fg - 80) * 0.125        rain_c = 0 if RUN month in RAIN_SUPPRESS_MONTHS else rain tiers (>1 / >=6 / >12)
gs_fg_pct = -(wind_c + cold_c + heat_c + rain_c)          # 0 when roof_state closed/dome
heat_away = heat_c if temp_fg > 80 and home_temp - away_temp >= 10   # NFL every era; CFB pre-2024-09-27
            (CFB from 2024-09-27 on: away_temp < 54 instead of the delta)
cold_away = max(0, 32 - temp_fg) * 0.125 if temp_fg < 32 and away_temp >= floor  # nfl 60 (65 pre-2026), cfb 65
alt_c     = tier(travel_alt_m, ALT_TIERS_M[sport]); CFB adds 2.0 if travel_alt >= 700 and home_elev >= 1100
away_fg_pct = -max(heat_away + cold_away, alt_c)   # NFL;  CFB sums: -(alt_c + heat_away + cold_away)
```
Era switches (2024-09-27 CFB heat_away, 2026-01 NFL cold_away floor) live only in the
golden replay via ``compute_impact_v1(..., era_date=...)``; live code always runs the
current-era rules.
Legacy NFL outputs divide by 100 (`gs_fg=-0.035`); CFB stays percent. Golden test: `tests/fixtures/golden_v1.parquet` (~43.7k rows from git history via `scripts/extract_golden.py`, carrying `run_month`/`commit_date`/`home_elev` for the era-aware replay) — mismatches logged with row + component diff; test asserts ≥99.5% exact match (within each file's storage rounding: NFL 5-dp fractions, CFB 2-dp) and prints boundary mismatches (rain ≈6.0, heat-away delta 8.31–11.37, alt intervals, CFB 1-dp wind/rain tier edges) rather than failing on them. The expected residue (~150 rows) is CFB `wind_fg` stored at 1 dp exactly on a tier threshold — irreducible from the stored files.

### 7.2 Legacy derived columns (CFB) — `model/fair.py`
`ref_total`, `ref_spread` (home-relative) from consensus (§7.3). `My_total = ref_total*(1+gs_fg/100)`; `Edge = (FD_now−My_total)/My_total`; `My_spread = ref_spread*(1+away_fg/100)`; `Edge_s = ref_spread−My_spread`; `Spread`/`Total_proj` columns = ref values with `ref_book` stamped in meta. `Move_t=(FD_now−Fd_open)/Fd_open`; `Move_s=Open−Current`; `wind_diff=wind_fg−wind_avg`. Consensus "now" for legacy: CFB = FanDuel (as before), NFL = BetOnline; fallback = devigged median.

### 7.3 Consensus / fair / edges (improvement)
- Devig per book per market via multiplicative normalization of the two sides (`american_to_prob`, copied from golf). Exchanges (Kalshi/Novig/ProphetX) use `prob_raw` directly.
- **Consensus line** = weighted median of main lines with `BOOK_WEIGHTS = {pinnacle:3, betonline:2, betcris:1.5, fanduel:1, draftkings:1, kalshi:1, novig:1, prophetx:0.75}`; `n_books<2 ⇒ thin=True` (no edges, no alerts). Consensus prob at that line = weighted mean of devigged probs after moving each book to the consensus line via pts→prob.
- **pts→prob**: totals `PTS_PROB_TOTAL = {"nfl": 0.026, "cfb": 0.020}` per point; spreads key-number-aware table `SPREAD_KEY_PROB["nfl"] = {0.5:.02,1:.015,1.5:.02,2:.02,2.5:.03,3:.095,3.5:.03,4:.02,...,6:.04,7:.075,...,10:.045,14:.04}` (cumulative half-point values from `data/calibration.json`, defaults shipped); CFB flatter table.
- **Fair line** v1: `fair_total = consensus_total * (1 + gs_fg/100)`; `fair_spread = consensus_spread * (1 + away_fg/100)` (sign: home-relative). v2 uses v2 components (§7.5).
- **Per (game, book, market, side)**: `edge_pts = signed(fair_line − book_line)` (positive means the side is favorable: under when book total > fair, over when book total < fair, home when book gives home more points than fair, etc.); `edge_prob = fair_prob(side at book line) − vigfree_prob(side)`; `best_book` per market/side.
- **Confidence** `conf = clamp(0,1, 1 − 0.5·min(1, wind_vol_fc/15) − 0.3·min(1, model_disagreement/8) − 0.2·min(1, max(0, lead_hours−36)/(168−36)))`; v1 uses `wind_vol_static` mapped {low:.2, mid:.5, high:.75, very high:1.0}·15 when ensemble missing.
- **Tier**: `strong` if edge_pts ≥ STRONG[sport][market] and conf ≥ 0.5; `edge` if ≥ EDGE[sport][market] and (conf ≥ 0.5 or lead ≤ 36 h) and edge_prob ≥ 0.03; `watch` if ≥ 0.6·EDGE (map only, never alerted); else none. `EDGE = {nfl:{total:1.5, spread:1.0}, cfb:{total:2.5, spread:1.5}}`, `STRONG = {nfl:{total:2.5, spread:1.5}, cfb:{total:4.0, spread:2.5}}`. Weather-driven requirement: `gs_fg ≤ −3.5` or `rain_c > 0` or `away_fg ≤ −2.0` (v1 percent).
- **CLV** (`model/clv.py`): at first run after kickoff, freeze `closings` = last `odds_history` row before `kickoff_utc` per key; `clv_pts = signed(closing_line − alert.first_line)` in the alerted side's favor; written to `alerts.clv_pts`; weekly Monday digest by tier/league/book and v1 vs v2.

### 7.4 Signals (`model/signals.py`, ported from pages/*)
- NFL (evaluate in this order so purple renders): `wind_fg>15 and 32≤temp_fg≤45` → High/purple/40; `(rain_fg>2) or (8<wind_fg<15 and temp_fg<60)` → Low/blue/15; `wind_fg>15 and temp_fg<60` → Mid/orange/25; else No/green/7. `wind_vol` forced 'Low' when wind_fg<11.99. `wind_diff = wind_fg − avg_wind`.
- CFB: DOW base `{Mon:11.14,Tue:11.14,Wed:10.10,Thu:10.10,Fri:9.31,Sat:8.79,Sun:11.93}` (DOW of run, ET); `hi = base+7.5`; `sp=|Open|`: Very High (darkred,50): wind>hi & temp<50 & sp≤10.5; High (purple,40): wind>hi & temp<65 & sp≤10.5; Mid (orange,25): ((wind>hi & temp<65) or (travel_alt>800 & temp>75)) & sp≤20.5; Low: ((wind>base & temp<65) or rain_fg>2 or (temp>80 & home_temp<57 & away_temp<57)) & sp≤20.5 → colors black 'Low (Rain)' if rain>2, red 'Low (Temp)' if heat cond, else blue 'Low (Wind)', size 15; No (green,7).
- Combined flags: `CFB Wind`: |Open|<10.5 & temp<70 & wind>14; `NFL Wind`: wind>15 & temp<60; `Heat`: home_temp<57 & away_temp<57 & temp>80; `Alt+Heat` (CFB): travel_alt>800 & −10≤Open≤10 & temp>75. Colors purple/blue/red/saddlebrown; `dot_size = |gs_fg_pct|*4+7` (NOTE: old NFL used fraction → ≈7; new uses percent for both, marked improvement).
- Backtest bucket lookup reproduces `pages/cfb_weather.py` first-match semantics against `backtest.json`.

### 7.5 v2 (additive, `model_version='v2'`, shown side by side)
- `w_eff = 0.7*wind_fg + 0.3*gust_fg`; `w_dir = sqrt(cross² + 0.5·head²)` computed from `w_eff` components; `wind_c2 = min(12, 0.55*max(0, w_dir−10)^1.15)`.
- `dir_mult = 0.5` when `wind_dir_fg` ∈ stadium weak set parsed from `weakest_wind_effect` (`'x N'`→all except N; `'E/W'`→{E,W}; `'all'`→∅); else 1.0. `wind_c2 *= dir_mult`.
- `rain_c2 = tier(expected_mm) if precip_prob ≥ 0.4 else 0`, `expected_mm = precip_prob_ens * rain_fg_mm`; no September suppression; zero when roof closed.
- `alt_c2 = min(3.5, 0.0035*max(0, travel_alt−800))`.
- `heat_away2 = heat_c if temp_fg>80 and (home_temp − away_temp) ≥ 12`.
- cold/heat unchanged. `gs_fg_v2 = −(wind_c2+cold_c+heat_c+rain_c2)`; `away_fg_v2 = −max(heat_away2+cold_away, alt_c2)`.
- Coefficients (0.7/0.3, 0.55, 1.15, cap 12, 0.0035, cutoffs) live in `data/calibration.json`, refit weekly by `calibrate.yml` against CLV/closing totals; v1 constants never refit.

## 8. Odds scrapers (`pipeline/odds/`) — per-book contract

All: `class XScraper(BaseScraper)`, `BOOK_NAME`, `async scrape(sport) -> list[GameLine]`, raw capture via `raw_out.capture(book, sport, payload)`, parse via `parsers/<book>.parse(payload, sport) -> list[GameLine]`, env flag `BOOK_<X>_ENABLED` (default 1). Team strings → `odds/teams.normalize_team(sport, raw, book)`; unresolved → `meta.unresolved_names[]` + Degradation(warn) + row dropped.

| Book | Transport | Endpoint / params | Parse notes |
|---|---|---|---|
| betonline | Playwright chromium + stealth; job B | `offering-by-league {Sport:'football', League: 'nfl'|'nfl-preseason'|'college-football', filterTime:0}` | `GamesDescription[].Game{AwayTeam,HomeTeam,ScheduleText,AwayLine{Spread,Total,MoneyLine},HomeLine{...}}` — key names locked by recon fixture |
| betcris (bookmaker.eu) | httpx+BS4 | `/en/sports/football/{nfl,nfl-preseason,college-football}/` | `vTeam_N/hTeam_N`; `vN_/hN_` names; `vS_/hS_` spread; `vT_/hT_` total; `vM_/hM_` ML; '½'→.5; `th.oddsSubTitle` neutral venue; time title 'START m/d h:mmam PT' (year inferred from season) |
| fanduel | httpx | `content-managed-page?page=CUSTOM&customPageId={nfl,ncaaf}&_ak=FhMFpcPWXMeyZxOx`; fallback `competition-page?eventTypeId=1&competitionId=` (12282733 NFL, 11432305 NFL Preseason, 12529073 NCAA Games) | `MONEY_LINE`, `MATCH_HANDICAP_(2-WAY)`, `TOTAL_POINTS_(OVER/UNDER)`; runner `handicap`, `result.type`; event name 'Away @ Home', `openDate` |
| kalshi | httpx | `/events?series_ticker={KXNFLGAME,KXNFLSPREAD,KXNFLTOTAL,KXNCAAFGAME,KXNCAAFSPREAD,KXNCAAFTOTAL}&status=open&with_nested_markets=true&limit=200` | ticker `{SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{TEAMn|n}`; `floor_strike`; `yes_bid_dollars/yes_ask_dollars`; fee 0.07·P(1−P); main line = strike nearest consensus (else nearest 0.5 prob); ladder → alternates with `is_main=False`; abbr aliases in `aliases/*.json` |
| novig | httpx GraphQL | `event(where:{league:{_in:["NFL","NCAAF"]}, type:{_eq:"Game"}, status:{_in:[OPEN_PREGAME,OPEN_INGAME]}}){... markets(where:{status:{_eq:"OPEN"}, type:{_in:["MONEY","SPREAD","TOTAL"]}}){id type strike is_consensus outcomes{type description available last}}}` | strike home-relative; `is_consensus` = main; `available` prob (null ⇒ skip) |
| prophetx | httpx partner API | tournaments filtered by sport 'American Football'/NFL/NCAAF; `get_multiple_markets?market_types=moneyline,spread,total` | `selection.line`, decimal price → American; liquidity |
| pinnacle | httpx guest | `/sports/15/matchups`, `/sports/15/markets/straight?primaryOnly=false` | `type` moneyline/spread/total, period 0; leagues NFL + NCAA; reference weight 3 |
| draftkings (optional) | curl_cffi | eventGroup 88808/87637, category 492 | `displayOdds.american` (U+2212), `points` |

`odds/merge.py`: build `game_key` by matching `(away_id, home_id)` + kickoff within ±36 h to the schedule `Game`; neutral-site flips handled by trying swapped sides; pivot by book; select main line per book/market (`is_main` or nearest consensus); update openers (first-seen per key, from `openers.json` rehydrated from D1 `openers` if R2 empty); emit `odds_history` deltas (change-only vs `archive_last.json`).

## 9. Scheduling and GitHub Actions

### 9.1 Cloudflare crons (`site/worker/wrangler.toml`)
Assume **Workers Paid** ($5/mo). If free (2 crons left on account): `"*/30 * * * *"` + `"15 9,14,20 * * *"` with in-handler trimming. Paid explicit set (UTC, Quartz DOW 1=Sun; scheduled() re-checks ET so DST is handled):
```
crons = [
  "0 14,20 * * 3,4",        # Tue/Wed 10:00,16:00 ET-ish (openers) scope=light
  "0 12-2/2 * * 5,6",       # Thu/Fri every 2h 08:00-22:00 ET scope=light (full at 12,18 ET)
  "0 10-1 * * 7",           # Sat hourly 06:00-21:00 ET (CFB) scope=full at :00 of 10,14 ET
  "0 10-21 * * 1",          # Sun hourly 06:00-17:00 ET (NFL)
  "30 16,19,23 * * 1",      # Sun pre-kickoff 12:30/15:30/19:30 ET scope=full
  "0 22,23 * * 2,5",        # Mon/Thu night 18:00,19:00 ET
]
```
`scheduled()` computes ET hour/DOW via `Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',...})`, maps `event.cron` → `{sport, scope}` and trims (e.g. no CFB fires Jan–Aug). Every fire writes heartbeat first.

### 9.2 `pipeline.yml`
```
on: workflow_dispatch {sport: nfl|cfb|all (default all), scope: full|light (default light), force: bool}
    schedule: ['17 9,14,20 * * *']    # UTC backstop ≈ old 05:15/10:00/16:20 ET cadence, off-the-minute
concurrency: group football-refresh, cancel-in-progress: false
jobs:
  gate:  ubuntu-latest, 3 min. checkout; setup-python 3.11; pip install httpx; python -m pipeline.gate_check --sport
         outputs: run (skip|scrape), need_playwright (true when scope==full and any book playwright-enabled). fail-open.
  light: needs gate; if run==scrape; timeout 15. checkout; setup-python 3.11 (pip cache); setup-node 20;
         pip install -r requirements.txt (no playwright);
         R2 state get loop (wrangler r2 object get football-board/board/$f.json --remote) for
           openers history wx_history archive_last wx_last alerts scrape_baseline telegram_state cf_heartbeat closings
           -> on non-NoSuchKey error: exit 1;
         python -m pipeline.build --sport $sport --scope light --run-id $RUN_ID;
         (Phase 1-2 only) git commit legacy files with 3-attempt pull --rebase -X theirs --autostash loop;
         R2 put loop: raw/ manifest, snapshots, data payloads, state files, meta.json LAST (3 retries each, --remote);
         d1 execute --remote --yes --file=data/d1_inserts.sql if hashFiles;
         python -m pipeline.outputs.r2 --self-check (re-fetch meta, assert run_id, content floor);
         upload-artifact logs; Telegram if: failure().
  playwright: needs [gate, light]; if need_playwright==true; timeout 12. setup-python; pip install playwright playwright-stealth;
         python -m playwright install --with-deps chromium;
         python -m pipeline.build --sport $sport --scope odds --books betonline,fanduel_pw --merge-into-r2
           (fetches board/games_*.json + state, re-runs merge/fair/edges/alerts for changed rows, republishes meta last);
         Telegram if: failure().
```
Runtime targets: gate 20 s, light 2–3 min, playwright 3–4 min. `timeout-minutes` 20 total.

### 9.3 Other workflows
- `deploy.yml`: on push paths `site/**` → `wrangler d1 migrations apply football-odds --remote` then `cloudflare/wrangler-action@v3` deploy from `site/worker`.
- `ci.yml`: on PR/push → ruff, pytest (with sys.modules stubs), workflow-contract tests, `node --test site/worker/test`.
- `build-stadiums.yml`: workflow_dispatch → `python -m pipeline.stadiums.build_stadiums` → opens PR (peter-evans/create-pull-request) with diff; never commits to main.
- `backtest.yml`: schedule `'0 11 * * 2'` (Tue 06:00 ET-ish) + dispatch → `python -m pipeline.backtest` → R2 `board/backtest.json` + `data/backtest/*.parquet` to R2; Telegram Monday CLV digest is sent by `pipeline.alerts --digest` inside this job.
- `calibrate.yml`: schedule weekly Tue after backtest → `python -m pipeline.calibrate` → PR updating `data/calibration.json`.
- Season gating: `gate_check.py` skips when no kickoff within `HORIZON_DAYS` = 45 days for the requested sport (CFB dark Jan–Jul after bowls; NFL skip Mar–Jul); `--force` bypasses.
- Two horizons in `pipeline/build.py`: the **weather window** `[now−6h, now+10d]` (`WINDOW_AFTER_D`) bounds forecasts, impact, cards, legacy files and alerts; the **odds horizon** `[now−6h, now+45d]` (`ODDS_WINDOW_AFTER_D` == `gate_check.HORIZON_DAYS`, pinned by test) bounds which schedule games scraped lines are matched against, so openers, `history.json` series, `archive_last` and D1 `odds_history` / `openers` / `games` rows (impact columns NULL until the game has a card) start when a book first posts the line. The OPENERS digest keys on openers new *to the board* (game entered the window since the previous run) rather than new to state.

## 10. Telegram alert spec (`pipeline/alerts.py`)

Transport: `utils/telegram.py` `send_message` (HTML). Dedup: `alerts.json` `{sent:{key:ts}}` copied from golf `board/state.py`, ALERTS_CAP 500, mark ONLY after successful send, R2 round-trip, mirrored to D1 `alerts`; rehydrate from D1 if R2 missing. Every sent alert also appended to `alerts_feed.json`.

Families and keys:
1. **EDGE** `edge|{season}|{week}|{game_id}|total|under|{book}|{model_version}` — fires for **every game in a signal tier** (legacy bet rules, §7.4: `card.signal.label` ≠ "No Impact", Low through Very High, both sports). The play is the TOTAL UNDER: the `fair.edges` under entry with the largest `edge_pts` (any book), else a `consensus`-book entry synthesised from `consensus.total_now` vs `fair.fair_total`. The market edge is a NOTE (shown even when ≤ 0), never a gate — `fair.weather_driven`, `consensus.thin` and the §7.3 tiers are board-only. Record/Candidate `tier` = signal slug `low|mid|high|very_high`; record carries `last_signal`. One send per key. Message:
```
<b>🌬 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>
Gillette · wind 18 mph SE (gust 26 · vol 6 · cross 15) · 41°F · rain 20% / 0.8 mm
Impact −6.5% (wind 6.5 · v1) · conf 0.72 · fair total 34.6 (ref pinnacle, 6 books)
<b>Mid Impact</b> · wind 18 mph · 41°F · rain 0.8 mm · NFL Wind
<b>UNDER 38 −110 @ BetOnline</b> · market edge +3.4 pts / +4.1% · open 38
Books: <b>FD u38.5 −108</b> · BetOnline u38.5 −110 · Betcris u38.0 −112 · Kalshi u38.0 (53¢) · Pinnacle u37.5 −105 · ref u37.5
<a href="https://football-board.<acct>.workers.dev/#sport=nfl&week=3&game=nfl:2026:3:sea@ne">board</a>
```
   Consensus-synthesised bet line: `<b>UNDER 37.5 (consensus)</b> · market edge −0.6 pts vs fair 38.1 (market already there)`; no line: `<b>UNDER</b> · no line posted yet`. `Books:` = every book in `card.odds` pricing the alerted side, best-first for that bettor (higher total for UNDER, then better odds; Kalshi as `(¢)`), consensus `ref` last, wrapped past 180 chars, `Books: no lines posted` when empty.
2. **MOVE** on alerted keys only: `move|{edge_key}|{bucket}`, bucket = `floor(|line_now − first_alerted_line| / step)`, step 1.0 total / 0.5 spread; direction 'toward fair' / 'away from fair'; ≤1 per key per 2 h (consensus records move on `consensus.total_now`). **SIGNAL GONE**: `gone|{edge_key}` when the signal drops to "No Impact" → status=closed (`SIGNAL GONE: was Mid Impact → No Impact · UNDER 38 @ BetOnline · market edge now …`). **SIGNAL CHANGE**: `wx|{edge_key}|sig-{slug}` when the label changes but stays in a tier (`SIGNAL Low Impact → Mid Impact` + wind/temp/rain + bet line + Books); updates the record's `last_signal`. **FORECAST MOVE**: `wx|{edge_key}|{bucket}` when fair line moved ≥1.0 pt due to weather; message shows old→new wind/rain.
3. **OPENER DIGEST**: `openers|{sport}|{season}|{week}|{ET-day}` once per run when `utils/state.compute_delta` finds new `game_id|market|book` keys, only games with `gs_fg ≤ −2` or `wind_fg ≥ 12`.
4. **OPS**: scrape volume (copied `_check_scrape_volume`, per (book,market) high-water per season-week, dark-book edge-triggered w/ re-arm); Degradation warn+ (`degr|{component}|{reason}|{ET-day}`); heartbeat/meta stale >20 h; stadium unresolved (`stadium|{game_id}`); unresolved team names (`names|{book}|{ET-day}`, list); no reference line (`noref|{sport}|{ET-day}`); workflow `if: failure()` curl; Worker dispatch failure via `notifyTelegram`; self-check failure.
5. **WEEKLY CLV DIGEST** (Monday, from backtest.yml): by tier/league/book, v1 vs v2, top 5 / bottom 5 alerts.

Rate/quiet: max 25 messages per run, overflow → one digest; quiet hours 23:00–07:00 ET queue Low/Mid alerts in `telegram_state.json` and flush as 07:00 digest; `high` / `very_high` signal tiers, GONE and kickoff <3 h bypass. Optional routing `TELEGRAM_CHAT_ID_NFL` / `TELEGRAM_CHAT_ID_CFB` fallback to `TELEGRAM_CHAT_ID`. `--no-alerts` and `--dry-run` print instead of sending.

## 11. Frontend spec (`site/web/`)

Vanilla JS, no build step. Copy golf `index.html` CSS shell + `app.js` scaffolding (fetch `data/*.json?bust`, `auth/me`, meta polling → reload, table sort/filter, hover cards). Vendored MapLibre GL JS/CSS + uPlot. Basemap: OpenFreeMap style URL (no key); fallback option Protomaps PMTiles in R2 range-served by Worker.

Views (tabs, URL hash state `#sport=nfl&week=3&view=map&game=...&signal=...&book=...&minEdge=...`): **NFL Map**, **CFB Map**, **Signals** (preset filters replacing combined_signals.py), **Table**, **Alerts**, **Backtest**, **Status**.

Header: sport/week selectors; 'Updated HH:MM ET (viewer tz) · next run ~HH:MM'; book-status chips green/amber/red from `meta.books`; degradation banner(s).

Map markers (SVG symbol layer): fill by impact tier (palette green No, blue Low(Wind), black Low(Rain), red Low(Temp), orange Mid, purple High, darkred Very High; Signals view purple CFB Wind/blue NFL Wind/red Heat/saddlebrown Alt+Heat); ring color = rain (black) / heat (red) driver; radius = old buckets 7/15/25/40/50 by tier, or by |edge_pts| of best edge in Edge mode; opacity = confidence (0.4..1.0) with toggle to static wind_vol; wind arrow rotated to `wind_dir_deg` with length ∝ wind_fg; thin field-axis line rotated to `orient_deg`; hollow marker for dome/closed roof; clustering below zoom 4; toggles show domes / show watch tier. Hover card = old template (Game, Wind, Gust, Temp, Rain mm + prob, Impact %, Total open/now, Spread open/now, Location, Volatility, Best edge, Record/ROI where backtest matched).

Detail drawer (click / deep link): hourly weather strip kickoff−1h..+4h with ensemble P10–P90 band; forecast-drift sparkline (wx_history); stadium card with compass (orientation, weakest wind set, roof, elevation, year built); three old tables (Weather / Odds per book open→now with fair column and edge chips / Game Info); line-history uPlot per book with fair overlay + alert markers; per-game alerts timeline with CLV.

Table view: all GameCard columns, sortable, filters (sport, week, signal, min edge, book, kickoff window, domes); default view under 700 px. Alerts tab: `alerts_feed.json` with filters and CLV. Backtest tab: bucket grid + stadium results + matched games (old bottom table). Status tab: last 20 runs, stage timings, counts vs baseline, degradations, unresolved names.

## 12. Worker routes (`site/worker/index.js`)
- Basic Auth gate (any user + `BOARD_PASSWORD`; admin `BOARD_ADMIN_USERNAME`/`BOARD_ADMIN_PASSWORD`), realm 'football-board'.
- `GET /` → `/index.html` after auth; other paths → `env.ASSETS.fetch`.
- `GET /data/<name>.json` → R2 `board/<name>` (sanitized `[a-zA-Z0-9._-]`, `.json` required), `no-store`.
- `GET /api/history?game_id&market&book` → D1 odds_history (≤2000 rows).
- `GET /api/wx?game_id` → weather_history.
- `GET /api/alerts?sport&season&week` → alerts.
- `GET /api/runs?limit=20` → runs.
- `POST /refresh {sport, scope}` (admin) → `dispatchBoard` unless `boardRunActive`.
- `GET /auth/me` → `{role}`.
- `scheduled()` per §9.1. Bindings: `ODDS` R2 `football-board`, `DB` D1 `football-odds`, `ASSETS`.

## 13. Robustness contracts (tests pin these)
- R2 state fetch: NoSuchKey → empty default with `schema_version`; any other error → `exit 1`.
- State JSON `migrate()` from any older `schema_version`; unknown newer version → fail.
- `allow_nan=False` on every served JSON; probabilities clamped to finite (0,1).
- meta.json pushed last; self-check after publish; content floor 50% unless `--force`.
- Per-row identity checks: every GameLine `game_id` must exist in this run's schedule; kickoff drift >36 h → drop + Degradation.
- Openers never overwritten; D1 `openers` used to rehydrate.
- `git commit` (Phases 1–2) happens before R2 push.
- All PAT curls: `-fsS --max-time 30`, capture status, `::error` on 401/403.
- Every workflow has `if: failure()` Telegram; concurrency group `football-refresh` on pipeline/backtest/calibrate/build-stadiums.

## 14. Secrets
GitHub repo secrets: `CLOUDFLARE_API_TOKEN` (Workers Scripts:Edit, R2:Edit, D1:Edit), `CF_ACCOUNT_ID` (= ba4875f01f2bc46dd48e1e26d2ec9080, exported as `CLOUDFLARE_ACCOUNT_ID`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (+ optional `TELEGRAM_CHAT_ID_NFL`, `TELEGRAM_CHAT_ID_CFB`), `CFBD_API_KEY`, `PROPHETX_API_KEY` or `PROPHETX_ACCESS_KEY`+`PROPHETX_SECRET_KEY`, optional `ODDS_API_KEY`, optional `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` (boto3 path), optional `NOVIG_CLIENT_ID`/`NOVIG_CLIENT_SECRET` (NBX fallback). Derived env: `PROPHETX_ENABLED`, `BOOK_*_ENABLED`.
Worker secrets (`wrangler secret put`): `BOARD_PASSWORD`, `BOARD_ADMIN_USERNAME`, `BOARD_ADMIN_PASSWORD`, `GH_DISPATCH_TOKEN` (fine-grained PAT, Actions:write on football_weather; expiry tracked), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Local dev: `.env` (python-dotenv), never committed.

## 15. Risks (carried from design + judges)
Model reverse-engineering boundary ambiguities (rain 5.1–6.6 mm, heat-away cutoff 62–67, alt 900/1000, alt-vs-heat override); anti-bot from Actions IPs (BetOnline CF, FanDuel Akamai) → Playwright fallback, low cadence, dark-book alerts, Odds API gap-fill; ToS (Kalshi/FanDuel/Novig deprecation) → private site, no republishing; team-name canonicalization across 6+ books for ~135 FBS + FCS → alias tables + rapidfuzz + unresolved alerts; CF free cron budget/10 ms CPU → Workers Paid; weather semantic shifts (mm vs in, curated vs computed vol/orientation) → keep `*_static` columns and validate via backtest before promoting v2; Open-Meteo non-commercial tier, NBM no gusts, HRRR 18 h, NWS 7 d → stitching with source/lead stamps + confidence gate; Edge semantics now market-relative → documented in UI; state integrity → R2 fetch fails job on transient error, D1 second source; stadium build deps (shapely, timezonefinder, Overpass limits) → preseason PR workflow with overrides; pip installs need user approval per CLAUDE.md.
