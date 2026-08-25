"""Backtest + CLV grid (PLAN Phase 6; AUDIT §4.3; ARCHITECTURE §7.3 CLV, §7.4 lookup).

Regenerates the legacy ``cfb_weather_backtest.xlsx`` as ``board/backtest.json`` and
``data/backtest/*.parquet``::

    python -m pipeline.backtest [--snapshot-dir data/snapshots] [--state-dir data/state]
                                [--export-dir <wrangler d1 export json dir>] [--sqlite <db|d1_inserts.sql>]
                                [--board-dir site/web/data] [--parquet-dir data/backtest]
                                [--season 2026] [--sport nfl|cfb] [--no-network] [--freeze]

Inputs (every one optional; the grid is emitted with empty buckets when nothing is
available so the Backtest tab never blanks):

* **snapshots/** – per-run GameCard lists. Per game: the last snapshot before
  kickoff gives the closing consensus total/spread and the forecast the bettor saw;
  the snapshot whose ``weather.lead_hours`` is closest to 24/72/120 h gives the
  lead-1/3/5 forecast.
* **D1** – ``--export-dir`` (``wrangler d1 export`` / ``/api/*`` JSON per table:
  games, odds_history, closings, alerts) or ``--sqlite`` (a SQLite db, or
  ``d1_inserts.sql`` replayed on the migrations into memory). D1 ``closings`` win
  over snapshot consensus; ``alerts`` feed the CLV-by-tier/league/book/model table.
* **state/** – ``history.json`` (freeze fallback), ``closings.json``, ``alerts.json``.
* **Open-Meteo historical-forecast** (HRRR) actuals over the kickoff window and
  **previous-runs** ``_previous_dayN`` forecasts when a snapshot at that lead is missing.
* **Results** – CFBD ``/games`` (``CFBD_API_KEY``) else ESPN scoreboard; nflverse
  ``games.csv`` ``home_score/away_score/result/total``.

Bucket definitions are the 118 legacy rows (ids = ``Signal``) read from
``tests/fixtures/legacy/cfb_weather_backtest.xlsx``. Matching follows
``pages/cfb_weather.py`` exactly for the hover lookup (first match, inclusive
bounds, ``Wind Below`` NaN→100, ``Spread_l`` NaN→0, ``Temp Above`` NaN→0, a NaN
``Spread_h`` / ``CLV from Open`` never matches) while the grid aggregation lets a
NaN bound mean "unbounded" and a NaN CLV mean "all" (that is how the legacy sheet
was built: the NFL rows and the all-CLV rows carry samples).

Grid metrics grade the UNDER at the closing total: ``Wins/Losses/Push``,
``Sample = W+L+P``, ``Margin = mean(close_total - actual_total)``,
``ROI = (W*100/110 - L)/Sample``, ``+ CLV`` = games whose total moved down from
open to close, ``CLV % = +CLV/Sample`` (formulas recovered from the sheet values).
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import state as pstate
from pipeline.model import clv as clv_mod
from pipeline.outputs import d1_out, json_out
from pipeline.run_context import REPO_ROOT
from pipeline.weather.merge import compass16, mean3, vector_mean_deg
from utils.env import load_repo_dotenv
from utils.timeutil import ensure_utc, now_utc, parse_iso, utc_iso

PathLike = Union[str, Path]

GRID_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legacy" / "cfb_weather_backtest.xlsx"
MIGRATIONS_DIR = REPO_ROOT / "site" / "worker" / "migrations"
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"
DEFAULT_STATE_DIR = REPO_ROOT / "data" / "state"
DEFAULT_BOARD_DIR = REPO_ROOT / "site" / "web" / "data"
DEFAULT_PARQUET_DIR = REPO_ROOT / "data" / "backtest"

SPORT_LABEL = {"cfb": "NCAAF", "nfl": "NFL"}
LABEL_SPORT = {v: k for k, v in SPORT_LABEL.items()}
LEADS: tuple[int, ...] = (1, 3, 5)
LEAD_TOL_H = 12.0
WIN_PAYOUT = 100.0 / 110.0
LEGACY_WIND_BELOW_FILL = 100.0

HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ACTUAL_MODELS = "ncep_hrrr_conus"
PREVIOUS_MODELS = "best_match"
HOURLY_VARS = ("temperature_2m", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "precipitation")
CFBD_BASE = "https://api.collegefootballdata.com"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
USER_AGENT = "football_weather (mckinleyslade@gmail.com)"
NET_SLEEP_S = 0.3
BATCH = 50

PARQUET_TABLES = ("games", "grid", "stadium_results", "alerts_clv")


# ---- helpers ------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return ensure_utc(v)
    if isinstance(v, str) and v.strip():
        try:
            return ensure_utc(parse_iso(v))
        except ValueError:
            return None
    return None


def _round(v: Optional[float], nd: int = 3) -> Optional[float]:
    return None if v is None else round(v, nd)


# ---- bucket definitions (legacy Backtesting sheet) -----------------------------------------

@dataclass(frozen=True)
class Bucket:
    id: int
    sport: Optional[str]            # 'cfb' | 'nfl' | None (separator row)
    wind_lo: Optional[float]
    wind_hi: Optional[float]
    temp_lo: Optional[float]
    temp_hi: Optional[float]
    spread_lo: Optional[float]
    spread_hi: Optional[float]
    clv: Optional[str]              # 'Positive' | 'Negative' | None (= all)
    legacy: dict[str, Optional[float]] = field(default_factory=dict)  # the sheet's own numbers, for reference

    @property
    def is_separator(self) -> bool:
        return self.sport is None

    def legacy_columns(self) -> dict[str, Any]:
        return {
            "Sport": SPORT_LABEL.get(self.sport or "", None), "Wind Above": self.wind_lo, "Wind Below": self.wind_hi,
            "Temp Above": self.temp_lo, "Temp Below": self.temp_hi, "Spread_l": self.spread_lo, "Spread_h": self.spread_hi,
            "CLV from Open": self.clv, "Signal": self.id,
        }


def bucket_from_row(row: Mapping[str, Any]) -> Bucket:
    """One sheet row (column names as in the xlsx) → Bucket."""
    label = row.get("Sport")
    sport = LABEL_SPORT.get(str(label).strip().upper()) if isinstance(label, str) else None
    clv = row.get("CLV from Open")
    clv = clv.strip().title() if isinstance(clv, str) and clv.strip() else None
    legacy = {k: _num(row.get(k)) for k in ("Wins", "Losses", "Push", "Sample", "Margin", "ROI", "+ CLV", "CLV %")}
    return Bucket(
        id=int(row["Signal"]), sport=sport,
        wind_lo=_num(row.get("Wind Above")), wind_hi=_num(row.get("Wind Below")),
        temp_lo=_num(row.get("Temp Above")), temp_hi=_num(row.get("Temp Below")),
        spread_lo=_num(row.get("Spread_l")), spread_hi=_num(row.get("Spread_h")),
        clv=clv, legacy=legacy,
    )


def load_grid_defs(path: PathLike = GRID_FIXTURE) -> list[Bucket]:
    """The 118 legacy buckets, in sheet order (ids preserved, separator row kept)."""
    import pandas as pd

    df = pd.read_excel(path, sheet_name="Backtesting")
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in rec.items()}
        out.append(bucket_from_row(clean))
    return out


def load_stadium_sheet(path: PathLike = GRID_FIXTURE) -> list[dict[str, Any]]:
    """Legacy Stadiums sheet rows (Team, Stadium, Record 'W-L-P', Percentage) for reference."""
    import pandas as pd

    df = pd.read_excel(path, sheet_name="Stadiums")
    rows = []
    for rec in df.to_dict(orient="records"):
        rows.append({k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in rec.items()})
    return rows


def _in(lo: Optional[float], hi: Optional[float], x: float) -> bool:
    return (lo is None or lo <= x) and (hi is None or x <= hi)


def bucket_matches(
    b: Bucket, sport: str, temp: Optional[float], wind: Optional[float], spread_abs: Optional[float],
    clv: Optional[str], *, legacy_lookup: bool = False,
) -> bool:
    """Inclusive bounds on temp / wind / |spread|; ``clv`` is 'Positive'/'Negative'.

    ``legacy_lookup=True`` reproduces ``pages/cfb_weather.py``: NaN ``Spread_h``,
    ``Temp Below`` or ``CLV from Open`` never match (so NFL rows and all-CLV rows
    are never hover results); ``Wind Below`` NaN→100, ``Spread_l``/``Temp Above``
    NaN→0. Aggregation mode treats every NaN bound as unbounded and NaN CLV as all."""
    if b.is_separator or b.sport != sport or temp is None or wind is None:
        return False
    wind_hi = b.wind_hi if b.wind_hi is not None else (LEGACY_WIND_BELOW_FILL if legacy_lookup else None)
    temp_lo = b.temp_lo if b.temp_lo is not None else (0.0 if legacy_lookup else None)
    if legacy_lookup:
        if b.temp_hi is None or b.spread_hi is None or b.clv is None:
            return False
        if spread_abs is None or clv is None:
            return False
    if not _in(temp_lo, b.temp_hi, temp) or not _in(b.wind_lo, wind_hi, wind):
        return False
    if b.spread_lo is not None or b.spread_hi is not None:
        spread_lo = b.spread_lo if b.spread_lo is not None else 0.0
        if spread_abs is None or not _in(spread_lo, b.spread_hi, spread_abs):
            return False
    if b.clv is not None and clv != b.clv:
        return False
    return True


def first_match(defs: Sequence[Bucket], sport: str, temp: Optional[float], wind: Optional[float],
                spread_abs: Optional[float], clv: Optional[str]) -> Optional[Bucket]:
    """Legacy hover lookup: first bucket in sheet order that matches."""
    for b in defs:
        if bucket_matches(b, sport, temp, wind, spread_abs, clv, legacy_lookup=True):
            return b
    return None


# ---- per-game rows --------------------------------------------------------------------------

@dataclass
class GameRow:
    game_id: str
    sport: str
    season: Optional[int] = None
    week: Optional[int] = None
    kickoff_utc: Optional[str] = None
    home_id: Optional[str] = None
    away_id: Optional[str] = None
    home_name: Optional[str] = None
    away_name: Optional[str] = None
    stadium_id: Optional[str] = None
    stadium_name: Optional[str] = None
    neutral: bool = False
    roof_state: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    # forecast the bettor saw (last snapshot before kickoff)
    temp_fc: Optional[float] = None
    wind_fc: Optional[float] = None
    gust_fc: Optional[float] = None
    rain_fc: Optional[float] = None
    wind_dir_fc: Optional[str] = None
    lead_fc: Optional[float] = None
    gs_fg_v1: Optional[float] = None
    away_fg_v1: Optional[float] = None
    gs_fg_v2: Optional[float] = None
    away_fg_v2: Optional[float] = None
    fair_total_v1: Optional[float] = None
    fair_total_v2: Optional[float] = None
    # lead-N forecasts (snapshots, else previous-runs API)
    wind_lead1: Optional[float] = None
    temp_lead1: Optional[float] = None
    rain_lead1: Optional[float] = None
    wind_lead3: Optional[float] = None
    temp_lead3: Optional[float] = None
    rain_lead3: Optional[float] = None
    wind_lead5: Optional[float] = None
    temp_lead5: Optional[float] = None
    rain_lead5: Optional[float] = None
    # actuals (historical-forecast HRRR over kickoff..+2h)
    temp_act: Optional[float] = None
    wind_act: Optional[float] = None
    gust_act: Optional[float] = None
    rain_act: Optional[float] = None
    wind_dir_act: Optional[str] = None
    # market
    total_open: Optional[float] = None
    total_close: Optional[float] = None
    spread_open: Optional[float] = None
    spread_close: Optional[float] = None
    ref_book: Optional[str] = None
    clv_status: Optional[str] = None
    # result
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    actual_total: Optional[float] = None
    result: Optional[float] = None       # home margin
    under_result: Optional[str] = None   # W | L | P
    margin: Optional[float] = None       # close_total - actual_total
    # sources
    src_forecast: Optional[str] = None
    src_actual: Optional[str] = None
    src_result: Optional[str] = None

    @property
    def spread_abs(self) -> Optional[float]:
        s = self.spread_open if self.spread_open is not None else self.spread_close
        return abs(s) if s is not None else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spread_abs"] = self.spread_abs
        return d


ROW_FIELDS = [f.name for f in fields(GameRow)]


def grade_under(close_total: Optional[float], actual_total: Optional[float]) -> Optional[str]:
    if close_total is None or actual_total is None:
        return None
    if actual_total < close_total:
        return "W"
    if actual_total > close_total:
        return "L"
    return "P"


def finalize_row(r: GameRow) -> GameRow:
    """Derived columns once market + result are known."""
    if r.home_score is not None and r.away_score is not None:
        r.actual_total = float(r.home_score + r.away_score)
        r.result = float(r.home_score - r.away_score)
    r.clv_status = clv_mod.clv_status(r.total_open, r.total_close)
    r.under_result = grade_under(r.total_close, r.actual_total)
    r.margin = _round(r.total_close - r.actual_total, 3) if r.total_close is not None and r.actual_total is not None else None
    return r


# ---- snapshots ------------------------------------------------------------------------------

def _snapshot_ts(path: Path, meta: Mapping[str, Any]) -> Optional[datetime]:
    ts = _dt(meta.get("last_updated"))
    if ts is not None:
        return ts
    stem = path.stem.split("-", 1)[0]  # 20260824T041107Z
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_snapshots(snapshot_dir: PathLike, sport: Optional[str] = None, season: Optional[int] = None
                   ) -> dict[str, list[tuple[datetime, dict[str, Any]]]]:
    """``{game_id: [(snapshot_ts, card), ...]}`` sorted by time from ``snapshots/{sport}/{season}/{week}/*.json``."""
    root = Path(snapshot_dir)
    out: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    if not root.is_dir():
        return {}
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root).parts
        if len(rel) < 4:
            continue
        sp, yr = rel[0], rel[1]
        if sport and sp != sport:
            continue
        if season is not None and str(yr) != str(season):
            continue
        d = pstate._load_any(p)
        if not isinstance(d, dict):
            continue
        ts = _snapshot_ts(p, d.get("meta") or {})
        if ts is None:
            continue
        for card in d.get("games") or []:
            if isinstance(card, dict) and card.get("game_id"):
                out[str(card["game_id"])].append((ts, card))
    for seq in out.values():
        seq.sort(key=lambda x: x[0])
    return dict(out)


def _card_weather(card: Mapping[str, Any]) -> dict[str, Any]:
    return card.get("weather") or {}


def _impact(card: Mapping[str, Any], ver: str) -> Mapping[str, Any]:
    return (card.get("impact") or {}).get(ver) or {}


def row_from_snapshots(game_id: str, snaps: Sequence[tuple[datetime, Mapping[str, Any]]]) -> Optional[GameRow]:
    """Closing forecast/market from the last snapshot before kickoff; lead-N from the
    snapshot closest to 24N h (±LEAD_TOL_H); opener from the earliest snapshot."""
    if not snaps:
        return None
    last_card = snaps[-1][1]
    kick = _dt(last_card.get("kickoff_utc"))
    pre = [(ts, c) for ts, c in snaps if kick is None or ts < kick]
    if not pre:
        pre = list(snaps[:1])
    ts_close, close = pre[-1]
    first = snaps[0][1]
    st = close.get("stadium") or {}
    wx = _card_weather(close)
    cons = close.get("consensus") or {}
    cons0 = first.get("consensus") or {}
    fair = close.get("fair") or {}
    r = GameRow(
        game_id=game_id, sport=str(close.get("sport") or game_id.split(":", 1)[0]),
        season=close.get("season"), week=close.get("week"), kickoff_utc=close.get("kickoff_utc"),
        home_id=(close.get("home") or {}).get("team_id"), away_id=(close.get("away") or {}).get("team_id"),
        home_name=(close.get("home") or {}).get("name"), away_name=(close.get("away") or {}).get("name"),
        stadium_id=st.get("stadium_id"), stadium_name=st.get("name"), neutral=bool(close.get("neutral")),
        roof_state=st.get("roof_state"), lat=_num(st.get("lat")), lon=_num(st.get("lon")),
        temp_fc=_num(wx.get("temp_fg")), wind_fc=_num(wx.get("wind_fg")), gust_fc=_num(wx.get("gust_fg")),
        rain_fc=_num(wx.get("rain_fg")), wind_dir_fc=wx.get("wind_dir_fg"), lead_fc=_num(wx.get("lead_hours")),
        gs_fg_v1=_num(_impact(close, "v1").get("gs_fg_pct")), away_fg_v1=_num(_impact(close, "v1").get("away_fg_pct")),
        gs_fg_v2=_num(_impact(close, "v2").get("gs_fg_pct")), away_fg_v2=_num(_impact(close, "v2").get("away_fg_pct")),
        fair_total_v1=_num(fair.get("fair_total")), fair_total_v2=_num(fair.get("fair_total_v2")),
        total_open=_num(cons0.get("total_open")) if _num(cons0.get("total_open")) is not None else _num(cons.get("total_open")),
        total_close=_num(cons.get("total_now")),
        spread_open=_num(cons0.get("spread_open")) if _num(cons0.get("spread_open")) is not None else _num(cons.get("spread_open")),
        spread_close=_num(cons.get("spread_now")), ref_book=cons.get("ref_book"),
        src_forecast=f"snapshot:{utc_iso(ts_close)}",
    )
    for n in LEADS:
        target = 24.0 * n
        best: Optional[tuple[float, Mapping[str, Any]]] = None
        for _, c in snaps:
            lead = _num(_card_weather(c).get("lead_hours"))
            if lead is None:
                continue
            gap = abs(lead - target)
            if gap <= LEAD_TOL_H and (best is None or gap < best[0]):
                best = (gap, c)
        if best is not None:
            w = _card_weather(best[1])
            setattr(r, f"wind_lead{n}", _num(w.get("wind_fg")))
            setattr(r, f"temp_lead{n}", _num(w.get("temp_fg")))
            setattr(r, f"rain_lead{n}", _num(w.get("rain_fg")))
    return r


# ---- D1 export / sqlite ---------------------------------------------------------------------

@dataclass
class D1Data:
    games: list[dict[str, Any]] = field(default_factory=list)
    odds_history: list[dict[str, Any]] = field(default_factory=list)
    closings: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    stadiums: list[dict[str, Any]] = field(default_factory=list)
    teams: list[dict[str, Any]] = field(default_factory=list)
    weather_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.games or self.odds_history or self.closings or self.alerts)


D1_TABLES = ("games", "odds_history", "closings", "alerts", "stadiums", "teams", "weather_history")


def load_export_dir(export_dir: PathLike) -> D1Data:
    """``{table}.json`` per table in any of the shapes ``state._export_rows`` accepts."""
    root = Path(export_dir)
    d = D1Data()
    if not root.is_dir():
        return d
    for table in D1_TABLES:
        for cand in (root / f"{table}.json", root / f"{table}_export.json"):
            if cand.is_file():
                setattr(d, table, pstate._export_rows(pstate._load_any(cand)))
                break
    return d


def _sqlite_from_sql(sql_path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for m in sorted(migrations_dir.glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.executescript(sql_path.read_text(encoding="utf-8"))
    return con


def load_sqlite(path: PathLike, migrations_dir: Path = MIGRATIONS_DIR) -> D1Data:
    """A SQLite database file, or a ``d1_inserts.sql`` replayed onto the migrations in memory."""
    p = Path(path)
    d = D1Data()
    if not p.is_file():
        return d
    con = _sqlite_from_sql(p, migrations_dir) if p.suffix.lower() == ".sql" else sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    try:
        for table in D1_TABLES:
            try:
                rows = con.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            setattr(d, table, [dict(r) for r in rows])
    finally:
        con.close()
    return d


def rows_from_games(games: Iterable[Mapping[str, Any]], stadiums: Iterable[Mapping[str, Any]] = (),
                    teams: Iterable[Mapping[str, Any]] = ()) -> dict[str, GameRow]:
    st_by = {s.get("stadium_id"): s for s in stadiums}
    tm_by = {t.get("team_id"): t for t in teams}
    out: dict[str, GameRow] = {}
    for g in games:
        gid = str(g.get("game_id") or "")
        if not gid:
            continue
        st = st_by.get(g.get("stadium_id")) or {}
        out[gid] = GameRow(
            game_id=gid, sport=str(g.get("sport") or gid.split(":", 1)[0]), season=g.get("season"), week=g.get("week"),
            kickoff_utc=g.get("kickoff_utc"), home_id=g.get("home_id"), away_id=g.get("away_id"),
            home_name=(tm_by.get(g.get("home_id")) or {}).get("name"), away_name=(tm_by.get(g.get("away_id")) or {}).get("name"),
            stadium_id=g.get("stadium_id"), stadium_name=st.get("name"), neutral=bool(g.get("neutral")),
            roof_state=g.get("roof_state"), lat=_num(st.get("lat")), lon=_num(st.get("lon")),
            gs_fg_v1=_num(g.get("gs_fg")), away_fg_v1=_num(g.get("away_fg")),
            gs_fg_v2=_num(g.get("gs_fg_v2")), away_fg_v2=_num(g.get("away_fg_v2")),
        )
    return out


def apply_closings(rows: Mapping[str, GameRow], closings: Iterable[Mapping[str, Any]], ref_books: Sequence[str] = ("pinnacle", "betonline", "betcris", "fanduel", "draftkings")) -> int:
    """Closing total (over side) / home spread from frozen D1 ``closings``: the first
    book in ``ref_books`` that has one. Overrides snapshot consensus."""
    by_game: dict[str, dict[tuple[str, str, str], Mapping[str, Any]]] = defaultdict(dict)
    for c in closings:
        gid = str(c.get("game_id") or "")
        by_game[gid][(str(c.get("market")), str(c.get("side")), str(c.get("book")))] = c
    n = 0
    for gid, r in rows.items():
        keys = by_game.get(gid)
        if not keys:
            continue
        for book in ref_books:
            tot = keys.get(("total", "over", book)) or keys.get(("total", "under", book))
            spr = keys.get(("spread", "home", book))
            if tot is not None and _num(tot.get("line")) is not None:
                r.total_close, r.ref_book = _num(tot.get("line")), book
                if spr is not None:
                    r.spread_close = _num(spr.get("line"))
                r.src_forecast = r.src_forecast or "d1"
                n += 1
                break
    return n


def apply_openers(rows: Mapping[str, GameRow], odds_history: Iterable[Mapping[str, Any]], ref_books: Sequence[str] = ("pinnacle", "betonline", "betcris", "fanduel", "draftkings")) -> int:
    """Opening total / home spread = first odds_history row per (game, book) (ARCH §4.3:
    the first row IS the opener). Overrides snapshot consensus like ``apply_closings``."""
    first: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for h in sorted(odds_history, key=lambda x: str(x.get("scraped_at") or "")):
        k = (str(h.get("game_id")), str(h.get("market")), str(h.get("side")), str(h.get("book")))
        first.setdefault(k, h)
    n = 0
    for gid, r in rows.items():
        for book in ref_books:
            tot = first.get((gid, "total", "over", book)) or first.get((gid, "total", "under", book))
            if tot is not None and _num(tot.get("line")) is not None:
                r.total_open = _num(tot.get("line"))
                spr = first.get((gid, "spread", "home", book))
                if spr is not None and _num(spr.get("line")) is not None:
                    r.spread_open = _num(spr.get("line"))
                n += 1
                break
    return n


# ---- Open-Meteo actuals + previous runs -------------------------------------------------------

def _window(kick: datetime) -> tuple[datetime, datetime]:
    k = ensure_utc(kick).replace(minute=0, second=0, microsecond=0)
    return k, k + timedelta(hours=2)


def _get_json(url: str, params: Mapping[str, str], client: Any = None) -> Any:
    import httpx

    own = client is None
    c = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    try:
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = c.get(url, params=dict(params))
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"request failed: {url}: {last}")
    finally:
        if own:
            c.close()


def hourly_series(payload: Any) -> list[dict[str, Any]]:
    """``[{time: [...], <var>: [...]}, ...]`` per location from a single or batched Open-Meteo payload."""
    locs = payload if isinstance(payload, list) else [payload]
    out = []
    for loc in locs:
        h = (loc or {}).get("hourly") or {}
        out.append({k: v for k, v in h.items() if isinstance(v, list)})
    return out


def window_stats(series: Mapping[str, Sequence[Any]], start: datetime, end: datetime, suffix: str = "") -> dict[str, Optional[float]]:
    """Legacy aggregation over the 3 hours kickoff..+2h: mean temp/wind/gust, sum precip, vector-mean dir."""
    times = [_dt(t) for t in series.get("time") or []]
    idx = [i for i, t in enumerate(times) if t is not None and start <= t <= end]

    def col(name: str) -> list[Optional[float]]:
        arr = series.get(f"{name}{suffix}") or []
        return [_num(arr[i]) if i < len(arr) else None for i in idx]

    dirs = col("wind_direction_10m")
    precip = [p for p in col("precipitation") if p is not None]
    return {
        "temp": mean3(col("temperature_2m")), "wind": mean3(col("wind_speed_10m")), "gust": mean3(col("wind_gusts_10m")),
        "rain": (sum(precip) if precip else None), "dir_deg": vector_mean_deg(dirs), "dir": compass16(vector_mean_deg(dirs)),
    }


def _group_windows(rows: Iterable[GameRow]) -> dict[tuple[str, str], list[GameRow]]:
    groups: dict[tuple[str, str], list[GameRow]] = defaultdict(list)
    for r in rows:
        kick = _dt(r.kickoff_utc)
        if kick is None or r.lat is None or r.lon is None:
            continue
        s, e = _window(kick)
        groups[(s.strftime("%Y-%m-%dT%H:00"), e.strftime("%Y-%m-%dT%H:00"))].append(r)
    return groups


def fetch_actuals(rows: Iterable[GameRow], *, get: Callable[[str, Mapping[str, str]], Any] = _get_json,
                  models: str = ACTUAL_MODELS, sleep: Callable[[float], None] = time.sleep) -> int:
    """Historical-forecast API (HRRR archive) over kickoff..+2h per game, batched by
    identical window (≤50 points). Falls back to ``best_match`` when the CONUS model
    returns nothing (international venues). Returns the number of rows filled."""
    n = 0
    for (start, end), grp in _group_windows(rows).items():
        for i in range(0, len(grp), BATCH):
            batch = grp[i:i + BATCH]
            params = {"latitude": ",".join(f"{r.lat:.4f}" for r in batch), "longitude": ",".join(f"{r.lon:.4f}" for r in batch),
                      "hourly": ",".join(HOURLY_VARS), "start_hour": start, "end_hour": end, "wind_speed_unit": "mph",
                      "temperature_unit": "fahrenheit", "precipitation_unit": "mm", "timezone": "UTC", "models": models}
            try:
                series = hourly_series(get(HIST_FORECAST_URL, params))
            except Exception:  # noqa: BLE001
                params["models"] = "best_match"
                try:
                    series = hourly_series(get(HIST_FORECAST_URL, params))
                except Exception:  # noqa: BLE001
                    continue
            s_dt, e_dt = _dt(start), _dt(end)
            for r, ser in zip(batch, series, strict=False):
                st = window_stats(ser, s_dt, e_dt)
                if st["wind"] is None and st["temp"] is None:
                    continue
                r.temp_act, r.wind_act, r.gust_act, r.rain_act, r.wind_dir_act = st["temp"], st["wind"], st["gust"], st["rain"], st["dir"]
                r.src_actual = f"openmeteo_hist:{params['models']}"
                n += 1
            sleep(NET_SLEEP_S)
    return n


def fetch_previous_runs(rows: Iterable[GameRow], *, leads: Sequence[int] = LEADS,
                        get: Callable[[str, Mapping[str, str]], Any] = _get_json, models: str = PREVIOUS_MODELS,
                        sleep: Callable[[float], None] = time.sleep) -> int:
    """previous-runs API ``<var>_previous_dayN`` for rows missing a lead-N snapshot forecast."""
    need = [r for r in rows if any(getattr(r, f"wind_lead{n}") is None for n in leads)]
    n_filled = 0
    hourly = [f"{v}_previous_day{n}" for n in leads for v in ("temperature_2m", "wind_speed_10m", "precipitation")]
    for (start, end), grp in _group_windows(need).items():
        for i in range(0, len(grp), BATCH):
            batch = grp[i:i + BATCH]
            params = {"latitude": ",".join(f"{r.lat:.4f}" for r in batch), "longitude": ",".join(f"{r.lon:.4f}" for r in batch),
                      "hourly": ",".join(hourly), "start_hour": start, "end_hour": end, "wind_speed_unit": "mph",
                      "temperature_unit": "fahrenheit", "precipitation_unit": "mm", "timezone": "UTC", "models": models}
            try:
                series = hourly_series(get(PREVIOUS_RUNS_URL, params))
            except Exception:  # noqa: BLE001
                continue
            s_dt, e_dt = _dt(start), _dt(end)
            for r, ser in zip(batch, series, strict=False):
                for n in leads:
                    if getattr(r, f"wind_lead{n}") is not None:
                        continue
                    st = window_stats(ser, s_dt, e_dt, suffix=f"_previous_day{n}")
                    if st["wind"] is None:
                        continue
                    setattr(r, f"wind_lead{n}", st["wind"])
                    setattr(r, f"temp_lead{n}", st["temp"])
                    setattr(r, f"rain_lead{n}", st["rain"])
                    n_filled += 1
            sleep(NET_SLEEP_S)
    return n_filled


# ---- results ----------------------------------------------------------------------------------

def parse_cfbd_scores(payload: Iterable[Mapping[str, Any]], resolve: Callable[[str], Optional[str]]) -> dict[tuple[str, str], tuple[int, int]]:
    """``{(home_id, away_id): (home_pts, away_pts)}`` from CFBD ``/games`` (camel or snake case)."""
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for g in payload or []:
        hp = g.get("homePoints", g.get("home_points"))
        ap = g.get("awayPoints", g.get("away_points"))
        if hp is None or ap is None:
            continue
        h = resolve(str(g.get("homeTeam") or g.get("home_team") or ""))
        a = resolve(str(g.get("awayTeam") or g.get("away_team") or ""))
        if h and a:
            out[(h, a)] = (int(hp), int(ap))
    return out


def parse_espn_scores(payload: Mapping[str, Any], resolve: Callable[[Mapping[str, Any]], Optional[str]]) -> dict[tuple[str, str], tuple[int, int]]:
    """``{(home_id, away_id): (home, away)}`` for completed events of an ESPN scoreboard."""
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for ev in payload.get("events") or []:
        for comp in ev.get("competitions") or []:
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            home = away = None
            for c in comp.get("competitors") or []:
                if c.get("homeAway") == "home":
                    home = c
                elif c.get("homeAway") == "away":
                    away = c
            if not home or not away:
                continue
            h, a = resolve(home.get("team") or {}), resolve(away.get("team") or {})
            hs, as_ = _num(home.get("score")), _num(away.get("score"))
            if h and a and hs is not None and as_ is not None:
                out[(h, a)] = (int(hs), int(as_))
    return out


def parse_nflverse_scores(csv_text: str, season: Optional[int] = None) -> dict[tuple[str, str, int, int], tuple[int, int]]:
    """``{(home, away, season, week): (home_score, away_score)}`` from nflverse games.csv (result/total agree)."""
    import csv
    import io

    from pipeline.schedule.nfl import _week

    out: dict[tuple[str, str, int, int], tuple[int, int]] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        hs, as_ = _num(row.get("home_score")), _num(row.get("away_score"))
        if hs is None or as_ is None:
            continue
        try:
            yr = int(float(row.get("season") or 0))
        except ValueError:
            continue
        if season is not None and yr != int(season):
            continue
        out[((row.get("home_team") or "").lower(), (row.get("away_team") or "").lower(), yr, _week(row))] = (int(hs), int(as_))
    return out


def apply_scores(rows: Iterable[GameRow], scores: Mapping[tuple, tuple[int, int]], source: str, *, keyed_by_week: bool = False) -> int:
    n = 0
    for r in rows:
        if r.home_score is not None:
            continue
        key = (r.home_id, r.away_id, r.season, r.week) if keyed_by_week else (r.home_id, r.away_id)
        sc = scores.get(key)
        if sc is None:
            continue
        r.home_score, r.away_score, r.src_result = sc[0], sc[1], source
        n += 1
    return n


def fetch_results(rows: Sequence[GameRow], book: Any = None, *, get: Callable[[str, Mapping[str, str]], Any] = _get_json,
                  cfbd_key: Optional[str] = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """CFB: CFBD /games per (season, week) when ``CFBD_API_KEY`` is set, else ESPN
    scoreboard; NFL: nflverse games.csv. ``book`` = StadiumBook for team resolution."""
    import httpx

    counts = {"cfbd": 0, "espn": 0, "nflverse": 0}
    cfb = [r for r in rows if r.sport == "cfb" and r.home_score is None]
    nfl = [r for r in rows if r.sport == "nfl" and r.home_score is None]

    def resolve_name(sport: str, name: str) -> Optional[str]:
        if book is not None:
            return book.resolve_team(sport, name, fuzzy=True)
        from pipeline.stadiums.loader import slug
        return slug(name) if name else None

    def resolve_espn(team: Mapping[str, Any]) -> Optional[str]:
        from pipeline.schedule.espn import _team_id
        return _team_id("cfb", dict(team), book)

    if cfb:
        weeks = sorted({(r.season, r.week) for r in cfb if r.season is not None and r.week is not None})
        key = cfbd_key or os.environ.get("CFBD_API_KEY")
        for season, week in weeks:
            grp = [r for r in cfb if (r.season, r.week) == (season, week)]
            if key:
                try:
                    stype, wk = ("postseason", week - 15) if week > 15 else ("regular", week)
                    payload = httpx.get(f"{CFBD_BASE}/games", params={"year": season, "week": wk, "seasonType": stype},
                                        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=30.0).json()
                    counts["cfbd"] += apply_scores(grp, parse_cfbd_scores(payload, lambda n: resolve_name("cfb", n)), "cfbd")
                    sleep(NET_SLEEP_S)
                    continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                stype, wk = (3, week - 15) if week > 15 else (2, week)
                payload = get(ESPN_SCOREBOARD.format(league="college-football"),
                              {"dates": str(season), "week": str(wk), "seasontype": str(stype), "groups": "80", "limit": "400"})
                counts["espn"] += apply_scores(grp, parse_espn_scores(payload, resolve_espn), "espn")
            except Exception:  # noqa: BLE001
                pass
            sleep(NET_SLEEP_S)
    if nfl:
        try:
            text = httpx.get(NFLVERSE_GAMES_URL, timeout=60.0, headers={"User-Agent": USER_AGENT}).text
            counts["nflverse"] += apply_scores(nfl, parse_nflverse_scores(text), "nflverse", keyed_by_week=True)
        except Exception:  # noqa: BLE001
            pass
    return counts


# ---- aggregation -------------------------------------------------------------------------------

def _stats(rows: Sequence[GameRow]) -> dict[str, Any]:
    graded = [r for r in rows if r.under_result is not None]
    w = sum(1 for r in graded if r.under_result == "W")
    losses = sum(1 for r in graded if r.under_result == "L")
    p = sum(1 for r in graded if r.under_result == "P")
    n = len(graded)
    margins = [r.margin for r in graded if r.margin is not None]
    pos = sum(1 for r in graded if r.clv_status == clv_mod.POSITIVE)
    return {
        "Wins": w, "Losses": losses, "Push": p, "Sample": n,
        "Margin": _round(sum(margins) / len(margins), 2) if margins else None,
        "ROI": _round((w * WIN_PAYOUT - losses) / n, 3) if n else None,
        "+ CLV": pos if n else None, "CLV %": _round(pos / n, 4) if n else None,
    }


def bucket_inputs(r: GameRow, on: str = "forecast") -> tuple[Optional[float], Optional[float]]:
    """(temp, wind) used for bucket assignment: the closing forecast (what the bettor
    saw; default) or the HRRR actuals."""
    if on == "actual":
        return r.temp_act, r.wind_act
    return r.temp_fc, r.wind_fc


def grid_stats(rows: Sequence[GameRow], defs: Sequence[Bucket], on: str = "forecast") -> list[dict[str, Any]]:
    """One record per legacy bucket (sheet order, ids preserved) with the legacy
    columns recomputed from ``rows`` plus ``legacy`` (the sheet's numbers) and ``n_games``."""
    out = []
    for b in defs:
        members = [] if b.is_separator else [
            r for r in rows if bucket_matches(b, r.sport, *bucket_inputs(r, on), r.spread_abs, r.clv_status)
        ]
        rec = {"id": b.id, "sport": b.sport, **b.legacy_columns(), **_stats(members), "n_games": len(members), "legacy": b.legacy}
        out.append(rec)
    return out


def stadium_results(rows: Sequence[GameRow], now: Optional[str] = None) -> list[dict[str, Any]]:
    """Legacy Stadiums sheet (Team / Stadium / Record 'W-L-P' / Percentage) per
    (stadium, sport, season) → also the D1 ``stadium_results`` rows."""
    groups: dict[tuple[str, str, Optional[int]], list[GameRow]] = defaultdict(list)
    for r in rows:
        if r.stadium_id and r.under_result is not None:
            groups[(r.stadium_id, r.sport, r.season)].append(r)
    out = []
    for (sid, sport, season), members in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2] or 0)):
        st = _stats(members)
        home_teams = sorted({r.home_name or r.home_id or "" for r in members if not r.neutral})
        out.append({
            "stadium_id": sid, "sport": sport, "season": season, "Team": ", ".join(t for t in home_teams if t) or None,
            "Stadium": next((r.stadium_name for r in members if r.stadium_name), sid),
            "Record": f"{st['Wins']}-{st['Losses']}-{st['Push']}", "Percentage": st["ROI"],
            "under_w": st["Wins"], "under_l": st["Losses"], "under_p": st["Push"], "roi": st["ROI"], "n": st["Sample"],
            "updated_at": now,
        })
    return out


def alerts_clv(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """CLV per settled EDGE alert grouped by tier / league / book / model (v1 vs v2)."""
    recs = [r for r in records if isinstance(r, dict) and r.get("family", "edge") == "edge" and _num(r.get("clv_pts")) is not None]

    def group(keyfn: Callable[[Mapping[str, Any]], str]) -> list[dict[str, Any]]:
        acc: dict[str, list[float]] = defaultdict(list)
        for r in recs:
            acc[keyfn(r)].append(float(r["clv_pts"]))
        rows = []
        for k, xs in sorted(acc.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            rows.append({"key": k, "n": len(xs), "avg_clv": _round(sum(xs) / len(xs), 3), "sum_clv": _round(sum(xs), 2),
                         "pos": sum(1 for x in xs if x > 0), "pos_frac": _round(sum(1 for x in xs if x > 0) / len(xs), 3)})
        return rows

    return {
        "n": len(recs),
        "by_tier": group(lambda r: str(r.get("tier"))),
        "by_league": group(lambda r: str(r.get("sport")).upper()),
        "by_book": group(lambda r: str(r.get("book"))),
        "by_model": group(lambda r: str(r.get("model_version") or "v1")),
        "by_market": group(lambda r: str(r.get("market"))),
        "alerts": [{k: r.get(k) for k in ("alert_key", "sport", "season", "week", "game_id", "market", "side", "book", "tier",
                                          "model_version", "first_line", "first_odds", "closing_line", "clv_pts", "first_sent_at")}
                   for r in sorted(recs, key=lambda r: float(r["clv_pts"]), reverse=True)],
    }


def matched_games(rows: Sequence[GameRow], defs: Sequence[Bucket], grid: Sequence[Mapping[str, Any]], on: str = "forecast") -> list[dict[str, Any]]:
    """Legacy bottom table: each game with its first-match bucket id + that bucket's Sample/Margin/ROI."""
    by_id = {g["id"]: g for g in grid}
    out = []
    for r in rows:
        b = first_match(defs, r.sport, *bucket_inputs(r, on), r.spread_abs, r.clv_status)
        d = r.to_dict()
        d["Signal"] = b.id if b else None
        g = by_id.get(b.id) if b else None
        d["Sample"], d["Margin"], d["ROI"] = (g["Sample"], g["Margin"], g["ROI"]) if g else (None, None, None)
        out.append(d)
    return out


# ---- assembly ------------------------------------------------------------------------------------

@dataclass
class BacktestResult:
    rows: list[GameRow]
    grid: list[dict[str, Any]]
    stadiums: list[dict[str, Any]]
    alerts: dict[str, Any]
    games: list[dict[str, Any]]
    sources: dict[str, Any]

    def payload(self, *, now: Optional[datetime] = None, run_id: Optional[str] = None, on: str = "forecast") -> dict[str, Any]:
        ts = utc_iso(now or now_utc())
        return json_out.sanitize({
            "meta": {"run_id": run_id or f"backtest-{ts}", "last_updated": ts, "generated_at": ts, "bucket_on": on,
                     "n_games": len(self.rows), "n_graded": sum(1 for r in self.rows if r.under_result is not None),
                     "sources": self.sources},
            "grid": self.grid, "stadium_results": self.stadiums, "alerts_clv": self.alerts, "games": self.games,
        })


def build_rows(
    *,
    snapshots: Optional[Mapping[str, Sequence[tuple[datetime, Mapping[str, Any]]]]] = None,
    d1: Optional[D1Data] = None,
    closings_state: Optional[Mapping[str, Any]] = None,
    sport: Optional[str] = None,
    season: Optional[int] = None,
    now: Optional[datetime] = None,
) -> list[GameRow]:
    """Per-game rows from snapshots ⊕ D1 (snapshot fields fill what D1 lacks; D1
    closings override consensus). Only games that have kicked off by ``now``."""
    now = ensure_utc(now or now_utc())
    d1 = d1 or D1Data()
    rows = rows_from_games(d1.games, d1.stadiums, d1.teams)
    for gid, snaps in (snapshots or {}).items():
        sr = row_from_snapshots(gid, snaps)
        if sr is None:
            continue
        cur = rows.get(gid)
        if cur is None:
            rows[gid] = sr
            continue
        for f in ROW_FIELDS:
            if getattr(cur, f) in (None, False, "") and getattr(sr, f) not in (None, ""):
                setattr(cur, f, getattr(sr, f))
    closings: list[Mapping[str, Any]] = list(d1.closings)
    if closings_state:
        closings += clv_mod.closing_rows(closings_state)
    apply_openers(rows, d1.odds_history)
    apply_closings(rows, closings)
    out = []
    for r in rows.values():
        if sport and r.sport != sport:
            continue
        if season is not None and r.season != season:
            continue
        kick = _dt(r.kickoff_utc)
        if kick is None or kick > now:
            continue
        out.append(finalize_row(r))
    out.sort(key=lambda r: (r.kickoff_utc or "", r.game_id))
    return out


def assemble(rows: Sequence[GameRow], defs: Sequence[Bucket], alert_records: Iterable[Mapping[str, Any]] = (),
             sources: Optional[dict[str, Any]] = None, on: str = "forecast", now: Optional[str] = None) -> BacktestResult:
    rows = [finalize_row(r) for r in rows]
    grid = grid_stats(rows, defs, on)
    return BacktestResult(list(rows), grid, stadium_results(rows, now), alerts_clv(alert_records),
                          matched_games(rows, defs, grid, on), sources or {})


def write_parquet(res: BacktestResult, out_dir: PathLike) -> dict[str, Path]:
    import pandas as pd

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "games": pd.DataFrame([r.to_dict() for r in res.rows], columns=[*ROW_FIELDS, "spread_abs"]),
        "grid": pd.DataFrame([{k: v for k, v in g.items() if k != "legacy"} for g in res.grid]),
        "stadium_results": pd.DataFrame(res.stadiums),
        "alerts_clv": pd.DataFrame(res.alerts.get("alerts") or []),
    }
    written = {}
    for name, df in tables.items():
        p = out / f"{name}.parquet"
        df.to_parquet(p, index=False)
        written[name] = p
    return written


def write_outputs(res: BacktestResult, *, board_dir: PathLike, parquet_dir: Optional[PathLike] = None,
                  now: Optional[datetime] = None, run_id: Optional[str] = None, on: str = "forecast") -> dict[str, Path]:
    board = Path(board_dir)
    board.mkdir(parents=True, exist_ok=True)
    written = {f"{json_out.BOARD_DIRNAME}/{json_out.BACKTEST_FILE}": json_out.dump_json(board / json_out.BACKTEST_FILE, res.payload(now=now, run_id=run_id, on=on))}
    if parquet_dir is not None:
        for name, p in write_parquet(res, parquet_dir).items():
            written[f"backtest/{name}.parquet"] = p
    return written


def d1_statements(res: BacktestResult, new_closings: Sequence[dict[str, Any]] = ()) -> list[str]:
    return d1_out.build_statements(closings=list(new_closings),
                                   stadium_results=[{c: s.get(c) for c in d1_out.STADIUM_RESULT_COLS} for s in res.stadiums])


# ---- CLI ---------------------------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.backtest", description=__doc__.split("\n\n")[0])
    p.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--export-dir", type=Path, default=None, help="wrangler d1 export / /api JSON per table")
    p.add_argument("--sqlite", type=Path, default=None, help="SQLite db or d1_inserts.sql (replayed on the migrations)")
    p.add_argument("--board-dir", type=Path, default=DEFAULT_BOARD_DIR)
    p.add_argument("--parquet-dir", type=Path, default=DEFAULT_PARQUET_DIR)
    p.add_argument("--d1-sql", type=Path, default=None, help="write closings/stadium_results SQL here")
    p.add_argument("--grid", type=Path, default=GRID_FIXTURE, help="legacy xlsx with the bucket definitions")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--sport", choices=("nfl", "cfb"), default=None)
    p.add_argument("--bucket-on", choices=("forecast", "actual"), default="forecast")
    p.add_argument("--no-network", action="store_true", help="skip Open-Meteo / results fetches")
    p.add_argument("--freeze", action="store_true", help="freeze closings from state history.json first (model/clv.py)")
    p.add_argument("--now", default=None, help="ISO override of the clock (tests)")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_repo_dotenv()
    args = parse_args(argv)
    now = _dt(args.now) or now_utc()
    defs = load_grid_defs(args.grid)
    print(f"backtest: {len(defs)} buckets from {args.grid}")

    d1 = D1Data()
    if args.export_dir:
        d1 = load_export_dir(args.export_dir)
    elif args.sqlite:
        d1 = load_sqlite(args.sqlite)
    snaps = load_snapshots(args.snapshot_dir, args.sport, args.season)
    print(f"  inputs: {len(snaps)} games in snapshots, d1 games={len(d1.games)} odds={len(d1.odds_history)} "
          f"closings={len(d1.closings)} alerts={len(d1.alerts)}")

    new_closing_rows: list[dict[str, Any]] = []
    closings_state = clv_mod.load_closings(args.state_dir)
    if args.freeze:
        cards = [c for seq in snaps.values() for _, c in seq[-1:]]
        cards += d1.games
        res_clv = clv_mod.run_clv_stage(args.state_dir, cards, now, run_id=f"backtest-{utc_iso(now)}")
        closings_state = res_clv.store
        new_closing_rows = res_clv.new_rows
        print(f"  clv: froze {len(res_clv.frozen)} keys ({len(res_clv.new)} new), settled {len(res_clv.settled)} alerts")

    rows = build_rows(snapshots=snaps, d1=d1, closings_state=closings_state, sport=args.sport, season=args.season, now=now)
    sources: dict[str, Any] = {"snapshots": len(snaps), "d1_games": len(d1.games), "network": not args.no_network}
    if rows and not args.no_network:
        book = None
        try:
            from pipeline.stadiums.loader import load_stadium_book
            book = load_stadium_book()
        except Exception:  # noqa: BLE001
            pass
        sources["actuals"] = fetch_actuals(rows)
        sources["previous_runs"] = fetch_previous_runs(rows)
        sources["results"] = fetch_results(rows, book)
        print(f"  network: actuals={sources['actuals']} previous_runs={sources['previous_runs']} results={sources['results']}")

    alerts_state, _ = pstate.load_alerts_rehydrated(args.state_dir)
    records = list((alerts_state.get("records") or {}).values()) + list(d1.alerts)
    res = assemble(rows, defs, records, sources, on=args.bucket_on, now=utc_iso(now))
    written = write_outputs(res, board_dir=args.board_dir, parquet_dir=args.parquet_dir, now=now, on=args.bucket_on)
    graded = sum(1 for r in rows if r.under_result is not None)
    print(f"  rows: {len(rows)} games ({graded} graded); buckets with samples: {sum(1 for g in res.grid if g['Sample'])}; "
          f"stadiums: {len(res.stadiums)}; settled alerts: {res.alerts['n']}")
    if args.d1_sql:
        stmts = d1_statements(res, new_closing_rows)
        d1_out.write_sql(args.d1_sql, stmts)
        print(f"  d1: {len(stmts)} statement(s) -> {args.d1_sql}")
    for k, p in written.items():
        print(f"  wrote {k} -> {p}")
    return 0


__all__ = [
    "GRID_FIXTURE", "LEADS", "SPORT_LABEL", "Bucket", "GameRow", "D1Data", "BacktestResult",
    "bucket_from_row", "load_grid_defs", "load_stadium_sheet", "bucket_matches", "first_match",
    "grade_under", "finalize_row", "load_snapshots", "row_from_snapshots", "load_export_dir", "load_sqlite",
    "rows_from_games", "apply_closings", "apply_openers", "hourly_series", "window_stats", "fetch_actuals",
    "fetch_previous_runs", "parse_cfbd_scores", "parse_espn_scores", "parse_nflverse_scores", "apply_scores",
    "fetch_results", "grid_stats", "stadium_results", "alerts_clv", "matched_games", "build_rows", "assemble",
    "write_parquet", "write_outputs", "d1_statements", "parse_args", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
