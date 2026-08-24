"""Pure parser for Open-Meteo `/v1/ensemble` payloads (single or batched).

Key layout observed live (ARCH §6):
  ``wind_speed_10m_ecmwf_ifs025_ensemble``            control run of a model
  ``wind_speed_10m_member07_ecmwf_ifs025_ensemble``   perturbed member 07
  ``precipitation_member30_ncep_gefs_seamless``       GFS is reported as ``ncep_gefs_seamless``

Every (model, member) pair becomes one :class:`Member` with parallel per-hour
lists in canonical units (mph / mm — the client requests them explicitly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pipeline.weather.parsers.openmeteo import parse_time

VARIABLES = {
    "wind_speed_10m": "wind",
    "wind_gusts_10m": "gust",
    "precipitation": "precip",
}
CONTROL = "control"

_KEY_RE = re.compile(r"^(?P<var>wind_speed_10m|wind_gusts_10m|precipitation)(?:_member(?P<member>\d+))?_(?P<model>.+)$")


@dataclass
class Member:
    model: str
    member: str  # 'control' or 'member07'
    wind: list[Optional[float]] = field(default_factory=list)
    gust: list[Optional[float]] = field(default_factory=list)
    precip: list[Optional[float]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.model}:{self.member}"


@dataclass
class EnsembleLocation:
    latitude: float
    longitude: float
    times: list[datetime] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    members: dict[str, Member] = field(default_factory=dict)  # 'model:member' -> Member

    @property
    def models(self) -> list[str]:
        seen: list[str] = []
        for m in self.members.values():
            if m.model not in seen:
                seen.append(m.model)
        return seen

    def n_members(self, model: Optional[str] = None) -> int:
        return sum(1 for m in self.members.values() if model is None or m.model == model)

    def series(self, name: str) -> dict[str, list[Optional[float]]]:
        """{member_key: per-hour values} for ``name`` in ('wind','gust','precip')."""
        return {k: getattr(m, name) for k, m in self.members.items()}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_ensemble_location(payload: dict[str, Any]) -> EnsembleLocation:
    hourly = payload.get("hourly") or {}
    loc = EnsembleLocation(
        latitude=float(payload.get("latitude", 0.0)),
        longitude=float(payload.get("longitude", 0.0)),
        times=[parse_time(t) for t in hourly.get("time", [])],
        units=dict(payload.get("hourly_units") or {}),
    )
    for key, values in hourly.items():
        if key == "time":
            continue
        m = _KEY_RE.match(key)
        if not m:
            continue
        canon = VARIABLES[m.group("var")]
        member = f"member{m.group('member')}" if m.group("member") else CONTROL
        model = m.group("model")
        mk = f"{model}:{member}"
        mem = loc.members.get(mk)
        if mem is None:
            mem = loc.members[mk] = Member(model=model, member=member)
        setattr(mem, canon, [_num(v) for v in values])
    return loc


def parse_ensemble(payload: Any) -> list[EnsembleLocation]:
    if isinstance(payload, list):
        return [parse_ensemble_location(p) for p in payload]
    if isinstance(payload, dict):
        if payload.get("error"):
            raise ValueError(f"open-meteo ensemble error: {payload.get('reason')}")
        return [parse_ensemble_location(payload)]
    raise TypeError(f"unexpected ensemble payload type: {type(payload).__name__}")


__all__ = ["VARIABLES", "CONTROL", "Member", "EnsembleLocation", "parse_ensemble_location", "parse_ensemble"]
