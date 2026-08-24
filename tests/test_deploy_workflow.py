"""Contract tests for .github/workflows/deploy.yml + site/worker/wrangler.toml
(PLAN Phase 3): migrations run before deploy, Worker tests gate the deploy,
free-plan cron budget (2 triggers), bindings the Worker expects."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
WRANGLER = ROOT / "site" / "worker" / "wrangler.toml"
WORKER = ROOT / "site" / "worker" / "index.js"


@pytest.fixture(scope="module")
def text() -> str:
    return DEPLOY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf(text: str) -> dict:
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def toml_text() -> str:
    return WRANGLER.read_text(encoding="utf-8")


def _steps(wf: dict) -> list[dict]:
    return wf["jobs"]["deploy"]["steps"]


def test_parses_and_targets_site_paths(wf: dict):
    on = wf[True]
    assert on["push"]["branches"] == ["main"]
    assert "site/**" in on["push"]["paths"]
    assert "workflow_dispatch" in on


def test_concurrency_queues(wf: dict):
    assert wf["concurrency"]["group"] == "football-deploy"
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_migrations_apply_remote_before_deploy(wf: dict):
    names = [s.get("name", s.get("uses", "")) for s in _steps(wf)]
    i_test = next(i for i, n in enumerate(names) if "unit tests" in n)
    i_mig = next(i for i, n in enumerate(names) if "migrations" in n)
    i_dep = next(i for i, n in enumerate(names) if "Deploy" in n)
    assert i_test < i_mig < i_dep
    mig = _steps(wf)[i_mig]
    assert "d1 migrations apply football-odds --remote" in mig["run"]
    assert mig["working-directory"] == "site/worker"


def test_deploy_uses_wrangler_action(wf: dict):
    dep = next(s for s in _steps(wf) if "wrangler-action" in s.get("uses", ""))
    assert dep["with"]["workingDirectory"] == "site/worker"
    assert dep["with"]["command"] == "deploy"


def test_no_continue_on_error(text: str):
    assert "continue-on-error" not in text


def test_telegram_only_on_failure(wf: dict):
    tg = next(s for s in _steps(wf) if "Telegram" in s.get("name", ""))
    assert tg["if"] == "failure()"


# ---- wrangler.toml ------------------------------------------------------------------

def test_wrangler_bindings(toml_text: str):
    assert 'name = "football-board"' in toml_text
    assert 'main = "index.js"' in toml_text
    assert 'directory = "../web"' in toml_text
    assert 'run_worker_first = ["/*"]' in toml_text
    assert 'html_handling = "none"' in toml_text
    assert 'binding = "ODDS"' in toml_text and 'bucket_name = "football-board"' in toml_text
    assert 'binding = "DB"' in toml_text and 'database_name = "football-odds"' in toml_text
    assert 'migrations_dir = "migrations"' in toml_text


def test_wrangler_free_plan_two_crons(toml_text: str):
    block = toml_text.split("crons = [", 1)[1].split("]", 1)[0]
    crons = re.findall(r'"([^"]+)"', block)
    assert crons == ["*/30 * * * *", "15 17 * * *"]
    for c in crons:
        dow = c.split()[4]
        assert "0" not in dow.replace("*", ""), f"Quartz DOW is 1-7: {c}"


def test_worker_handles_every_cron(toml_text: str):
    block = toml_text.split("crons = [", 1)[1].split("]", 1)[0]
    worker = WORKER.read_text(encoding="utf-8")
    for c in re.findall(r'"([^"]+)"', block):
        assert c in worker, f"{c} missing from CRON_PLAN"


def test_migrations_exist_in_order():
    mig_dir = ROOT / "site" / "worker" / "migrations"
    names = sorted(p.name for p in mig_dir.glob("*.sql"))
    assert names[:3] == ["0001_init.sql", "0002_alerts.sql", "0003_runs.sql"]
    sql = "\n".join(p.read_text(encoding="utf-8") for p in mig_dir.glob("*.sql"))
    for table in ("stadiums", "teams", "games", "weather_history", "odds_history", "openers", "alerts", "runs"):
        assert re.search(rf"CREATE TABLE(?: IF NOT EXISTS)? {table}\b", sql), table
