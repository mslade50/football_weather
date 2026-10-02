"""Fetch/reuse verified individual model snapshots, then pool original members."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.weather.member_cache import SOURCES, MemberCache, combine, complete, parameter_signature  # noqa: F401


def members(
    ctx: Any, om: Any, points: list[tuple[float, float]], windows: dict[Any, Any],
    hours: dict[Any, set[datetime]], capture: Callable[..., Any], fetch_batches: Callable[..., Any],
    *, state_dir: Path | None = None, sport: str,
) -> tuple[dict[Any, Any], dict[Any, dict[str, Any]]]:
    if not points:
        return {}, {}
    budget = ctx.request_budgets.get("openmeteo")
    signature = parameter_signature(om.ENSEMBLE_HOURLY, om.ENSEMBLE_UNIT_PARAMS)
    if "member_cache" not in ctx.weather_state:
        ctx.weather_state["member_cache"] = MemberCache(state_dir / "ensemble_cache.json" if state_dir else None, signature)
    cache = ctx.weather_state["member_cache"]
    locations: dict[Any, list[Any]] = {point: [] for point in points}
    coverage = {point: {"fetched_at": {}, "source_versions": {}, "cached_sources": [], "errors": {}} for point in points}
    fetch_versions = getattr(om, "fetch_model_versions", None)
    for source, (model, _) in SOURCES.items():
        try:
            if not callable(fetch_versions):
                raise RuntimeError("model-version client unavailable")
            before = fetch_versions(source, capture=lambda name, payload, url=None: capture(f"{name}_before", payload, url), budget=budget)
        except Exception as exc:  # noqa: BLE001
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble source cannot be verified for {len(points)} selected locations: {exc}", "warn")
            continue
        hits, missing = {}, []
        for point in points:
            hit = cache.get(source, point, before, hours[point], now=ctx.now_utc)
            if hit:
                hits[point] = hit
            else:
                missing.append(point)
        received_at = {}
        def batch_size(remaining, point_model=model):
            start = min(windows[point][0] for point in remaining)
            end = max(windows[point][1] for point in remaining)
            return budget.batch_size(om.ENSEMBLE_URL, om.build_ensemble_params(remaining[:1], start, end, point_model), om.BATCH_SIZE)
        fetched, failures = fetch_batches(
            missing, om.fetch_ensemble, batch_size=batch_size if budget else om.ENSEMBLE_BATCH_SIZE,
            source_prefix=f"openmeteo_selected_{source}", point_windows=windows,
            models=model, capture=capture, **({"budget": budget} if budget else {}),
            received=lambda batch, stamps=received_at: stamps.update({point: ctx.now_utc for point in batch}),
        )
        # Bind both freshly retrieved and reused members to a stable metadata
        # snapshot. A transition/failure never stamps old data as a new cycle.
        try:
            after = fetch_versions(source, capture=lambda name, payload, url=None: capture(f"{name}_after", payload, url), budget=budget)
            if before != after:
                raise RuntimeError("source version changed during retrieval")
        except Exception as exc:  # noqa: BLE001
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble verification failed after retrieval: {exc}", "warn")
            continue
        for point, location in fetched.items():
            fetched_at = received_at[point]
            try:
                cache.put(source, point, before, location, hours[point], fetched_at)
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                coverage[point]["errors"][source] = f"invalid member response: {exc}"
                continue
            hits[point] = (location, fetched_at.isoformat())
        for point, (location, stamp) in hits.items():
            locations[point].append(location)
            coverage[point]["fetched_at"][source] = stamp
            coverage[point]["source_versions"][source] = before
            if point not in fetched:
                coverage[point]["cached_sources"].append(source)
        for point in points:
            if source not in coverage[point]["fetched_at"]:
                coverage[point]["errors"].setdefault(source, str(failures[0][2]) if failures else "no member response")
        if failures:
            ctx.degrade("weather", f"{sport}: {source} full ensemble unavailable for {sum(size for _, size, _ in failures)} selected locations; successful batches retained ({failures[0][2]})", "warn")
    if cache.invalid:
        ctx.degrade("weather", f"{sport}: invalid raw-member cache entries were ignored", "warn")
    if not ctx.dry_run:
        cache.save(ctx.run_id, ctx.now_utc)
    return {point: combine(values) for point, values in locations.items() if values}, coverage
