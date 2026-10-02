"""Raw point inputs for the dependent job only, never across ordinary runs."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.weather.member_cache import finite_float, reject_constant
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.openmeteo import ParsedLocation


def cycle() -> str | None:
    run = os.environ.get("GITHUB_RUN_ID")
    return f"{run}:{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}" if run else None


def identities(games: list[Any], stadiums: dict[str, Any], roofs: dict[str, Any]) -> list[Any]:
    return sorted((g.game_id, g.kickoff_utc.isoformat(), g.tz, stadiums[g.game_id].stadium_id,
                   stadiums[g.game_id].lat, stadiums[g.game_id].lon, stadiums[g.game_id].orientation_deg,
                   stadiums[g.game_id].roof_type, roofs.get(g.game_id)) for g in games)


def key(point: tuple[float, float]) -> str:
    return f"{point[0]:.4f},{point[1]:.4f}"


def signature(ctx: Any, om: Any) -> Any:
    return {"schema_version": 1, "cycle": cycle(), "git_sha": ctx.git_sha,
            "models": [om.CONUS_MODELS, om.INTL_MODELS], "hourly": om.HOURLY, "units": om.UNIT_PARAMS}


def rows(values: list[dict[str, Any]]) -> list[HourlyRow]:
    return [HourlyRow(**{**value, "t": datetime.fromisoformat(value["t"])}) for value in values]


def read(directory: Path | None, ctx: Any, sport: str, om: Any, by_point: dict[Any, Any], stadiums: dict[str, Any], roofs: dict[str, Any], hours: dict[Any, Any]) -> dict[Any, Any]:
    if not directory or not cycle() or not ctx.git_sha:
        return {}
    try:
        payload = json.loads((directory / f"{sport}.json").read_text(encoding="utf-8"), parse_constant=reject_constant, parse_float=finite_float)
        if payload["signature"] != signature(ctx, om):
            return {}
        result = {}
        for point, games in by_point.items():
            entry = payload["entries"].get(key(point))
            if not entry or entry["identities"] != json.loads(json.dumps(identities(games, stadiums, roofs))):
                continue
            fetched = datetime.fromisoformat(entry["fetched_at"])
            if not 0 <= (ctx.now_utc - fetched).total_seconds() <= 1800:
                continue
            data = entry["om"]
            location = ParsedLocation(**{**data, "models": {model: rows(values) for model, values in data["models"].items()}}) if data else None
            nws = rows(entry["nws"])
            covered = {r.t for values in (location.models.values() if location else []) for r in values} | {r.t for r in nws}
            if not hours[point].issubset(covered):
                continue
            result[point] = location, nws, fetched
        if result:
            ctx.degrade("weather", f"{sport}: reused {len(result)} same-workflow raw point inputs; original timestamps, current lead weights", "info")
        return result
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        ctx.degrade("weather", f"{sport}: point handoff invalid/unavailable; fetching fresh sources", "info")
        return {}


def write(directory: Path | None, ctx: Any, sport: str, om: Any, by_point: dict[Any, Any], stadiums: dict[str, Any], roofs: dict[str, Any], hours: dict[Any, Any], forecasts: dict[Any, Any], nws: dict[Any, Any], stamps: dict[Any, Any]) -> None:
    if not directory or not cycle() or not ctx.git_sha or ctx.dry_run:
        return
    entries = {}
    for point, games in by_point.items():
        location = forecasts.get(point)
        data = asdict(location) if location is not None else None
        if data:
            data["models"] = {model: [row for row in values if row["t"] in hours[point]] for model, values in data["models"].items()}
        entries[key(point)] = {"identities": identities(games, stadiums, roofs), "om": data,
                               "nws": [asdict(row) for row in nws.get(point, []) if row.t in hours[point]],
                               "fetched_at": stamps[point].isoformat()}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sport}.json").write_text(json.dumps({"signature": signature(ctx, om), "entries": entries}, default=lambda v: v.isoformat(), allow_nan=False), encoding="utf-8")
