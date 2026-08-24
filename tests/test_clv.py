"""pipeline/model/clv.py: closing freeze (last pre-kickoff row per key), clv_pts sign
conventions per side, closings.json first-write-wins, alert settlement, D1 wiring;
pipeline/odds/oddsapi.py opener seeding (never overwrites)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import state as pstate
from pipeline.model import clv
from pipeline.odds import oddsapi
from pipeline.outputs import d1_out

GID = "nfl:2026:3:sea@ne"
GID2 = "cfb:2026:3:ohio-state@michigan"
KICK = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
KICK2 = KICK + timedelta(days=5)   # not kicked off at NOW
NOW = KICK + timedelta(hours=4)


def _row(ts: datetime, line: float, odds: int = -110, gid: str = GID, market: str = "total", side: str = "under", book: str = "betonline") -> dict:
    return {"scraped_at": ts.isoformat().replace("+00:00", "Z"), "game_id": gid, "book": book, "market": market,
            "side": side, "line": line, "odds": odds}


# ---- sign conventions ------------------------------------------------------------------

@pytest.mark.parametrize(
    "market,side,first,close,expected",
    [
        ("total", "under", 45.0, 43.0, 2.0),    # total dropped: under got the better number
        ("total", "under", 45.0, 47.0, -2.0),
        ("total", "over", 45.0, 47.0, 2.0),     # total rose: over got the better number
        ("total", "over", 45.0, 43.0, -2.0),
        ("spread", "home", -3.0, -4.0, 1.0),    # home -3 alerted, closes -4 → +1
        ("spread", "home", -3.0, -2.5, -0.5),
        ("spread", "away", 3.0, 4.0, -1.0),     # side-relative away +3 alerted, closes +4 → -1
        ("spread", "away", 3.0, 2.5, 0.5),
    ],
)
def test_clv_pts_side_relative(market, side, first, close, expected):
    assert clv.clv_pts(market, side, first, close) == pytest.approx(expected)


def test_clv_pts_home_relative_away_flips_sign():
    # home-relative: away alerted when home was -3, closing home -4 → away's number went from +3 to +4 → -1
    assert clv.clv_pts("spread", "away", -3.0, -4.0, home_relative=True) == pytest.approx(-1.0)
    assert clv.clv_pts("spread", "home", -3.0, -4.0, home_relative=True) == pytest.approx(1.0)


def test_clv_pts_none_for_ml_or_missing():
    assert clv.clv_pts("ml", "home", None, None) is None
    assert clv.clv_pts("total", "under", 45.0, None) is None
    assert clv.clv_pts("total", "under", "nan", 44.0) is None


def test_clv_status_legacy_labels():
    assert clv.clv_status(50.5, 49.0) == "Positive"
    assert clv.clv_status(50.5, 50.5) == "Negative"   # ties are Negative in pages/cfb_weather.py
    assert clv.clv_status(48.0, 49.0) == "Negative"
    assert clv.clv_status(None, 49.0) is None


# ---- freeze ---------------------------------------------------------------------------

def test_freeze_picks_last_row_before_kickoff():
    rows = [
        _row(KICK - timedelta(days=3), 46.0),
        _row(KICK - timedelta(hours=5), 45.5, -108),
        _row(KICK - timedelta(minutes=10), 45.0, -112),   # closing
        _row(KICK + timedelta(minutes=5), 44.0),          # after kickoff (live) → ignored
        _row(KICK - timedelta(hours=1), 47.0, gid=GID2),  # not kicked off yet
        _row(KICK - timedelta(hours=1), -3.0, book="pinnacle", market="spread", side="home"),
    ]
    frozen = clv.freeze_from_rows(rows, {GID: KICK, GID2: KICK2}, NOW)
    key = pstate.odds_key(GID, "total", "under", "betonline")
    assert set(frozen) == {key, pstate.odds_key(GID, "spread", "home", "pinnacle")}
    c = frozen[key]
    assert (c.line, c.odds) == (45.0, -112)
    assert c.scraped_at == "2026-09-27T16:50:00Z"
    assert c.kickoff_utc == "2026-09-27T17:00:00Z"
    assert c.frozen_at == "2026-09-27T21:00:00Z"
    assert c.to_row()["book"] == "betonline"


def test_freeze_rows_at_kickoff_excluded_and_unordered_input():
    rows = [_row(KICK, 44.0), _row(KICK - timedelta(hours=2), 45.0), _row(KICK - timedelta(hours=9), 46.0)]
    frozen = clv.freeze_from_rows(rows, {GID: KICK}, NOW)
    assert frozen[pstate.odds_key(GID, "total", "under", "betonline")].line == 45.0


def test_freeze_from_history_series_matches_rows():
    hist = pstate.migrate({}, "history")
    key = pstate.odds_key(GID, "total", "under", "betonline")
    hist["series"][key] = [["2026-09-20T12:00:00Z", 46.0, -110], ["2026-09-27T10:00:00Z", 45.5, -110]]
    hist["series"][pstate.odds_key(GID2, "total", "under", "betonline")] = [["2026-09-27T10:00:00Z", 50.0, -110]]
    hist["series"]["bad|key"] = [["2026-09-27T10:00:00Z", 1.0, -110]]
    frozen = clv.freeze_from_series(hist, {GID: KICK, GID2: KICK2}, NOW)
    assert list(frozen) == [key]
    assert frozen[key].line == 45.5
    # a series point recorded days before kickoff is still the closing (change-only series)
    hist["series"][key] = [["2026-09-20T12:00:00Z", 46.0, -110]]
    assert clv.freeze_from_series(hist, {GID: KICK}, NOW)[key].line == 46.0


# ---- store + settlement -----------------------------------------------------------------

def _alert_state() -> dict:
    alerts = pstate.migrate({}, "alerts")
    k1 = "edge|2026|3|nfl:2026:3:sea@ne|total|under|betonline|v1"
    pstate.upsert_alert_record(alerts, k1, {"family": "edge", "game_id": GID, "sport": "nfl", "season": 2026, "week": 3,
                                            "market": "total", "side": "under", "book": "betonline", "tier": "edge",
                                            "model_version": "v1", "last_line": 46.5, "last_odds": -110, "last_fair": 43.0,
                                            "last_edge": 3.5, "status": "open"}, "2026-09-25T12:00:00Z")
    k2 = "edge|2026|3|nfl:2026:3:sea@ne|spread|away|pinnacle|v2"
    pstate.upsert_alert_record(alerts, k2, {"family": "edge", "game_id": GID, "sport": "nfl", "market": "spread", "side": "away",
                                            "book": "pinnacle", "tier": "strong", "model_version": "v2", "last_line": 3.5,
                                            "last_odds": -105, "status": "open"}, "2026-09-25T12:00:00Z")
    k3 = "edge|2026|3|cfb:2026:3:ohio-state@michigan|total|under|betonline|v1"
    pstate.upsert_alert_record(alerts, k3, {"family": "edge", "game_id": GID2, "sport": "cfb", "market": "total", "side": "under",
                                            "book": "betonline", "tier": "edge", "last_line": 50.0, "status": "open"}, "2026-09-25T12:00:00Z")
    pstate.append_feed(alerts, {"alert_key": k1, "family": "edge", "tier": "edge", "game_id": GID, "text_html": "x",
                                "sent_at": "2026-09-25T12:00:00Z", "clv_pts": None})
    return alerts


def test_record_closings_first_write_wins_and_settle_alerts():
    store = clv.load_closings(Path("does-not-exist"))
    rows = [_row(KICK - timedelta(hours=1), 45.0), _row(KICK - timedelta(hours=1), 3.0, -105, market="spread", side="away", book="pinnacle")]
    frozen = clv.freeze_from_rows(rows, {GID: KICK}, NOW)
    new = clv.record_closings(store, frozen)
    assert len(new) == 2
    # a second freeze with a different number never replaces the frozen closing
    later = clv.freeze_from_rows([_row(KICK - timedelta(minutes=1), 40.0)], {GID: KICK}, NOW + timedelta(hours=3))
    assert clv.record_closings(store, later) == []
    assert clv.get_closing(store, pstate.odds_key(GID, "total", "under", "betonline"))["line"] == 45.0

    alerts = _alert_state()
    touched = clv.settle_alerts(alerts, store, "2026-09-27T21:00:00Z")
    assert {t["alert_key"].split("|")[4] + "/" + t["alert_key"].split("|")[5] for t in touched} == {"total/under", "spread/away"}
    rec = alerts["records"]["edge|2026|3|nfl:2026:3:sea@ne|total|under|betonline|v1"]
    assert rec["status"] == "settled"
    assert rec["closing_line"] == 45.0
    assert rec["clv_pts"] == pytest.approx(1.5)          # under 46.5 → close 45.0
    rec2 = alerts["records"]["edge|2026|3|nfl:2026:3:sea@ne|spread|away|pinnacle|v2"]
    assert rec2["clv_pts"] == pytest.approx(0.5)         # away +3.5 → close +3.0
    assert alerts["records"]["edge|2026|3|cfb:2026:3:ohio-state@michigan|total|under|betonline|v1"]["status"] == "open"
    assert alerts["feed"][0]["clv_pts"] == pytest.approx(1.5)
    # idempotent: already-settled records are not touched again
    assert clv.settle_alerts(alerts, store) == []
    # D1 rows: INSERT OR IGNORE closings with the 0001 column set
    stmts = d1_out.build_statements(closings=clv.closing_rows(store))
    assert len(stmts) == 1 and stmts[0].startswith("INSERT OR IGNORE INTO closings (game_id, book, market, side, line, odds, scraped_at, kickoff_utc, frozen_at)")
    assert "'nfl:2026:3:sea@ne','betonline','total','under',45.0,-110" in stmts[0]
    assert d1_out.alert_rows(touched)[0]["clv_pts"] == pytest.approx(1.5)


def test_prune_closings_keeps_active_games():
    store = {"closings": {pstate.odds_key(GID, "total", "under", "b"): {}, pstate.odds_key(GID2, "total", "under", "b"): {}}}
    assert clv.prune_closings(store, [GID2]) == 1
    assert list(store["closings"]) == [pstate.odds_key(GID2, "total", "under", "b")]


def test_run_clv_stage_round_trips_state_files(tmp_path: Path):
    hist = pstate.migrate({}, "history")
    hist["series"][pstate.odds_key(GID, "total", "under", "betonline")] = [["2026-09-27T10:00:00Z", 45.0, -110]]
    hist["series"][pstate.odds_key(GID, "spread", "away", "pinnacle")] = [["2026-09-27T10:00:00Z", 3.0, -105]]
    pstate.save_history(tmp_path, hist)
    pstate.save_alerts(tmp_path, _alert_state())
    cards = [{"game_id": GID, "kickoff_utc": "2026-09-27T17:00:00Z"}, {"game_id": GID2, "kickoff_utc": KICK2}]

    res = clv.run_clv_stage(tmp_path, cards, NOW, run_id="r1", dry_run=True)
    assert len(res.new) == 2 and len(res.settled) == 2
    assert not (tmp_path / clv.CLOSINGS_FILE).exists()

    res = clv.run_clv_stage(tmp_path, cards, NOW, run_id="r1")
    saved = json.loads((tmp_path / clv.CLOSINGS_FILE).read_text())
    assert saved["schema_version"] == pstate.SCHEMA_VERSION and saved["run_id"] == "r1"
    assert saved["closings"][pstate.odds_key(GID, "total", "under", "betonline")]["line"] == 45.0
    alerts = pstate.load_alerts(tmp_path)
    assert alerts["records"]["edge|2026|3|nfl:2026:3:sea@ne|total|under|betonline|v1"]["clv_pts"] == pytest.approx(1.5)
    assert res.new_rows[0]["frozen_at"] == "2026-09-27T21:00:00Z"
    # second run: nothing new, nothing re-settled, file still readable
    res2 = clv.run_clv_stage(tmp_path, cards, NOW + timedelta(hours=1), run_id="r2")
    assert res2.new == [] and res2.settled == []
    assert clv.load_closings(tmp_path)["run_id"] == "r2"


def test_load_closings_rejects_newer_schema(tmp_path: Path):
    (tmp_path / clv.CLOSINGS_FILE).write_text(json.dumps({"schema_version": 99, "closings": {}}))
    with pytest.raises(pstate.StateSchemaError):
        clv.load_closings(tmp_path)
    (tmp_path / clv.CLOSINGS_FILE).write_text("not json")
    assert clv.load_closings(tmp_path)["closings"] == {}


# ---- oddsapi (optional seeding) -------------------------------------------------------------

ODDSAPI_PAYLOAD = {
    "timestamp": "2026-09-21T12:00:00Z",
    "data": [{
        "id": "abc", "commence_time": "2026-09-27T17:00:00Z", "home_team": "New England Patriots", "away_team": "Seattle Seahawks",
        "bookmakers": [
            {"key": "pinnacle", "last_update": "2026-09-21T11:58:00Z", "markets": [
                {"key": "spreads", "outcomes": [{"name": "New England Patriots", "price": -108, "point": -3.0},
                                                {"name": "Seattle Seahawks", "price": -102, "point": 3.0}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 44.5},
                                               {"name": "Under", "price": -110, "point": 44.5}]},
                {"key": "h2h", "outcomes": [{"name": "New England Patriots", "price": -150}, {"name": "Seattle Seahawks", "price": 130}]},
            ]},
            {"key": "unknownbook", "markets": [{"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 40.0}]}]},
        ],
    }],
}


def test_oddsapi_parse_and_seed_never_overwrites(monkeypatch):
    resolve = {"New England Patriots": "ne", "Seattle Seahawks": "sea"}.get
    rows = oddsapi.parse_historical(ODDSAPI_PAYLOAD, "nfl", resolve)
    keys = {(r["market"], r["side"], r["line"], r["odds"]) for r in rows}
    assert ("spread", "home", -3.0, -108) in keys and ("spread", "away", 3.0, -102) in keys
    assert ("total", "under", 44.5, -110) in keys and ("ml", "away", None, 130) in keys
    assert all(r["book"] == "pinnacle" for r in rows) and len(rows) == 6

    openers = pstate.migrate({}, "openers")
    openers["openers"][pstate.odds_key(GID, "total", "under", "pinnacle")] = {"line": 46.0, "odds": -110, "ts": "x"}
    added = oddsapi.seed_openers(openers, rows, {("ne", "sea"): GID})
    assert added == 5
    assert openers["openers"][pstate.odds_key(GID, "total", "under", "pinnacle")]["line"] == 46.0
    assert openers["openers"][pstate.odds_key(GID, "spread", "away", "pinnacle")] == {"line": 3.0, "odds": -102, "ts": "2026-09-21T11:58:00Z", "source": "oddsapi"}

    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert not oddsapi.enabled()
    assert oddsapi.seed_from_dates("nfl", ["2026-09-21T12:00:00Z"], openers, {}, resolve) == 0
    with pytest.raises(RuntimeError):
        oddsapi.fetch_historical("nfl", "2026-09-21T12:00:00Z")
    calls = []

    def fake_get(url, params):
        calls.append((url, dict(params)))
        return ODDSAPI_PAYLOAD

    n = oddsapi.seed_from_dates("nfl", ["2026-09-21T12:00:00Z"], pstate.migrate({}, "openers"), {("ne", "sea"): GID},
                                resolve, key="k", get=fake_get, sleep=lambda s: None)
    assert n == 6
    assert calls[0][0].endswith("/historical/sports/americanfootball_nfl/odds")
    assert calls[0][1]["oddsFormat"] == "american" and calls[0][1]["apiKey"] == "k"
