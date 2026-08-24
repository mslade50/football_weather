"""Column-exact legacy writers: ``nfl_weather.csv`` and ``cfb_weather.xlsx`` (AUDIT §4.1/4.2).

The Streamlit pages read these files by column name, so the column lists below
are the contract and are pinned by ``tests/test_legacy_columns.py`` against the
files committed in the repo root.

Formats reproduced (AUDIT §3/§4):
- Date ``'SUN 11/09'``, Time ``'01:00 PM'`` (local kickoff), Timestamp naive ET ISO.
- NFL Game ``'away vs home'`` lowercase city names; CFB Game ``'Away @ Home'``
  CFBD School names; ``Other`` sheet Game ``'Away vs Home'`` + split team columns.
- game_loc ``'lat, lon'``.
- NFL gs_fg/away_fg are FRACTIONS (-0.035); CFB are PERCENT (-3.5) rounded 2dp.
- NFL weather floats unrounded; CFB wind 1dp, temp 2dp; wind_diff 1dp.
- NFL travel_alt/year_built ints; CFB travel_alt float.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Union

import pandas as pd

from utils.timeutil import date_label, naive_et_iso, time_label

NFL_COLUMNS: list[str] = [
    "Game", "Date", "Time", "stadium", "avg_wind", "wind_vol", "orient", "wind_impact",
    "weakest_wind_effect", "game_loc", "travel_alt", "home_temp", "away_temp", "year_built",
    "wind_dir_1h", "wind_dir_2h", "temp_fg", "wind_fg", "wind_dir_fg", "rain_fg", "gs_fg", "away_fg",
    "Spread_now", "Odds_now", "Total_now", "Under_now",
    "Spread_open", "Odds_open", "Total_open", "Under_open", "Timestamp",
]

CFB_FBS_COLUMNS: list[str] = [
    "Game", "Date", "Time", "wind_vol", "orient", "wind_impact", "weakest_wind_effect",
    "travel_alt", "home_temp", "away_temp", "wind_avg", "year_built",
    "wind_dir_1h", "wind_dir_2h", "temp_fg", "wind_fg", "wind_dir_fg", "rain_fg", "gs_fg", "away_fg",
    "wind_diff", "game_loc",
    "Fd_open", "Odds_o", "FD_now", "Odds_n", "Open", "Current", "Spread", "Total_proj",
    "Move_t", "Move_s", "My_total", "Edge", "My_spread", "Edge_s", "Timestamp",
]

CFB_OTHER_COLUMNS: list[str] = [
    "Game", "Home Team", "Away Team", "Date", "Time", "wind_vol", "orient", "wind_impact",
    "weakest_wind_effect", "travel_alt", "home_temp", "away_temp", "wind_avg", "year_built",
    "wind_dir_1h", "wind_dir_2h", "temp_fg", "wind_fg", "wind_dir_fg", "rain_fg", "gs_fg", "away_fg",
    "wind_diff", "game_loc",
]

CFB_SHEET_FBS = "FBS"
CFB_SHEET_OTHER = "Other"

NFL_FILENAME = "nfl_weather.csv"
CFB_FILENAME = "cfb_weather.xlsx"

PathLike = Union[str, Path]


@dataclass
class LegacyRecord:
    """Sport-agnostic per-game record assembled by ``pipeline.build`` for the writers.

    Impact values are in PERCENT (model convention); the writers scale/round per sport.
    ``odds`` keys (all optional):
      NFL: spread_now odds_now total_now under_now spread_open odds_open total_open under_open
      CFB: fd_open odds_o fd_now odds_n open current spread total_proj
    """

    sport: str
    away_name: str
    home_name: str
    kickoff_local: datetime
    stadium_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    avg_wind: float | None = None
    avg_wind_month: float | None = None
    wind_vol: str | None = None
    orient: str | None = None
    wind_impact: str | None = None
    weakest_wind_effect: str | None = None
    travel_alt: float | None = None
    home_temp: float | None = None
    away_temp: float | None = None
    year_built: int | None = None
    wind_dir_1h: str | None = None
    wind_dir_2h: str | None = None
    temp_fg: float | None = None
    wind_fg: float | None = None
    wind_dir_fg: str | None = None
    rain_fg: float | None = None
    gs_fg_pct: float | None = None
    away_fg_pct: float | None = None
    odds: dict[str, Any] = field(default_factory=dict)
    is_fbs: bool = True
    game_id: str | None = None


# ---- formatting helpers ---------------------------------------------------------

def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _nan(x: Any) -> Any:
    """None -> NaN so pandas writes an empty cell rather than 'None'."""
    return math.nan if x is None else x


def _round(x: Any, nd: int) -> Any:
    v = _num(x)
    return math.nan if v is None else round(v, nd)


def _int(x: Any) -> Any:
    v = _num(x)
    return math.nan if v is None else int(round(v))


def format_game_loc(lat: float | None, lon: float | None) -> Any:
    la, lo = _num(lat), _num(lon)
    if la is None or lo is None:
        return math.nan
    return f"{la}, {lo}"


def nfl_game_label(away: str, home: str) -> str:
    return f"{away.strip().lower()} vs {home.strip().lower()}"


def cfb_game_label(away: str, home: str) -> str:
    return f"{away.strip()} @ {home.strip()}"


def cfb_other_game_label(away: str, home: str) -> str:
    return f"{away.strip()} vs {home.strip()}"


def _move_t(fd_now: Any, fd_open: Any) -> Any:
    n, o = _num(fd_now), _num(fd_open)
    if n is None or o is None or o == 0:
        return math.nan
    return (n - o) / o


def _move_s(open_: Any, current: Any) -> Any:
    o, c = _num(open_), _num(current)
    if o is None or c is None:
        return math.nan
    return o - c


def _cfb_derived(rec: LegacyRecord) -> dict[str, Any]:
    """AUDIT §5 legacy derived columns (only when a projection feed is present)."""
    total_proj = _num(rec.odds.get("total_proj"))
    spread = _num(rec.odds.get("spread"))
    fd_now = _num(rec.odds.get("fd_now"))
    gs = _num(rec.gs_fg_pct)
    away = _num(rec.away_fg_pct)
    out: dict[str, Any] = {"My_total": math.nan, "Edge": math.nan, "My_spread": math.nan, "Edge_s": math.nan}
    if total_proj is not None and gs is not None:
        my_total = total_proj * (1 + gs / 100.0)
        out["My_total"] = my_total
        if fd_now is not None and my_total != 0:
            out["Edge"] = (fd_now - my_total) / my_total
    if spread is not None and away is not None:
        my_spread = spread * (1 + away / 100.0)
        out["My_spread"] = my_spread
        out["Edge_s"] = spread - my_spread
    return out


# ---- row builders ----------------------------------------------------------------

def nfl_row(rec: LegacyRecord, timestamp: str) -> dict[str, Any]:
    o = rec.odds
    gs = _num(rec.gs_fg_pct)
    aw = _num(rec.away_fg_pct)
    return {
        "Game": nfl_game_label(rec.away_name, rec.home_name),
        "Date": date_label(rec.kickoff_local),
        "Time": time_label(rec.kickoff_local),
        "stadium": _nan(rec.stadium_name),
        "avg_wind": _nan(_num(rec.avg_wind)),
        "wind_vol": _nan(rec.wind_vol),
        "orient": _nan(rec.orient),
        "wind_impact": _nan(rec.wind_impact),
        "weakest_wind_effect": _nan(rec.weakest_wind_effect),
        "game_loc": format_game_loc(rec.lat, rec.lon),
        "travel_alt": _int(rec.travel_alt),
        "home_temp": _nan(_num(rec.home_temp)),
        "away_temp": _nan(_num(rec.away_temp)),
        "year_built": _int(rec.year_built),
        "wind_dir_1h": _nan(rec.wind_dir_1h),
        "wind_dir_2h": _nan(rec.wind_dir_2h),
        "temp_fg": _nan(_num(rec.temp_fg)),
        "wind_fg": _nan(_num(rec.wind_fg)),
        "wind_dir_fg": _nan(rec.wind_dir_fg),
        "rain_fg": _nan(_num(rec.rain_fg)),
        "gs_fg": math.nan if gs is None else gs / 100.0 + 0.0,
        "away_fg": math.nan if aw is None else aw / 100.0 + 0.0,
        "Spread_now": _nan(_num(o.get("spread_now"))),
        "Odds_now": _int(o.get("odds_now")),
        "Total_now": _nan(_num(o.get("total_now"))),
        "Under_now": _int(o.get("under_now")),
        "Spread_open": _nan(_num(o.get("spread_open"))),
        "Odds_open": _int(o.get("odds_open")),
        "Total_open": _nan(_num(o.get("total_open"))),
        "Under_open": _int(o.get("under_open")),
        "Timestamp": timestamp,
    }


def _cfb_weather_cols(rec: LegacyRecord) -> dict[str, Any]:
    wind_fg = _round(rec.wind_fg, 1)
    wind_avg = _num(rec.avg_wind_month)
    wind_diff = math.nan
    if wind_avg is not None and _num(wind_fg) is not None:
        wind_diff = round(float(wind_fg) - wind_avg, 1)
    return {
        "wind_vol": _nan(rec.wind_vol),
        "orient": _nan(rec.orient),
        "wind_impact": _nan(rec.wind_impact),
        "weakest_wind_effect": _nan(rec.weakest_wind_effect),
        "travel_alt": _nan(_num(rec.travel_alt)),
        "home_temp": _nan(_num(rec.home_temp)),
        "away_temp": _nan(_num(rec.away_temp)),
        "wind_avg": _nan(wind_avg),
        "year_built": _int(rec.year_built),
        "wind_dir_1h": _nan(rec.wind_dir_1h),
        "wind_dir_2h": _nan(rec.wind_dir_2h),
        "temp_fg": _round(rec.temp_fg, 2),
        "wind_fg": wind_fg,
        "wind_dir_fg": _nan(rec.wind_dir_fg),
        "rain_fg": _nan(_num(rec.rain_fg)),
        "gs_fg": _round(rec.gs_fg_pct, 2),
        "away_fg": _round(rec.away_fg_pct, 2),
        "wind_diff": wind_diff,
        "game_loc": format_game_loc(rec.lat, rec.lon),
    }


def cfb_fbs_row(rec: LegacyRecord, timestamp: str) -> dict[str, Any]:
    o = rec.odds
    row: dict[str, Any] = {
        "Game": cfb_game_label(rec.away_name, rec.home_name),
        "Date": date_label(rec.kickoff_local),
        "Time": time_label(rec.kickoff_local),
    }
    row.update(_cfb_weather_cols(rec))
    row.update({
        "Fd_open": _nan(_num(o.get("fd_open"))),
        "Odds_o": _nan(_num(o.get("odds_o"))),
        "FD_now": _nan(_num(o.get("fd_now"))),
        "Odds_n": _nan(_num(o.get("odds_n"))),
        "Open": _nan(_num(o.get("open"))),
        "Current": _nan(_num(o.get("current"))),
        "Spread": _nan(_num(o.get("spread"))),
        "Total_proj": _nan(_num(o.get("total_proj"))),
        "Move_t": _move_t(o.get("fd_now"), o.get("fd_open")),
        "Move_s": _move_s(o.get("open"), o.get("current")),
    })
    row.update(_cfb_derived(rec))
    row["Timestamp"] = timestamp
    return {c: row[c] for c in CFB_FBS_COLUMNS}


def cfb_other_row(rec: LegacyRecord) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Game": cfb_other_game_label(rec.away_name, rec.home_name),
        "Home Team": rec.home_name,
        "Away Team": rec.away_name,
        "Date": date_label(rec.kickoff_local),
        "Time": time_label(rec.kickoff_local),
    }
    row.update(_cfb_weather_cols(rec))
    return {c: row[c] for c in CFB_OTHER_COLUMNS}


# ---- frames + writers ------------------------------------------------------------

def _sorted(records: Iterable[LegacyRecord]) -> list[LegacyRecord]:
    return sorted(records, key=lambda r: (r.kickoff_local.replace(tzinfo=None), r.home_name))


def nfl_frame(records: Iterable[LegacyRecord], timestamp: str | None = None) -> pd.DataFrame:
    ts = timestamp or naive_et_iso()
    rows = [nfl_row(r, ts) for r in _sorted(records)]
    return pd.DataFrame(rows, columns=NFL_COLUMNS)


def cfb_frames(records: Iterable[LegacyRecord], timestamp: str | None = None) -> dict[str, pd.DataFrame]:
    ts = timestamp or naive_et_iso()
    recs = _sorted(records)
    fbs = [cfb_fbs_row(r, ts) for r in recs if r.is_fbs]
    other = [cfb_other_row(r) for r in recs if not r.is_fbs]
    return {
        CFB_SHEET_FBS: pd.DataFrame(fbs, columns=CFB_FBS_COLUMNS),
        CFB_SHEET_OTHER: pd.DataFrame(other, columns=CFB_OTHER_COLUMNS),
    }


def write_nfl_csv(records: Sequence[LegacyRecord], path: PathLike, timestamp: str | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    nfl_frame(records, timestamp).to_csv(out, index=False)
    return out


def write_cfb_xlsx(records: Sequence[LegacyRecord], path: PathLike, timestamp: str | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = cfb_frames(records, timestamp)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for sheet in (CFB_SHEET_FBS, CFB_SHEET_OTHER):
            frames[sheet].to_excel(xw, sheet_name=sheet, index=False)
    return out


def write_legacy(sport: str, records: Sequence[LegacyRecord], out_dir: PathLike, timestamp: str | None = None) -> Path:
    d = Path(out_dir)
    if sport == "nfl":
        return write_nfl_csv(records, d / NFL_FILENAME, timestamp)
    if sport == "cfb":
        return write_cfb_xlsx(records, d / CFB_FILENAME, timestamp)
    raise ValueError(f"unknown sport {sport!r}")


__all__ = [
    "NFL_COLUMNS",
    "CFB_FBS_COLUMNS",
    "CFB_OTHER_COLUMNS",
    "CFB_SHEET_FBS",
    "CFB_SHEET_OTHER",
    "NFL_FILENAME",
    "CFB_FILENAME",
    "LegacyRecord",
    "format_game_loc",
    "nfl_game_label",
    "cfb_game_label",
    "cfb_other_game_label",
    "nfl_row",
    "cfb_fbs_row",
    "cfb_other_row",
    "nfl_frame",
    "cfb_frames",
    "write_nfl_csv",
    "write_cfb_xlsx",
    "write_legacy",
]
