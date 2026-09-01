# football_weather

Weather-driven totals/spreads board for the NFL and college football.

Every run: schedule (nflverse / CFBD) → stadium resolution → hourly forecast at
kickoff (Open-Meteo + NWS) → weather impact model (v1 exact reproduction of the
legacy sheet, v2 side by side) → sportsbook lines (Betcris, FanDuel, Kalshi,
Novig, Pinnacle, ProphetX, BetOnline) → consensus fair lines and edges → JSON
board on a private Cloudflare Worker (R2 + D1) → Telegram edge / line-move alerts.

Status: **Phase 4** (Worker board live, alerts, Alerts + Status tabs; Streamlit
retired). Roadmap in `docs/PLAN.md`; design in `docs/ARCHITECTURE.md`; legacy
audit in `docs/AUDIT.md`; Cloudflare setup in `site/worker/SETUP.md`.

## Architecture

```
GitHub Actions pipeline.yml            Cloudflare
┌──────────────────────────────┐      ┌──────────────────────────────────────┐
│ gate (httpx kickoff horizon) │      │ Worker football-board                 │
│ light: python -m pipeline.   │ R2   │   Basic Auth · /  → site/web assets   │
│   build --scope light        ├─────▶│   /data/*.json → R2 board/            │
│ playwright: BetOnline, merge │ D1   │   /api/history|wx|alerts|runs|status  │
│   into R2 (--merge-into-r2)  ├─────▶│   /refresh (admin) → workflow_dispatch│
└──────────────────────────────┘      │   scheduled(): heartbeat + dispatch   │
         │ Telegram alerts             └──────────────────────────────────────┘
```

* **Pipeline** (`pipeline/build.py`): one process per run; every external fetch
  is captured raw first; state (openers, history, alert markers, baselines)
  round-trips through R2 `board/` and is never committed.
* **Outputs** (`pipeline/outputs/`): `json_out.py` board payloads
  (`meta.json` pushed last, `games_*.json`, `board.json`, `history.json`,
  `wx_history.json`, `alerts_feed.json`, `status.json`), `d1_out.py` change-only
  SQL for D1 (`odds_history`, `weather_history`, `alerts`, `runs`), `legacy.py`
  column-exact `nfl_weather.csv` / `cfb_weather.xlsx` (now uploaded to R2
  `legacy/`, no longer committed), `r2.py` publisher + self-check.
* **Alerts** (`pipeline/alerts.py`): concise PLAY / UPDATE / CLOSED / SYSTEM
  messages. Telegram defaults to actionable Mid+ plays with a posted price and
  at least a 1-point edge; lower tiers stay on the board. Stable game-level keys,
  one update per game/run, no post-kickoff betting alerts, current-price morning
  summaries, and a four-message default cap keep the channel readable. Keys are
  marked only after a successful send.
* **Site** (`site/web/`, vanilla JS, no build step): NFL/CFB maps (MapLibre +
  OpenFreeMap), Table, Alerts, Status tabs; game drawer with weather / odds by
  book / line-history uPlot (fair overlay + alert markers). `site/worker/` is
  the Worker (auth, R2/D1 proxy, cron dispatch) with D1 migrations.

## Local run

```
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
python -m pytest tests -q                            # pipeline tests
node --test site/worker/test                         # Worker tests

# build the board locally (no Telegram, no R2)
python -m pipeline.build --sport all --scope light --print --no-alerts \
  --board-dir site/web/data --snapshot-dir site/web/data/snapshots --d1-sql site/web/data/d1_inserts.sql

# serve the static site against the local data/ (no auth, /api/* unavailable)
cd site/web && python -m http.server 8765           # http://localhost:8765/

# or run the real Worker locally with wrangler (needs the bindings in SETUP.md)
cd site/worker && npx wrangler dev
```

`python main.py --help` is the on-demand single-book scraper CLI. Optional:
`playwright install chromium` for the Playwright books (BetOnline) and
`scripts/recon_book.py`.

## Layout

| Path | Purpose |
|---|---|
| `pipeline/build.py` | orchestrator: `python -m pipeline.build --sport nfl|cfb|all --scope full|light|odds|weather` |
| `pipeline/contracts.py` | frozen dataclasses: Game, Stadium, Team, WeatherForecast, GameLine, Edge, Degradation, RunMeta |
| `pipeline/model/` | `config.py` constants, `impact.py` v1/v2, `signals.py`, `fair.py` |
| `pipeline/odds/` | per-book scrapers (`BaseScraper`) + pure `parsers/` |
| `pipeline/state.py` | R2-round-tripped state: openers, history, alerts dedup, baseline (`schema_version` + `migrate()`) |
| `pipeline/alerts.py` | Telegram alert families, keys, quiet hours, digest |
| `pipeline/outputs/` | legacy csv/xlsx, JSON board, D1 inserts, R2 push + self-check |
| `utils/` | `telegram.py`, `state.py`, `timeutil.py` |
| `scripts/` | `recon_book.py`, `recover_static.py`, `extract_golden.py` |
| `site/worker/` | Cloudflare Worker (`index.js`, `wrangler.toml`, `migrations/`, `SETUP.md`) |
| `site/web/` | static board (`app.js`, `table.js`, `map.js`, `drawer.js`, `alerts.js`, `status.js`, `vendor/`) |
| `tests/fixtures/legacy/` | frozen copies of the old `nfl_weather.csv` / `cfb_weather*.xlsx` (column contract fixtures only) |
| `.github/workflows/` | `pipeline.yml` (build + publish), `deploy.yml` (Worker), `backtest.yml` |

## Secrets

Local `.env` (python-dotenv, never committed) and GitHub Actions secrets:

| Name | Used by |
|---|---|
| `CFBD_API_KEY` | CFB schedule (`pipeline/schedule/cfb.py`, gate) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts + workflow failure pings; optional `TELEGRAM_CHAT_ID_NFL` / `TELEGRAM_CHAT_ID_CFB` routing |
| `TELEGRAM_MIN_TIER`, `TELEGRAM_MIN_EDGE_PTS` | alert gate (defaults: `mid`, `1.0`) |
| `TELEGRAM_MAX_PER_RUN`, `TELEGRAM_INCLUDE_OPENERS` | volume controls (defaults: `4`, `0`) |
| `CLOUDFLARE_API_TOKEN`, `CF_ACCOUNT_ID` | wrangler R2 / D1 / deploy in the workflows |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | optional: `pipeline.outputs.r2 --publish` locally (boto3) |
| `PROPHETX_API_KEY`, `PROPHETX_ACCESS_KEY`, `PROPHETX_SECRET_KEY` | optional ProphetX book |
| `BOOK_<X>_ENABLED=0` | repo variable / env to disable a book (e.g. `BOOK_BETONLINE_ENABLED`) |

Worker secrets (`wrangler secret put`): `BOARD_PASSWORD`, `BOARD_ADMIN_USERNAME`,
`BOARD_ADMIN_PASSWORD`, `GH_DISPATCH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Full list and rationale in `docs/ARCHITECTURE.md` §14; step-by-step Cloudflare
provisioning (bucket, D1 database, migrations, secrets, first deploy) in
`site/worker/SETUP.md`.

## Recon

```
python scripts/recon_book.py --url https://www.betonline.ag/sportsbook/football/nfl --keyword offering --out tests/fixtures/raw/betonline/
```

Logs every API-looking response and saves matching JSON bodies as raw fixtures
(scrub with `scripts/fixtures_scrub.py` before committing).
