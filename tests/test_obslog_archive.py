"""Local SQL replay proof, including a failed change followed by another move."""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

from pipeline import state as pstate
from pipeline.obslog import archive
from pipeline.outputs import d1_out
from tests.test_obslog import T, game, line, text


def sql(rows, run):
    return "\n".join(d1_out.build_statements(odds=d1_out.odds_rows(rows, text(T), run))) + "\n"


def test_failed_delta_and_future_move_replay_without_new_object(tmp_path):
    g = game()
    state = tmp_path / "state"
    state.mkdir()
    output = tmp_path / "d1.sql"
    b, c = line(g, value=46), line(g, value=47, now=T + timedelta(minutes=20))
    pstate.save_archive_last(state, {"last": {b.key: {"line": b.line, "odds": b.odds}}})
    output.write_text(sql([b], "failed"))
    archive.prepare(state, output)
    saved = (state / "archive_last.json").read_bytes()  # existing R2 upload on failure
    pending_b = json.loads(saved)[archive.PENDING]
    # Next build advances the ordinary marker to C, preserving unrecognized state fields.
    (state / "archive_last.json").write_bytes(saved)
    next_state = pstate.load_archive_last(state)
    next_state["last"][c.key] = {"line": c.line, "odds": c.odds}
    pstate.save_archive_last(state, next_state)
    output.write_text(sql([c], "next"))
    archive.prepare(state, output)
    combined = output.read_text()
    assert combined.startswith(pending_b)
    assert combined.index("'failed'") < combined.index("'next'")
    assert json.loads((state / "archive_last.json").read_text())[archive.PENDING] == combined
    # A partial prior application is safe: existing insert-ignore keys deduplicate retries.
    columns = [f"{c} TEXT" for c in d1_out.ODDS_COLS]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE odds_history (" + ",".join(columns)
                   + ",PRIMARY KEY(scraped_at,game_id,book,market,side))")
        db.executescript(pending_b)
        db.executescript(combined)
        assert db.execute("SELECT line FROM odds_history ORDER BY scraped_at").fetchall() == [("46",), ("47",)]
    archive.acknowledge(state)
    result = json.loads((state / "archive_last.json").read_text())
    assert archive.PENDING not in result
    assert result["last"][c.key]["line"] == 47
    assert sorted(p.name for p in state.iterdir()) == ["archive_last.json"]


def test_pending_sql_is_replayed_even_when_no_new_sql(tmp_path):
    pstate.save_archive_last(tmp_path, {"last": {}, archive.PENDING: "SELECT 1;\n"})
    output = tmp_path / "data" / "d1.sql"
    archive.prepare(tmp_path, output)
    assert output.read_text().strip() == "SELECT 1;"


def test_success_preserves_original_state_bytes_and_no_sql_is_noop(tmp_path):
    pstate.save_archive_last(tmp_path, {"last": {"key": {"line": 45}}})
    path = tmp_path / "archive_last.json"
    original = path.read_bytes()
    output = tmp_path / "d1.sql"
    archive.prepare(tmp_path, output)
    assert not output.exists() and path.read_bytes() == original
    output.write_text("SELECT 1;\n")
    archive.main(["prepare", "--state-dir", str(tmp_path), "--sql", str(output)])
    archive.main(["acknowledge", "--state-dir", str(tmp_path)])
    assert path.read_bytes() == original


def test_invalid_pending_sql_fails_closed(tmp_path):
    import pytest
    pstate.save_archive_last(tmp_path, {"last": {}, archive.PENDING: ["not SQL text"]})
    with pytest.raises(ValueError, match="refusing to discard"):
        archive.prepare(tmp_path, tmp_path / "d1.sql")
