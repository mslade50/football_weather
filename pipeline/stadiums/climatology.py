"""Per-stadium climatology from the Open-Meteo ERA5 archive -> data/climatology.csv.

Two layers live in one csv (ARCH §6):

* **summary row** (one per stadium, ``iso_week`` blank): the legacy
  ``avg_wind_sep..avg_wind_jan`` (mean wind by calendar month) and ``avg_temp_f``
  (mean over the whole window). The stadium loader reads these directly,
  with explicit curated exceptions taking precedence.
* **cell rows** (stadium × ISO week × 4 local-time-of-day bins, Aug–Jan only):
  mean / P10 / P50 / P90 of wind and temperature, gust mean / P90 and the
  frequency of ≥1 mm/h rain. ``weather/climatology_blend.py`` reads them as the
  base rate the kickoff forecast is shrunk toward at long leads. "Local" is mean
  solar time (``round(lon/15)`` h) — the same rule the lookup uses.

Both come from ONE hourly ERA5 request per stadium for the whole window
(``wind_speed_10m,wind_gusts_10m,temperature_2m,precipitation``), throttled and
cached verbatim under ``--cache-dir`` so a re-run is offline / resume-safe. The
hourly reduction is the only archive reduction path.

Per stadium the summary row is written LAST (after its cells): readers that key
rows by stadium_id still see the summary columns. The loader explicitly
selects summary rows, independent of ordering.

CLI: python -m pipeline.stadiums.climatology [--ids a,b] [--refresh] [--start 2015-01-01]
       [--end 2024-12-31] [--cache-dir DIR] [--offline]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Optional

from pipeline.stadiums.loader import DATA_DIR, load_stadium_rows, read_climatology_summary
from pipeline.weather.climatology_blend import CELL_FIELDS, RAIN_MM, in_season, local_key

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = ("wind_speed_10m", "wind_gusts_10m", "temperature_2m", "precipitation")
MONTHS = {"sep": 9, "oct": 10, "nov": 11, "dec": 12, "jan": 1}
DEFAULT_START = "2015-01-01"
DEFAULT_END = "2024-12-31"
HOURLY_THROTTLE_S = 1.5
MIN_CELL_HOURS = 24
SUMMARY_COLUMNS = ["stadium_id", "lat", "lon", "start_date", "end_date", "avg_wind_sep", "avg_wind_oct", "avg_wind_nov", "avg_wind_dec", "avg_wind_jan",
                   "avg_temp_f", "n_days", "fetched_at"]
CELL_COLUMNS = ["iso_week", "tod_bin", "n_hours", *CELL_FIELDS]
COLUMNS = SUMMARY_COLUMNS + CELL_COLUMNS


def _percentile(xs: list[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


def _parse_hour(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), tzinfo=dt.timezone.utc)
    except (ValueError, IndexError):
        return None


def reduce_hourly(hourly: dict[str, Any], lon: float) -> tuple[dict[str, Optional[float]], list[dict[str, Any]]]:
    """One location's ERA5 ``hourly`` block -> (legacy summary values, weekly cells).

    Summary: monthly wind means (Sep..Jan) and the all-window temperature mean, from the
    hourly series (identical to the daily-mean path up to rounding). Cells: for every
    in-season (Aug–Jan) hour, keyed by ISO week and 6-hour solar-time bin at ``lon``."""
    times = hourly.get("time") or []
    winds = hourly.get("wind_speed_10m") or []
    gusts = hourly.get("wind_gusts_10m") or []
    temps = hourly.get("temperature_2m") or []
    precs = hourly.get("precipitation") or []

    def at(col: list, i: int) -> Optional[float]:
        v = col[i] if i < len(col) else None
        return None if v is None else float(v)

    by_month: dict[int, list[float]] = {m: [] for m in MONTHS.values()}
    all_temps: list[float] = []
    days: set[str] = set()
    cells: dict[tuple[int, int], dict[str, list[float]]] = {}
    for i, ts in enumerate(times):
        t = _parse_hour(str(ts))
        if t is None:
            continue
        w, g, tf, p = at(winds, i), at(gusts, i), at(temps, i), at(precs, i)
        if w is not None and t.month in by_month:
            by_month[t.month].append(w)
        if tf is not None:
            all_temps.append(tf)
            days.add(str(ts)[:10])
        month, wk, tb = local_key(t, lon)
        if not in_season(month):
            continue
        cell = cells.setdefault((wk, tb), {"wind": [], "gust": [], "temp": [], "rain": []})
        if w is not None:
            cell["wind"].append(w)
        if g is not None:
            cell["gust"].append(g)
        if tf is not None:
            cell["temp"].append(tf)
        if p is not None:
            cell["rain"].append(1.0 if p >= RAIN_MM else 0.0)

    summary: dict[str, Optional[float]] = {}
    for key, month in MONTHS.items():
        summary[f"avg_wind_{key}"] = _r(_mean(by_month[month]))
    summary["avg_temp_f"] = _r(_mean(all_temps))
    summary["n_days"] = float(len(days))

    rows: list[dict[str, Any]] = []
    for (wk, tb), c in sorted(cells.items()):
        n = max(len(c["wind"]), len(c["temp"]))
        if n < MIN_CELL_HOURS:
            continue
        rows.append({
            "iso_week": wk, "tod_bin": tb, "n_hours": n,
            "wind_mean": _r(_mean(c["wind"])), "wind_p10": _r(_percentile(c["wind"], 0.10)),
            "wind_p50": _r(_percentile(c["wind"], 0.50)), "wind_p90": _r(_percentile(c["wind"], 0.90)),
            "gust_mean": _r(_mean(c["gust"])), "gust_p90": _r(_percentile(c["gust"], 0.90)),
            "temp_mean": _r(_mean(c["temp"])), "temp_p10": _r(_percentile(c["temp"], 0.10)),
            "temp_p50": _r(_percentile(c["temp"], 0.50)), "temp_p90": _r(_percentile(c["temp"], 0.90)),
            "rain_freq": _r(_mean(c["rain"]), 3),
        })
    return summary, rows


def hourly_params(lat: float, lon: float, start: str, end: str) -> dict[str, str]:
    return {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(HOURLY_VARS),
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "mm",
        "timezone": "UTC",
    }


def fetch_archive_hourly(
    points: dict[str, tuple[float, float]],
    start: str,
    end: str,
    fetcher: Any = None,
    throttle_s: float = HOURLY_THROTTLE_S,
    log: Callable[[str], None] = print,
) -> dict[str, dict[str, Any]]:
    """One hourly ERA5 request per stadium for the whole window; returns ``{sid: hourly block}``.
    The fetcher's json cache (``era5h_<start>_<end>_<sid>``) makes re-runs offline and resumable."""
    from pipeline.stadiums.build_stadiums import Fetcher

    f = fetcher or Fetcher(None)
    out: dict[str, dict[str, Any]] = {}
    ids = sorted(points)
    for n, sid in enumerate(ids, 1):
        lat, lon = points[sid]
        name = f"era5h_{start}_{end}_{sid}"
        cached = f.cached(name) if hasattr(f, "cached") else None
        if not (isinstance(cached, dict) and "hourly" in cached):
            log(f"era5 hourly {n}/{len(ids)} {sid}")
        try:
            payload = f.json(name, "GET", ARCHIVE_URL, params=hourly_params(lat, lon, start, end), throttle=throttle_s, retries=6)
        except RuntimeError as exc:  # rate limit exhausted etc.: keep what we have, the next run resumes
            log(f"  era5 hourly {sid} failed, skipping: {exc}")
            continue
        if isinstance(payload, dict) and isinstance(payload.get("hourly"), dict):
            out[sid] = payload["hourly"]
    return out


