"""Replay d1_out statements against the real D1 migrations with foreign keys ON.

The first CI publish failed with ``FOREIGN KEY constraint failed`` because the
teams batch referenced home stadiums (away sides' venues) that were not in the
stadiums batch. These tests pin the two guards in ``build_statements``:
dedupe per key and ``fk_safe_teams``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.outputs import d1_out

MIGRATIONS = Path(__file__).resolve().parents[1] / "site" / "worker" / "migrations"
NOW = "2026-08-24T00:00:00Z"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys=ON")
    for m in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    return con


def _stadium(sid: str) -> dict:
    row = dict.fromkeys(d1_out.STADIUM_COLS)
    row.update({"stadium_id": sid, "name": sid, "lat": 40.0, "lon": -90.0, "updated_at": NOW})
    return row


def _team(tid: str, sid: str | None) -> dict:
    row = dict.fromkeys(d1_out.TEAM_COLS)
    row.update({"team_id": tid, "sport": "cfb", "name": tid, "home_stadium_id": sid, "updated_at": NOW})
    return row


def _run(con: sqlite3.Connection, stmts: list[str]) -> None:
    for s in stmts:
        con.execute(s)


def test_teams_referencing_missing_stadium_do_not_break_fk() -> None:
    con = _db()
    stmts = d1_out.build_statements(
        stadiums=[_stadium("amon-g-carter-stadium")],
        teams=[_team("tcu", "amon-g-carter-stadium"), _team("north-carolina", "kenan-memorial-stadium")],
    )
    _run(con, stmts)
    rows = dict(con.execute("SELECT team_id, home_stadium_id FROM teams").fetchall())
    assert rows == {"tcu": "amon-g-carter-stadium", "north-carolina": None}


def test_stadium_present_in_batch_keeps_fk() -> None:
    con = _db()
    stmts = d1_out.build_statements(
        stadiums=[_stadium("a"), _stadium("b")],
        teams=[_team("home", "a"), _team("away", "b")],
    )
    _run(con, stmts)
    assert con.execute("SELECT COUNT(*) FROM teams WHERE home_stadium_id IS NOT NULL").fetchone()[0] == 2


def test_duplicate_rows_across_sports_are_deduped_before_upsert() -> None:
    # Two SportResults (nfl + cfb) can both emit the same shared venue / team;
    # a multi-row upsert hitting one row twice is a SQLite error.
    con = _db()
    stmts = d1_out.build_statements(
        stadiums=[_stadium("shared"), _stadium("shared")],
        teams=[_team("t", "shared"), _team("t", "shared")],
    )
    _run(con, stmts)
    assert con.execute("SELECT COUNT(*) FROM stadiums").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 1
    _run(con, stmts)  # idempotent re-run (upsert)


def test_fk_safe_teams_does_not_mutate_input() -> None:
    src = [_team("x", "missing")]
    out = d1_out.fk_safe_teams(src, set())
    assert out[0]["home_stadium_id"] is None
    assert src[0]["home_stadium_id"] == "missing"
