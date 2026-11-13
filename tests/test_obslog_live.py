"""Offline live-shadow contracts: cache recovery, retention, coverage and parity."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import WeatherForecast
from pipeline.model import clv
from pipeline.obslog import append_forecasts, append_games, append_odds, connect, maintain, parity, shadow
from pipeline.obslog import queries as q
from pipeline.odds.weekly_openers import record_weekly_totals
from pipeline.outputs import d1_out, json_out
from pipeline.scrape_outcomes import ScrapeOutcome, ScrapeResult
from tests.test_obslog import HOUR, T, advance_archive, game, key, line, text


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def fixture(tmp_path, *, kick=None):
    g = game(kickoff=kick)
    state = tmp_path / "state"
    for name, data in {
        "openers": pstate.migrate(None, "openers"), "archive_last": {"last": {}},
        "history": {"series": {}}, "closings": {"closings": {}},
        "wx_history": {"series": {}}, "wx_last": {"last": {}},
    }.items():
        write(state / f"{name}.json", data)
    card = {"game_id": g.game_id, "kickoff_utc": text(g.kickoff_utc)}
    write(tmp_path / "board" / "games_nfl.json", {"games": [card]})
    return g, state, tmp_path / "log.sqlite", card


def append(path, state, g, now=T, *, value=45, history_at=None, weather=True):
    rows = [line(g, side=s, value=value, now=now) for s in ("under", "over")]
    with closing(connect(path)) as conn:
        append_odds(conn, rows, games=[g], run_id=text(now), observed_at=now,
                    attempted={("betonline", g.sport)}, successful={("betonline", g.sport)})
        op = pstate.load_openers(state)
        pstate.record_openers(op, rows, text(now))
        pstate.save_openers(state, op)
        hist = pstate.load_history(state)
        pstate.update_history(hist, rows, text(history_at or now))
        pstate.save_history(state, hist)
        archive = pstate.load_archive_last(state)
        advance_archive(archive, rows, ["betonline"], g.sport, now)
        pstate.save_archive_last(state, archive)
        if weather:
            fc = WeatherForecast(g.game_id, "merged", lead_hours=24, wind_fg=12, gust_fg=18,
                                 temp_fg=60, rain_fg_mm=0, precip_prob=10)
            append_forecasts(conn, [fc], games=[g], run_id=text(now), fetched_at=now)
            wx_hist = json_out.load_wx_history(state)
            json_out.update_wx_history(wx_hist, {g.game_id: json_out.wx_point(fc, None)}, text(history_at or now))
            json_out.save_wx_history(state, wx_hist)
            last = d1_out.load_wx_last(state)
            d1_out.weather_deltas([d1_out.weather_row(fc, None, text(now), text(now))], last)
            d1_out.save_wx_last(state, last)
    return rows


def compare(path, state, card, as_of=T):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return parity.compare(conn, state, [card], as_of)


def test_build_style_append_to_cli_report_is_read_only_and_scoped(tmp_path, monkeypatch):
    g, state, path, card = fixture(tmp_path)
    append(path, state, g)
    append(path, state, g, T + timedelta(minutes=20), value=46)
    # Off-board facts must not inflate counts or create findings.
    with closing(connect(path)) as conn:
        other = replace(g, game_id="nfl:2026:4:other")
        append_odds(conn, [line(other)], games=[other], run_id="other", observed_at=T)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    db_before = path.read_bytes()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "parity.json"
    assert parity.main(["--log", str(path), "--state-dir", str(state), "--out", str(out),
                        "--as-of", text(T + timedelta(minutes=30))]) == 0
    report = json.loads(out.read_text())
    assert report["status"] == "ok" and report["findings"] == 0
    assert report["counts"]["baseline"]["match"] == 2
    assert report["counts"]["latest"]["match"] == 2
    assert report["counts"]["history"]["match"] == 2
    assert report["counts"]["wx_history"]["match"] == 1
    assert report["counts"]["wx_last"]["match"] == 1
    assert "| baseline | 2 | 0 |" in summary.read_text()
    assert before == {p.name: p.read_bytes() for p in state.iterdir()}
    assert db_before == path.read_bytes()


def test_parity_matches_closings_and_strict_one_hour_ceiling(tmp_path):
    g, state, path, card = fixture(tmp_path, kick=T + 2 * HOUR)
    append(path, state, g)
    frozen = clv.freeze_from_series(pstate.load_history(state), {g.game_id: g.kickoff_utc}, T + 2 * HOUR)
    write(state / "closings.json", {"closings": {k: v.to_row() for k, v in frozen.items()}})
    assert compare(path, state, card, T + HOUR)["counts"]["latest"]["match"] == 2
    result = compare(path, state, card, T + 2 * HOUR)
    assert result["findings"] == 0
    assert result["counts"]["closings"]["match"] == 2
    assert result["counts"]["latest"]["match"] == 2  # both expired to None


def test_parity_late_start_in_every_timestamped_store(tmp_path):
    g, state, path, card = fixture(tmp_path, kick=T + 2 * HOUR)
    rows = append(path, state, g)
    for name, bucket in (("openers", "openers"), ("archive_last", "last")):
        data = parity.load(state / f"{name}.json")
        for row in data[bucket].values():
            row["ts"] = text(T - HOUR)
        write(state / f"{name}.json", data)
    for name in ("history", "wx_history"):
        data = parity.load(state / f"{name}.json")
        for seq in data["series"].values():
            seq.insert(0, [text(T - HOUR), *seq[0][1:]])
        write(state / f"{name}.json", data)
    write(state / "closings.json", {"closings": {ln.key: {"line": ln.line, "odds": ln.odds,
                                                               "scraped_at": text(T - HOUR)} for ln in rows}})
    report = compare(path, state, card, T + 2 * HOUR)
    for area in ("baseline", "history", "wx_history", "closings"):
        assert report["counts"][area]["log_started_late"] > 0, area
    # At a usable current clock, even a same-value quote with an earlier clock is unjudgeable.
    assert compare(path, state, card, T)["counts"]["latest"]["log_started_late"] == 2
    assert report["findings"] == 0


def test_empty_log_cannot_judge_preexisting_stores(tmp_path):
    g, state, path, card = fixture(tmp_path)
    append(path, state, g)
    with closing(connect(path)) as conn:
        for table in ("quote_observations", "forecast_observations", "game_observations", "runs"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    report = compare(path, state, card)
    for area in ("baseline", "latest", "history", "wx_history", "wx_last"):
        assert report["counts"][area]["log_started_late"] > 0
    assert report["findings"] == 0


def test_mismatches_include_both_values_and_observation_evidence(tmp_path):
    g, state, path, card = fixture(tmp_path)
    rows = append(path, state, g)
    op = pstate.load_openers(state)
    op["openers"][rows[0].key]["line"] = 100
    pstate.save_openers(state, op)
    report = compare(path, state, card)
    assert report["findings"] == 1
    sample = report["sample_mismatches"][0]
    assert sample["area"] == "baseline"
    assert sample["store"][0] == 100 and sample["log"][0] == 45
    assert sample["observations"][0]["kind"] == "quote"


def test_classifier_requires_a_named_known_difference_and_prioritizes_late_evidence():
    assert parity.classify(1, 1) == "match"
    assert parity.classify(1, 2) == "mismatch"
    assert parity.classify(1, 1, late=True) == "log_started_late"
    assert parity.classify(1, 2, known="unreviewed_difference") == "mismatch"
    for name in parity.CLASSIFICATIONS:
        if name.startswith("known_"):
            assert parity.classify(1, 2, known=name) == name
            assert parity.classify(1, 2, late=True, known=name) == "log_started_late"


def test_cfb_t6_baseline_and_attestation_match_without_false_late_start(tmp_path):
    g, state, path, card = fixture(tmp_path)
    g = game("cfb", T + timedelta(days=5))
    card = {"game_id": g.game_id, "kickoff_utc": text(g.kickoff_utc)}
    rows = [line(g, side=s, opening_line=42, opening_odds=-105, opening_source="provider", opened_at=T - HOUR)
            for s in ("under", "over")]
    op = pstate.load_openers(state)
    pstate.record_openers(op, rows, text(T))
    build.retarget_cfb_total_openers(op, {"series": {}}, rows, [g], T)
    pstate.save_openers(state, op)
    with closing(connect(path)) as conn:
        append_odds(conn, rows, games=[g], run_id="cfb", observed_at=T)
    assert compare(path, state, card)["counts"]["baseline"]["match"] == 2
    # NFL uses the actual attestation even though the provider opener predates the log.
    g = game()
    card = {"game_id": g.game_id, "kickoff_utc": text(g.kickoff_utc)}
    rows = [replace(r, game_id=g.game_id, sport=g.sport) for r in rows]
    pstate.record_openers(op, rows, text(T))
    pstate.save_openers(state, op)
    with closing(connect(path)) as conn:
        append_odds(conn, rows, games=[g], run_id="nfl", observed_at=T)
    assert compare(path, state, card)["counts"]["baseline"]["match"] == 2


def test_i1_failed_scrape_is_proved_by_coverage_receipt(tmp_path):
    g, state, path, card = fixture(tmp_path)
    append(path, state, g)
    archive = pstate.load_archive_last(state)
    advance_archive(archive, [], ["betonline"], g.sport, T + timedelta(minutes=10))
    pstate.save_archive_last(state, archive)
    # Without a failed receipt this is a real mismatch, never an assumed accident.
    assert compare(path, state, card)["counts"]["latest"]["mismatch"] == 2
    with closing(connect(path)) as conn:
        append_odds(conn, [], games=[g], run_id="failed", observed_at=T + timedelta(minutes=10),
                    attempted={("betonline", g.sport)})
    report = compare(path, state, card, T + timedelta(minutes=20))
    assert report["counts"]["latest"]["known_i1_failed_scrape"] == 2
    assert report["findings"] == 0


def test_i2_history_and_forecast_clock_difference(tmp_path):
    g, state, path, card = fixture(tmp_path, kick=T + 2 * HOUR)
    append(path, state, g, history_at=T + timedelta(minutes=10))
    frozen = clv.freeze_from_series(pstate.load_history(state), {g.game_id: g.kickoff_utc}, T + 2 * HOUR)
    write(state / "closings.json", {"closings": {k: v.to_row() for k, v in frozen.items()}})
    report = compare(path, state, card, T + 2 * HOUR)
    for area in ("history", "wx_history", "closings"):
        assert report["counts"][area]["known_i2_history_clock"] > 0
    assert report["findings"] == 0


def test_i3_capped_history_lost_closing(tmp_path):
    g, state, path, card = fixture(tmp_path, kick=T + timedelta(minutes=1))
    for n in range(121):
        append(path, state, g, T + timedelta(minutes=n * 2), value=45 + n, weather=False)
    report = compare(path, state, card, T + 5 * HOUR)
    assert report["counts"]["closings"]["known_i3_history_cap"] == 2
    assert report["findings"] == 0


def test_i4_weekly_history_backfill_loses_unchanged_receipt(tmp_path):
    g, state, path, card = fixture(tmp_path)
    append(path, state, g, T - 7 * HOUR)
    append(path, state, g)
    op = pstate.load_openers(state)
    record_weekly_totals(op, pstate.load_history(state), [], [g], T + HOUR)
    pstate.save_openers(state, op)
    report = compare(path, state, card, T + HOUR)
    assert report["counts"]["weekly_totals"]["known_i4_weekly_backfill"] == 1
    assert report["findings"] == 0


@pytest.mark.parametrize("outcome", list(ScrapeOutcome))
def test_shadow_coverage_uses_only_typed_results(tmp_path, monkeypatch, outcome):
    g, path = game(), tmp_path / "log.sqlite"
    with closing(connect(path)) as conn:
        append_odds(conn, [line(g)], games=[g], run_id="seed", observed_at=T)
    class Clock:
        @staticmethod
        def now(tz):
            return T + timedelta(minutes=10)
    monkeypatch.setattr(shadow, "datetime", Clock)
    # Human diagnostic wording cannot veto OK/EMPTY or promote any failure.
    ctx = SimpleNamespace(run_id="next", now_utc=T, degradations=[SimpleNamespace(component="odds.merge", severity="warn")],
                          degrade=lambda *args: pytest.fail("unexpected shadow failure"))
    result = ScrapeResult("betonline", g.sport, outcome, 0)
    shadow.odds(path, ctx, g.sport, [g], [], ["betonline"], {"betonline": result})
    with closing(connect(path)) as conn:
        observations = q.series(conn, key(line(g)), as_of=T + HOUR)
        assert (observations[-1].kind == "withdrawn") == (outcome in (ScrapeOutcome.OK, ScrapeOutcome.EMPTY))
        receipt = conn.execute("SELECT * FROM runs WHERE run_id='next'").fetchone()
        assert bool(json.loads(receipt["attempted"])) == (outcome not in (ScrapeOutcome.SKIPPED, ScrapeOutcome.NOT_REQUESTED))


@pytest.mark.parametrize("changes", [{"coverage_ok": False}, {"attempted": False}, {"sport": "cfb"}])
def test_incomplete_or_other_sport_coverage_cannot_withdraw(tmp_path, changes):
    g, path = game(), tmp_path / "log.sqlite"
    with closing(connect(path)) as conn:
        append_odds(conn, [line(g)], games=[g], run_id="seed", observed_at=T)
    ctx = SimpleNamespace(run_id="next", now_utc=T, degrade=lambda *args: pytest.fail("unexpected shadow failure"))
    result = replace(ScrapeResult("betonline", "nfl", ScrapeOutcome.EMPTY, 0), **changes)
    shadow.odds(path, ctx, "nfl", [g], [], ["betonline"], {"betonline": result})
    with closing(connect(path)) as conn:
        assert q.latest(conn, as_of=datetime.now(timezone.utc))[key(line(g))].kind == "quote"


def test_cache_cold_corrupt_recovery_and_old_format_quarantine(tmp_path):
    path = tmp_path / "nested" / "cache.sqlite"
    warnings = []
    with closing(maintain.recover_connect(path, warnings.append, check=True)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        append_games(conn, [game()], run_id="kept", observed_at=T)
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM game_observations").fetchone()[0] == 1
    assert not warnings
    # A v1/v2 (one row per run) file is never read as v3: quarantined, empty log started.
    for old in (1, 2):
        with closing(sqlite3.connect(path)) as raw:
            raw.execute("UPDATE schema_version SET version=?", (old,))
            raw.commit()
        with pytest.raises(ValueError, match="schema"):
            connect(path)
        with closing(maintain.recover_connect(path, warnings.append, check=True)) as conn:
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
            assert conn.execute("SELECT count(*) FROM game_observations").fetchone()[0] == 0
            append_games(conn, [game()], run_id="again", observed_at=T)
        assert "old-format cache" in warnings[-1]
    path.write_bytes(b"not sqlite")
    with closing(maintain.recover_connect(path, warnings.append, check=True)) as conn:
        assert conn.execute("SELECT count(*) FROM game_observations").fetchone()[0] == 0
    assert "corrupt cache" in warnings[-1]
    assert any(p.read_bytes() == b"not sqlite" for p in path.parent.glob("*.corrupt-*.sqlite"))


def test_prepare_corruption_is_run_log_only_never_a_degradation(tmp_path, capsys):
    path = tmp_path / "log.sqlite"
    path.write_bytes(b"corrupt")
    assert maintain.main(["--path", str(path), "--prepare"]) == 0
    events = []
    ctx = SimpleNamespace(run_id="after-recovery", now_utc=T,
                          degrade=lambda *args: events.append(args))
    shadow.weather(path, ctx, [game()], {})
    assert events == []
    assert "Observation log shadow: corrupt or unsupported cache moved aside" in capsys.readouterr().out
    assert not path.with_suffix(".recovered").exists()


def test_retention_both_age_and_latest_kickoff_and_orphan_runs(tmp_path):
    path = tmp_path / "log.sqlite"
    old = game(kickoff=T - timedelta(days=10))
    active = replace(old, game_id="active", kickoff_utc=T + HOUR)
    postponed = replace(old, game_id="postponed")
    with closing(connect(path)) as conn:
        for g in (old, active, postponed):
            append_odds(conn, [line(g, now=T - timedelta(days=9))], games=[g], run_id=g.game_id, observed_at=T - timedelta(days=9))
        append_games(conn, [replace(postponed, kickoff_utc=T + HOUR)], run_id="new-schedule", observed_at=T)
        # Recently received correction to an old game is still retained.
        append_odds(conn, [line(old, value=46)], games=[old], run_id="recent", observed_at=T)
        append_forecasts(conn, [WeatherForecast(old.game_id, "merged")], run_id="old-weather", fetched_at=T - timedelta(days=9))
    result = maintain.maintain(path, keep_days=2, as_of=T)
    assert result["quote_observations"] == 1 and result["forecast_observations"] == 1
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM quote_observations").fetchone()[0] == 3
        assert not conn.execute("SELECT * FROM runs WHERE run_id='old-weather'").fetchone()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_size_budget_resets_disposable_cache_and_cli_errors_are_advisory(tmp_path, capsys):
    path = tmp_path / "log.sqlite"
    with closing(connect(path)) as conn:
        append_odds(conn, [line(game())], games=[game()], run_id="seed", observed_at=T)
    result = maintain.maintain(path, as_of=T, max_mib=.001)
    assert result["budget_evicted"]
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM quote_observations").fetchone()[0] == 0
    assert maintain.main(["--path", str(path), "--keep-days", "0"]) == 0
    assert parity.main(["--log", str(path), "--out", str(tmp_path / "error.json"), "--state-dir", str(tmp_path / "missing")]) == 0
    assert json.loads((tmp_path / "error.json").read_text())["status"] == "error"
    assert "::warning::" in capsys.readouterr().out


def test_report_output_failure_and_findings_never_fail(tmp_path, monkeypatch, capsys):
    g, state, path, card = fixture(tmp_path)
    append(path, state, g)
    # An unreadable summary cannot change status or the process exit code.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    assert parity.main(["--log", str(path), "--state-dir", str(state), "--out", str(tmp_path / "out.json")]) == 0
    assert "::warning::" in capsys.readouterr().out
    assert parity.main(["--out"]) == 0  # argparse failures too


def test_query_key_index_filters_before_materialization(tmp_path):
    with closing(connect(tmp_path / "log.sqlite")) as conn:
        g = game()
        append_odds(conn, [line(g), line(g, book="pinnacle")], games=[g], run_id="seed", observed_at=T)
        assert len(q.series(conn, key(line(g)), as_of=T)) == 1
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM quote_observations WHERE game_id=? "
                            "AND book=? AND market=? AND side=? AND observed_at<=?",
                            (g.game_id, "betonline", "total", "under", text(T))).fetchall()
        assert any("quote_asof" in r[3] for r in plan)


def test_shadow_partial_mapping_does_not_withdraw_other_keys(tmp_path, monkeypatch):
    g = game()
    rows = [line(g, side=s) for s in ("under", "over")]
    async def scrape(*args):
        return {"betonline": rows}, {"betonline": SimpleNamespace(fetch_errors={})}
    monkeypatch.setattr(build, "scrape_books", scrape)
    monkeypatch.setattr(build, "_merge_lines", lambda *args: ([rows[0]], []))
    captured = []
    monkeypatch.setattr(shadow, "odds", lambda *args: captured.append(args[-1]))
    monkeypatch.setenv("OBSLOG_SHADOW_PATH", str(tmp_path / "log.sqlite"))
    from pipeline.outputs.raw_out import NullRawStore
    from pipeline.run_context import RunContext
    build.stage_odds(RunContext("nfl", started_at=T, git_sha="test"), "nfl", [g], None,
                     NullRawStore("nfl", "test"), ["betonline"], tmp_path / "state", 2026)
    assert captured[0]["betonline"].outcome == ScrapeOutcome.OK
    assert not captured[0]["betonline"].coverage_ok


def test_unknown_schema_version_is_quarantined_like_corruption(tmp_path):
    path = tmp_path / "log.sqlite"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        raw.execute("INSERT INTO schema_version VALUES (99)")
        raw.commit()
    assert maintain.main(["--path", str(path), "--prepare"]) == 0
    assert len(list(path.parent.glob("*.corrupt-*.sqlite"))) == 1
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchall()[0][0] == 4