def _is_cell(row: dict[str, Any]) -> bool:
    return str(row.get("iso_week") or "").strip() != ""


def read_all_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def read_climatology(path: Path) -> dict[str, dict[str, str]]:
    """Summary rows (one per stadium); cell rows are skipped."""
    return read_climatology_summary(path)


def read_cells(path: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for r in read_all_rows(path):
        if _is_cell(r):
            out.setdefault(r["stadium_id"], []).append(r)
    return out


def _sort_key(r: dict[str, Any]) -> tuple:
    if _is_cell(r):
        return (r["stadium_id"], 0, int(float(r["iso_week"])), int(float(r.get("tod_bin") or 0)))
    return (r["stadium_id"], 1, 0, 0)


def write_climatology(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Cells first, summary row last, per stadium (see module docstring)."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=_sort_key):
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in COLUMNS})


def _stadium_points(data_dir: Path, ids: Optional[set[str]], scope_all: bool) -> dict[str, tuple[float, float]]:
    from pipeline.stadiums.build_stadiums import _f, scope_ids
    from pipeline.stadiums.loader import load_stadium_book

    rows = load_stadium_rows(data_dir)
    in_scope, _ = scope_ids(rows, load_stadium_book(data_dir), all_rows=scope_all)
    if ids:
        in_scope = ids
    pts: dict[str, tuple[float, float]] = {}
    for r in rows:
        if r["stadium_id"] not in in_scope:
            continue
        la, lo = _f(r.get("lat")), _f(r.get("lon"))
        if la is not None and lo is not None:
            pts[r["stadium_id"]] = (la, lo)
    return pts


