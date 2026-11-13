"""Pin the pre-refactor object sets and ordering, using fake transports only."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.outputs import r2
from scripts import fetch_r2, publish_r2

# Literal historical lists: changing group composition must be a visible decision.
GROUPS = {
    "pipeline": (
        "openers", "history", "wx_history", "archive_last", "wx_last", "alerts",
        "scrape_baseline", "telegram_state", "cf_heartbeat", "closings", "status", "ensemble_cache",
        "nws_points", "bet_confirmations", "book_health",
    ),
    "backtest": ("alerts", "closings", "history", "wx_history", "openers"),
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publish_r2, "s3_config", lambda: None)
    monkeypatch.delenv("STATE_FILES", raising=False)


def test_groups_pin_the_single_pipeline_list_and_backtest_subset():
    assert r2.STATE_GROUPS == GROUPS
    assert r2.STATE_FILES == GROUPS["pipeline"]
    assert r2.state_files(env={}) == GROUPS["pipeline"]
    assert not hasattr(publish_r2, "STATE_FILES")
    assert r2.INPUT_GROUPS == {"calibrate": (
        ("backtest/games.parquet", "backtest/games.parquet"),
        ("board/backtest.json", "board/backtest.json"),
    )}


@pytest.mark.parametrize("group", GROUPS)
def test_state_targets_preserve_key_order_and_meta_destination(group, tmp_path):
    targets = fetch_r2._state_targets(tmp_path, group)
    assert [key for _, key, _ in targets] == [f"board/{name}.json" for name in GROUPS[group]] + ["board/meta.json"]
    assert [path.name for _, _, path in targets] == [f"{name}.json" for name in GROUPS[group]] + ["prev_meta.json"]


def test_explicit_group_ignores_legacy_override_but_default_still_accepts_it(monkeypatch):
    monkeypatch.setenv("STATE_FILES", "openers custom")
    assert r2.state_files() == ("openers", "custom")
    assert r2.state_files("backtest") == GROUPS["backtest"]


def test_pipeline_writes_exact_old_nonprotected_subset_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_r2, "ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    for name in GROUPS["pipeline"]:
        (tmp_path / "state" / f"{name}.json").write_text("{}")
    assert [key for key, _, _ in publish_r2.files_for_phase("state", group="pipeline")] == [
        "board/openers.json", "board/history.json", "board/wx_history.json", "board/archive_last.json",
        "board/wx_last.json", "board/scrape_baseline.json", "board/closings.json", "board/status.json",
        "board/ensemble_cache.json", "board/nws_points.json", "board/book_health.json",
    ]


def test_backtest_fetch_serial_order_and_missing_removal(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "closings.json").write_text("stale")

    def fake(bucket, key, **kwargs):
        calls.append(key)
        return None if key == "board/closings.json" else b"{}"

    monkeypatch.setattr(fetch_r2, "fetch_bytes_wrangler", fake)
    assert fetch_r2.fetch_state("bucket", tmp_path, group="backtest") == 0
    assert calls == [f"board/{name}.json" for name in GROUPS["backtest"]] + ["board/meta.json"]
    assert not (tmp_path / "closings.json").exists()
    assert (tmp_path / "prev_meta.json").read_bytes() == b"{}"


def test_backtest_fetch_stops_and_fails_closed_on_nonmissing_error(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "alerts.json").write_text("old")

    def fake(bucket, key, **kwargs):
        calls.append(key)
        if key == "board/closings.json":
            raise fetch_r2.FetchError("AccessDenied")
        return b"new"

    monkeypatch.setattr(fetch_r2, "fetch_bytes_wrangler", fake)
    assert fetch_r2.fetch_state("bucket", tmp_path, group="backtest") == 1
    assert calls == ["board/alerts.json", "board/closings.json"]
    assert (tmp_path / "alerts.json").read_text() == "old"
    assert list(tmp_path.iterdir()) == [tmp_path / "alerts.json"]


def test_calibrate_reads_same_two_products_in_order_and_removes_missing(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "games.parquet").write_bytes(b"stale")

    def fake(bucket, key, **kwargs):
        calls.append(key)
        return None if key.endswith(".parquet") else b"{}"

    monkeypatch.setattr(fetch_r2, "fetch_bytes_wrangler", fake)
    assert fetch_r2.fetch_inputs("bucket", "calibrate", tmp_path) == 0
    assert calls == ["backtest/games.parquet", "board/backtest.json"]
    assert not (tmp_path / "backtest" / "games.parquet").exists()
    assert (tmp_path / "board" / "backtest.json").read_bytes() == b"{}"


def test_calibrate_nonmissing_failure_is_fatal_and_stops_before_next_product(tmp_path, monkeypatch):
    calls = []

    def fake(bucket, key, **kwargs):
        calls.append(key)
        raise fetch_r2.FetchError("InternalError")

    monkeypatch.setattr(fetch_r2, "fetch_bytes_wrangler", fake)
    assert fetch_r2.fetch_inputs("bucket", "calibrate", tmp_path) == 1
    assert calls == ["backtest/games.parquet"]


def _backtest_files(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_r2, "ROOT", tmp_path)
    (tmp_path / "backtest").mkdir()
    (tmp_path / "board").mkdir()
    for name in ("z.parquet", "a.parquet", "ignored.json"):
        (tmp_path / "backtest" / name).write_bytes(b"data")
    for name in ("backtest.json", "meta.json", "openers.json"):
        (tmp_path / "board" / name).write_text("{}")


def test_backtest_publish_is_serial_parquet_then_report_never_meta_or_state(tmp_path, monkeypatch):
    _backtest_files(tmp_path, monkeypatch)
    calls = []
    keys = publish_r2.publish_phase("backtest", "bucket", upload_fn=lambda *args: calls.append(args))
    assert keys == ["backtest/a.parquet", "backtest/z.parquet", "board/backtest.json"]
    assert [args[1] for args in calls] == keys
    assert [args[3] for args in calls] == ["application/octet-stream", "application/octet-stream", "application/json"]


def test_backtest_publish_exhausted_error_stops_before_later_objects(tmp_path, monkeypatch):
    _backtest_files(tmp_path, monkeypatch)
    calls = []

    def failing(bucket, key, path, content_type):
        calls.append(key)
        raise RuntimeError("exhausted")

    with pytest.raises(RuntimeError, match="exhausted"):
        publish_r2.publish_phase("backtest", "bucket", upload_fn=failing)
    assert calls == ["backtest/a.parquet"]


def test_backtest_required_report_is_not_silently_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_r2, "ROOT", tmp_path)
    assert publish_r2.files_for_phase("backtest") == [
        ("board/backtest.json", tmp_path / "board" / "backtest.json", "application/json"),
    ]


def test_backtest_uses_original_three_attempts_and_five_second_sleeps(tmp_path):
    calls, sleeps = [], []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stderr="failed", stdout="")

    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        publish_r2.upload_one("bucket", "backtest/games.parquet", Path(tmp_path / "games.parquet"),
                              "application/octet-stream", run=run, sleep=sleeps.append, retry_delay_seconds=5)
    assert len(calls) == 3 and sleeps == [5, 5, 5]
