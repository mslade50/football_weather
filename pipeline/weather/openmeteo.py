"""Open-Meteo forecast + ensemble clients (ARCH §6). Batched <=50 points per call, unit params fixed.

Historical-forecast / previous-runs clients are Phase 6. Every raw response goes
through the optional ``capture`` hook BEFORE parsing (ARCH §1.3).

ECMWF AIFS (the data-driven medium-range model) is requested as
``ecmwf_aifs025_single`` (verified 2026-08: the bare ``ecmwf_aifs025`` id is
accepted but returns only nulls). It is a 6-hourly model that Open-Meteo
interpolates to hourly; it carries temperature / wind speed / direction /
precipitation out to 15 days but NO gusts and NO precipitation probability, so the
stitching never lets its nulls zero those fields (see merge.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from pipeline.weather.member_cache import METADATA_BASE, SOURCES, version
from pipeline.weather.parsers.ensemble import EnsembleLocation, parse_ensemble
from pipeline.weather.parsers.ensemble_mean import EnsembleMeanLocation, parse_ensemble_mean
from pipeline.weather.parsers.openmeteo import ParsedLocation, parse_forecast
from pipeline.weather.rate_limit import ENSEMBLE_BATCH_SIZE, MINUTE_BUDGET, RequestBudget, query_weight

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "ecmwf_ifs025,gfs_seamless"
ENSEMBLE_HOURLY = "wind_speed_10m,wind_gusts_10m,precipitation"
ENSEMBLE_UNIT_PARAMS = {"wind_speed_unit": "mph", "precipitation_unit": "mm", "timezone": "UTC"}
ENSEMBLE_MEAN_MODEL = "ncep_gefs_ensemble_mean_seamless"
ENSEMBLE_MEAN_HOURLY = (
    "wind_speed_10m,wind_speed_10m_spread,"
    "wind_gusts_10m,wind_gusts_10m_spread,"
    "precipitation,precipitation_spread"
)
AIFS_MODEL = "ecmwf_aifs025_single"
CONUS_MODELS = f"ncep_nbm_conus,ncep_hrrr_conus,ncep_gfs_seamless,ecmwf_ifs025,{AIFS_MODEL}"
INTL_MODELS = f"best_match,ecmwf_ifs025,{AIFS_MODEL}"
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


def _request_kwargs(
    budget: Optional[RequestBudget], capture: Optional[CaptureFn], source: str,
) -> dict[str, Any]:
    if budget is None:
        return {}
    kwargs: dict[str, Any] = {"budget": budget}
    if capture is not None:
        kwargs["capture_error"] = lambda payload, url: capture(f"{source}_error", payload, url)
    return kwargs


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
    hourly: str = HOURLY,
) -> dict[str, str]:
    params: dict[str, str] = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "models": models,
        "hourly": hourly,
    }
    params.update(UNIT_PARAMS)
    if start is not None and end is not None:
        params["start_hour"] = _fmt_hour(start)
        params["end_hour"] = _fmt_hour(end)
    elif forecast_days is not None:
        params["forecast_days"] = str(forecast_days)
    return params


def _get_json(
    client: httpx.Client, url: str, params: dict[str, str], budget: Optional[RequestBudget] = None,
    capture_error: Optional[Callable[[Any, str], None]] = None,
) -> tuple[Any, str]:
    last_exc: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            if budget is not None:
                budget.acquire(url, params)
            r = client.get(url, params=params)
            # Retrying a rate-limited multi-location request immediately only
            # extends the outage and increases provider load. Let the caller
            # preserve other successful batches and use NWS/static fallbacks.
            if r.status_code == 429:
                reason = budget.rate_limited(r, capture_error) if budget is not None else ""
                detail = f": {reason}" if reason else ""
                raise RuntimeError(f"open-meteo rate limited (HTTP 429){detail}")
            if r.status_code >= 500:
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
    budget: Optional[RequestBudget] = None,
    hourly: str = HOURLY,
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
            params = build_params(batch, start, end, models, forecast_days, hourly)
            request_kwargs = _request_kwargs(budget, capture, f"{source_prefix}_{b:02d}")
            payload, url = _get_json(c, FORECAST_URL, params, **request_kwargs)
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


def fetch_model_versions(
    source: str, *, capture: Optional[CaptureFn] = None, budget: Optional[RequestBudget] = None,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Metadata is free of forecast quota; never guess a six-hour cycle id.

    GEFS seamless depends on both 0.25 and 0.5 degree dataset versions. The
    initialization stamp does not attribute every retained long-range hour.
    """
    own = client is None
    c = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
    versions = {}
    try:
        for dataset in SOURCES[source][1]:
            url = f"{METADATA_BASE}/{dataset}/static/meta.json"
            response = c.get(url)
            if response.status_code == 429:
                reason = budget.rate_limited(response) if budget else "HTTP 429"
                if capture:
                    capture(f"ensemble_metadata_{dataset}_error", response.json(), url)
                raise RuntimeError(f"model metadata restricted: {reason}")
            response.raise_for_status()
            body = response.json()
            if capture:
                capture(f"ensemble_metadata_{dataset}", body, url)
            versions[dataset] = version(body, datetime.now(timezone.utc))
        return versions
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