def build_climatology(
    data_dir: Path = DATA_DIR,
    ids: Optional[set[str]] = None,
    refresh: bool = False,
    scope_all: bool = False,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    cache_dir: Optional[Path] = None,
    offline: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, dict[str, Any]]:
    """Fetch (hourly ERA5) + reduce every in-scope stadium missing a summary row or its
    cells; rewrite data/climatology.csv. Returns the summary rows by stadium id."""
    from pipeline.stadiums.build_stadiums import Fetcher

    path = data_dir / "climatology.csv"
    existing = read_climatology(path)
    cells = read_cells(path)
    points = _stadium_points(data_dir, ids, scope_all)

    def stale(sid: str) -> bool:
        row = existing.get(sid)
        return row is None or row.get("start_date") != start or row.get("end_date") != end or not cells.get(sid)

    todo = {sid: p for sid, p in points.items() if refresh or stale(sid)}
    log(f"climatology: {len(points)} stadiums in scope, {len(todo)} to fetch")
    if todo:
        fetcher = Fetcher(cache_dir, offline=offline, log=log)
        got = fetch_archive_hourly(todo, start, end, fetcher=fetcher, log=log)
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for sid, hourly in got.items():
            summary, cell_rows = reduce_hourly(hourly, todo[sid][1])
            if summary.get("avg_temp_f") is None:
                continue
            base: dict[str, Any] = {"stadium_id": sid, "lat": todo[sid][0], "lon": todo[sid][1], "start_date": start, "end_date": end, "fetched_at": now}
            row = dict(base)
            row.update(summary)
            row["n_days"] = int(summary.get("n_days") or 0)
            existing[sid] = row
            cells[sid] = [{**base, **c} for c in cell_rows]
        write_climatology(path, [*existing.values(), *(c for rows in cells.values() for c in rows)])
    return existing


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ERA5 climatology per stadium -> data/climatology.csv")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--ids", default="", help="comma-separated stadium ids (default: NFL + FBS + nflverse venues)")
    ap.add_argument("--all", action="store_true", help="every row of stadiums.csv")
    ap.add_argument("--refresh", action="store_true", help="re-fetch rows already cached")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--cache-dir", type=Path, default=None)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    ids = {s.strip() for s in a.ids.split(",") if s.strip()} or None
    rows = build_climatology(a.data_dir, ids=ids, refresh=a.refresh, scope_all=a.all, start=a.start, end=a.end, cache_dir=a.cache_dir, offline=a.offline)
    points = _stadium_points(a.data_dir, ids, a.all)
    missing = sorted(set(points) - set(rows))
    n_cells = sum(len(v) for v in read_cells(a.data_dir / "climatology.csv").values())
    print(f"climatology.csv: {len(rows)} stadiums, {n_cells} weekly cells; {len(points)} in scope; missing: {missing or 'none'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ARCHIVE_URL", "COLUMNS", "SUMMARY_COLUMNS", "CELL_COLUMNS", "HOURLY_VARS", "build_climatology",
    "fetch_archive_hourly", "hourly_params", "reduce_hourly", "read_climatology",
    "read_cells", "read_all_rows", "write_climatology",
]
