"""Fetch/reuse verified individual model snapshots, then pool original members."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.weather.member_cache import (  # noqa: F401
    SOURCES,
    MemberCache,
    combine,
    complete,
    parameter_signature,
    version,
)

VERIFIED_SOURCE_SECONDS = 60
FAILED_CHECK_RETENTION_HOURS = 3


def _recent_check(ctx: Any, source: str, signature: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """A stable provider check from this build, never a persisted/new-run claim."""
    check = ctx.weather_state.get("verified_member_sources", {}).get(source)
    if not check or check.get("identity") != (ctx.run_id, ctx.git_sha, signature):
        return None
    try:
        at = datetime.fromisoformat(check["verified_at"])
        if at.tzinfo is None or not 0 <= (ctx.now_utc - at).total_seconds() < VERIFIED_SOURCE_SECONDS:
            return None
        versions = check["versions"]
        if set(versions) != set(SOURCES[source][1]):
            return None
        # Reapply future, settling, overdue and initialization-age rules now.
        for data in versions.values():
            version(data, ctx.now_utc)
        return deepcopy(versions), check["verified_at"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _cache_hits(cache: MemberCache, ctx: Any, source: str, points: list[Any], versions: dict[str, Any], hours: dict[Any, Any]) -> tuple[dict[Any, Any], list[Any]]:
    hits, missing = {}, []
    for point in points:
        hit = cache.get(source, point, versions, hours[point], now=ctx.now_utc)
        if hit:
            hits[point] = (*hit, versions)
            continue
        old = cache.previous(source, point, versions, hours[point], now=ctx.now_utc,
                             max_age_h=ctx.weather_state.get("member_max_age", {}).get(point, 0))
        if old:
            hits[point] = old
        else:
            missing.append(point)
    return hits, missing


def members(
    ctx: Any, om: Any, points: list[tuple[float, float]], windows: dict[Any, Any],
    hours: dict[Any, set[datetime]], capture: Callable[..., Any], fetch_batches: Callable[..., Any],
    *, state_dir: Path | None = None, sport: str,
) -> tuple[dict[Any, Any], dict[Any, dict[str, Any]]]:
    if not points:
        return {}, {}
    budget = ctx.request_budgets.get("openmeteo")
    signature = parameter_signature(om.ENSEMBLE_HOURLY, om.ENSEMBLE_UNIT_PARAMS)
    if "member_cache" not in ctx.weather_state or ctx.weather_state["member_cache"].parameters != signature:
        ctx.weather_state["member_cache"] = MemberCache(state_dir / "ensemble_cache.json" if state_dir else None, signature)
    cache = ctx.weather_state["member_cache"]
    locations: dict[Any, list[Any]] = {point: [] for point in points}
    coverage = {point: {"fetched_at": {}, "source_versions": {}, "cached_sources": [], "aged_sources": [], "unverified_sources": [], "errors": {}} for point in points}

    def retain_unverified(source: str) -> None:
        for point in points:
            bound = max(FAILED_CHECK_RETENTION_HOURS, ctx.weather_state.get("member_max_age", {}).get(point, 0))
            hit = cache.retained(source, point, hours[point], now=ctx.now_utc, max_age_h=bound)
            if hit is None:
                continue
            location, stamp, original_versions = hit
            locations[point].append(location)
            coverage[point]["fetched_at"][source] = stamp
            coverage[point]["source_versions"][source] = original_versions
            coverage[point]["cached_sources"].append(source)
            coverage[point]["aged_sources"].append(source)
            coverage[point]["unverified_sources"].append(source)
    fetch_versions = getattr(om, "fetch_model_versions", None)
    for source, (model, _) in SOURCES.items():
        recent = _recent_check(ctx, source, signature)
        if recent:
            hits, missing = _cache_hits(cache, ctx, source, points, recent[0], hours)
            # Any new/extended window requires fresh before/after network checks.
            if missing or _recent_check(ctx, source, signature) is None:
                recent = None
        try:
            if not callable(fetch_versions):
                raise RuntimeError("model-version client unavailable")
            if recent:
                before = recent[0]
                capture(f"ensemble_verified_{source}_reused", {"versions": before, "verified_at": recent[1],
                        "run_id": ctx.run_id, "git_sha": ctx.git_sha, "parameters": signature, "cache_only": True})
                ctx.degrade("weather", f"{sport}: {source} cached members use stable provider metadata checked less than 60 seconds ago in this build; original versions/timestamps", "info")
            else:
                before = fetch_versions(source, capture=lambda name, payload, url=None: capture(f"{name}_before", payload, url), budget=budget)
        except Exception as exc:  # noqa: BLE001
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble source cannot be verified for {len(points)} selected locations: {exc}", "warn")
            retain_unverified(source)
            continue
        if not recent:
            hits, missing = _cache_hits(cache, ctx, source, points, before, hours)
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
            if not recent:
                after = fetch_versions(source, capture=lambda name, payload, url=None: capture(f"{name}_after", payload, url), budget=budget)
                if before != after:
                    raise RuntimeError("source version changed during retrieval")
                ctx.weather_state.setdefault("verified_member_sources", {})[source] = {
                    "identity": (ctx.run_id, ctx.git_sha, deepcopy(signature)),
                    "versions": deepcopy(after), "verified_at": ctx.now_utc.isoformat(),
                }
        except Exception as exc:  # noqa: BLE001
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble verification failed after retrieval: {exc}", "warn")
            retain_unverified(source)
            continue
        for point, location in fetched.items():
            fetched_at = received_at[point]
            try:
                cache.put(source, point, before, location, hours[point], fetched_at)
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                coverage[point]["errors"][source] = f"invalid member response: {exc}"
                continue
            hits[point] = (location, fetched_at.isoformat(), before)
        for point, (location, stamp, actual_versions) in hits.items():
            locations[point].append(location)
            coverage[point]["fetched_at"][source] = stamp
            coverage[point]["source_versions"][source] = actual_versions
            if actual_versions != before:
                coverage[point]["aged_sources"].append(source)
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
