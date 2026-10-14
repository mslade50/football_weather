"""Payload-shape fingerprint (raw_out manifest) + shape diff / null-rate checks (payload_health),
wired into build.stage_odds. Fixtures only; Telegram is never touched (conftest disables it and the
checks only ``ctx.degrade`` -> alerts.ops_candidates)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline import payload_health as ph
from pipeline import state as pstate
from pipeline.outputs.raw_out import NullRawStore, RawStore, payload_shape, shape_hash
from pipeline.run_context import RunContext
from tests.test_build_odds import _Book, _game, _ln

PAYLOAD = {"events": [{"id": "1", "markets": [{"outcomes": [{"price": -110, "name": "a"}, {"price": 100.5, "name": "b"}]}]},
                      {"id": "2", "markets": []}], "ok": True, "cursor": None}


# ---- fingerprint -------------------------------------------------------------------------

def test_payload_shape_paths_and_types():
    shape = payload_shape(PAYLOAD)
    assert shape == sorted(shape)
    assert "events[].markets[].outcomes[].price:number" in shape
    assert "events[].id:string" in shape and "ok:boolean" in shape and "cursor:null" in shape
    assert "events:array" in shape and "$:object" in shape


def test_payload_shape_ignores_array_length_and_key_order():
    a = payload_shape({"x": [{"p": 1}], "y": 2})
    b = payload_shape({"y": 5, "x": [{"p": 3}, {"p": 4}, {"p": 9}]})
    assert a == b and shape_hash(a) == shape_hash(b)


def test_payload_shape_parses_json_strings_and_skips_non_json():
    assert payload_shape(json.dumps({"a": 1})) == payload_shape({"a": 1})
    assert payload_shape("<html></html>") is None
    assert payload_shape(b"\x00\x01") is None
    assert payload_shape(42) is None


def test_manifest_change_is_additive(tmp_path: Path):
    store = RawStore("nfl", "r1", base_dir=tmp_path)
    store.put("pinnacle_nfl", PAYLOAD, url="http://x")
    store.put("page_html", "<html>not json</html>", ext="html")
    store.finalize()
    man = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    entry = man["pinnacle_nfl"]
    assert {"sha256", "bytes", "fetched_at", "url", "path"} <= set(entry)   # original keys intact
    assert entry["path"] == "pinnacle_nfl.json" and entry["url"] == "http://x"
    assert entry["shape"] == payload_shape(PAYLOAD) and entry["shape_sha256"] == shape_hash(entry["shape"])
    assert set(man["page_html"]) == {"sha256", "bytes", "fetched_at", "url", "path"}   # non-JSON: unchanged


def test_null_store_records_shape_in_memory_only():
    store = NullRawStore("nfl", "dry")
    store.put("pinnacle_nfl", PAYLOAD)
    assert "shape" in store.manifest["pinnacle_nfl"]


# ---- diff --------------------------------------------------------------------------------

BOOKS = ["pinnacle", "betonline", "fanduel"]


def test_book_for_source():
    assert ph.book_for_source("pinnacle_nfl", BOOKS) == "pinnacle"
    assert ph.book_for_source("cfb_fanduel", BOOKS) == "fanduel"
    assert ph.book_for_source("nfl_schedule", BOOKS) is None


def test_diff_removed_changed_added_and_null_tolerance():
    old = set(payload_shape({"a": {"price": 1, "gone": "x"}, "t": 1, "n": 1}))
    new = set(payload_shape({"a": {"price": "1.5", "fresh": 2}, "t": 2, "n": None}))
    d = ph.diff_shape(new, [old])
    assert d["removed"] == ["a.gone"]
    assert d["changed"] == ["a.price: number -> string", "n: number -> null"]
    assert d["added"] == ["a.fresh"]
    # gaining a type next to a known one / losing null is not a break
    assert ph.diff_shape({"p:number"}, [{"p:number", "p:null"}])["changed"] == []
    assert ph.diff_shape({"p:number", "p:string"}, [{"p:number"}])["changed"] == []


def test_shape_issues_skip_no_history_and_dark_books():
    cur = {"pinnacle": {"a:number"}, "fanduel": {"a:number"}}
    hist = {"pinnacle": [{"a:number", "b:number"}]}
    alerts, _ = ph.shape_issues(cur, hist)
    assert len(alerts) == 1 and alerts[0].startswith("pinnacle:") and "removed b" in alerts[0]
    assert ph.shape_issues(cur, hist, skip_books=["pinnacle"]) == ([], [])
    alerts, infos = ph.shape_issues({"pinnacle": {"a:number", "b:number", "c:number"}}, hist)
    assert alerts == [] and len(infos) == 1 and "c" in infos[0]


def test_load_manifest_history_last_n_per_book_and_corrupt_tolerated(tmp_path: Path):
    for i in range(4):
        s = RawStore("nfl", f"r{i}", base_dir=tmp_path)
        s.put("pinnacle_nfl", {"v": [{"k": i}]})
        if i == 0:
            s.put("fanduel_nfl", {"f": 1})
        s.finalize()
        os.utime(s.manifest_path, (1000 + i, 1000 + i))
    bad = tmp_path / "nfl" / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    os.utime(bad / "manifest.json", (5000, 5000))
    cur = tmp_path / "nfl" / "cur"
    cur.mkdir()
    (cur / "manifest.json").write_text("{}", encoding="utf-8")
    h = ph.load_manifest_history(tmp_path, "nfl", "cur", BOOKS, 2)
    assert len(h["pinnacle"]) == 2 and len(h["fanduel"]) == 1
    assert ph.load_manifest_history(tmp_path / "missing", "nfl", "cur", BOOKS, 5) == {}


# ---- null rates --------------------------------------------------------------------------

def _row(book, market="spread", odds=-110, line=-3.0, side="home"):
    return SimpleNamespace(book=book, market=market, odds=odds, line=line, side=side)


def test_null_rates_and_threshold_and_jump():
    rows = [_row("pinnacle") for _ in range(30)] + [_row("pinnacle", market="ml", line=None) for _ in range(10)]
    rows += [_row("fanduel", odds=None) for _ in range(10)] + [_row("fanduel") for _ in range(30)]
    r = ph.null_rates(rows)
    assert r["pinnacle"]["line"] == 0.0 and r["pinnacle"]["n"] == 40    # ml rows have no line by contract
    assert r["fanduel"]["odds"] == 0.25
    assert ph.null_issues(r, {}) == []                                   # 25% is not > 25%
    r["fanduel"]["odds"] = 0.3
    assert any("fanduel: odds null rate 30%" in m for m in ph.null_issues(r, {}))
    # jump vs history while still below the absolute threshold
    r2 = {"pinnacle": {"n": 50, "odds": 0.0, "line": 0.2, "side": 0.0}}
    hist = {"pinnacle": [{"odds": 0.0, "line": 0.01, "side": 0.0}] * 3}
    assert any("line null rate jumped to 20%" in m for m in ph.null_issues(r2, hist))
    assert ph.null_issues(r2, {}) == []
    # too few rows: not judged
    assert ph.null_issues({"x": {"n": 5, "odds": 1.0, "line": 1.0, "side": 1.0}}, {}) == []


def test_nan_and_blank_count_as_null():
    r = ph.null_rates([_row("b", odds=float("nan")), _row("b", side=" "), _row("b", line=None)])
    assert r["b"]["odds"] == pytest.approx(1 / 3, abs=1e-3) and r["b"]["side"] == pytest.approx(1 / 3, abs=1e-3)


# ---- stage_odds wiring -------------------------------------------------------------------

def _run_stage(tmp_path: Path, monkeypatch, payload, run_id: str, *, n_lines: int = 1):
    prov = "nfl:raw:20260913:buffalo-bills@kansas-city-chiefs"

    async def fake_scrape(sport, bks, raw, rid, degrade):
        raw.put("pinnacle_nfl", payload, "http://x")
        return {"pinnacle": [_ln("pinnacle", "spread", "home", -3.0, game_id=prov)] * n_lines}, {}

    monkeypatch.setattr(build, "scrape_books", fake_scrape)
    monkeypatch.setattr(build, "_import", lambda name: None if name in ("pipeline.odds.merge", "pipeline.model.fair")
                        else __import__(name, fromlist=["_"]))
    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    raw = RawStore("nfl", run_id, base_dir=tmp_path / "raw")
    res = build.stage_odds(ctx, "nfl", [_game()], _Book(), raw, ["pinnacle"], tmp_path / "state", 2026)
    raw.finalize()
    return ctx, res


def _issues(ctx, component):
    return [d for d in ctx.degradations if d.component == component]


def test_stage_odds_alerts_on_renamed_field_and_no_history_is_silent(tmp_path: Path, monkeypatch):
    (tmp_path / "state").mkdir()
    good = {"events": [{"markets": [{"outcomes": [{"price": -110}]}]}]}
    ctx1, _ = _run_stage(tmp_path, monkeypatch, good, "r1")
    assert _issues(ctx1, "odds.shape") == []                  # first run: no history, graceful skip
    ctx2, res = _run_stage(tmp_path, monkeypatch, good, "r2")
    assert _issues(ctx2, "odds.shape") == [] and res.lines    # same shape: silent
    renamed = {"events": [{"markets": [{"outcomes": [{"odds_price": -110}]}]}]}
    ctx3, res3 = _run_stage(tmp_path, monkeypatch, renamed, "r3")
    shape = _issues(ctx3, "odds.shape")
    assert len(shape) == 1 and shape[0].severity == "warn"
    assert "pinnacle" in shape[0].reason and "events[].markets[].outcomes[].price" in shape[0].reason
    assert res3.lines                                         # pipeline continued
    assert os.environ["TELEGRAM_DISABLED"] == "1"             # delivery is the ops path; nothing sent here


def test_stage_odds_added_key_is_log_only(tmp_path: Path, monkeypatch, caplog):
    (tmp_path / "state").mkdir()
    _run_stage(tmp_path, monkeypatch, {"events": [{"id": 1}]}, "r1")
    with caplog.at_level("INFO", logger="pipeline.build"):
        ctx, _ = _run_stage(tmp_path, monkeypatch, {"events": [{"id": 1, "extra": 2}]}, "r2")
    assert _issues(ctx, "odds.shape") == []
    assert "new payload key-path" in caplog.text


def test_shape_history_survives_without_raw_runs_dir_via_baseline(tmp_path: Path, monkeypatch):
    """CI starts with an empty data/raw_runs: history must come from scrape_baseline.json."""
    (tmp_path / "state").mkdir()
    _run_stage(tmp_path, monkeypatch, {"a": {"price": 1}}, "r1")
    shutil.rmtree(tmp_path / "raw")
    ctx, _ = _run_stage(tmp_path, monkeypatch, {"a": {"cost": 1}}, "r2")
    assert len(_issues(ctx, "odds.shape")) == 1
    bl = json.loads((tmp_path / "state" / "scrape_baseline.json").read_text(encoding="utf-8"))
    assert bl["shape_history"]["nfl"]["pinnacle"] and bl["peaks"]       # volume baseline keys untouched


def test_zero_line_book_is_skipped(tmp_path: Path, monkeypatch):
    (tmp_path / "state").mkdir()
    _run_stage(tmp_path, monkeypatch, {"a": {"price": 1}}, "r1")
    ctx, _ = _run_stage(tmp_path, monkeypatch, {}, "r2", n_lines=0)
    assert _issues(ctx, "odds.shape") == []


def test_check_payload_health_null_alert_via_degrade(tmp_path: Path):
    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    lines = [_row("pinnacle", line=None) for _ in range(30)]
    build.check_payload_health(ctx, "nfl", NullRawStore("nfl", "x"), ["pinnacle"], lines, {"pinnacle": 30}, tmp_path, [], 2026)
    nulls = _issues(ctx, "odds.nulls")
    assert len(nulls) == 1 and "line null rate 100%" in nulls[0].reason
    saved = pstate.load_baseline(tmp_path, "nfl:2026:?")
    assert saved["null_rate_history"]["nfl"]["pinnacle"][0]["line"] == 1.0


def test_dry_run_does_not_write_state(tmp_path: Path):
    ctx = RunContext(sport="nfl", scope="full", git_sha="t")
    build.check_payload_health(ctx, "nfl", NullRawStore("nfl", "x"), ["pinnacle"], [], {}, tmp_path, [], 2026, dry_run=True)
    assert not (tmp_path / "scrape_baseline.json").exists()


def test_fail_open_when_checks_raise(tmp_path: Path, monkeypatch, caplog):
    (tmp_path / "state").mkdir()

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ph, "shapes_by_book", boom)
    with caplog.at_level("WARNING", logger="pipeline.build"):
        _, res = _run_stage(tmp_path, monkeypatch, {"a": 1}, "r1")
    assert res.lines and "non-fatal" in caplog.text and "kaboom" in caplog.text


def test_fail_open_on_corrupt_state(tmp_path: Path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "scrape_baseline.json").write_text(
        json.dumps({"schema_version": 1, "shape_history": "garbage", "null_rate_history": [1]}), encoding="utf-8")
    _, res = _run_stage(tmp_path, monkeypatch, {"a": 1}, "r1")
    assert res.lines
