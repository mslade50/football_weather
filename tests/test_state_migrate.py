"""pipeline/state.py: schema_version + migrate(), openers, history, alerts caps."""

import json
from pathlib import Path

import pytest

from pipeline import state


def _line(game_id: str, market: str, side: str, book: str, line, odds: int) -> dict:
    return {"game_id": game_id, "market": market, "side": side, "book": book, "line": line, "odds": odds}


G1 = "nfl:2026:1:kc@lac"
G2 = "nfl:2026:2:sea@ne"


# ── migrate ────────────────────────────────────────────────────────────────────

def test_constants():
    assert state.SCHEMA_VERSION == 1
    assert state.ALERTS_CAP == 500
    assert state.HISTORY_CAP == 120


@pytest.mark.parametrize("kind", ["openers", "archive_last", "baseline", "alerts", "history"])
def test_migrate_empty_gives_fresh_default(kind):
    for empty in (None, {}, [], "junk"):
        d = state.migrate(empty, kind)
        assert d["schema_version"] == state.SCHEMA_VERSION
        for k, v in state._DEFAULTS[kind].items():
            assert k in d
            assert isinstance(d[k], type(v))


def test_migrate_v0_golf_style_openers():
    old = {"event_id": "123", "openers": {f"{G1}|total|over|betcris": {"line": 44.5, "odds": -110, "ts": "t0"}}}
    d = state.migrate(old, "openers")
    assert d["schema_version"] == 1
    assert "event_id" not in d
    assert d["openers"][f"{G1}|total|over|betcris"]["line"] == 44.5


def test_migrate_current_version_is_noop():
    cur = {"schema_version": 1, "sent": {"k": "t"}}
    assert state.migrate(cur, "alerts") == cur


def test_migrate_fills_missing_keys_and_fixes_wrong_types():
    d = state.migrate({"schema_version": 1, "sent": ["not", "a", "dict"]}, "alerts")
    assert d["sent"] == {}
    h = state.migrate({"schema_version": 1}, "history")
    assert h["series"] == {} and h["fair_series"] == {}


def test_migrate_newer_version_fails():
    with pytest.raises(state.StateSchemaError):
        state.migrate({"schema_version": state.SCHEMA_VERSION + 1, "sent": {}}, "alerts")


def test_migrate_unknown_kind():
    with pytest.raises(ValueError):
        state.migrate({}, "nope")


def test_corrupt_file_fails_open(tmp_path: Path):
    (tmp_path / state.OPENERS_FILE).write_text("{not json", encoding="utf-8")
    d = state.load_openers(tmp_path)
    assert d == {"schema_version": 1, "openers": {}}


def test_save_stamps_schema_version(tmp_path: Path):
    state.save_alerts(tmp_path, {"sent": {}})
    raw = json.loads((tmp_path / state.ALERTS_FILE).read_text(encoding="utf-8"))
    assert raw["schema_version"] == state.SCHEMA_VERSION
    assert state.load_alerts(tmp_path)["sent"] == {}


def test_save_rejects_nan(tmp_path: Path):
    with pytest.raises(ValueError):
        state.save_history(tmp_path, {"series": {"k": [["t", float("nan"), -110]]}})


# ── openers ────────────────────────────────────────────────────────────────────

def test_openers_first_seen_never_overwritten(tmp_path: Path):
    op = state.load_openers(tmp_path)
    key = state.odds_key(G1, "total", "over", "betcris")
    assert state.record_openers(op, [_line(G1, "total", "over", "betcris", 44.5, -110)], "t0") == 1
    assert state.record_openers(op, [_line(G1, "total", "over", "betcris", 46.0, -115)], "t1") == 0
    assert state.get_opener(op, key) == {"line": 44.5, "odds": -110, "ts": "t0"}
    state.save_openers(tmp_path, op)
    assert state.get_opener(state.load_openers(tmp_path), key)["line"] == 44.5


