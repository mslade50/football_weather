"""D1 SQL emitter (ARCH §4.3): ``data/d1_inserts.sql`` for ``wrangler d1 execute --file``.

Patterns copied from golf_scraping/board/build.py L1887-1983 (``_d1_sql_value``,
``_write_d1_deltas`` change-only archive, ≤100-row VALUES chunks). Football adds:

* ``games`` / ``stadiums`` / ``teams``  ``INSERT ... ON CONFLICT(pk) DO UPDATE`` upserts
* ``odds_history``                      change-only ``INSERT OR IGNORE`` (moved line OR odds)
* ``weather_history``                   change-only ``INSERT OR IGNORE`` (any tracked field moved)
* ``openers``                           ``INSERT OR IGNORE`` (never overwritten)
* ``runs``                              upsert of the RunMeta

Every statement carries at most ``CHUNK`` (100) rows; the file is a sequence of
statements separated by newlines so wrangler can run it in one call.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import state as pstate
from pipeline.contracts import Edge, Game, GameLine, Stadium, Team, WeatherForecast
from utils.timeutil import utc_iso

PathLike = Union[str, Path]

CHUNK = 100
D1_SQL_FILENAME = "d1_inserts.sql"

GAME_COLS = ["game_id", "sport", "season", "week", "kickoff_utc", "kickoff_local", "tz", "home_id", "away_id",
             "stadium_id", "neutral", "roof_state", "status", "source", "updated_at",
             "gs_fg", "away_fg", "gs_fg_v2", "away_fg_v2"]  # impact columns: migrations/0004_v2.sql
STADIUM_COLS = ["stadium_id", "name", "city", "state", "country", "lat", "lon", "elevation_m", "timezone",
                "orientation_deg", "orientation_bucket", "orientation_src", "roof_type", "surface", "capacity",
                "year_built", "avg_wind_static", "wind_vol_static", "wind_impact_static", "weakest_wind_effect",
                "avg_wind_sep", "avg_wind_oct", "avg_wind_nov", "avg_wind_dec", "avg_wind_jan", "avg_temp_f",
                "updated_at"]
TEAM_COLS = ["team_id", "sport", "name", "short", "home_stadium_id", "avg_temp_f", "conference", "updated_at"]
ODDS_COLS = ["scraped_at", "game_id", "book", "market", "side", "line", "odds", "prob", "fair_line", "fair_prob",
             "edge_pts", "edge_prob", "is_main", "run_id"]
WX_COLS = ["game_id", "source", "fetched_at", "run_id", "lead_hours", "temp_f", "wind_mph", "gust_mph",
           "wind_dir_deg", "wind_dir", "precip_mm", "precip_prob", "wind_vol", "wind_p10", "wind_p90",
           "cross_mph", "head_mph", "gs_fg", "away_fg", "gs_fg_v2", "away_fg_v2", "model_version"]
OPENER_COLS = ["game_id", "book", "market", "side", "line", "odds", "seen_at", "run_id"]
RUN_COLS = ["run_id", "sport", "season", "week", "git_sha", "scope", "started_at", "finished_at", "duration_s",
            "status", "stage_timings_json", "counts_json", "degradations_json", "unresolved_json",
            "n_games", "n_lines", "n_alerts"]
CLOSING_COLS = ["game_id", "book", "market", "side", "line", "odds", "scraped_at", "kickoff_utc", "frozen_at"]
STADIUM_RESULT_COLS = ["stadium_id", "sport", "season", "under_w", "under_l", "under_p", "roi", "n", "updated_at"]
ALERT_COLS = list(pstate.ALERT_RECORD_COLS)
# first_* / first_sent_at are frozen at the first send; everything else follows the latest send.
ALERT_UPDATE_COLS = [c for c in ALERT_COLS if c != "alert_key" and not c.startswith("first_")]
ALERT_STATUSES = ("open", "closed", "settled")

ROOF_TYPES = ("open", "dome", "retractable")


# ---- SQL helpers (golf ``_d1_sql_value``) ---------------------------------------------

def d1_sql_value(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "NULL"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, datetime):
        return "'" + utc_iso(v) + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _values(rows: Sequence[dict[str, Any]], cols: Sequence[str]) -> str:
    return ",\n".join("(" + ",".join(d1_sql_value(r.get(c)) for c in cols) + ")" for r in rows)


def chunked(rows: Sequence[dict[str, Any]], size: int = CHUNK) -> Iterable[Sequence[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def insert_ignore_sql(table: str, cols: Sequence[str], rows: Sequence[dict[str, Any]]) -> list[str]:
    return [f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES\n{_values(chunk, cols)};"
            for chunk in chunked(list(rows))]


def upsert_sql(table: str, cols: Sequence[str], pk: Sequence[str], rows: Sequence[dict[str, Any]]) -> list[str]:
    update = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    return [f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{_values(chunk, cols)}\n"
            f"ON CONFLICT({', '.join(pk)}) DO UPDATE SET {update};"
            for chunk in chunked(list(rows))]


def dedupe_rows(rows: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Last row per ``key`` wins, first-seen order kept."""
    out: dict[Any, dict[str, Any]] = {}
    for r in rows:
        out[r[key]] = r
    return list(out.values())


