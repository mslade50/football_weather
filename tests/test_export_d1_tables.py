"""scripts/export_d1_tables.py: one SELECT * until D1's 50 MB result limit, then pages.

No wrangler and no network. A failed page must not leave a partial odds_history
that the backtest would treat as the opener set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import export_d1_tables as exp


def _ok(rows: list[dict]) -> tuple[int, str, str]:
    return 0, json.dumps([{"results": rows}]), ""


def test_single_select_is_written_unchanged(tmp_path: Path):
    calls = []

    def execute(database: str, sql: str) -> tuple[int, str, str]:
        calls.append((database, sql))
        return _ok([{"game_id": "nfl:2026:10:a@b"}])

    status = exp.export_tables("football-odds", tmp_path, ["games"], execute)
    assert status == {"games": "single"}
    assert calls == [("football-odds", "SELECT * FROM games")]
    assert json.loads((tmp_path / "games.json").read_text(encoding="utf-8"))[0]["results"][0]["game_id"] == "nfl:2026:10:a@b"


def test_odds_history_over_50mb_is_paged_on_the_primary_key(tmp_path: Path):
    calls = []

    def execute(database: str, sql: str) -> tuple[int, str, str]:
        calls.append(sql)
        if sql == "SELECT * FROM odds_history":
            return 1, "", "D1 query result exceeded the maximum response size limit of 50 MB"
        if "LIMIT 2" not in sql:
            raise AssertionError(sql)
        if "WHERE" not in sql:
            return _ok([
                {"game_id": "a", "book": "pin", "market": "total", "side": "over", "scraped_at": "t1", "line": 40},
                {"game_id": "a", "book": "pin", "market": "total", "side": "over", "scraped_at": "t2", "line": 41},
            ])
        return _ok([
            {"game_id": "b", "book": "pin", "market": "total", "side": "over", "scraped_at": "t3", "line": 42},
        ])

    status = exp.export_tables(
        "football-odds", tmp_path, ["odds_history"], execute, page_size=2, min_page_size=2,
    )
    assert status == {"odds_history": "paged"}
    body = json.loads((tmp_path / "odds_history.json").read_text(encoding="utf-8"))
    assert [r["scraped_at"] for r in body[0]["results"]] == ["t1", "t2", "t3"]
    assert "WHERE (game_id, book, market, side, scraped_at) >" in calls[-1]
    assert "'t2'" in calls[-1]


def test_oversized_page_shrinks_and_keeps_reading(tmp_path: Path):
    def execute(database: str, sql: str) -> tuple[int, str, str]:
        if "SELECT * FROM odds_history" == sql or "LIMIT 4" in sql:
            return 1, "D1 query result exceeded the maximum response size limit of 50 MB", ""
        if "WHERE" not in sql:
            return _ok([
                {"game_id": "a", "book": "b", "market": "total", "side": "over", "scraped_at": "1"},
                {"game_id": "a", "book": "b", "market": "total", "side": "over", "scraped_at": "2"},
            ])
        return _ok([
            {"game_id": "a", "book": "b", "market": "total", "side": "under", "scraped_at": "3"},
        ])

    status = exp.export_tables(
        "football-odds", tmp_path, ["odds_history"], execute, page_size=4, min_page_size=2,
    )
    rows = json.loads((tmp_path / "odds_history.json").read_text(encoding="utf-8"))[0]["results"]
    assert status == {"odds_history": "paged"}
    assert [r["scraped_at"] for r in rows] == ["1", "2", "3"]


def test_failed_later_page_leaves_no_partial_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    secret = "super-secret-token"

    def execute(database: str, sql: str) -> tuple[int, str, str]:
        if sql == "SELECT * FROM odds_history":
            return 1, "", "maximum response size limit of 50 MB"
        if "WHERE" not in sql:
            return _ok([
                {"game_id": "a", "book": "b", "market": "total", "side": "over", "scraped_at": "1"},
                {"game_id": "a", "book": "b", "market": "total", "side": "over", "scraped_at": "2"},
            ])
        return 1, "", f"network down Authorization: Bearer {secret}"

    monkey_env = pytest.MonkeyPatch()
    monkey_env.setenv("CLOUDFLARE_API_TOKEN", secret)
    try:
        status = exp.export_tables(
            "football-odds", tmp_path, ["odds_history"], execute, page_size=2, min_page_size=2,
        )
    finally:
        monkey_env.undo()
    assert status == {"odds_history": "failed"}
    assert not (tmp_path / "odds_history.json").exists()
    err = capsys.readouterr().err
    assert "d1 export odds_history failed" in err
    assert secret not in err
    assert "Bearer ***" in err


def test_missing_table_warns_and_other_tables_still_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    def execute(database: str, sql: str) -> tuple[int, str, str]:
        if "odds_history" in sql:
            return 1, "", "no such table: odds_history"
        return _ok([{"alert_key": "k"}])

    status = exp.export_tables("football-odds", tmp_path, ["odds_history", "alerts"], execute)
    assert status == {"odds_history": "failed", "alerts": "single"}
    assert not (tmp_path / "odds_history.json").exists()
    assert json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))[0]["results"] == [{"alert_key": "k"}]
    assert "fresh database?" in capsys.readouterr().err


def test_refuses_unsafe_names(tmp_path: Path):
    def execute(database: str, sql: str) -> tuple[int, str, str]:
        raise AssertionError("wrangler must not be called")

    with pytest.raises(exp.ConfigError):
        exp.export_tables("football-odds; drop", tmp_path, ["games"], execute)
    with pytest.raises(exp.ConfigError):
        exp.export_tables("football-odds", tmp_path, ["odds_history;drop"], execute)


def test_main_uses_env_database_and_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("D1_DATABASE", "football-odds")
    monkeypatch.setenv("D1_TABLES", "teams")

    def execute(database: str, sql: str) -> tuple[int, str, str]:
        assert database == "football-odds"
        assert sql == "SELECT * FROM teams"
        return _ok([{"team_id": "kc"}])

    monkeypatch.setattr(exp, "execute_sql", execute)
    assert exp.main(["--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "teams.json").read_text(encoding="utf-8"))[0]["results"] == [{"team_id": "kc"}]