def test_openers_skip_missing_odds_and_prune():
    op = state.migrate(None, "openers")
    state.record_openers(op, [_line(G1, "ml", "home", "kalshi", None, None)], "t0")
    assert op["openers"] == {}
    state.record_openers(op, [_line(G1, "ml", "home", "kalshi", None, -150),
                              _line(G2, "ml", "home", "kalshi", None, 120)], "t0")
    assert state.prune_openers(op, [G2]) == 1
    assert list(op["openers"]) == [state.odds_key(G2, "ml", "home", "kalshi")]


# ── history ────────────────────────────────────────────────────────────────────

def test_history_change_only_and_cap():
    h = state.migrate(None, "history")
    ln = _line(G1, "spread", "home", "pinnacle", -3.0, -108)
    assert state.update_history(h, [ln], "t0") == 1
    assert state.update_history(h, [ln], "t1") == 0          # unchanged → no point
    ln2 = dict(ln, odds=-112)
    assert state.update_history(h, [ln2], "t2") == 1         # odds moved
    ln3 = dict(ln2, line=-3.5)
    assert state.update_history(h, [ln3], "t3") == 1         # line moved
    key = state.odds_key(G1, "spread", "home", "pinnacle")
    assert h["series"][key] == [["t0", -3.0, -108], ["t2", -3.0, -112], ["t3", -3.5, -112]]

    for i in range(200):
        state.update_history(h, [dict(ln, odds=-100 - i)], f"u{i}", cap=state.HISTORY_CAP)
    assert len(h["series"][key]) == state.HISTORY_CAP
    assert h["series"][key][-1][2] == -299


def test_fair_history_and_prune():
    h = state.migrate(None, "history")
    k1 = f"{G1}|total|over"
    assert state.update_fair_history(h, {k1: 0.52, f"{G2}|total|over": float("nan")}, "t0") == 1
    assert state.update_fair_history(h, {k1: 0.52}, "t1") == 0
    state.update_history(h, [_line(G2, "ml", "away", "novig", None, 130)], "t0")
    assert state.prune_history(h, [G2]) == 1
    assert k1 not in h["fair_series"]
    assert state.odds_key(G2, "ml", "away", "novig") in h["series"]


# ── alerts ─────────────────────────────────────────────────────────────────────

def test_alert_mark_after_send_and_cap(tmp_path: Path):
    a = state.load_alerts(tmp_path)
    assert not state.alert_sent(a, "EDGE|x")
    state.mark_alert(a, "EDGE|x", "2026-09-01T00:00:00Z")
    assert state.alert_sent(a, "EDGE|x")
    for i in range(state.ALERTS_CAP + 50):
        state.mark_alert(a, f"k{i}", f"2026-09-02T00:{i:05d}")
    assert len(a["sent"]) == state.ALERTS_CAP
    assert not state.alert_sent(a, "EDGE|x")  # oldest pruned
    state.save_alerts(tmp_path, a)
    assert len(state.load_alerts(tmp_path)["sent"]) == state.ALERTS_CAP


# ── baseline / archive_last ────────────────────────────────────────────────────

def test_baseline_resets_on_scope_but_keeps_seen_books(tmp_path: Path):
    b = state.load_baseline(tmp_path, "nfl:2026:1")
    b["peaks"]["betcris|total"] = 16
    b["alerted"].append("betcris|total")
    b["seen_books"]["betcris"] = 48
    state.save_baseline(tmp_path, b)

    same = state.load_baseline(tmp_path, "nfl:2026:1")
    assert same["peaks"] == {"betcris|total": 16}

    nxt = state.load_baseline(tmp_path, "nfl:2026:2")
    assert nxt["scope"] == "nfl:2026:2"
    assert nxt["peaks"] == {} and nxt["alerted"] == []
    assert nxt["seen_books"] == {"betcris": 48}


def test_archive_last_roundtrip_and_prune(tmp_path: Path):
    d = state.load_archive_last(tmp_path)
    d["last"][state.odds_key(G1, "total", "over", "fanduel")] = [44.5, -110]
    d["last"][state.odds_key(G2, "total", "over", "fanduel")] = [41.0, -105]
    state.save_archive_last(tmp_path, d)
    d2 = state.load_archive_last(tmp_path)
    assert state.prune_archive_last(d2, [G1]) == 1
    assert len(d2["last"]) == 1
