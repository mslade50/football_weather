"""pipeline/outputs/json_out.py: allow_nan=False, GameCard keys, date/time labels,
odds/consensus blocks from openers, meta books status, wx history change-only,
write_board ordering (meta last) + snapshots; plus r2 publish/self-check contracts."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pipeline import state as pstate
from pipeline.build import ConsensusLine
from pipeline.contracts import Edge, Game, GameLine, Stadium, Team, WeatherForecast, WeatherPoint
from pipeline.model.impact import compute_impact_v1
from pipeline.model.signals import combined_flags, nfl_signal
from pipeline.outputs import json_out, r2
from pipeline.run_context import RunContext

GID = "nfl:2026:3:sea@ne"
KICK = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)  # Sun 1:00 PM ET
ET = ZoneInfo("America/New_York")


def _game() -> Game:
    return Game(game_id=GID, sport="nfl", season=2026, week=3, kickoff_utc=KICK, kickoff_local=KICK.astimezone(ET),
                tz="America/New_York", home_id="ne", away_id="sea", stadium_id="gillette-stadium", roof_state="outdoors")


def _stadium() -> Stadium:
    return Stadium(stadium_id="gillette-stadium", name="Gillette Stadium", lat=42.09, lon=-71.26, city="Foxborough",
                   state="MA", country="US", elevation_m=89.0, timezone="America/New_York", orientation_deg=158.0,
                   orientation_bucket="NW-SE", roof_type="open", year_built=2002, avg_wind_static=9.5,
                   wind_vol_static="High", wind_impact_static="x N", weakest_wind_effect="E/W",
                   avg_wind_by_month={"sep": 8.1, "oct": 9.9})


def _fc(wind: float = 18.0) -> WeatherForecast:
    return WeatherForecast(game_id=GID, source="hrrr", run_time=KICK - timedelta(hours=30), lead_hours=30.0,
                           temp_fg=41.0, wind_fg=wind, gust_fg=26.0, wind_dir_1h="SE", wind_dir_2h="SSE",
                           wind_dir_fg="SE", wind_dir_deg=135.0, rain_fg_mm=0.8, precip_prob=0.2,
                           wind_vol_fc=6.0, wind_p10=12.0, wind_p50=18.0, wind_p90=24.0, cross_mph=15.0,
                           hourly=[WeatherPoint(t=KICK + timedelta(hours=h), temp=41.0, wind=wind, gust=26.0, dir=135.0,
                                                precip=0.0, pop=0.2) for h in range(-1, 5)])


def _ln(book: str, market: str, side: str, line: float | None, odds: int = -110) -> GameLine:
    return GameLine(sport="nfl", game_id=GID, book=book, market=market, side=side, odds=odds, line=line,
                    scraped_at=KICK - timedelta(hours=29))


def _lines() -> list[GameLine]:
    return [
        _ln("betonline", "spread", "home", -3.0, -108), _ln("betonline", "spread", "away", 3.0, -112),
        _ln("betonline", "total", "over", 38.0, -110), _ln("betonline", "total", "under", 38.0, -110),
        _ln("betonline", "ml", "home", None, -160), _ln("betonline", "ml", "away", None, 140),
        _ln("pinnacle", "total", "over", 38.5, -105), _ln("pinnacle", "total", "under", 38.5, -105),
        _ln("pinnacle", "spread", "away", 3.5, -110),  # away-only spread -> home_line derived
    ]


def _openers() -> dict:
    op = pstate.migrate({}, "openers")
    pstate.record_openers(op, [_ln("betonline", "spread", "home", -2.5, -110), _ln("betonline", "total", "under", 39.0, -110),
                               _ln("betonline", "ml", "home", None, -150),
                               {"game_id": GID, "market": "total", "side": "under", "book": "consensus", "line": 39.0, "odds": -110},
                               {"game_id": GID, "market": "spread", "side": "home", "book": "consensus", "line": -2.5, "odds": -110}],
                          "2026-09-22T12:00:00Z")
    return op


def _consensus() -> dict:
    return {(GID, "spread"): ConsensusLine(-3.0, -110, 3, "pinnacle", "home"),
            (GID, "total"): ConsensusLine(38.0, -110, 3, "betonline", "under")}


def _impact():
    return compute_impact_v1(sport="nfl", month=9, temp_fg=41.0, wind_fg=18.0, rain_fg_mm=0.8, travel_alt_m=0.0,
                             away_temp=60.0, home_temp=55.0, roof_state="outdoors")


def _card(**kw: Any) -> dict[str, Any]:
    impact = _impact()
    sig = nfl_signal(18.0, 41.0, 0.8)
    flags = combined_flags("nfl", 18.0, 41.0, -3.0, 0.0, 55.0, 60.0)
    base = dict(lines=_lines(), openers=_openers(), consensus=_consensus(), travel_alt=0.0, home_temp=55.0,
                away_temp=60.0, roof_state="outdoors", avg_wind=9.5, avg_wind_month=8.1, run_id="r1")
    base.update(kw)
    return json_out.build_card("nfl", _game(), _stadium(), Team("ne", "nfl", "New England Patriots", "NE"),
                               Team("sea", "nfl", "Seattle Seahawks", "SEA"), _fc(), impact, sig, flags, **base)


# ---- sanitize / dump ------------------------------------------------------------------

def test_sanitize_nan_inf_datetime_dataclass():
    out = json_out.sanitize({"a": math.nan, "b": math.inf, "c": KICK, "d": [1.5, -math.inf], "e": {"x": None},
                             "f": Edge(GID, "b", "total", "under", 38.0, -110, 37.0, 0.55, 0.5, 1.0, 0.05, 0.7, "edge")})
    assert out["a"] is None and out["b"] is None and out["d"] == [1.5, None]
    assert out["c"] == "2026-09-27T17:00:00Z"
    assert out["f"]["tier"] == "edge" and out["f"]["game_id"] == GID
    json.dumps(out, allow_nan=False)


def test_dump_json_never_writes_nan(tmp_path: Path):
    p = json_out.dump_json(tmp_path / "x.json", {"v": math.nan, "w": float("inf"), "ok": 1.25})
    text = p.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"v": None, "w": None, "ok": 1.25}


def test_next_backstop_off_the_minute():
    now = datetime(2026, 9, 27, 14, 20, tzinfo=timezone.utc)
    nxt = json_out.next_backstop(now)
    assert (nxt.hour, nxt.minute) == (20, 17) and nxt.date() == now.date()
    late = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
    nxt2 = json_out.next_backstop(late)
    assert (nxt2.hour, nxt2.minute) == (9, 17) and nxt2.date() == (late + timedelta(days=1)).date()
    assert json_out.next_run_eta(now, {"NEXT_RUN_ETA": "2026-09-27T15:00:00Z"}) == "2026-09-27T15:00:00Z"
    assert json_out.next_run_eta(now, {}) == "2026-09-27T20:17:00Z"


# ---- GameCard ----------------------------------------------------------------------------

def test_card_has_every_required_key_and_labels():
    card = _card()
    for k in json_out.REQUIRED_CARD_KEYS:
        assert k in card, k
    assert card["date_label"] == "SUN 09/27"
    assert card["time_label"] == "01:00 PM"
    assert card["kickoff_utc"] == "2026-09-27T17:00:00Z"
    assert card["kickoff_local"].startswith("2026-09-27T13:00:00-04:00")
    assert card["home"] == {"team_id": "ne", "name": "New England Patriots", "short": "NE"}
    assert card["stadium"]["orient_deg"] == 158.0 and card["stadium"]["roof_state"] == "outdoors"
    assert card["stadium"]["avg_wind"] == 9.5 and card["stadium"]["avg_wind_month"] == 8.1
    assert card["weather"]["wind_fg"] == 18.0 and card["weather"]["wind_diff"] == 8.5
    assert len(card["weather"]["hourly"]) == 6 and card["weather"]["hourly"][0]["t"] == "2026-09-27T16:00:00Z"
    assert card["impact"]["v1"]["gs_fg_pct"] < 0 and "wind" in card["impact"]["v1"]["components"]
    assert card["signal"]["label"] and isinstance(card["signal"]["flags"], list)
    assert card["run_id"] == "r1"
    json.dumps(card, allow_nan=False)


def test_card_odds_block_uses_openers_and_derives_home_line():
    card = _card()
    bo = card["odds"]["betonline"]
    assert bo["spread"] == {"home_line": -3.0, "home_odds": -108, "away_odds": -112, "open_line": -2.5, "open_odds": -110,
                            "updated_at": "2026-09-26T12:00:00Z"}
    assert bo["total"]["line"] == 38.0 and bo["total"]["open_line"] == 39.0 and bo["total"]["under"] == -110
    assert bo["ml"] == {"home": -160, "away": 140, "open_home": -150, "open_away": None, "updated_at": "2026-09-26T12:00:00Z"}
    pin = card["odds"]["pinnacle"]
    assert pin["spread"]["home_line"] == -3.5 and pin["spread"]["home_odds"] is None and pin["spread"]["open_line"] is None
    assert "ml" not in pin


def test_card_consensus_block_moves_from_openers():
    c = _card()["consensus"]
    assert c["spread_open"] == -2.5 and c["spread_now"] == -3.0 and c["move_s"] == -0.5
    assert c["total_open"] == 39.0 and c["total_now"] == 38.0 and c["move_t"] == -1.0
    assert c["ref_book"] == "betonline" and c["n_books"] == 3 and c["thin"] is False


def test_card_fair_block_from_gamefair_and_legacy_derived():
    edges = [Edge(GID, "betonline", "total", "under", 38.0, -110, 34.6, 0.61, 0.5, 3.4, 0.11, 0.72, "edge", ref_book="pinnacle", n_books=6),
             Edge(GID, "pinnacle", "total", "under", 38.5, -105, 34.6, 0.62, 0.51, 3.9, 0.11, 0.72, "edge"),
             Edge(GID, "betonline", "spread", "home", -3.0, -108, -2.8, None, None, 0.2, None, 0.72, "none")]
    gf = SimpleNamespace(fair_total=34.6, fair_spread=-2.8, confidence=0.72, weather_driven=True, edges=edges)
    card = _card(fair=gf, legacy_derived={"My_total": 35.5, "Edge": 0.07, "My_spread": -2.9, "Edge_s": -0.1})
    f = card["fair"]
    assert f["fair_total"] == 34.6 and f["my_total"] == 35.5 and f["edge_legacy"] == 0.07
    assert len(f["edges"]) == 3 and f["best_total"]["book"] == "pinnacle" and f["best_total"]["edge_pts"] == 3.9
    assert f["best_spread"]["book"] == "betonline"
    row = json_out.table_row(card)
    assert row["best_total_edge"] == 3.9 and row["best_total_book"] == "pinnacle" and row["game"] == "SEA @ NE"
    assert row["total_now"] == 38.0 and row["gs_fg_pct"] == card["impact"]["v1"]["gs_fg_pct"]


def test_card_without_stadium_or_forecast_is_still_complete():
    impact = compute_impact_v1(sport="cfb", month=11, temp_fg=None, wind_fg=None, rain_fg_mm=None, travel_alt_m=None,
                               away_temp=None, home_temp=None, roof_state=None)
    g = Game(game_id="cfb:2026:10:ohio-state@michigan", sport="cfb", season=2026, week=10, kickoff_utc=KICK,
             kickoff_local=KICK, tz="", home_id="michigan", away_id="ohio-state", stadium_id=None)
    card = json_out.build_card("cfb", g, None, None, None, None, impact, nfl_signal(None, None, None), [])
    assert card["stadium"] is None and card["weather"] is None and card["odds"] == {}
    assert card["home"]["name"] == "michigan" and card["tz"] == "America/New_York"
    assert card["consensus"]["thin"] is True and card["fair"]["edges"] == []
    json.dumps(card, allow_nan=False)


# ---- meta / books --------------------------------------------------------------------------

def test_books_status_green_amber_red_and_last_ok_carry():
    counts = {"pinnacle": {"nfl": 60, "nfl.spread": 30, "nfl.total": 30}, "betcris": {"nfl": 10}, "kalshi": {"nfl": 0}}
    baselines = {"nfl": {"peaks": {"pinnacle|spread": 30, "pinnacle|total": 30, "betcris|spread": 30, "betcris|total": 30}}}
    prev = {"kalshi": {"last_ok": "2026-09-20T10:00:00Z"}}
    st = json_out.books_status(counts, ["pinnacle", "betcris", "kalshi", "novig"], baselines, "2026-09-27T10:00:00Z", prev)
    assert st["pinnacle"]["status"] == "green" and st["pinnacle"]["count"] == 60 and st["pinnacle"]["baseline"] == 60
    assert st["betcris"]["status"] == "amber"
    assert st["kalshi"]["status"] == "red" and st["kalshi"]["last_ok"] == "2026-09-20T10:00:00Z"
    assert st["novig"]["status"] == "red" and st["novig"]["last_ok"] is None
    assert st["pinnacle"]["last_ok"] == "2026-09-27T10:00:00Z"


def test_build_meta_and_slim_meta():
    ctx = RunContext(sport="all", scope="light", run_id="r1", git_sha="abc", started_at=KICK)
    ctx.degrade("odds", "kalshi returned 0 lines", "warn")
    ctx.stage_timings["nfl.weather"] = 1.5
    meta = json_out.build_meta(ctx, {"nfl": 14, "cfb": 60}, {"pinnacle": {"count": 60, "status": "green"}},
                               season=2026, week=3, finished_at=KICK + timedelta(minutes=2))
    assert meta["run_id"] == "r1" and meta["duration_s"] == 120.0 and meta["sport_counts"] == {"nfl": 14, "cfb": 60}
    assert meta["degradations"][0]["component"] == "odds" and meta["books"]["pinnacle"]["status"] == "green"
    assert meta["next_run_eta"] == "2026-09-27T20:17:00Z" and meta["last_updated"] == "2026-09-27T17:02:00Z"
    slim = json_out.slim_meta(meta)
    assert set(slim) == {"run_id", "last_updated", "season", "week", "sport_counts", "git_sha", "model_version", "next_run_eta", "degradations"}
    json.dumps(meta, allow_nan=False)


# ---- wx history --------------------------------------------------------------------------

def test_wx_history_is_change_only_and_capped(tmp_path: Path):
    hist = json_out.load_wx_history(tmp_path)
    pt = json_out.wx_point(_fc(), _impact())
    assert pt[0] == 30.0 and pt[1] == 18.0 and pt[-1] == _impact().gs_fg_pct
    assert json_out.update_wx_history(hist, {GID: pt}, "t1") == [GID]
    assert json_out.update_wx_history(hist, {GID: pt}, "t2") == []
    assert json_out.update_wx_history(hist, {GID: json_out.wx_point(_fc(wind=22.0), _impact())}, "t3") == [GID]
    assert [row[0] for row in hist["series"][GID]] == ["t1", "t3"]
    for i in range(json_out.WX_HISTORY_CAP + 5):
        json_out.update_wx_history(hist, {GID: json_out.wx_point(_fc(wind=float(i)), None)}, f"c{i}")
    assert len(hist["series"][GID]) == json_out.WX_HISTORY_CAP
    assert json_out.prune_wx_history(hist, set()) == 1
    p = json_out.save_wx_history(tmp_path, hist)
    assert json.loads(p.read_text())["schema_version"] == pstate.SCHEMA_VERSION


# ---- write_board -------------------------------------------------------------------------

def test_write_board_writes_payloads_meta_last_and_snapshots(tmp_path: Path):
    ctx = RunContext(sport="all", scope="light", run_id="r1", git_sha="abc", started_at=KICK)
    card = _card()
    meta = json_out.build_meta(ctx, {"nfl": 1, "cfb": 0}, {}, season=2026, week=3, finished_at=KICK)
    hist = {"series": {f"{GID}|total|under|betonline": [["t", 38.0, -110]]}, "fair_series": {}}
    files = json_out.write_board(tmp_path / "board", {"nfl": [card], "cfb": []}, meta, history=hist,
                                 wx_history={"series": {}}, snapshots_dir=tmp_path / "snapshots")
    keys = list(files)
    assert keys[-1] == "board/meta.json"
    assert "board/games_nfl.json" in keys and "board/games_cfb.json" in keys and "board/board.json" in keys
    assert "board/history.json" in keys and "board/wx_history.json" in keys
    assert "snapshots/nfl/2026/3/r1.json" in keys and not any(k.startswith("snapshots/cfb") for k in keys)
    games = json.loads(files["board/games_nfl.json"].read_text(encoding="utf-8"))
    assert games["meta"]["run_id"] == "r1" and games["games"][0]["game_id"] == GID
    board = json.loads(files["board/board.json"].read_text(encoding="utf-8"))
    assert board["rows"][0]["game"] == "SEA @ NE"
    snap = json.loads(files["snapshots/nfl/2026/3/r1.json"].read_text(encoding="utf-8"))
    assert snap["games"] == games["games"]
    hp = json.loads(files["board/history.json"].read_text(encoding="utf-8"))
    assert hp["series"] == hist["series"] and hp["schema_version"] == pstate.SCHEMA_VERSION
    for p in files.values():
        assert "NaN" not in p.read_text(encoding="utf-8")
    assert r2.order_keys(keys)[-1] == r2.META_KEY


# ---- r2 publisher contracts ----------------------------------------------------------------

class _FakeClient:
    def __init__(self, fail_keys: set[str] | None = None, objects: dict[str, bytes] | None = None):
        self.puts: list[str] = []
        self.fail_keys = fail_keys or set()
        self.objects = objects or {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        if Key in self.fail_keys:
            raise RuntimeError("boom")
        self.puts.append(Key)
        self.objects[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        if Key not in self.objects:
            class NoSuchKey(Exception):
                response = {"Error": {"Code": "NoSuchKey"}}
            raise NoSuchKey(Key)
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}


def test_publish_orders_raw_snapshots_payloads_state_meta_last(tmp_path: Path):
    names = ["board/meta.json", "board/openers.json", "board/games_nfl.json", "snapshots/nfl/2026/3/r1.json",
             "raw/nfl/r1/manifest.json", "board/history.json", "board/board.json"]
    files = {}
    for n in names:
        p = tmp_path / n.replace("/", "_")
        p.write_text("{}")
        files[n] = p
    client = _FakeClient()
    pushed = r2.publish(client, "b", files, sleep=lambda s: None)
    assert pushed[0].startswith("raw/") and pushed[1].startswith("snapshots/")
    assert pushed[-1] == "board/meta.json"
    assert pushed.index("board/openers.json") > pushed.index("board/games_nfl.json")
    assert pushed.index("board/history.json") > pushed.index("board/board.json")  # history is state: after payloads


def test_publish_failure_before_meta_leaves_meta_untouched(tmp_path: Path):
    files = {}
    for n in ("board/games_nfl.json", "board/meta.json"):
        p = tmp_path / n.replace("/", "_")
        p.write_text("{}")
        files[n] = p
    client = _FakeClient(fail_keys={"board/games_nfl.json"})
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        r2.publish(client, "b", files, sleep=lambda s: None)
    assert "board/meta.json" not in client.puts


def test_get_state_skips_nosuchkey_but_raises_otherwise(tmp_path: Path):
    client = _FakeClient(objects={"board/openers.json": b'{"schema_version": 1, "openers": {}}'})
    got = r2.get_state(client, "b", tmp_path, names=("openers", "alerts"))
    assert got["openers"] == tmp_path / "openers.json" and got["alerts"] is None

    class Broken(_FakeClient):
        def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
            raise RuntimeError("503 Service Unavailable")

    with pytest.raises(RuntimeError, match="503"):
        r2.get_state(Broken(), "b", tmp_path, names=("openers",))
    assert r2.is_no_such_key(SimpleNamespace(response={"ResponseMetadata": {"HTTPStatusCode": 404}}, __class__=type("ClientError", (Exception,), {})))


def test_self_check_run_id_and_content_floor(tmp_path: Path):
    meta = {"run_id": "r2", "sport_counts": {"nfl": 4, "cfb": 60}}
    prev = {"run_id": "r1", "sport_counts": {"nfl": 14, "cfb": 60}}
    assert r2.check_meta(meta, "r1") == ["meta.run_id='r2' != expected 'r1'"]
    probs = r2.check_meta(meta, "r2", prev)
    assert len(probs) == 1 and probs[0].startswith("content floor: nfl games 4 < 50%")
    assert r2.check_meta(meta, "r2", prev, force=True) == []
    assert r2.check_meta(meta, "r2", {"sport_counts": {"nfl": 0}}) == []  # nothing before -> no floor
    mf = tmp_path / "meta.json"
    mf.write_text(json.dumps(meta))
    pf = tmp_path / "prev.json"
    pf.write_text(json.dumps(prev))
    assert r2.self_check("r2", meta_file=mf, prev_meta_file=pf) == probs
    assert r2.main(["--self-check", "--run-id", "r2", "--meta-file", str(mf), "--prev-meta", str(pf)]) == 1
    assert r2.main(["--self-check", "--run-id", "r2", "--meta-file", str(mf), "--prev-meta", str(pf), "--force"]) == 0


def test_config_from_env_requires_all_three():
    assert r2.config_from_env({"CF_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k"}) is None
    cfg = r2.config_from_env({"CF_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"})
    assert cfg is not None and cfg.bucket == "football-board" and cfg.endpoint_url == "https://a.r2.cloudflarestorage.com"
    assert r2.configured({}) is False


def test_raw_store_mirror_uploads_captures_and_manifest(tmp_path: Path):
    from pipeline.outputs.raw_out import RawStore

    seen: list[tuple[str, str]] = []
    store = RawStore("nfl", "r1", tmp_path, mirror=lambda k, d, ct: seen.append((k, ct)))
    store.put("nflverse_games", {"a": 1}, "https://x")
    store.finalize()
    assert seen == [("raw/nfl/r1/nflverse_games.json", "application/json"), ("raw/nfl/r1/manifest.json", "application/json")]
    assert set(store.r2_files()) == {"raw/nfl/r1/nflverse_games.json", "raw/nfl/r1/manifest.json"}

    def boom(k: str, d: bytes, ct: str) -> None:
        raise RuntimeError("no r2")

    store2 = RawStore("nfl", "r2", tmp_path, mirror=boom)
    assert store2.put("x", "text") is not None  # mirror failure never fails the capture
    assert store2.mirror_errors == 1
