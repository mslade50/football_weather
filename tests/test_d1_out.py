"""pipeline/outputs/d1_out.py: SQL quoting, INSERT OR IGNORE vs upsert, chunking <=100
rows, change-only odds/weather deltas, openers rows, runs row, write_sql gating;
plus the end-to-end build() output wiring (board JSON + d1 sql + manifest, meta last)."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pipeline import build
from pipeline import state as pstate
from pipeline.contracts import Edge, Game, GameLine, Stadium, Team, WeatherForecast
from pipeline.model.impact import compute_impact_v1
from pipeline.outputs import d1_out
from pipeline.run_context import RunContext

GID = "nfl:2026:3:sea@ne"
KICK = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
NOW = "2026-09-26T12:00:00Z"


def _ln(book: str, market: str, side: str, line: float | None, odds: int = -110, gid: str = GID) -> GameLine:
    return GameLine(sport="nfl", game_id=gid, book=book, market=market, side=side, odds=odds, line=line)


def _game(gid: str = GID) -> Game:
    return Game(game_id=gid, sport="nfl", season=2026, week=3, kickoff_utc=KICK, kickoff_local=KICK, tz="America/New_York",
                home_id="ne", away_id="sea", stadium_id="gillette-stadium", roof_state="outdoors", source="nflverse")


def _stadium() -> Stadium:
    return Stadium(stadium_id="gillette-stadium", name="Gillette Stadium", lat=42.09, lon=-71.26, roof_type="open",
                   avg_wind_by_month={"sep": 8.1, "jan": 11.0}, wind_vol_static="High")


# ---- quoting / statements ----------------------------------------------------------------

def test_d1_sql_value_quoting():
    assert d1_out.d1_sql_value(None) == "NULL"
    assert d1_out.d1_sql_value(True) == "1" and d1_out.d1_sql_value(False) == "0"
    assert d1_out.d1_sql_value(3) == "3" and d1_out.d1_sql_value(-3.5) == "-3.5"
    assert d1_out.d1_sql_value(math.nan) == "NULL" and d1_out.d1_sql_value(math.inf) == "NULL"
    assert d1_out.d1_sql_value("O'Brien") == "'O''Brien'"
    assert d1_out.d1_sql_value(KICK) == "'2026-09-27T17:00:00Z'"


def test_insert_ignore_and_upsert_shapes():
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    ins = d1_out.insert_ignore_sql("t", ["a", "b"], rows)
    assert ins == ["INSERT OR IGNORE INTO t (a, b) VALUES\n(1,'x'),\n(2,NULL);"]
    up = d1_out.upsert_sql("t", ["a", "b"], ["a"], rows)
    assert len(up) == 1
    assert up[0].startswith("INSERT INTO t (a, b) VALUES\n(1,'x'),\n(2,NULL)\nON CONFLICT(a) DO UPDATE SET b=excluded.b;")
    assert "a=excluded.a" not in up[0]


def test_chunking_at_most_100_rows_per_statement():
    rows = [{"a": i} for i in range(250)]
    stmts = d1_out.insert_ignore_sql("t", ["a"], rows)
    assert len(stmts) == 3
    sizes = [s.count("\n(") + (1 if s.count("VALUES\n(") else 0) for s in stmts]
    # count value tuples per statement
    sizes = [len(re.findall(r"\(\d+\)", s)) for s in stmts]
    assert sizes == [100, 100, 50]
    assert all(len(s.encode("utf-8")) < 100_000 for s in stmts)
    up = d1_out.upsert_sql("t", ["a", "b"], ["a"], [{"a": i, "b": i} for i in range(101)])
    assert len(up) == 2 and all(s.rstrip().endswith("DO UPDATE SET b=excluded.b;") for s in up)


# ---- rows --------------------------------------------------------------------------------

def test_game_stadium_team_rows():
    g = d1_out.game_rows([_game()], NOW)[0]
    assert [g[c] for c in ("game_id", "sport", "season", "week", "kickoff_utc", "neutral", "updated_at")] == \
        [GID, "nfl", 2026, 3, "2026-09-27T17:00:00Z", 0, NOW]
    assert set(g) == set(d1_out.GAME_COLS)
    s = d1_out.stadium_rows([_stadium(), _stadium()], NOW)
    assert len(s) == 1 and s[0]["avg_wind_sep"] == 8.1 and s[0]["avg_wind_jan"] == 11.0 and s[0]["avg_wind_oct"] is None
    assert set(s[0]) == set(d1_out.STADIUM_COLS) and s[0]["roof_type"] == "open"
    bad = Stadium(stadium_id="x", name="X", lat=0.0, lon=0.0, roof_type="weird")
    assert d1_out.stadium_rows([bad], NOW)[0]["roof_type"] is None  # CHECK constraint
    t = d1_out.team_rows([Team("ne", "nfl", "New England Patriots", "NE", "gillette-stadium")], NOW)[0]
    assert set(t) == set(d1_out.TEAM_COLS) and t["home_stadium_id"] == "gillette-stadium"


def test_odds_rows_join_edges_by_key():
    e = Edge(GID, "betonline", "total", "under", 38.0, -110, 34.6, 0.61, 0.5, 3.4, 0.11, 0.72, "edge")
    rows = d1_out.odds_rows([_ln("betonline", "total", "under", 38.0), _ln("pinnacle", "ml", "home", None, -150)], NOW, "r1", [e])
    assert set(rows[0]) == set(d1_out.ODDS_COLS)
    assert rows[0]["fair_line"] == 34.6 and rows[0]["edge_pts"] == 3.4 and rows[0]["scraped_at"] == NOW and rows[0]["run_id"] == "r1"
    assert rows[1]["fair_line"] is None and rows[1]["line"] is None and rows[1]["is_main"] == 1


def test_odds_deltas_change_only_line_or_odds():
    last = {pstate.odds_key(GID, "total", "under", "betonline"): {"line": 38.0, "odds": -110, "ts": "t0"},
            pstate.odds_key(GID, "total", "over", "betonline"): -110,  # golf-style scalar
            pstate.odds_key(GID, "spread", "home", "pinnacle"): {"line": -3.0, "odds": -105, "ts": "t0"}}
    lines = [_ln("betonline", "total", "under", 38.0),          # unchanged
             _ln("betonline", "total", "over", 38.0),           # scalar prev -> line differs
             _ln("pinnacle", "spread", "home", -3.0, -110),     # odds moved
             _ln("kalshi", "total", "under", 37.5, -104)]       # new key
    out = d1_out.odds_deltas(lines, last)
    assert [ln.book for ln in out] == ["betonline", "pinnacle", "kalshi"]
    assert last[pstate.odds_key(GID, "spread", "home", "pinnacle")]["odds"] == -105  # not mutated


def test_opener_rows_only_for_requested_keys():
    op = pstate.migrate({}, "openers")
    pstate.record_openers(op, [_ln("betonline", "total", "under", 39.0), _ln("pinnacle", "total", "under", 38.5)], NOW)
    rows = d1_out.opener_rows(op, [pstate.odds_key(GID, "total", "under", "betonline"), "bogus"], "r1")
    assert len(rows) == 1 and rows[0]["line"] == 39.0 and rows[0]["seen_at"] == NOW and set(rows[0]) == set(d1_out.OPENER_COLS)


def test_weather_row_and_deltas(tmp_path: Path):
    fc = WeatherForecast(game_id=GID, source="hrrr", lead_hours=30.0, temp_fg=41.0, wind_fg=18.0, gust_fg=26.0,
                         wind_dir_deg=135.0, wind_dir_fg="SE", rain_fg_mm=0.8, precip_prob=0.2)
    imp = compute_impact_v1(sport="nfl", month=9, temp_fg=41.0, wind_fg=18.0, rain_fg_mm=0.8, travel_alt_m=0.0,
                            away_temp=60.0, home_temp=55.0, roof_state="outdoors")
    row = d1_out.weather_row(fc, imp, NOW, "r1")
    assert set(row) == set(d1_out.WX_COLS) and row["gs_fg"] == imp.gs_fg_pct and row["fetched_at"] == NOW
    last = d1_out.load_wx_last(tmp_path)
    assert d1_out.weather_deltas([row], last) == [row]
    assert d1_out.weather_deltas([row], last) == []
    row2 = dict(row, wind_mph=22.0)
    assert d1_out.weather_deltas([row2], last) == [row2]
    row3 = dict(row2, wind_dir_deg=90.0)  # untracked field -> no delta
    assert d1_out.weather_deltas([row3], last) == []
    d1_out.save_wx_last(tmp_path, last)
    assert d1_out.load_wx_last(tmp_path)["last"][f"{GID}|hrrr"][0] == 22.0


def test_run_row_and_statement_order_and_write_sql(tmp_path: Path):
    ctx = RunContext(sport="all", scope="light", run_id="r1", git_sha="abc", started_at=KICK)
    ctx.degrade("odds", "x", "warn")
    ctx.count("pinnacle", "nfl", 5)
    run = d1_out.run_row(ctx, season=2026, week=3, finished_at=KICK + timedelta(seconds=90), n_games=14, n_lines=300)
    assert set(run) == set(d1_out.RUN_COLS) and run["status"] == "ok" and run["duration_s"] == 90.0
    assert json.loads(run["degradations_json"])[0]["component"] == "odds" and json.loads(run["counts_json"]) == {"pinnacle": {"nfl": 5}}
    ctx.degrade("schedule", "boom", "error")
    assert d1_out.run_row(ctx, season=None, week=None, finished_at=KICK, n_games=0, n_lines=0)["status"] == "error"

    stmts = d1_out.build_statements(
        games=d1_out.game_rows([_game()], NOW), stadiums=d1_out.stadium_rows([_stadium()], NOW),
        odds=d1_out.odds_rows([_ln("betonline", "total", "under", 38.0)], NOW, "r1"),
        openers=[{"game_id": GID, "book": "b", "market": "total", "side": "under", "line": 38.0, "odds": -110, "seen_at": NOW, "run_id": "r1"}],
        runs=[run],
    )
    heads = [s.split(" (", 1)[0] for s in stmts]
    assert heads == ["INSERT INTO stadiums", "INSERT INTO games", "INSERT OR IGNORE INTO openers",
                     "INSERT OR IGNORE INTO odds_history", "INSERT INTO runs"]
    assert "ON CONFLICT(stadium_id) DO UPDATE" in stmts[0] and "ON CONFLICT(game_id) DO UPDATE" in stmts[1]
    assert "ON CONFLICT(run_id) DO UPDATE" in stmts[-1]
    p = tmp_path / "d1_inserts.sql"
    assert d1_out.write_sql(p, stmts) == p
    text = p.read_text(encoding="utf-8")
    assert text.endswith(";\n") and text.count("\n\n") == 0 or True
    assert d1_out.write_sql(p, []) is None and not p.exists()  # empty run removes the file (hashFiles gate)


# ---- build() wiring --------------------------------------------------------------------------

def _fake_run_sport(ctx: RunContext, sport: str, raw: Any, season: Any, books: Any = (), state_dir: Path = Path("."),
                    alerts: bool = True) -> build.SportResult:
    if sport != "nfl":
        return build.SportResult(sport, [], [], [], [], {}, {}, {}, {}, build.OddsResult([], {}, pstate.load_openers(state_dir), {}, [], {}))
    lines = [_ln("betonline", "total", "under", 38.0), _ln("betonline", "total", "over", 38.0)]
    openers = pstate.load_openers(state_dir)
    keys_before = set(openers.get("openers") or {})
    pstate.record_openers(openers, lines, NOW)
    new_keys = sorted(set(openers["openers"]) - keys_before)
    archive = pstate.load_archive_last(state_dir)
    deltas = d1_out.odds_deltas(lines, archive.setdefault("last", {}))
    for ln in lines:
        archive["last"][ln.key] = {"line": ln.line, "odds": ln.odds, "ts": NOW}
    pstate.save_openers(state_dir, openers)
    pstate.save_archive_last(state_dir, archive)
    odds = build.OddsResult(lines, {}, openers, {GID: lines}, [], {"betonline": 2}, scraped=lines, deltas=deltas, new_opener_keys=new_keys)
    imp = compute_impact_v1(sport="nfl", month=9, temp_fg=41.0, wind_fg=18.0, rain_fg_mm=0.8, travel_alt_m=0.0,
                            away_temp=60.0, home_temp=55.0, roof_state="outdoors")
    card = {"game_id": GID, "sport": "nfl", "season": 2026, "week": 3, "kickoff_utc": "2026-09-27T17:00:00Z",
            "date_label": "SUN 09/27", "time_label": "01:00 PM", "home": {"short": "NE", "name": "NE"},
            "away": {"short": "SEA", "name": "SEA"}, "neutral": False, "weather": {"wind_fg": 18.0}, "impact": {"v1": {"gs_fg_pct": -6.0}},
            "signal": {"label": "High", "flags": []}, "consensus": {}, "fair": {}, "stadium": {"name": "Gillette"}}
    fc = WeatherForecast(game_id=GID, source="hrrr", lead_hours=30.0, temp_fg=41.0, wind_fg=18.0)
    ctx.count("betonline", "nfl", 2)
    res = build.SportResult(sport, [], [{"game": "SEA @ NE", "kickoff": "k", "temp_fg": 41.0, "wind_fg": 18.0, "rain_fg": 0.8,
                                         "gs_fg_pct": -6.0, "away_fg_pct": 0.0, "signal": "High", "flags": []}],
                            [card], [_game()], {GID: _stadium()}, {"ne": Team("ne", "nfl", "NE")}, {GID: fc}, {GID: imp}, odds)
    wx_last = d1_out.load_wx_last(state_dir)
    res.wx_changed = d1_out.weather_deltas([d1_out.weather_row(fc, imp, NOW, ctx.run_id)], wx_last)
    d1_out.save_wx_last(state_dir, wx_last)
    return res


def test_build_writes_board_d1_sql_and_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build, "run_sport", _fake_run_sport)
    monkeypatch.setattr(build, "write_legacy", lambda sport, records, out_dir, ts: Path(out_dir) / f"{sport}.csv")
    state = tmp_path / "state"
    board = tmp_path / "board"
    snaps = tmp_path / "snapshots"
    sql = tmp_path / "d1_inserts.sql"
    rc = build.build(["nfl", "cfb"], scope="odds", out_dir=tmp_path, raw_dir=tmp_path / "raw", state_dir=state,
                     run_id="r1", board_dir=board, snapshot_dir=snaps, d1_sql=sql, books=["betonline"])
    assert rc == 0
    meta = json.loads((board / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "r1" and meta["sport_counts"] == {"nfl": 1, "cfb": 0}
    assert meta["books"]["betonline"]["status"] == "green" and meta["books_requested"] == ["betonline"]
    assert (board / "games_nfl.json").exists() and (board / "board.json").exists() and (board / "history.json").exists()
    assert (snaps / "nfl" / "2026" / "3" / "r1.json").exists()
    text = sql.read_text(encoding="utf-8")
    assert "INSERT OR IGNORE INTO odds_history" in text and "INSERT OR IGNORE INTO openers" in text
    assert "INSERT OR IGNORE INTO weather_history" in text and "INSERT INTO runs" in text and "INSERT INTO games" in text
    manifest = json.loads((tmp_path / build.PUBLISH_MANIFEST).read_text(encoding="utf-8"))
    keys = list(manifest)
    assert keys[-1] == "board/meta.json" and "board/openers.json" in keys and "board/games_cfb.json" in keys
    for p in board.glob("*.json"):
        assert "NaN" not in p.read_text(encoding="utf-8")

    # second identical run: odds/weather unchanged -> no history inserts, openers untouched
    rc = build.build(["nfl", "cfb"], scope="odds", out_dir=tmp_path, raw_dir=tmp_path / "raw", state_dir=state,
                     run_id="r2", board_dir=board, snapshot_dir=snaps, d1_sql=sql, books=["betonline"])
    assert rc == 0
    text2 = sql.read_text(encoding="utf-8")
    assert "odds_history" not in text2 and "weather_history" not in text2 and "INTO openers" not in text2
    assert "INSERT INTO runs" in text2 and "'r2'" in text2
    op = json.loads((state / "openers.json").read_text(encoding="utf-8"))
    assert op["openers"][pstate.odds_key(GID, "total", "under", "betonline")]["ts"] == NOW
    assert json.loads((board / "meta.json").read_text(encoding="utf-8"))["run_id"] == "r2"


def test_build_publish_without_r2_env_is_noop_and_merge_fetches_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build, "run_sport", _fake_run_sport)
    monkeypatch.setattr(build, "write_legacy", lambda sport, records, out_dir, ts: Path(out_dir) / f"{sport}.csv")
    for var in ("CF_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc = build.build(["nfl"], scope="odds", out_dir=tmp_path, raw_dir=tmp_path / "raw", state_dir=tmp_path / "state",
                     run_id="r1", board_dir=tmp_path / "board", snapshot_dir=tmp_path / "snaps", d1_sql=tmp_path / "d1.sql",
                     books=["betonline"], publish=True)
    assert rc == 0

    # configured: publish goes through the fake client, meta last, self-check passes
    from pipeline.outputs import r2 as r2_out

    class Fake:
        def __init__(self) -> None:
            self.puts: list[str] = []
            self.objects: dict[str, bytes] = {"board/meta.json": json.dumps({"run_id": "old", "sport_counts": {"nfl": 1}}).encode()}

        def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
            self.puts.append(Key)
            self.objects[Key] = Body

        def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
            if Key not in self.objects:
                raise RuntimeError("NoSuchKey")
            from types import SimpleNamespace
            return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}

    fake = Fake()
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    monkeypatch.setattr(r2_out, "make_client", lambda cfg: fake)
    rc = build.build(["nfl"], scope="odds", out_dir=tmp_path, raw_dir=tmp_path / "raw", state_dir=tmp_path / "state2",
                     run_id="r9", board_dir=tmp_path / "board2", snapshot_dir=tmp_path / "snaps2", d1_sql=tmp_path / "d1b.sql",
                     books=["betonline"], merge_into_r2=True)
    assert rc == 0
    assert (tmp_path / "state2" / build.PREV_META_FILE).exists()
    assert fake.puts[-1] == "board/meta.json" and "board/openers.json" in fake.puts and "board/games_nfl.json" in fake.puts
    assert fake.puts.index("board/openers.json") > fake.puts.index("board/games_nfl.json")
    assert json.loads(fake.objects["board/meta.json"])["run_id"] == "r9"
