"""NWS api.weather.gov client (ARCH §6): /points cache + /gridpoints raw grid.

User-Agent is mandatory; 5xx retried 3x; horizon ~7.5 days. Raw gridpoint
payloads go through the optional ``capture`` hook before parsing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import httpx

from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.nws import parse_gridpoints

BASE_URL = "https://api.weather.gov"
USER_AGENT = "football_weather (mckinleyslade@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
RETRIES = 3
HORIZON_HOURS = 7.5 * 24
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POINTS_CACHE = REPO_ROOT / "data" / "raw" / "nws_points.json"

CaptureFn = Callable[[str, Any, str], None]


def _point_key(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"


class PointsCache:
    def __init__(self, path: Path = POINTS_CACHE) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def get(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        return self._data.get(_point_key(lat, lon))

    def put(self, lat: float, lon: float, grid: dict[str, Any]) -> None:
        self._data[_point_key(lat, lon)] = grid
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")
        self._dirty = False


def _get_json(client: httpx.Client, url: str) -> tuple[Any, str]:
    last_exc: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            r = client.get(url)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json(), str(r.url)
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                break  # 404 outside NWS coverage etc. — do not retry
            if attempt < RETRIES - 1:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"nws request failed for {url}: {last_exc}")


def resolve_grid(
    lat: float,
    lon: float,
    cache: Optional[PointsCache] = None,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """{'gridId','gridX','gridY','forecastGridData'} for a point; cached in data/raw/nws_points.json."""
    if cache is not None:
        hit = cache.get(lat, lon)
        if hit:
            return hit
    own = client is None
    c = client or httpx.Client(timeout=30.0, headers=HEADERS)
    try:
        payload, _ = _get_json(c, f"{BASE_URL}/points/{lat:.4f},{lon:.4f}")
    finally:
        if own:
            c.close()
    props = payload.get("properties") or {}
    grid = {
        "gridId": props.get("gridId"),
        "gridX": props.get("gridX"),
        "gridY": props.get("gridY"),
        "forecastGridData": props.get("forecastGridData"),
        "timeZone": props.get("timeZone"),
    }
    if not grid["forecastGridData"]:
        raise RuntimeError(f"nws /points returned no forecastGridData for {lat},{lon}")
    if cache is not None:
        cache.put(lat, lon, grid)
    return grid


def fetch_gridpoints_raw(grid: dict[str, Any], client: Optional[httpx.Client] = None) -> tuple[Any, str]:
    url = grid.get("forecastGridData") or f"{BASE_URL}/gridpoints/{grid['gridId']}/{grid['gridX']},{grid['gridY']}"
    own = client is None
    c = client or httpx.Client(timeout=30.0, headers=HEADERS)
    try:
        return _get_json(c, url)
    finally:
        if own:
            c.close()


def fetch_hourly(
    lat: float,
    lon: float,
    cache: Optional[PointsCache] = None,
    capture: Optional[CaptureFn] = None,
    client: Optional[httpx.Client] = None,
    source_name: Optional[str] = None,
) -> list[HourlyRow]:
    """Resolve grid, fetch raw gridpoints (captured), parse to hourly rows in F/mph/mm."""
    own = client is None
    c = client or httpx.Client(timeout=30.0, headers=HEADERS)
    try:
        grid = resolve_grid(lat, lon, cache=cache, client=c)
        payload, url = fetch_gridpoints_raw(grid, client=c)
    finally:
        if own:
            c.close()
    if capture is not None:
        name = source_name or f"nws_gridpoints_{grid['gridId']}_{grid['gridX']}_{grid['gridY']}"
        capture(name, payload, url)
    return parse_gridpoints(payload)


__all__ = [
    "BASE_URL",
    "USER_AGENT",
    "HORIZON_HOURS",
    "POINTS_CACHE",
    "PointsCache",
    "resolve_grid",
    "fetch_gridpoints_raw",
    "fetch_hourly",
]
