"""Lead-weighted climatology shrinkage for the kickoff-window forecast (ARCH §6).

``blend(forecast, climo, lead_hours, kind)`` = w(lead)·forecast + (1−w)·climo where
w(lead) is a per-variable curve read from ``data/calibration.json``
``forecast_blend.weights`` (kinds ``wind`` / ``temp`` / ``rain_prob``; gusts use the
wind curve). Defaults until fitted: w = 1.0 for lead ≤ 48 h, linear to the floor at
168 h, hold beyond (floors wind 0.45 / temp 0.7 / rain_prob 0.5). A fitted curve may
carry ``points`` ``[[lead_h, w], ...]`` beyond ``full_h`` which are interpolated
piecewise-linearly (``scripts/fit_forecast_blend.py`` writes them).

The climatological base rate is the per stadium × ISO-week × time-of-day cell from
``data/climatology.csv`` (``stadiums/climatology.py`` builds it from ERA5 hourly).
"Local" time is mean solar time from the longitude (``round(lon / 15)`` h), the same
rule on both the build and the lookup side, so no tz database is needed.

The same block also carries ``medium_range_weights`` (AIFS / IFS / GFS member
weights for leads beyond NBM's range) and ``medium_range_start_h``.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CALIBRATION_PATH = DATA_DIR / "calibration.json"
CLIMATOLOGY_PATH = DATA_DIR / "climatology.csv"

KINDS = ("wind", "temp", "rain_prob")
DEFAULT_FULL_H = 48.0
DEFAULT_FLOOR_H = 168.0
DEFAULT_FLOORS: dict[str, float] = {"wind": 0.45, "temp": 0.7, "rain_prob": 0.5}
DEFAULT_MEDIUM_WEIGHTS: dict[str, float] = {"aifs": 0.4, "ifs": 0.35, "gfs": 0.25}
DEFAULT_MEDIUM_START_H = 168.0

SEASON_MONTHS = (8, 9, 10, 11, 12, 1)
TOD_BIN_HOURS = 6
N_TOD_BINS = 24 // TOD_BIN_HOURS
MATCH_TOL_DEG = 0.3
RAIN_MM = 1.0


# ---------------------------------------------------------------- curves / config


@dataclass(frozen=True)
class Curve:
    """w(lead): 1.0 up to ``full_h``; then linear to ``floor`` at ``floor_h`` (or through
    ``points`` when fitted); constant beyond the last knot."""

    full_h: float = DEFAULT_FULL_H
    floor_h: float = DEFAULT_FLOOR_H
    floor: float = 0.5
    points: tuple[tuple[float, float], ...] = ()

    def knots(self) -> list[tuple[float, float]]:
        pts = [(self.full_h, 1.0)]
        extra = sorted((float(x), _clamp01(w)) for x, w in self.points if float(x) > self.full_h)
        if extra:
            pts.extend(extra)
        else:
            pts.append((max(self.floor_h, self.full_h), _clamp01(self.floor)))
        return pts

    def weight(self, lead_hours: Optional[float]) -> float:
        if lead_hours is None or lead_hours <= self.full_h:
            return 1.0
        pts = self.knots()
        if lead_hours >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            if x0 <= lead_hours <= x1:
                return y1 if x1 <= x0 else y0 + (y1 - y0) * (lead_hours - x0) / (x1 - x0)
        return pts[-1][1]


@dataclass(frozen=True)
class BlendConfig:
    curves: Mapping[str, Curve]
    medium_weights: Mapping[str, float]
    medium_start_h: float = DEFAULT_MEDIUM_START_H
    origin: str = "defaults"

    def weight(self, lead_hours: Optional[float], kind: str) -> float:
        return self.curves.get(_curve_kind(kind), DEFAULT_CURVES[_curve_kind(kind)]).weight(lead_hours)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def _curve_kind(kind: str) -> str:
    return "wind" if kind == "gust" else kind


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


DEFAULT_CURVES: dict[str, Curve] = {k: Curve(floor=DEFAULT_FLOORS[k]) for k in KINDS}
DEFAULT_CONFIG = BlendConfig(curves=dict(DEFAULT_CURVES), medium_weights=dict(DEFAULT_MEDIUM_WEIGHTS))


def parse_curve(raw: Any, kind: str) -> Curve:
    base = DEFAULT_CURVES[kind]
    if not isinstance(raw, dict):
        return base
    pts: list[tuple[float, float]] = []
    for p in raw.get("points") or []:
        if isinstance(p, (list, tuple)) and len(p) == 2 and _num(p[0]) is not None and _num(p[1]) is not None:
            pts.append((float(p[0]), float(p[1])))
    return Curve(
        full_h=_num(raw.get("full_h")) if _num(raw.get("full_h")) is not None else base.full_h,
        floor_h=_num(raw.get("floor_h")) if _num(raw.get("floor_h")) is not None else base.floor_h,
        floor=_clamp01(_num(raw.get("floor"))) if _num(raw.get("floor")) is not None else base.floor,
        points=tuple(pts),
    )


def parse_blend_block(block: Any, origin: str = "calibration") -> BlendConfig:
    if not isinstance(block, dict):
        return DEFAULT_CONFIG
    weights = block.get("weights") if isinstance(block.get("weights"), dict) else {}
    curves = {k: parse_curve(weights.get(k), k) for k in KINDS}
    mw: dict[str, float] = {}
    raw_mw = block.get("medium_range_weights")
    if isinstance(raw_mw, dict):
        for k, v in raw_mw.items():
            f = _num(v)
            if f is not None and f >= 0.0:
                mw[str(k)] = f
    if sum(mw.values()) <= 0.0:
        mw = dict(DEFAULT_MEDIUM_WEIGHTS)
    start = _num(block.get("medium_range_start_h"))
    return BlendConfig(curves=curves, medium_weights=mw, medium_start_h=start if start is not None else DEFAULT_MEDIUM_START_H, origin=origin)


_cfg_cache: Optional[BlendConfig] = None


def load_blend_config(path: Path = CALIBRATION_PATH, use_cache: bool = True) -> BlendConfig:
    """``forecast_blend`` block of data/calibration.json (missing/invalid -> defaults)."""
    global _cfg_cache
    if use_cache and _cfg_cache is not None and path == CALIBRATION_PATH:
        return _cfg_cache
    cfg = DEFAULT_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg = parse_blend_block(data.get("forecast_blend"), origin=str(path.name))
    except (OSError, ValueError):
        pass
    if path == CALIBRATION_PATH:
        _cfg_cache = cfg
    return cfg


def weight(lead_hours: Optional[float], kind: str, cfg: Optional[BlendConfig] = None) -> float:
    return (cfg or load_blend_config()).weight(lead_hours, kind)


def blend(
    forecast_value: Optional[float],
    climo_mean: Optional[float],
    lead_hours: Optional[float],
    kind: str,
    cfg: Optional[BlendConfig] = None,
) -> Optional[float]:
    """w·forecast + (1−w)·climo. No forecast -> None (climatology never invents a forecast);
    no climatology -> the forecast unchanged (w treated as 1)."""
    if forecast_value is None:
        return None
    if climo_mean is None:
        return forecast_value
    w = weight(lead_hours, kind, cfg)
    out = w * float(forecast_value) + (1.0 - w) * float(climo_mean)
    if kind == "rain_prob":
        return _clamp01(out)
    if kind in ("wind", "gust"):
        return max(0.0, out)
    return out


# ---------------------------------------------------------------- local-time keys


def solar_offset_h(lon: float) -> int:
    return int(round(lon / 15.0))


def local_key(t_utc: datetime, lon: float) -> tuple[int, int, int]:
    """(month, iso_week, tod_bin) of ``t_utc`` in mean solar time at ``lon``."""
    loc = t_utc.astimezone(timezone.utc) + timedelta(hours=solar_offset_h(lon))
    return loc.month, loc.isocalendar()[1], loc.hour // TOD_BIN_HOURS


def in_season(month: int) -> bool:
    return month in SEASON_MONTHS


# ---------------------------------------------------------------- climatology table


CELL_FIELDS = (
    "wind_mean", "wind_p10", "wind_p50", "wind_p90", "gust_mean", "gust_p90",
    "temp_mean", "temp_p10", "temp_p50", "temp_p90", "rain_freq",
)


@dataclass(frozen=True)
class ClimoCell:
    stadium_id: str
    iso_week: int
    tod_bin: int
    n_hours: int = 0
    wind_mean: Optional[float] = None
    wind_p10: Optional[float] = None
    wind_p50: Optional[float] = None
    wind_p90: Optional[float] = None
    gust_mean: Optional[float] = None
    gust_p90: Optional[float] = None
    temp_mean: Optional[float] = None
    temp_p10: Optional[float] = None
    temp_p50: Optional[float] = None
    temp_p90: Optional[float] = None
    rain_freq: Optional[float] = None


def cell_from_row(row: Mapping[str, Any]) -> Optional[ClimoCell]:
    wk, tb = _num(row.get("iso_week")), _num(row.get("tod_bin"))
    sid = str(row.get("stadium_id") or "")
    if not sid or wk is None or tb is None:
        return None
    vals = {f: _num(row.get(f)) for f in CELL_FIELDS}
    return ClimoCell(stadium_id=sid, iso_week=int(wk), tod_bin=int(tb), n_hours=int(_num(row.get("n_hours")) or 0), **vals)


class ClimoTable:
    """Cells keyed by (stadium_id, iso_week, tod_bin) plus each stadium's coordinates for
    nearest-point lookup (Open-Meteo hands back grid-snapped coordinates, not ids)."""

    def __init__(self, cells: Iterable[ClimoCell] = (), points: Optional[Mapping[str, tuple[float, float]]] = None) -> None:
        self.cells: dict[tuple[str, int, int], ClimoCell] = {}
        self.points: dict[str, tuple[float, float]] = dict(points or {})
        self._ids: set[str] = set()
        for c in cells:
            self.add(c)

    def add(self, c: ClimoCell) -> None:
        self.cells[(c.stadium_id, c.iso_week, c.tod_bin)] = c
        self._ids.add(c.stadium_id)

    def __len__(self) -> int:
        return len(self.cells)

    def stadium_ids(self) -> set[str]:
        return set(self._ids)

    @classmethod
    def from_csv(cls, path: Path = CLIMATOLOGY_PATH) -> ClimoTable:
        table = cls()
        if not path.exists():
            return table
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sid = row.get("stadium_id") or ""
                la, lo = _num(row.get("lat")), _num(row.get("lon"))
                if sid and la is not None and lo is not None and sid not in table.points:
                    table.points[sid] = (la, lo)
                c = cell_from_row(row)
                if c is not None:
                    table.add(c)
        return table

    def nearest_stadium(self, lat: float, lon: float, tol: float = MATCH_TOL_DEG) -> Optional[str]:
        best: Optional[str] = None
        best_d = tol
        for sid, (la, lo) in self.points.items():
            if sid not in self._ids:
                continue
            d = max(abs(la - lat), abs(lo - lon))
            if d <= best_d:
                best, best_d = sid, d
        return best

    def lookup(
        self,
        when_utc: datetime,
        stadium_id: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> Optional[ClimoCell]:
        sid = stadium_id if stadium_id and stadium_id in self._ids else None
        if sid is None and lat is not None and lon is not None:
            sid = self.nearest_stadium(lat, lon)
        if sid is None:
            return None
        pt = self.points.get(sid)
        use_lon = pt[1] if pt is not None else (lon if lon is not None else 0.0)
        _month, wk, tb = local_key(when_utc, use_lon)
        return self.cells.get((sid, wk, tb))


_table_cache: Optional[ClimoTable] = None


def default_table(path: Path = CLIMATOLOGY_PATH, use_cache: bool = True) -> Optional[ClimoTable]:
    """data/climatology.csv cells (None when the file is missing or has no weekly cells)."""
    global _table_cache
    if use_cache and _table_cache is not None and path == CLIMATOLOGY_PATH:
        return _table_cache
    table = ClimoTable.from_csv(path)
    if path == CLIMATOLOGY_PATH:
        _table_cache = table
    return table if len(table) else None


__all__ = [
    "CALIBRATION_PATH",
    "CLIMATOLOGY_PATH",
    "KINDS",
    "DEFAULT_FLOORS",
    "DEFAULT_MEDIUM_WEIGHTS",
    "DEFAULT_MEDIUM_START_H",
    "DEFAULT_CONFIG",
    "SEASON_MONTHS",
    "TOD_BIN_HOURS",
    "N_TOD_BINS",
    "RAIN_MM",
    "CELL_FIELDS",
    "Curve",
    "BlendConfig",
    "ClimoCell",
    "ClimoTable",
    "parse_curve",
    "parse_blend_block",
    "load_blend_config",
    "weight",
    "blend",
    "solar_offset_h",
    "local_key",
    "in_season",
    "cell_from_row",
    "default_table",
]
