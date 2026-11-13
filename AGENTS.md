# AGENTS.md — football_weather

Global rules come from `mslade50/agent-rules`, installed on this machine as `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. This file holds football_weather facts only.

## Deploy and live board
- `.github/workflows/deploy.yml` deploys the Worker. It runs on push to `main` when `site/**`, `.github/workflows/deploy.yml` or `data/raw/stadium_game_climate.json` change, and on `workflow_dispatch`. It runs the Worker tests, applies D1 migrations, then deploys. Doc-only merges do not trigger it.
- After a deploy, check the run is green (`gh run list -R mslade50/football_weather --workflow deploy.yml`) and load the live board (Worker `football-board`; `/api/status`, Status tab) to confirm the change.
- To refresh board data and activate a data or model change, dispatch the `pipeline` workflow (`.github/workflows/pipeline.yml`, `gh workflow run pipeline.yml`), then confirm `board/meta.json` `run_id` matches the new run.

## Review
Delivery is direct push to main per the owner's global rules.
Checks are `python -m pytest tests -q` and `node --test site/worker/test/*.test.mjs`, chosen to fit the change.
