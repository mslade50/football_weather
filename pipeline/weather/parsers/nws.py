"""Pure parser for NWS `/gridpoints/{wfo}/{x},{y}` raw grid payloads.

Each field is a list of ``{validTime: '<iso>/<ISO8601 duration>', value}``.
Durations are expanded to hourly rows; the field's ``uom`` decides the unit
conversion into the canonical F / mph / degrees / mm / percent used by
:class:`pipeline.weather.parsers.HourlyRow`.

Instantaneous fields (temperature, wind, PoP) repeat their value across the
duration; accumulations (quantitativePrecipitation) are spread evenly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pipeline.weather.parsers import HourlyRow

KMH_TO_MPH = 0.621371
MS_TO_MPH = 2.236936

FIELDS: dict[str, str] = {
    "temperature": "temp",
    "windSpeed": "wind",
    "windGust": "gust",
    "windDirection": "dir",
    "probabilityOfPrecipitation": "pop",
    "quantitativePrecipitation": "precip",
}
ACCUMULATED = {"precip"}

_DUR_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(s: str) -> timedelta:
    m = _DUR_RE.match(s)
    if not m:
        raise ValueError(f"unsupported ISO8601 duration: {s!r}")
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    return timedelta(
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )


def parse_valid_time(s: str) -> tuple[datetime, timedelta]:
    start_s, _, dur_s = s.partition("/")
    start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    return start, parse_duration(dur_s) if dur_s else timedelta(hours=1)


def _converter(uom: Optional[str]) -> Callable[[float], float]:
    u = (uom or "").split(":")[-1]
    if u == "degC":
        return lambda c: c * 9.0 / 5.0 + 32.0
    if u == "degF":
        return lambda f: f
    if u == "km_h-1":
        return lambda k: k * KMH_TO_MPH
    if u == "m_s-1":
        return lambda m: m * MS_TO_MPH
    if u == "mm":
        return lambda mm: mm
    if u == "in":
        return lambda i: i * 25.4
    return lambda v: v  # percent, degree_(angle), mph, unknown


def expand_field(field: dict[str, Any], accumulated: bool = False) -> dict[datetime, float]:
    """Expand one gridpoint field into {hour_start_utc: value} in canonical units."""
    conv = _converter(field.get("uom"))
    out: dict[datetime, float] = {}
    for item in field.get("values") or []:
        raw = item.get("value")
        if raw is None:
            continue
        start, dur = parse_valid_time(item["validTime"])
        n_hours = max(1, int(round(dur.total_seconds() / 3600.0)))
        val = conv(float(raw))
        per_hour = val / n_hours if accumulated else val
        for h in range(n_hours):
            out[start + timedelta(hours=h)] = per_hour
    return out


def parse_gridpoints(payload: dict[str, Any]) -> list[HourlyRow]:
    props = payload.get("properties") or payload
    columns: dict[str, dict[datetime, float]] = {}
    for nws_name, canon in FIELDS.items():
        fld = props.get(nws_name)
        if isinstance(fld, dict):
            columns[canon] = expand_field(fld, accumulated=canon in ACCUMULATED)
        else:
            columns[canon] = {}
    all_hours = sorted({t for col in columns.values() for t in col})
    return [
        HourlyRow(
            t=t,
            temp=columns["temp"].get(t),
            wind=columns["wind"].get(t),
            gust=columns["gust"].get(t),
            dir=columns["dir"].get(t),
            precip=columns["precip"].get(t),
            pop=columns["pop"].get(t),
        )
        for t in all_hours
    ]


def grid_meta(payload: dict[str, Any]) -> dict[str, Any]:
    props = payload.get("properties") or payload
    return {
        "gridId": props.get("gridId"),
        "gridX": props.get("gridX"),
        "gridY": props.get("gridY"),
        "updateTime": props.get("updateTime"),
        "validTimes": props.get("validTimes"),
    }


__all__ = [
    "KMH_TO_MPH",
    "FIELDS",
    "parse_duration",
    "parse_valid_time",
    "expand_field",
    "parse_gridpoints",
    "grid_meta",
]
