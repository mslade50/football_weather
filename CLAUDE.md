# CLAUDE.md — football_weather

NFL + college-football weather/odds board: schedule → stadiums → weather →
impact model → odds scrapers → fair/edges → JSON board (Cloudflare Worker + R2 +
D1) + Telegram alerts. Design docs: `docs/AUDIT.md`, `docs/ARCHITECTURE.md`,
`docs/PLAN.md` (phased build; read the phase you are working on first).
Reference implementation for scrapers/board/workflows: `../golf_scraping`.

## Environment
- Native Windows, Git Bash. `python` (3.10) not `python3`. No 3.11-only syntax
  (no `except*`, `StrEnum`, `tomllib`).
- Do NOT `pip install` — flag what is needed and let the user decide. Contracts
  use stdlib dataclasses; fuzzy matching uses `difflib` behind a try-import of
  `rapidfuzz`.
- Never commit generated data. Only `data/stadiums*.csv`, `data/teams.csv`,
  `data/aliases/`, `data/raw/` and `tests/fixtures/` are tracked. The legacy
  `nfl_weather.csv` / `cfb_weather.xlsx` are written to `data/` and uploaded to
  R2 `legacy/`; the frozen copies under `tests/fixtures/legacy/` are column
  fixtures only. `pipeline.yml` has no `git commit` step (pinned by test).
- Secrets only via env / `.env` (python-dotenv) / GitHub + Worker secrets.

## Rules
- **`sport` is threaded through every function that touches games** (`nfl` |
  `cfb`). No module-level league constants.
- **Raw-first**: every external fetch is captured verbatim (`raw_out`) before
  parsing; parsers are pure (`parsers/<book>.parse(payload, sport)`) and are
  tested from scrubbed fixtures under `tests/fixtures/raw/`.
- Canonical keys (ARCH §4.1): `game_id = f"{sport}:{season}:{week}:{away}@{home}"`,
  odds key `game_id|market|side|book`, markets `ml|spread|total`, sides
  `home|away|over|under`.
- v1 impact model (`pipeline/model/impact.py`) is frozen: reproduces the legacy
  numbers exactly and is pinned by `tests/test_impact_v1.py`. Improvements go
  in v2 side by side; alerts stay on v1 until the Phase 6 promotion gate.
- Legacy outputs (`nfl_weather.csv`, `cfb_weather.xlsx`) stay column-exact
  (AUDIT §4, pinned by `tests/test_legacy_columns.py` against
  `tests/fixtures/legacy/`); Streamlit (`app.py`, `pages/`) is gone — the board
  is `site/web/` (Table, NFL/CFB maps, Alerts, Status tabs) served by the Worker.
- State files (`pipeline/state.py`) carry `schema_version`; load through
  `migrate()`; openers are never overwritten; alert markers are recorded only
  after a successful send.
- Served JSON uses `allow_nan=False`; meta.json is pushed last.

## Tests
- `python -m pytest tests -q` from the repo root; `pyproject.toml` sets
  `pythonpath = ["."]`.
- Stub heavy/optional deps at the top of a test with
  `sys.modules.setdefault("playwright", MagicMock())` (see
  `golf_scraping/tests/test_betcris.py`); never require network.
- Workflow YAML changes must update the workflow-contract tests
  (`tests/test_pipeline_workflow.py` etc.) that pin string invariants.
- Style: type hints, `pathlib`, f-strings, explicit imports; `ruff check .`.

## Layout (abridged; full tree in ARCH §3)
```
pipeline/   build.py contracts.py state.py gate_check.py run_context.py alerts.py
            backtest.py backtest_git.py (--from-git: replay the legacy git archive)
            stadium_wx.py (ERA5-keyed venue + wind-band under records)
            schedule/ stadiums/ weather/ model/ odds/ outputs/
utils/      telegram.py state.py timeutil.py
scripts/    recon_book.py recover_static.py extract_golden.py fixtures_scrub.py
            _git_history.py make_backtest_git_fixtures.py
site/       worker/ (wrangler.toml index.js migrations/ SETUP.md test/)
            web/ (index.html app.js table.js map.js drawer.js alerts.js status.js vendor/)
tests/      fixtures/ (raw/ legacy/ git_archive/ golden parquet)  test_*.py   (`node --test site/worker/test` for the Worker)
main.py     on-demand scraper CLI: --book --sport --market --output --headed
```