def build_ensemble_mean_params(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    model: str = ENSEMBLE_MEAN_MODEL,
    forecast_days: Optional[int] = None,
) -> dict[str, str]:
    """Parameters for Open-Meteo's lightweight precomputed mean/spread API."""
    params: dict[str, str] = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "models": model,
        "hourly": ENSEMBLE_MEAN_HOURLY,
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
    budget: Optional[RequestBudget] = None,
) -> list[EnsembleLocation]:
    """Full ensemble members in quota-sized batches, preserving point order.

    Raises on transport failure so the caller can degrade to the static wind_vol."""
    if not points:
        return []
    own = client is None
    c = client or httpx.Client(timeout=90.0, headers={"User-Agent": USER_AGENT})
    out: list[EnsembleLocation] = []
    try:
        offset, b = 0, 0
        while offset < len(points):
            one = build_ensemble_params(points[offset:offset + 1], start, end, models, forecast_days)
            size = (budget.batch_size(ENSEMBLE_URL, one, BATCH_SIZE) if budget else
                    min(BATCH_SIZE, int(MINUTE_BUDGET // query_weight(ENSEMBLE_URL, one))))
            if size < 1:
                raise RuntimeError("one location exceeds local minute budget")
            batch = list(points[offset:offset + size])
            offset += len(batch)
            params = build_ensemble_params(batch, start, end, models, forecast_days)
            request_kwargs = _request_kwargs(budget, capture, f"{source_prefix}_{b:02d}")
            payload, url = _get_json(c, ENSEMBLE_URL, params, **request_kwargs)
            if capture is not None:
                capture(f"{source_prefix}_{b:02d}", payload, url)
            parsed = parse_ensemble(payload)
            if len(parsed) != len(batch):
                raise RuntimeError(f"open-meteo ensemble returned {len(parsed)} locations for {len(batch)} points")
            out.extend(parsed)
            b += 1
    finally:
        if own:
            c.close()
    return out


def fetch_ensemble_mean(
    points: Sequence[Point],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    model: str = ENSEMBLE_MEAN_MODEL,
    forecast_days: Optional[int] = None,
    capture: Optional[CaptureFn] = None,
    client: Optional[httpx.Client] = None,
    source_prefix: str = "openmeteo_ensemble_mean",
    budget: Optional[RequestBudget] = None,
) -> list[EnsembleMeanLocation]:
    """Fetch precomputed GEFS mean/spread as a low-cost 429 fallback.

    This deliberately requests no individual members.  The response is much
    smaller than ``fetch_ensemble`` while retaining the wind uncertainty needed
    for P10/P90 bands.
    """
    if not points:
        return []
    own = client is None
    c = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    out: list[EnsembleMeanLocation] = []
    try:
        for b, i in enumerate(range(0, len(points), BATCH_SIZE)):
            batch = list(points[i : i + BATCH_SIZE])
            params = build_ensemble_mean_params(batch, start, end, model, forecast_days)
            request_kwargs = _request_kwargs(budget, capture, f"{source_prefix}_{b:02d}")
            payload, url = _get_json(c, ENSEMBLE_URL, params, **request_kwargs)
            if capture is not None:
                capture(f"{source_prefix}_{b:02d}", payload, url)
            parsed = parse_ensemble_mean(payload, model=model)
            if len(parsed) != len(batch):
                raise RuntimeError(
                    f"open-meteo ensemble mean returned {len(parsed)} locations for {len(batch)} points"
                )
            out.extend(parsed)
    finally:
        if own:
            c.close()
    return out


__all__ = [
    "FORECAST_URL",
    "ENSEMBLE_URL",
    "AIFS_MODEL",
    "CONUS_MODELS",
    "INTL_MODELS",
    "ENSEMBLE_MODELS",
    "ENSEMBLE_MEAN_MODEL",
    "BATCH_SIZE",
    "ENSEMBLE_BATCH_SIZE",
    "RequestBudget",
    "CaptureFn",
    "window_for",
    "build_params",
    "build_ensemble_params",
    "build_ensemble_mean_params",
    "fetch_forecast",
    "fetch_forecast_raw",
    "fetch_ensemble",
    "fetch_ensemble_mean",
]
