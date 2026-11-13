# CLAUDE.md — football_weather

Global rules come from `mslade50/agent-rules`, installed on this machine as `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. This file holds football_weather facts only.

NFL + college-football weather/odds board: schedule → stadiums → weather →
impact model → odds scrapers → fair/edges → JSON board (Cloudflare Worker + R2 +
D1) + Telegram alerts. Design docs: `docs/AUDIT.md`, `docs/ARCHITECTURE.md`,
`docs/PLAN.md` (phased build; read the phase you are working on first).
Reference implementation for scrapers/board/workflows: `../golf_scraping`.

## Environment
- Native Windows, Git Bash. `python` (3.10) not `python3`. No 3.11-only syntax
  (no `except*`, `StrEnum`, `tomllib`).
- Contracts use stdlib dataclasses; fuzzy matching uses `difflib` behind a
  try-import of `rapidfuzz`.
- Never commit generated data. Only `data/stadiums*.csv`, `data/teams.csv`,
  `data/aliases/`, `data/raw/` and `tests/fixtures/` are tracked. Board JSON,
  raw captures and snapshots are published to R2; SQL is archived to D1.
  `pipeline.yml` has no `git commit` step (pinned by test).
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
- The board is `site/web/` (Table, NFL/CFB maps, Alerts, Status tabs) served
  by the Worker. Preserve published JSON keys, including `fair.my_total`,
  `fair.my_spread`, `fair.edge_legacy` and `fair.edge_s_legacy`.
- State files (`pipeline/state.py`) carry `schema_version`; load through
  `migrate()`; openers are never overwritten; alert markers are recorded only
  after a successful send.
- Served JSON uses `allow_nan=False`; meta.json is pushed last.

## Review

Delivery is direct push to main per the owner's global rules.
Checks are `python -m pytest tests -q` and `node --test site/worker/test/*.test.mjs`, chosen to fit the change.

## Tests
- `python -m pytest tests -q` from the repo root; `pyproject.toml` sets
  `pythonpath = ["."]`.
- Stub heavy/optional deps at the top of a test with
  `sys.modules.setdefault("playwright", MagicMock())` (see
  `golf_scraping/tests/test_betcris.py`); never require network.
- Workflow YAML changes must update the workflow-contract tests
  (`tests/test_pipeline_workflow.py` etc.) that pin string invariants.
- Lint: `ruff check .`.

## Layout (abridged; full tree in ARCH §3)
```
pipeline/   build.py contracts.py state.py gate_check.py run_context.py alerts.py
            schedule/ stadiums/ weather/ model/ odds/ outputs/
utils/      telegram.py state.py timeutil.py
scripts/    recon_book.py _git_history.py fixtures_scrub.py
site/       worker/ (wrangler.toml index.js migrations/ SETUP.md test/)
            web/ (index.html app.js table.js map.js drawer.js alerts.js status.js vendor/)
tests/      fixtures/ (raw/ legacy/ golden parquet)  test_*.py   (`node --test site/worker/test/*.test.mjs` for the Worker)
main.py     on-demand scraper CLI: --book --sport --market --output --headed
```

## Notification engine

`pipeline/alert_policy.py` is the single policy for first notices, routine reminders
and explicitly confirmed-bet CLEAR notices. `pipeline/alerts.py` retains formatting,
delivery receipts/feed, book-health paging and the postmortem CLI/email fallback.
Telegram delivery defaults off behind `FOOTBALL_TELEGRAM_ENABLED`. Legacy stored
queue keys remain loadable but are never replayed. See `docs/ARCHITECTURE.md` §10.
