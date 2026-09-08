"""Pure parser for Open-Meteo's precomputed ensemble mean + spread payload.

The lighter endpoint is used when a full-member ensemble request is rate
limited.  Values are already in canonical units requested by the client
(mph / mm), and a single GEFS mean model returns unsuffixed hourly keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pipeline.weather.parsers.openmeteo import parse_time


@dataclass
class EnsembleMeanLocation:
    latitude: float
    longitude: float
    times: list[datetime] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    model: str = "ncep_gefs_ensemble_mean_seamless"
    wind: list[Optional[float]] = field(default_factory=list)
    wind_spread: list[Optional[float]] = field(default_factory=list)
    gust: list[Optional[float]] = field(default_factory=list)
    gust_spread: list[Optional[float]] = field(default_factory=list)
    precip: list[Optional[float]] = field(default_factory=list)
    precip_spread: list[Optional[float]] = field(default_factory=list)


def _nums(values: Any) -> list[Optional[float]]:
    if not isinstance(values, list):
        return []
    out: list[Optional[float]] = []
    for value in values:
        try:
            out.append(None if value is None else float(value))
        except (TypeError, ValueError):
            out.append(None)
    return out


def parse_ensemble_mean_location(
    payload: dict[str, Any],
    *,
    model: str = "ncep_gefs_ensemble_mean_seamless",
) -> EnsembleMeanLocation:
    hourly = payload.get("hourly") or {}
    return EnsembleMeanLocation(
        latitude=float(payload.get("latitude", 0.0)),
        longitude=float(payload.get("longitude", 0.0)),
        times=[parse_time(t) for t in hourly.get("time", [])],
        units=dict(payload.get("hourly_units") or {}),
        model=model,
        wind=_nums(hourly.get("wind_speed_10m")),
        wind_spread=_nums(hourly.get("wind_speed_10m_spread")),
        gust=_nums(hourly.get("wind_gusts_10m")),
        gust_spread=_nums(hourly.get("wind_gusts_10m_spread")),
        precip=_nums(hourly.get("precipitation")),
        precip_spread=_nums(hourly.get("precipitation_spread")),
    )


def parse_ensemble_mean(
    payload: Any,
    *,
    model: str = "ncep_gefs_ensemble_mean_seamless",
) -> list[EnsembleMeanLocation]:
    if isinstance(payload, list):
        return [parse_ensemble_mean_location(item, model=model) for item in payload]
    if isinstance(payload, dict):
        if payload.get("error"):
            raise ValueError(f"open-meteo ensemble-mean error: {payload.get('reason')}")
        return [parse_ensemble_mean_location(payload, model=model)]
    raise TypeError(f"unexpected ensemble-mean payload type: {type(payload).__name__}")


__all__ = ["EnsembleMeanLocation", "parse_ensemble_mean_location", "parse_ensemble_mean"]
