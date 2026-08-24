"""Open-Meteo forecast + ensemble clients (ARCH §6). Batched <=50 points per call, unit params fixed.

Historical-forecast / previous-runs clients are Phase 6. Every raw response goes
through the optional ``capture`` hook BEFORE parsing (ARCH §1.3).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from pipeline.weather.parsers.ensemble import EnsembleLocation, parse_ensemble
from pipeline.weather.parsers.openmeteo import ParsedLocation, parse_forecast

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "ecmwf_ifs025,gfs_seamless"
ENSEMBLE_HOURLY = "wind_speed_10m,wind_gusts_10m,precipitation"
ENSEMBLE_UNIT_PARAMS = {"wind_speed_unit": "mph", "precipitation_unit": "mm", "timezone": "UTC"}
CONUS_MODELS = "ncep_nbm_conus,ncep_hrrr_conus,ncep_gfs_seamless,ecmwf_ifs025"
INTL_MODELS = "best_match,ecmwf_ifs025"
HOURLY = "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
UNIT_PARAMS = {
    "wind_speed_unit": "mph",
    "temperature_unit": "fahrenheit",
    "precipitation_unit": "mm",
    "timezone": "UTC",
}
BATCH_SIZE = 50
USER_AGENT = "football_weather (mckinleyslade@gmail.com)"
RETRIES = 3

# capture(source_name, payload, url)
CaptureFn = Callable[[str, Any, str], None]
Point = tuple[float, float]


def _fmt_hour(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")


def window_for(kickoffs_utc: Sequence[datetime]) -> tuple[datetime, datetime]:
    """kickoff-1h .. kickoff+4h across a batch (Open-Meteo start_hour/end_hour are global per call)."""
    ks = [k.astimezone(timezone.utc) for k in kickoffs_utc]
    return min(ks) - timedelta(hours=1), max(ks) + timedelta(hours=4)


def build_params(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    models: str = CONUS_MODELS,
    forecast_days: Optional[int] = None,
) -> dict[str, str]:
    params: dict[str, str] = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "models": models,
        "hourly": HOURLY,
    }
    params.update(UNIT_PARAMS)
    if start is not None and end is not None:
        params["start_hour"] = _fmt_hour(start)
        params["end_hour"] = _fmt_hour(end)
    elif forecast_days is not None:
        params["forecast_days"] = str(forecast_days)
    return params


def _get_json(client: httpx.Client, url: str, params: dict[str, str]) -> tuple[Any, str]:
    last_exc: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            r = client.get(url, params=params)
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json(), str(r.url)
        except (httpx.HTTPError, ValueError) as exc:  # ValueError: bad JSON
            last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"open-meteo request failed after {RETRIES} attempts: {last_exc}")


def fetch_forecast(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    models: str = CONUS_MODELS,
    forecast_days: Optional[int] = None,
    capture: Optional[CaptureFn] = None,
    client: Optional[httpx.Client] = None,
    source_prefix: str = "openmeteo_forecast",
) -> list[ParsedLocation]:
    """Fetch and parse forecasts for `points`; batches of <=50. Returns one ParsedLocation per input point (order preserved)."""
    if not points:
        return []
    own = client is None
    c = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    out: list[ParsedLocation] = []
    try:
        for b, i in enumerate(range(0, len(points), BATCH_SIZE)):
            batch = list(points[i : i + BATCH_SIZE])
            params = build_params(batch, start, end, models, forecast_days)
            payload, url = _get_json(c, FORECAST_URL, params)
            if capture is not None:
                capture(f"{source_prefix}_{b:02d}", payload, url)
            parsed = parse_forecast(payload)
            if len(parsed) != len(batch):
                raise RuntimeError(f"open-meteo returned {len(parsed)} locations for {len(batch)} points")
            out.extend(parsed)
    finally:
        if own:
            c.close()
    return out


def fetch_forecast_raw(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    models: str = CONUS_MODELS,
    forecast_days: Optional[int] = None,
    client: Optional[httpx.Client] = None,
) -> Any:
    """Single-batch raw payload (<=50 points); handy for fixture building."""
    own = client is None
    c = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    try:
        payload, _ = _get_json(c, FORECAST_URL, build_params(points[:BATCH_SIZE], start, end, models, forecast_days))
        return payload
    finally:
        if own:
            c.close()


def build_ensemble_params(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    models: str = ENSEMBLE_MODELS,
    forecast_days: Optional[int] = None,
) -> dict[str, str]:
    params: dict[str, str] = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "models": models,
        "hourly": ENSEMBLE_HOURLY,
    }
    params.update(ENSEMBLE_UNIT_PARAMS)
    if start is not None and end is not None:
        params["start_hour"] = _fmt_hour(start)
        params["end_hour"] = _fmt_hour(end)
    elif forecast_days is not None:
        params["forecast_days"] = str(forecast_days)
    return params


def fetch_ensemble(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    models: str = ENSEMBLE_MODELS,
    forecast_days: Optional[int] = None,
    capture: Optional[CaptureFn] = None,
    client: Optional[httpx.Client] = None,
    source_prefix: str = "openmeteo_ensemble",
) -> list[EnsembleLocation]:
    """Ensemble members (ECMWF IFS 0.25 + GEFS) for `points`; batches of <=50, order preserved.

    Raises on transport failure so the caller can degrade to the static wind_vol."""
    if not points:
        return []
    own = client is None
    c = client or httpx.Client(timeout=90.0, headers={"User-Agent": USER_AGENT})
    out: list[EnsembleLocation] = []
    try:
        for b, i in enumerate(range(0, len(points), BATCH_SIZE)):
            batch = list(points[i : i + BATCH_SIZE])
            params = build_ensemble_params(batch, start, end, models, forecast_days)
            payload, url = _get_json(c, ENSEMBLE_URL, params)
            if capture is not None:
                capture(f"{source_prefix}_{b:02d}", payload, url)
            parsed = parse_ensemble(payload)
            if len(parsed) != len(batch):
                raise RuntimeError(f"open-meteo ensemble returned {len(parsed)} locations for {len(batch)} points")
            out.extend(parsed)
    finally:
        if own:
            c.close()
    return out


__all__ = [
    "FORECAST_URL",
    "ENSEMBLE_URL",
    "CONUS_MODELS",
    "INTL_MODELS",
    "ENSEMBLE_MODELS",
    "BATCH_SIZE",
    "CaptureFn",
    "window_for",
    "build_params",
    "build_ensemble_params",
    "fetch_forecast",
    "fetch_forecast_raw",
    "fetch_ensemble",
]
