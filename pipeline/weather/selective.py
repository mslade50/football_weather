"""Fetch/reuse verified individual model snapshots, then pool original members."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.weather import ecmwf_fallback
from pipeline.weather.member_cache import (  # noqa: F401
    SOURCES,
    MemberCache,
    ModelSettling,
    combine,
    complete,
    parameter_signature,
    version,
)

log = logging.getLogger(__name__)

# The provider's consistency window is 10 minutes. Wait it out once, then
# recheck, instead of publishing a partial ensemble for the rest of the run.
SETTLE_PAD_S = 2.0
DEFAULT_SETTLE_WAIT_S = 620.0
DEFAULT_PREVIOUS_RUN_H = 12.0

VERIFIED_SOURCE_SECONDS = 60
FAILED_CHECK_RETENTION_HOURS = 3


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def settle_wait_cap() -> float:
    """``ECMWF_OM_SETTLE_WAIT_S=0`` skips the wait and keeps the previous run."""
    raw = os.environ.get("ECMWF_OM_SETTLE_WAIT_S", "").strip()
    if not raw:
        return DEFAULT_SETTLE_WAIT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SETTLE_WAIT_S


def previous_run_hours() -> float:
    """How long a complete prior IFS/GEFS snapshot may stand in after a failed check."""
    raw = os.environ.get("ECMWF_OM_PREVIOUS_RUN_HOURS", "").strip()
    if not raw:
        return DEFAULT_PREVIOUS_RUN_H
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PREVIOUS_RUN_H


def _versions_after_settle(fetch_versions: Callable[..., Any], source: str, budget: Any, capture: Callable[..., Any], sport: str) -> Any:
    """Fetch provider metadata, waiting out one settle window when it still fits the cap."""

    def once(suffix: str) -> Any:
        return fetch_versions(
            source,
            capture=lambda name, payload, url=None, suffix=suffix: capture(f"{name}_{suffix}", payload, url),
            budget=budget,
        )

    try:
        return once("before")
    except ModelSettling as exc:
        cap = settle_wait_cap()
        wait = exc.remaining_s + SETTLE_PAD_S
        if cap <= 0 or wait > cap:
            log.warning(
                "%s: %s ensemble is settling (%.0fs left, wait cap %.0fs); using the previous complete run if it covers these games",
                sport, source, exc.remaining_s, cap,
            )
            raise
        log.warning(
            "%s: %s ensemble is settling; waiting %.0fs for the provider window, then rechecking",
            sport, source, wait,
        )
        _sleep(wait)
        log.info("%s: %s ensemble settle wait finished; rechecking provider metadata", sport, source)
        return once("before")


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

    def retain_unverified(source: str, *, max_age_h: float | None = None) -> int:
        kept = 0
        for point in points:
            bound = max(FAILED_CHECK_RETENTION_HOURS, ctx.weather_state.get("member_max_age", {}).get(point, 0))
            if max_age_h is not None:
                bound = max(bound, max_age_h)
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
            kept += 1
        return kept

    def log_source(source: str, how: str) -> None:
        model_name = {"ifs": "ecmwf_ifs025_ensemble", "gefs": "ncep_gefs_seamless"}[source]
        counts = []
        run_text = "unknown"
        for point in points:
            counts.append(sum(
                1 for loc in locations[point] for member in loc.members.values() if member.model == model_name
            ))
            versions = (coverage[point].get("source_versions") or {}).get(source) or {}
            if run_text == "unknown" and isinstance(versions, dict):
                for meta in versions.values():
                    if not isinstance(meta, dict):
                        continue
                    if isinstance(meta.get("last_run_initialisation_time"), int):
                        run_text = datetime.fromtimestamp(
                            meta["last_run_initialisation_time"], timezone.utc,
                        ).strftime("%Y-%m-%d %HZ")
                        break
                    if meta.get("run"):
                        run_text = str(meta["run"])
                        break
        log.info(
            "%s %s ensemble %s: run=%s locations=%d member_counts=%s",
            sport, source, how, run_text, len(points), counts,
        )
        for point, count in zip(points, counts, strict=True):
            log.info(
                "%s %s ensemble %.4f,%.4f source=%s run=%s members=%d",
                sport, source, point[0], point[1], how, run_text, count,
            )

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
                before = _versions_after_settle(fetch_versions, source, budget, capture, sport)
        except ModelSettling as exc:
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble source cannot be verified for {len(points)} selected locations: {exc}", "warn")
            kept = retain_unverified(source, max_age_h=previous_run_hours())
            log.warning(
                "%s: %s ensemble stayed unverified after the settle check; previous complete run covered %d/%d locations",
                sport, source, kept, len(points),
            )
            log_source(source, "previous-run" if kept else "missing")
            continue
        except Exception as exc:  # noqa: BLE001
            for point in points:
                coverage[point]["errors"][source] = str(exc)
            ctx.degrade("weather", f"{sport}: {source} ensemble source cannot be verified for {len(points)} selected locations: {exc}", "warn")
            retain_unverified(source)
            log_source(source, "previous-run" if any(source in coverage[point]["fetched_at"] for point in points) else "missing")
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
            log_source(source, "previous-run" if any(source in coverage[point]["fetched_at"] for point in points) else "missing")
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
        cached_points = [source in coverage[point].get("cached_sources", []) for point in points]
        if points and all(cached_points):
            how = "cache"
        elif any(cached_points):
            how = "open-meteo+cache"
        else:
            how = "open-meteo"
        log_source(source, how)
    ecmwf_fallback.supplement_ensemble(ctx, sport, points, hours, locations, coverage, capture)
    if cache.invalid:
        ctx.degrade("weather", f"{sport}: invalid raw-member cache entries were ignored", "warn")
    if not ctx.dry_run:
        cache.save(ctx.run_id, ctx.now_utc)
    return {point: combine(values) for point, values in locations.items() if values}, coverage
