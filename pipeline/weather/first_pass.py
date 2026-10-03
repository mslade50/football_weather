"""NWS first where its actual rows cover the game, lean global fallback elsewhere.

NWS QPF is an interval accumulation. The existing parser distributes that amount
over its valid interval; this is a screening estimate, not observed hourly rain.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from pipeline.weather.screening import finite

MODELS = "ncep_nbm_conus,ncep_hrrr_conus,ncep_gfs_seamless,ecmwf_ifs025,ecmwf_aifs025_single"
INTL_MODELS = "best_match,ecmwf_ifs025,ecmwf_aifs025_single"
HOURLY = "temperature_2m,wind_speed_10m,precipitation"
FIELDS = ("temp", "wind", "precip")
EXTRA_HOURLY = "wind_gusts_10m,wind_direction_10m,precipitation_probability"
MAX_NWS_SOURCE_AGE_H = 12


def complete(rows: Any, hours: set[datetime]) -> bool:
    index = {row.t: row for row in rows or []}
    return all(t in index and all(finite(getattr(index[t], name)) for name in FIELDS) for t in hours)


def updated_at(metadata: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(metadata["updateTime"].replace("Z", "+00:00"))
        return value if value.tzinfo is not None else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def aged(metadata: dict[str, Any], now: datetime) -> bool:
    updated = updated_at(metadata)
    return updated is None or not 0 <= (now - updated).total_seconds() <= MAX_NWS_SOURCE_AGE_H * 3600


def needs_refinement(decision: Any) -> bool:
    # Missing/aged first-pass inputs request detailed POINTS, never members merely
    # because distant weather is unknown. The final screen must have real coverage.
    return decision.eligible or decision.priority == 2


def collect(ctx, sport, om, nws, by_point, conus, hours, reused, start, end, capture, fetch_batches, state_dir, budget_kwargs):
    """Fresh NWS grids once per location/run, cheap global points for uncovered hours."""
    forecasts = {point: value[0] for point, value in reused.items() if value[0] is not None}
    grids = {point: value[1] for point, value in reused.items()}
    stamps = {point: reused[point][2] if point in reused else ctx.now_utc for point in by_point}
    meta = ctx.weather_state.setdefault("point_meta", {})
    cache = nws.PointsCache(path=state_dir / "nws_points.json") if state_dir is not None else nws.PointsCache()
    run_grids = ctx.weather_state.setdefault("run_nws", {})
    for point in conus:
        if point in reused or not any((game.kickoff_utc - ctx.now_utc).total_seconds() <= 7 * 24 * 3600 for game in by_point[point]):
            continue
        try:
            if point not in run_grids:
                metadata = {}
                rows = nws.fetch_hourly(*point, cache=cache, capture=capture, metadata=metadata)
                run_grids[point] = rows, metadata, ctx.now_utc
            rows, metadata, stamp = run_grids[point]
            grids[point], stamps[point] = rows, stamp
            meta[point] = {"stage": "nws_first_pass", "updated_at": metadata.get("updateTime"), "aged": aged(metadata, ctx.now_utc), "nws_aged": aged(metadata, ctx.now_utc)}
        except Exception:  # noqa: BLE001 - bounded global fallback next
            ctx.degrade("weather", f"{sport}: NWS first pass unavailable at {point}; trying lean global points", "warn")
    try:
        cache.save()
    except OSError:
        pass
    lean = [point for point in by_point if point not in reused and
            (not complete(grids.get(point), hours[point]) or meta.get(point, {}).get("aged", True))]
    for group, models in (([p for p in lean if p in conus], MODELS), ([p for p in lean if p not in conus], INTL_MODELS)):
        if not group:
            continue
        fetched, failures = fetch_batches(
            group, om.fetch_forecast, batch_size=om.BATCH_SIZE,
            source_prefix="openmeteo_first_pass", start=start, end=end, models=models, hourly=HOURLY,
            capture=capture, received=lambda batch: stamps.update({point: ctx.now_utc for point in batch}), **budget_kwargs,
        )
        forecasts.update(fetched)
        for point in fetched:
            meta[point] = {**meta.get(point, {}), "stage": "global_first_pass", "aged": False}
        if failures:
            ctx.degrade("weather", f"{sport}: lean global points unavailable at {sum(size for _, size, _ in failures)} locations", "warn")
    return forecasts, grids, stamps, meta


def join_details(first, detail):
    """Keep signal values/times; attach only supplemental fields by model/hour."""
    models = {}
    for model, rows in first.models.items():
        index = {row.t: row for row in detail.models.get(model, [])}
        models[model] = [replace(row, **{name: getattr(index[row.t], name) if row.t in index else None
                                       for name in ("gust", "dir", "pop")}) for row in rows]
    return replace(first, models=models, units={**first.units, **detail.units})


def refine(ctx, sport, om, points, conus, forecasts, stamps, meta, start, end, capture, fetch_batches, budget_kwargs, hours):
    for region, models, prefix in (([p for p in points if p in conus], om.CONUS_MODELS, "openmeteo_refine_conus"),
                                   ([p for p in points if p not in conus], om.INTL_MODELS, "openmeteo_refine_intl")):
        # Only extend this run's complete lean response, with the identical model
        # set. NWS-only, failed or partial first passes still request all fields.
        split = [p for p in region if meta.get(p, {}).get("stage") == "global_first_pass"
                 and set(models.split(",")).issubset(getattr(forecasts.get(p), "models", {}))
                 and any(complete(rows, hours[p]) for rows in forecasts[p].models.values())
                 and 0 <= (ctx.now_utc - stamps[p]).total_seconds() <= 1800]
        for group, hourly in ((split, EXTRA_HOURLY), ([p for p in region if p not in split], om.HOURLY)):
            if not group:
                continue
            fetched, failures = fetch_batches(
                group, om.fetch_forecast,
                batch_size=(lambda remaining, point_models=models, fields=hourly: budget_kwargs["budget"].batch_size(
                    om.FORECAST_URL, om.build_params(remaining[:1], start, end, point_models, hourly=fields), om.BATCH_SIZE)) if budget_kwargs else om.BATCH_SIZE,
                source_prefix=prefix + ("_details" if hourly == EXTRA_HOURLY else ""), start=start, end=end,
                models=models, hourly=hourly, capture=capture, **budget_kwargs,
            )
            for point, location in fetched.items():
                if hourly == EXTRA_HOURLY:
                    forecasts[point] = join_details(forecasts[point], location)
                    # The whole forecast keeps the oldest contributing receipt.
                    # The detail receipt is recorded separately in raw handoff.
                    meta[point] = {**meta[point], "stage": "refined_split_fields", "details_fetched_at": ctx.now_utc.isoformat(), "aged": False}
                else:
                    forecasts[point], stamps[point] = location, ctx.now_utc
                    meta[point] = {**meta.get(point, {}), "stage": "refined_multimodel", "aged": False}
            if failures:
                ctx.degrade("weather", f"{sport}: detailed point refinement unavailable at {sum(size for _, size, _ in failures)} locations; retaining first pass", "warn")
