"""Per-stadium climatology from the Open-Meteo ERA5 archive -> data/climatology.csv.

For each stadium the daily ERA5 series (`wind_speed_10m_mean` mph,
`temperature_2m_mean` F) over a multi-year window is reduced to
`avg_wind_sep..avg_wind_jan` (mean daily wind by calendar month, the legacy
month-specific columns) and `avg_temp_f` (mean over the whole window, the legacy
annual figure). Results are cached in `data/climatology.csv` (one row per
stadium with the window and coordinates used); `build_stadiums.py` fills blank
stadium columns from that file.

Requests are batched (several locations per call), throttled, and cached as raw
json under `--cache-dir` so a re-run is offline.

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

from pipeline.stadiums.loader import DATA_DIR

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = ("wind_speed_10m_mean", "temperature_2m_mean")
MONTHS = {"sep": 9, "oct": 10, "nov": 11, "dec": 12, "jan": 1}
DEFAULT_START = "2015-01-01"
DEFAULT_END = "2024-12-31"
BATCH = 5
THROTTLE_S = 6.0
COLUMNS = ["stadium_id", "lat", "lon", "start_date", "end_date", "avg_wind_sep", "avg_wind_oct", "avg_wind_nov", "avg_wind_dec", "avg_wind_jan",
           "avg_temp_f", "n_days", "fetched_at"]


def monthly_means(daily: dict[str, Any]) -> dict[str, Optional[float]]:
    """Reduce one location's `daily` block to the legacy columns (None when no data)."""
    times = daily.get("time") or []
    winds = daily.get("wind_speed_10m_mean") or []
    temps = daily.get("temperature_2m_mean") or []
    by_month: dict[int, list[float]] = {m: [] for m in MONTHS.values()}
    all_temps: list[float] = []
    for i, t in enumerate(times):
        try:
            month = int(str(t)[5:7])
        except ValueError:
            continue
        w = winds[i] if i < len(winds) else None
        tf = temps[i] if i < len(temps) else None
        if w is not None and month in by_month:
            by_month[month].append(float(w))
        if tf is not None:
            all_temps.append(float(tf))
    out: dict[str, Optional[float]] = {}
    for key, month in MONTHS.items():
        vals = by_month[month]
        out[f"avg_wind_{key}"] = round(sum(vals) / len(vals), 2) if vals else None
    out["avg_temp_f"] = round(sum(all_temps) / len(all_temps), 2) if all_temps else None
    out["n_days"] = float(len(all_temps))
    return out


def parse_archive(payload: Any, ids: Sequence[str]) -> dict[str, dict[str, Optional[float]]]:
    """Map a (single- or multi-location) archive payload onto stadium ids in request order."""
    items = payload if isinstance(payload, list) else [payload]
    out: dict[str, dict[str, Optional[float]]] = {}
    for sid, item in zip(ids, items, strict=False):
        if not isinstance(item, dict) or "daily" not in item:
            continue
        out[sid] = monthly_means(item["daily"])
    return out


def fetch_archive(
    points: dict[str, tuple[float, float]],
    start: str,
    end: str,
    fetcher: Any = None,
    batch: int = BATCH,
    throttle_s: float = THROTTLE_S,
    log: Callable[[str], None] = print,
) -> dict[str, dict[str, Optional[float]]]:
    """Fetch daily ERA5 for every point (batched) and reduce. `fetcher` is a build_stadiums.Fetcher."""
    from pipeline.stadiums.build_stadiums import Fetcher, _digest

    f = fetcher or Fetcher(None)
    out: dict[str, dict[str, Optional[float]]] = {}
    # per-stadium cache: a location fetched in an earlier (differently batched) run is never re-fetched
    ids = []
    for sid in sorted(points):
        hit = f.cached(f"era5_{start}_{end}_{sid}") if hasattr(f, "cached") else None
        if isinstance(hit, dict) and "daily" in hit:
            out[sid] = monthly_means(hit["daily"])
        else:
            ids.append(sid)
    nb = (len(ids) + batch - 1) // batch
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        params = {
            "latitude": ",".join(f"{points[s][0]:.5f}" for s in chunk),
            "longitude": ",".join(f"{points[s][1]:.5f}" for s in chunk),
            "start_date": start,
            "end_date": end,
            "daily": ",".join(DAILY_VARS),
            "wind_speed_unit": "mph",
            "temperature_unit": "fahrenheit",
            "timezone": "UTC",
        }
        log(f"era5 batch {i // batch + 1}/{nb} ({len(chunk)} stadiums)")
        try:
            payload = f.json(f"era5_{start}_{end}_{_digest(chunk)}", "GET", ARCHIVE_URL, params=params, throttle=throttle_s, retries=6)
        except RuntimeError as exc:  # rate limit exhausted etc.: keep what we have, the next run resumes
            log(f"  era5 batch failed, skipping: {exc}")
            continue
        items = payload if isinstance(payload, list) else [payload]
        if hasattr(f, "store"):
            for sid, item in zip(chunk, items, strict=False):
                if isinstance(item, dict) and "daily" in item:
                    f.store(f"era5_{start}_{end}_{sid}", item)
        out.update(parse_archive(payload, chunk))
    return out


def read_climatology(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["stadium_id"]: dict(r) for r in csv.DictReader(fh)}


def write_climatology(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["stadium_id"]):
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in COLUMNS})


def _stadium_points(data_dir: Path, ids: Optional[set[str]], scope_all: bool) -> dict[str, tuple[float, float]]:
    from pipeline.stadiums.build_stadiums import _f, _read_rows, scope_ids
    from pipeline.stadiums.loader import load_stadium_book

    rows, _ = _read_rows(data_dir / "stadiums.csv")
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
    from pipeline.stadiums.build_stadiums import Fetcher

    path = data_dir / "climatology.csv"
    existing = read_climatology(path)
    points = _stadium_points(data_dir, ids, scope_all)
    todo = {sid: p for sid, p in points.items() if refresh or sid not in existing or existing[sid].get("start_date") != start or existing[sid].get("end_date") != end}
    log(f"climatology: {len(points)} stadiums in scope, {len(todo)} to fetch")
    if todo:
        fetcher = Fetcher(cache_dir, offline=offline, log=log)
        got = fetch_archive(todo, start, end, fetcher=fetcher, log=log)
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for sid, vals in got.items():
            if vals.get("avg_temp_f") is None:
                continue
            row: dict[str, Any] = {"stadium_id": sid, "lat": todo[sid][0], "lon": todo[sid][1], "start_date": start, "end_date": end, "fetched_at": now}
            row.update({k: v for k, v in vals.items()})
            row["n_days"] = int(vals.get("n_days") or 0)
            existing[sid] = row
        write_climatology(path, existing.values())
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
    print(f"climatology.csv: {len(rows)} rows; {len(points)} in scope; missing: {missing or 'none'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["ARCHIVE_URL", "COLUMNS", "build_climatology", "fetch_archive", "monthly_means", "parse_archive", "read_climatology", "write_climatology"]
