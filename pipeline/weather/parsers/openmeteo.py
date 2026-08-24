"""Pure parser for Open-Meteo `/v1/forecast` payloads (single or batched).

Multi-model responses suffix each hourly variable with the model name
(`wind_speed_10m_ncep_hrrr_conus`); single-model / default responses have no
suffix and are filed under model ``best_match``.

Units are whatever the request asked for; the client always requests
F / mph / mm, so rows here are canonical without conversion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pipeline.weather.parsers import HourlyRow

VARIABLES = {
    "temperature_2m": "temp",
    "wind_speed_10m": "wind",
    "wind_gusts_10m": "gust",
    "wind_direction_10m": "dir",
    "precipitation": "precip",
    "precipitation_probability": "pop",
}

DEFAULT_MODEL = "best_match"


@dataclass
class ParsedLocation:
    latitude: float
    longitude: float
    elevation: Optional[float] = None
    units: dict[str, str] = field(default_factory=dict)
    models: dict[str, list[HourlyRow]] = field(default_factory=dict)

    def model_names(self) -> list[str]:
        return list(self.models.keys())

    def rows(self, model: str) -> list[HourlyRow]:
        return self.models.get(model, [])


def parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _split_key(key: str) -> Optional[tuple]:
    """'wind_speed_10m_ncep_hrrr_conus' -> ('wind', 'ncep_hrrr_conus'); 'temperature_2m' -> ('temp', 'best_match')."""
    # Longest variable name first so 'precipitation_probability' wins over 'precipitation'.
    for var in sorted(VARIABLES, key=len, reverse=True):
        if key == var:
            return VARIABLES[var], DEFAULT_MODEL
        if key.startswith(var + "_"):
            return VARIABLES[var], key[len(var) + 1:]
    return None


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_location(payload: dict[str, Any]) -> ParsedLocation:
    hourly = payload.get("hourly") or {}
    times = [parse_time(t) for t in hourly.get("time", [])]
    per_model: dict[str, dict[str, list[Optional[float]]]] = {}
    for key, values in hourly.items():
        if key == "time":
            continue
        split = _split_key(key)
        if split is None:
            continue
        canon, model = split
        per_model.setdefault(model, {})[canon] = [_num(v) for v in values]

    loc = ParsedLocation(
        latitude=float(payload.get("latitude", 0.0)),
        longitude=float(payload.get("longitude", 0.0)),
        elevation=_num(payload.get("elevation")),
        units=dict(payload.get("hourly_units") or {}),
    )
    for model, cols in per_model.items():
        rows: list[HourlyRow] = []
        for i, t in enumerate(times):
            def at(name: str, _cols: dict = cols, _i: int = i) -> Optional[float]:
                col = _cols.get(name)
                return col[_i] if col is not None and _i < len(col) else None

            rows.append(
                HourlyRow(
                    t=t,
                    temp=at("temp"),
                    wind=at("wind"),
                    gust=at("gust"),
                    dir=at("dir"),
                    precip=at("precip"),
                    pop=at("pop"),
                )
            )
        loc.models[model] = rows
    return loc


def parse_forecast(payload: Any) -> list[ParsedLocation]:
    """Accepts a single-location dict or the list Open-Meteo returns for batched coordinates."""
    if isinstance(payload, list):
        return [parse_location(p) for p in payload]
    if isinstance(payload, dict):
        if "error" in payload and payload.get("error"):
            raise ValueError(f"open-meteo error: {payload.get('reason')}")
        return [parse_location(payload)]
    raise TypeError(f"unexpected open-meteo payload type: {type(payload).__name__}")


def match_location(locations: Sequence[ParsedLocation], lat: float, lon: float, tol: float = 0.25) -> Optional[ParsedLocation]:
    """Open-Meteo snaps coordinates to its grid; pick the nearest returned location within `tol` degrees."""
    best: Optional[ParsedLocation] = None
    best_d = tol
    for loc in locations:
        d = max(abs(loc.latitude - lat), abs(loc.longitude - lon))
        if d <= best_d:
            best, best_d = loc, d
    return best


__all__ = ["ParsedLocation", "parse_forecast", "parse_location", "match_location", "parse_time", "VARIABLES"]
