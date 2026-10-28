"""Use ECMWF Open Data when Open-Meteo errors, returns 429, or serves a stale ECMWF run.

``ECMWF_OPENDATA_FALLBACK`` selects the mode:

* ``auto`` — keep a fresh Open-Meteo ECMWF forecast; replace it after an error,
  an HTTP 429, an empty ECMWF window, or an initialization that is more than
  18 hours old or at least one cycle behind the published open-data run
* ``force`` — always take ECMWF fields from open data (other models still come
  from Open-Meteo when that call succeeds)
* ``off`` — never call the mirrors

Forced mode is also ``python -m pipeline.build --ecmwf-opendata force`` and the
``ecmwf_opendata`` workflow input. The replacement rows use the same model ids
(``ecmwf_ifs025``, ``ecmwf_aifs025_single``, ``ecmwf_ifs025_ensemble``) as
Open-Meteo, and the forecast records ``ecmwf_source=ecmwf-opendata``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from pipeline.weather import ecmwf_opendata as od
from pipeline.weather.parsers.ensemble import EnsembleLocation
from pipeline.weather.parsers.openmeteo import ParsedLocation

log = logging.getLogger(__name__)

IFS_ENSEMBLE = od.ENS_MODEL


def prepare(ctx: Any) -> dict[str, Any]:
    """Status block for meta.json / the status tab. Safe to call more than once."""
    current = ctx.weather_state.get("ecmwf_opendata")
    if isinstance(current, dict) and current.get("license") == od.LICENSE:
        current["mode"] = od.fallback_mode()
        return current
    status = {
        "mode": od.fallback_mode(),
        "used": False,
        "license": od.LICENSE,
        "attribution": od.ATTRIBUTION,
        "bytes": 0,
        "seconds": 0.0,
        "points": 0,
        "ensemble_points": 0,
        "runs": {},
        "mirror": None,
        "reason": None,
    }
    ctx.weather_state["ecmwf_opendata"] = status
    return status


def session_for(ctx: Any) -> od.OpenDataSession:
    session = ctx.weather_state.get("ecmwf_session")
    if not isinstance(session, od.OpenDataSession):
        session = od.OpenDataSession()
        ctx.weather_state["ecmwf_session"] = session
    return session


def forecast_markers(point_meta: Optional[dict[str, Any]], trace: Optional[dict[str, Any]]) -> tuple[Optional[str], dict[str, str]]:
    """Source label and the receipt times that belong on the served forecast."""
    meta = point_meta or {}
    source = meta.get("ecmwf_source")
    if (trace or {}).get("ecmwf_source") == "ecmwf-opendata":
        source = "ecmwf-opendata"
    updated: dict[str, str] = {}
    if meta.get("updated_at"):
        updated["nws"] = str(meta["updated_at"])
    if meta.get("ecmwf_run"):
        updated["ecmwf_opendata"] = str(meta["ecmwf_run"])
    return source, updated


def _publish(ctx: Any, session: od.OpenDataSession, reason: str, count: int, *, ensemble: bool) -> None:
    status = prepare(ctx)
    status["used"] = True
    status["reason"] = reason
    status["bytes"] = session.downloaded_bytes
    status["seconds"] = round(session.seconds, 3)
    status["mirror"] = session.mirror
    status["runs"] = {name: stamp.isoformat() for name, stamp in session.runs.items()}
    if ensemble:
        status["ensemble_points"] = int(status.get("ensemble_points") or 0) + count
    else:
        status["points"] = int(status.get("points") or 0) + count


def _capture(ctx: Any, session: od.OpenDataSession, capture: Any) -> None:
    if capture is None:
        return
    status = prepare(ctx)
    start = int(status.get("captured") or 0)
    for manifest in session.manifests[start:]:
        capture("ecmwf_opendata", manifest, manifest.get("mirror"))
    status["captured"] = len(session.manifests)


def _mark(ctx: Any, point: tuple[float, float], source: str, run_iso: Optional[str], *, filled_gap: bool) -> None:
    meta = ctx.weather_state.setdefault("point_meta", {})
    details = dict(meta.get(point) or {})
    details["ecmwf_source"] = source
    if run_iso:
        details["ecmwf_run"] = run_iso
    if filled_gap:
        details["stage"] = "ecmwf_opendata"
        details["aged"] = False
    meta[point] = details


def _failure_reason(failures: list[tuple[Any, ...]]) -> str:
    text = str(failures[0][2]) if failures else "open-meteo request failed"
    if "429" in text:
        return "open-meteo rate limited (HTTP 429)"
    return text


def supplement_forecasts(
    ctx: Any,
    sport: str,
    points: list[tuple[float, float]],
    forecasts: dict[tuple[float, float], ParsedLocation],
    failures: list[tuple[Any, ...]],
    hours: dict[Any, set[datetime]],
    capture: Any,
    stamps: dict[Any, datetime],
) -> None:
    """Fill or replace ECMWF point models. Other models on the location stay put."""
    status = prepare(ctx)
    mode = status["mode"]
    if mode == "off" or not points:
        return
    session = session_for(ctx)
    if not session.can_fetch():
        return
    failed = [point for point in points if point not in forecasts] if failures else []
    reason = None
    targets: list[tuple[float, float]] = []
    if mode == "force":
        targets = list(points)
        reason = "forced"
    else:
        if failed:
            targets.extend(failed)
            reason = _failure_reason(failures)
        init = session.openmeteo_init() if any(point in forecasts for point in points) else None
        for point in points:
            if point not in forecasts:
                continue
            missing = od.window_missing(forecasts[point], set(hours.get(point) or ()))
            if od.init_is_stale(init, ctx.now_utc, missing=missing):
                if point not in targets:
                    targets.append(point)
                reason = reason or ("ECMWF window empty" if missing else "stale ECMWF initialization")
        if not targets:
            for point in points:
                location = forecasts.get(point)
                if location is not None and od.IFS_MODEL in location.models:
                    _mark(ctx, point, "open-meteo", None, filled_gap=False)
            return
    try:
        filled = session.point_forecasts(targets, {point: set(hours.get(point) or ()) for point in targets})
    except Exception as exc:  # noqa: BLE001 - Open-Meteo result remains in place
        log.warning("%s ECMWF open-data point fallback failed: %s", sport, exc)
        ctx.degrade("weather", f"{sport}: ECMWF Open Data fallback failed ({exc})", "warn")
        status["error"] = f"{type(exc).__name__}: {exc}"
        return
    run_iso = session.runs.get("ifs", session.runs.get("aifs-single"))
    run_text = run_iso.isoformat() if run_iso is not None else None
    used = 0
    for point, location in filled.items():
        if not location.models:
            continue
        base = forecasts.get(point)
        if base is None:
            forecasts[point] = location
            stamps[point] = ctx.now_utc
            _mark(ctx, point, "ecmwf-opendata", run_text, filled_gap=True)
        else:
            models = dict(base.models)
            models.update(location.models)
            forecasts[point] = type(base)(
                latitude=base.latitude, longitude=base.longitude,
                elevation=base.elevation, units={**base.units, **location.units}, models=models,
            )
            _mark(ctx, point, "ecmwf-opendata", run_text, filled_gap=False)
        used += 1
    if not used:
        return
    _publish(ctx, session, reason or "ecmwf-opendata", used, ensemble=False)
    _capture(ctx, session, capture)
    ctx.degrade(
        "weather",
        f"{sport}: ECMWF Open Data fallback ({reason}) at {used} locations; {od.ATTRIBUTION}",
        "info",
    )


def _has_ifs(locations: list[EnsembleLocation]) -> bool:
    return any(member.model == IFS_ENSEMBLE for location in locations for member in location.members.values())


def supplement_ensemble(
    ctx: Any,
    sport: str,
    points: list[tuple[float, float]],
    hours: dict[Any, set[datetime]],
    locations: dict[Any, list[EnsembleLocation]],
    coverage: dict[Any, dict[str, Any]],
    capture: Any,
) -> None:
    """Replace a failed or forced IFS member pull. GEFS members on the same point stay."""
    status = prepare(ctx)
    mode = status["mode"]
    if mode == "off" or not points:
        return
    session = session_for(ctx)
    if not session.can_fetch():
        return
    targets = []
    for point in points:
        present = _has_ifs(locations.get(point) or [])
        errored = "ifs" in (coverage.get(point) or {}).get("errors", {})
        if mode == "force" or not present or errored:
            targets.append(point)
    if not targets:
        return
    reason = "forced" if mode == "force" else "IFS ensemble unavailable"
    try:
        filled = session.ensemble_forecasts(targets, {point: set(hours.get(point) or ()) for point in targets})
    except Exception as exc:  # noqa: BLE001
        log.warning("%s ECMWF open-data ensemble fallback failed: %s", sport, exc)
        ctx.degrade("weather", f"{sport}: ECMWF Open Data ensemble fallback failed ({exc})", "warn")
        status["error"] = f"{type(exc).__name__}: {exc}"
        return
    if not filled:
        detail = session.stopped_early or session.cache_note or (
            "no control or perturbed index entries at the requested steps"
        )
        log.warning(
            "%s ECMWF open-data ensemble fallback found no fields (%s); the Open-Meteo ensemble is unchanged",
            sport, detail,
        )
        ctx.degrade(
            "weather",
            f"{sport}: ECMWF Open Data ensemble fallback found no fields ({detail})",
            "warn",
        )
        return
    run = session.runs.get("ifs")
    used = 0
    for point, location in filled.items():
        if not location.members:
            continue
        previous = [item for item in locations.get(point, []) if _has_ifs([item])]
        previous_members = sum(item.n_members(IFS_ENSEMBLE) for item in previous)
        # A short download must not wipe a fuller ensemble already in hand
        # (the previous Open-Meteo run, or an earlier complete open-data pass).
        if location.n_members(IFS_ENSEMBLE) < 51 and location.n_members(IFS_ENSEMBLE) <= previous_members:
            log.warning(
                "%s ECMWF open-data %.4f,%.4f returned %d members; keeping the existing %d-member IFS ensemble",
                sport, point[0], point[1], location.n_members(IFS_ENSEMBLE), previous_members,
            )
            continue
        kept = [item for item in locations.get(point, []) if not _has_ifs([item])]
        kept.append(location)
        locations[point] = kept
        slot = coverage.setdefault(point, {})
        slot["ecmwf_source"] = "ecmwf-opendata"
        errors = slot.setdefault("errors", {})
        errors.pop("ifs", None)
        slot.setdefault("fetched_at", {})["ifs"] = ctx.now_utc.isoformat()
        slot.setdefault("source_versions", {})["ifs"] = {
            "ecmwf_ifs025_ensemble": {
                "source": "ecmwf-opendata",
                "run": None if run is None else run.isoformat(),
                "license": od.LICENSE,
            }
        }
        meta = ctx.weather_state.setdefault("point_meta", {})
        details = dict(meta.get(point) or {})
        details["ecmwf_source"] = "ecmwf-opendata"
        if run is not None:
            details["ecmwf_run"] = run.isoformat()
        meta[point] = details
        used += 1
    if not used:
        return
    _publish(ctx, session, reason, used, ensemble=True)
    _capture(ctx, session, capture)
    counts = [location.n_members(IFS_ENSEMBLE) for location in filled.values() if location.members]
    mirror = session.mirror or "cache"
    run_text = None if run is None else run.isoformat()
    log.info(
        "%s ECMWF ensemble open-data: run=%s mirror=%s locations=%d member_counts=%s",
        sport, run_text or "unknown", mirror, used, counts,
    )
    ctx.degrade(
        "weather",
        f"{sport}: ECMWF Open Data ensemble fallback ({reason}) at {used} locations "
        f"via {mirror} run {run_text or 'unknown'} members={counts}; {od.ATTRIBUTION}",
        "info",
    )


__all__ = [
    "forecast_markers",
    "prepare",
    "session_for",
    "supplement_ensemble",
    "supplement_forecasts",
]