def fk_safe_teams(teams: Sequence[dict[str, Any]], stadium_ids: set[str]) -> list[dict[str, Any]]:
    """``teams.home_stadium_id`` REFERENCES ``stadiums`` — null it when the venue is not in the same batch."""
    out = []
    for r in teams:
        sid = r.get("home_stadium_id")
        if sid is not None and sid not in stadium_ids:
            r = {**r, "home_stadium_id": None}
        out.append(r)
    return out


# ---- row builders ------------------------------------------------------------------

def game_rows(
    games: Iterable[Game], now: str, impacts: dict[str, Any] | None = None, impacts_v2: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """``impacts`` / ``impacts_v2``: game_id -> ImpactV1 / ImpactV2 (percent) for the v1/v2 columns."""
    impacts = impacts or {}
    impacts_v2 = impacts_v2 or {}
    rows = []
    for g in games:
        i1 = impacts.get(g.game_id)
        i2 = impacts_v2.get(g.game_id)
        rows.append({
            "game_id": g.game_id, "sport": g.sport, "season": g.season, "week": g.week,
            "kickoff_utc": utc_iso(g.kickoff_utc),
            "kickoff_local": g.kickoff_local.isoformat() if g.kickoff_local else None,
            "tz": g.tz, "home_id": g.home_id, "away_id": g.away_id, "stadium_id": g.stadium_id,
            "neutral": 1 if g.neutral else 0, "roof_state": g.roof_state, "status": g.status,
            "source": g.source, "updated_at": now,
            "gs_fg": getattr(i1, "gs_fg_pct", None), "away_fg": getattr(i1, "away_fg_pct", None),
            "gs_fg_v2": getattr(i2, "gs_fg_pct", None), "away_fg_v2": getattr(i2, "away_fg_pct", None),
        })
    return rows


def stadium_rows(stadiums: Iterable[Stadium], now: str) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for st in stadiums:
        if st is None or st.stadium_id in seen:
            continue
        seen.add(st.stadium_id)
        m = st.avg_wind_by_month or {}
        rows.append({
            "stadium_id": st.stadium_id, "name": st.name, "city": st.city, "state": st.state, "country": st.country,
            "lat": st.lat, "lon": st.lon, "elevation_m": st.elevation_m, "timezone": st.timezone,
            "orientation_deg": st.orientation_deg, "orientation_bucket": st.orientation_bucket,
            "orientation_src": st.orientation_src,
            "roof_type": st.roof_type if st.roof_type in ROOF_TYPES else None,
            "surface": st.surface, "capacity": st.capacity, "year_built": st.year_built,
            "avg_wind_static": st.avg_wind_static, "wind_vol_static": st.wind_vol_static,
            "wind_impact_static": st.wind_impact_static, "weakest_wind_effect": st.weakest_wind_effect,
            "avg_wind_sep": m.get("sep"), "avg_wind_oct": m.get("oct"), "avg_wind_nov": m.get("nov"),
            "avg_wind_dec": m.get("dec"), "avg_wind_jan": m.get("jan"),
            "avg_temp_f": st.avg_temp_f, "updated_at": now,
        })
    return rows


def team_rows(teams: Iterable[Team], now: str) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for t in teams:
        if t is None or t.team_id in seen:
            continue
        seen.add(t.team_id)
        rows.append({"team_id": t.team_id, "sport": t.sport, "name": t.name, "short": t.short,
                     "home_stadium_id": t.home_stadium_id, "avg_temp_f": t.avg_temp_f,
                     "conference": t.conference, "updated_at": now})
    return rows


def _edge_index(edges: Iterable[Edge]) -> dict[str, Edge]:
    return {pstate.odds_key(e.game_id, e.market, e.side, e.book): e for e in edges}


def odds_rows(lines: Iterable[GameLine], now: str, run_id: str, edges: Iterable[Edge] = ()) -> list[dict[str, Any]]:
    """Long-format rows (one per book price) with the edge fields joined by key."""
    idx = _edge_index(edges)
    rows = []
    for ln in lines:
        key = ln.key
        e = idx.get(key)
        ts = utc_iso(ln.scraped_at) if isinstance(ln.scraped_at, datetime) else now
        rows.append({
            "scraped_at": ts, "game_id": ln.game_id, "book": ln.book, "market": ln.market, "side": ln.side,
            "line": ln.line, "odds": ln.odds, "prob": ln.prob_raw,
            "fair_line": e.fair_line if e else None, "fair_prob": e.fair_prob if e else None,
            "edge_pts": e.edge_pts if e else None, "edge_prob": e.edge_prob if e else None,
            "is_main": 1 if ln.is_main else 0, "run_id": run_id,
        })
    return rows


def odds_deltas(lines: Iterable[GameLine], last_map: dict[str, Any]) -> list[GameLine]:
    """Change-only filter (golf ``_write_d1_deltas``): keep a line when its
    (line, odds) differs from ``last_map[key]`` — which holds either the golf-style
    scalar odds or football's ``{"line","odds","ts"}`` dict. Does NOT mutate
    ``last_map`` (``pipeline.build`` owns archive_last)."""
    out = []
    for ln in lines:
        prev = last_map.get(ln.key)
        if isinstance(prev, dict):
            prev_pair = (prev.get("line"), prev.get("odds"))
        elif prev is None:
            prev_pair = None
        else:
            prev_pair = (None, prev)
        if prev_pair != (ln.line, ln.odds):
            out.append(ln)
    return out


def opener_rows(openers: dict, keys: Iterable[str], run_id: str) -> list[dict[str, Any]]:
    store = openers.get("openers") or {}
    rows = []
    for key in keys:
        val = store.get(key)
        parts = key.split("|")
        if not isinstance(val, dict) or len(parts) != 4:
            continue
        game_id, market, side, book = parts
        rows.append({"game_id": game_id, "book": book, "market": market, "side": side,
                     "line": val.get("line"), "odds": val.get("odds"), "seen_at": val.get("ts"), "run_id": run_id})
    return rows


WX_TRACKED = ("wind_mph", "gust_mph", "temp_f", "precip_mm", "precip_prob", "gs_fg", "gs_fg_v2")


def weather_row(fc: WeatherForecast, impact: Any, now: str, run_id: str, impact_v2: Any = None) -> dict[str, Any]:
    g = getattr
    return {
        "game_id": fc.game_id, "source": fc.source or "openmeteo",
        "fetched_at": utc_iso(fc.run_time) if isinstance(fc.run_time, datetime) else now,
        "run_id": run_id, "lead_hours": fc.lead_hours, "temp_f": fc.temp_fg, "wind_mph": fc.wind_fg,
        "gust_mph": fc.gust_fg, "wind_dir_deg": fc.wind_dir_deg, "wind_dir": fc.wind_dir_fg,
        "precip_mm": fc.rain_fg_mm, "precip_prob": fc.precip_prob, "wind_vol": fc.wind_vol_fc,
        "wind_p10": fc.wind_p10, "wind_p90": fc.wind_p90, "cross_mph": fc.cross_mph, "head_mph": fc.head_mph,
        "gs_fg": g(impact, "gs_fg_pct", None), "away_fg": g(impact, "away_fg_pct", None),
        "gs_fg_v2": g(impact_v2, "gs_fg_pct", None), "away_fg_v2": g(impact_v2, "away_fg_pct", None),
        "model_version": g(impact, "model_version", "v1"),
    }


def weather_deltas(rows: Iterable[dict[str, Any]], wx_last: dict) -> list[dict[str, Any]]:
    """Change-only: a row is kept when any ``WX_TRACKED`` field moved vs
    ``wx_last["last"][game_id|source]``. Mutates ``wx_last`` to the new values."""
    last = wx_last.setdefault("last", {})
    out = []
    for r in rows:
        key = f"{r['game_id']}|{r['source']}"
        cur = [r.get(c) for c in WX_TRACKED]
        if last.get(key) != cur:
            out.append(r)
            last[key] = cur
    return out


def load_wx_last(state_dir: PathLike) -> dict:
    d = pstate._load(Path(state_dir) / "wx_last.json")
    if not isinstance(d.get("last"), dict):
        d["last"] = {}
    return d


def save_wx_last(state_dir: PathLike, d: dict) -> None:
    pstate._save(Path(state_dir) / "wx_last.json", d)


def alert_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """D1 ``alerts`` rows from pipeline/alerts.py records (extra JSON-only keys
    dropped, status coerced to the CHECK set, one row per alert_key)."""
    rows: dict[str, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("alert_key") or not rec.get("family"):
            continue
        row = {c: rec.get(c) for c in ALERT_COLS}
        if row.get("status") not in ALERT_STATUSES:
            row["status"] = "open"
        row["sends"] = int(row.get("sends") or 1)
        row["last_sent_at"] = row.get("last_sent_at") or row.get("first_sent_at")
        row["first_sent_at"] = row.get("first_sent_at") or row["last_sent_at"]
        rows[row["alert_key"]] = row
    return list(rows.values())


def alert_upsert_sql(rows: Sequence[dict[str, Any]]) -> list[str]:
    """``INSERT ... ON CONFLICT(alert_key) DO UPDATE`` that never touches the
    ``first_*`` columns (ARCH §4.3: INSERT OR IGNORE semantics for the first send,
    update semantics after)."""
    update = ", ".join(f"{c}=excluded.{c}" for c in ALERT_UPDATE_COLS)
    return [f"INSERT INTO alerts ({', '.join(ALERT_COLS)}) VALUES\n{_values(chunk, ALERT_COLS)}\n"
            f"ON CONFLICT(alert_key) DO UPDATE SET {update};"
            for chunk in chunked(list(rows))]


def run_row(
    ctx: Any,
    *,
    season: Optional[int],
    week: Optional[int],
    finished_at: datetime,
    n_games: int,
    n_lines: int,
    n_alerts: int = 0,
    status: Optional[str] = None,
) -> dict[str, Any]:
    if status is None:
        status = "error" if any(d.severity == "error" for d in ctx.degradations) else "ok"
    return {
        "run_id": ctx.run_id, "sport": ctx.sport, "season": season, "week": week, "git_sha": ctx.git_sha,
        "scope": ctx.scope, "started_at": utc_iso(ctx.started_at), "finished_at": utc_iso(finished_at),
        "duration_s": round((finished_at - ctx.started_at).total_seconds(), 3), "status": status,
        "stage_timings_json": json.dumps(ctx.stage_timings, allow_nan=False),
        "counts_json": json.dumps(ctx.counts, allow_nan=False),
        "degradations_json": json.dumps([{"component": d.component, "reason": d.reason, "severity": d.severity}
                                         for d in ctx.degradations], allow_nan=False),
        "unresolved_json": json.dumps(list(ctx.unresolved_names), allow_nan=False),
        "n_games": n_games, "n_lines": n_lines, "n_alerts": n_alerts,
    }


# ---- assembly --------------------------------------------------------------------------

def build_statements(
    *,
    games: Sequence[dict[str, Any]] = (),
    stadiums: Sequence[dict[str, Any]] = (),
    teams: Sequence[dict[str, Any]] = (),
    odds: Sequence[dict[str, Any]] = (),
    weather: Sequence[dict[str, Any]] = (),
    openers: Sequence[dict[str, Any]] = (),
    runs: Sequence[dict[str, Any]] = (),
    alerts: Sequence[dict[str, Any]] = (),
    closings: Sequence[dict[str, Any]] = (),
    stadium_results: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """``closings`` (pipeline/model/clv.py rows) are frozen once → INSERT OR IGNORE;
    ``stadium_results`` (pipeline/backtest.py) are recomputed weekly → upsert."""
    stmts: list[str] = []
    if closings:
        stmts += insert_ignore_sql("closings", CLOSING_COLS, closings)
    if stadium_results:
        stmts += upsert_sql("stadium_results", STADIUM_RESULT_COLS, ["stadium_id", "sport", "season"], stadium_results)
    # One row per key per statement (SQLite rejects a multi-row upsert that hits the same
    # row twice) and never reference a stadium that is not in this batch (D1 FK).
    stadiums = dedupe_rows(stadiums, "stadium_id")
    teams = fk_safe_teams(dedupe_rows(teams, "team_id"), {r["stadium_id"] for r in stadiums})
    if stadiums:
        stmts += upsert_sql("stadiums", STADIUM_COLS, ["stadium_id"], stadiums)
    if teams:
        stmts += upsert_sql("teams", TEAM_COLS, ["team_id"], teams)
    if games:
        stmts += upsert_sql("games", GAME_COLS, ["game_id"], games)
    if openers:
        stmts += insert_ignore_sql("openers", OPENER_COLS, openers)
    if odds:
        stmts += insert_ignore_sql("odds_history", ODDS_COLS, odds)
    if weather:
        stmts += insert_ignore_sql("weather_history", WX_COLS, weather)
    if alerts:
        stmts += alert_upsert_sql(alerts)
    if runs:
        stmts += upsert_sql("runs", RUN_COLS, ["run_id"], runs)
    return stmts


def write_sql(path: PathLike, statements: Sequence[str]) -> Optional[Path]:
    """Write the statements (one file for ``wrangler d1 execute --file``). Removes a
    stale file and returns None when there is nothing to execute — the workflow
    gates the D1 step on ``hashFiles``."""
    p = Path(path)
    if not statements:
        if p.exists():
            p.unlink()
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(statements) + "\n", encoding="utf-8")
    return p


__all__ = [
    "CHUNK", "D1_SQL_FILENAME", "GAME_COLS", "STADIUM_COLS", "TEAM_COLS", "ODDS_COLS", "WX_COLS", "OPENER_COLS",
    "CLOSING_COLS", "STADIUM_RESULT_COLS",
    "RUN_COLS", "ALERT_COLS", "ALERT_UPDATE_COLS", "WX_TRACKED", "d1_sql_value", "chunked", "insert_ignore_sql",
    "upsert_sql", "game_rows", "stadium_rows", "team_rows", "odds_rows", "odds_deltas", "opener_rows",
    "weather_row", "weather_deltas", "load_wx_last", "save_wx_last", "alert_rows", "alert_upsert_sql", "run_row",
    "build_statements", "write_sql",
]
