"""Raw full-member snapshots keyed by verified provider dataset versions.

This caches no merged point forecast or blended percentile. Metadata identifies
the dataset snapshot, not a guaranteed initialization time for every retained
hour (a short newer IFS run may leave older long-range hours in place).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.weather.parsers.ensemble import EnsembleLocation, Member

SOURCES = {"ifs": ("ecmwf_ifs025", ("ecmwf_ifs025_ensemble",)),
           "gefs": ("gfs_seamless", ("ncep_gefs025", "ncep_gefs05"))}
METADATA_BASE = "https://ensemble-api.open-meteo.com/data"
SCHEMA = 1
MAX_CURRENT_DATASET_AGE_H = 18
MAX_REUSED_DATASET_AGE_H = 30
CONSISTENCY_SECONDS = 600


def version(payload: dict[str, Any], now: datetime) -> dict[str, int]:
    keys = ("last_run_initialisation_time", "last_run_modification_time",
            "last_run_availability_time", "update_interval_seconds", "data_end_time")
    value = {key: payload[key] for key in keys}
    if any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in value.values()):
        raise ValueError("invalid model-version metadata")
    clock = now.timestamp()
    available = value["last_run_availability_time"]
    if value["last_run_initialisation_time"] > available or value["last_run_modification_time"] > clock or available > clock:
        raise ValueError("future/inconsistent model-version metadata")
    if clock - value["last_run_initialisation_time"] > MAX_CURRENT_DATASET_AGE_H * 3600:
        raise ValueError("provider dataset initialization overdue/stale")
    if clock - available < CONSISTENCY_SECONDS:
        raise ValueError("model update settling (provider recommends 10 minutes)")
    if clock - available > value["update_interval_seconds"] + 1200:
        raise ValueError("provider source overdue/stale")
    return value


def reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite cache value: {value}")


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        reject_constant(value)
    return number


def decode_location(data: dict[str, Any]) -> EnsembleLocation:
    times = [datetime.fromisoformat(t) for t in data["times"]]
    if any(t.tzinfo is None for t in times) or len(set(times)) != len(times):
        raise ValueError("invalid member-cache times")
    members = {key: Member(**member) for key, member in data["members"].items()}
    if not members or any(key != member.key for key, member in members.items()):
        raise ValueError("invalid member-cache identities")
    for member in members.values():
        for values in (member.wind, member.gust, member.precip):
            if len(values) != len(times) or any(v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v)) for v in values):
                raise ValueError("invalid member-cache coverage")
    return EnsembleLocation(data["latitude"], data["longitude"], times, data["units"], members)


def combine(locations: list[EnsembleLocation]) -> EnsembleLocation | None:
    if not locations:
        return None
    times = sorted({t for location in locations for t in location.times})
    members = {}
    for location in locations:
        indexes = {t: i for i, t in enumerate(location.times)}
        for key, member in location.members.items():
            if key in members:
                raise ValueError("duplicate source/member identity")
            members[key] = Member(member.model, member.member, **{
                field: [getattr(member, field)[indexes[t]] if t in indexes else None for t in times]
                for field in ("wind", "gust", "precip")
            })
    first = locations[0]
    return EnsembleLocation(first.latitude, first.longitude, times, first.units, members)


def subset(location: EnsembleLocation, hours: set[datetime]) -> EnsembleLocation:
    indexes = [i for i, t in enumerate(location.times) if t in hours]
    return EnsembleLocation(location.latitude, location.longitude, [location.times[i] for i in indexes], location.units,
                            {key: Member(member.model, member.member, **{
                                field: [getattr(member, field)[i] for i in indexes] for field in ("wind", "gust", "precip")
                            }) for key, member in location.members.items()})


class MemberCache:
    def __init__(self, path: Path | None, parameters: dict[str, Any]) -> None:
        self.path = path
        self.parameters = parameters
        self.entries: dict[str, Any] = {}
        self.invalid = False
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant, parse_float=finite_float)
                if data["schema_version"] == SCHEMA and data["parameters"] == parameters:
                    self.entries = data["entries"]
                    if not isinstance(self.entries, dict):
                        raise ValueError("invalid member-cache entries")
            except (OSError, ValueError, TypeError, KeyError):
                self.invalid = True

    @staticmethod
    def key(source: str, point: tuple[float, float]) -> str:
        return f"{source}:{point[0]:.4f},{point[1]:.4f}"

    def get(self, source: str, point: tuple[float, float], versions: dict[str, Any], hours: set[datetime], *, now: datetime | None = None) -> tuple[EnsembleLocation, str] | None:
        entry = self.entries.get(self.key(source, point))
        if not entry:
            return None
        try:
            if entry.get("versions") != versions:
                return None
            location = decode_location(entry["location"])
            # Null member values remain null: a covered-but-partial response
            # need not be re-requested until its actual source version changes.
            if not hours.issubset(location.times):
                return None
            fetched = datetime.fromisoformat(entry["fetched_at"])
            timestamps = entry.get("fetched_by_hour", {})
            stamps = [datetime.fromisoformat(timestamps.get(t.isoformat(), entry["fetched_at"])) for t in hours]
            if fetched.tzinfo is None or any(t.tzinfo is None or (now is not None and t > now) for t in stamps):
                raise ValueError("invalid member retrieval timestamp")
            self.validate_source(source, location)
            fetched_at = min(stamps).isoformat()
            return subset(location, hours), fetched_at
        except (ValueError, TypeError, KeyError, AttributeError):
            self.invalid = True
            self.entries.pop(self.key(source, point), None)
            return None

    def previous(self, source: str, point: tuple[float, float], current: dict[str, Any], hours: set[datetime], *, now: datetime, max_age_h: float) -> tuple[EnsembleLocation, str, dict[str, Any]] | None:
        """Explicit age-bounded OLD version reuse; never relabel it as current."""
        entry = self.entries.get(self.key(source, point), {})
        if not isinstance(entry, dict):
            self.invalid = True
            return None
        versions = entry.get("versions")
        if not isinstance(versions, dict) or versions == current or versions.keys() != current.keys() or max_age_h <= 0:
            return None
        # These are dataset metadata ages, not provenance for every member hour.
        # An unknown/too-old initialization cannot use the distant reuse path.
        initializations = [v.get("last_run_initialisation_time") if isinstance(v, dict) else None for v in versions.values()]
        if any(not isinstance(t, int) or isinstance(t, bool) or not 0 <= now.timestamp() - t <= MAX_REUSED_DATASET_AGE_H * 3600 for t in initializations):
            return None
        hit = self.get(source, point, versions, hours, now=now)
        if hit and 0 <= (now - datetime.fromisoformat(hit[1])).total_seconds() < max_age_h * 3600:
            return hit[0], hit[1], versions
        return None

    def retained(self, source: str, point: tuple[float, float], hours: set[datetime], *, now: datetime, max_age_h: float) -> tuple[EnsembleLocation, str, dict[str, Any]] | None:
        """Bounded previously verified evidence when a current check fails.

        This makes no current-cycle claim. Only the original stored dataset
        metadata and per-hour retrieval timestamps can authorize retention.
        """
        entry = self.entries.get(self.key(source, point), {})
        if not isinstance(entry, dict):
            self.invalid = True
            return None
        versions = entry.get("versions")
        if not isinstance(versions, dict) or set(versions) != set(SOURCES[source][1]) or max_age_h <= 0:
            return None
        for metadata in versions.values():
            if not isinstance(metadata, dict):
                return None
            keys = ("last_run_initialisation_time", "last_run_modification_time",
                    "last_run_availability_time", "update_interval_seconds", "data_end_time")
            if any(not isinstance(metadata.get(k), int) or isinstance(metadata[k], bool) or metadata[k] <= 0 for k in keys):
                return None
            init, modified, available = (metadata[k] for k in keys[:3])
            if not init <= available <= now.timestamp() or modified > now.timestamp():
                return None
            if now.timestamp() - init > MAX_REUSED_DATASET_AGE_H * 3600:
                return None
        hit = self.get(source, point, versions, hours, now=now)
        if hit and 0 <= (now - datetime.fromisoformat(hit[1])).total_seconds() < max_age_h * 3600:
            return hit[0], hit[1], versions
        return None

    @staticmethod
    def validate_source(source: str, location: EnsembleLocation) -> None:
        expected = {"ifs": "ecmwf_ifs025_ensemble", "gefs": "ncep_gefs_seamless"}[source]
        if not location.members or any(m.model != expected for m in location.members.values()):
            raise ValueError("incorrect ensemble model identity")
        data = asdict(location)
        data["times"] = [t.isoformat() for t in location.times]
        decode_location(data)

    def put(self, source: str, point: tuple[float, float], versions: dict[str, Any], location: EnsembleLocation, hours: set[datetime], now: datetime) -> None:
        self.validate_source(source, location)
        clipped = subset(location, hours)
        fetched_by_hour = {t.isoformat(): now.isoformat() for t in clipped.times}
        # Accumulate additional game windows at a shared location only inside
        # an identical source version. Rescheduling can never reuse wrong hours.
        key = self.key(source, point)
        existing = self.entries.get(key)
        if existing and existing.get("versions") == versions:
            try:
                previous = decode_location(existing["location"])
                old_hours = set(previous.times) - set(clipped.times)
                if old_hours and previous.members.keys() == clipped.members.keys():
                    times = sorted(old_hours | set(clipped.times))
                    old_index, new_index = {t: i for i, t in enumerate(previous.times)}, {t: i for i, t in enumerate(clipped.times)}
                    members = {}
                    for name, member in clipped.members.items():
                        members[name] = Member(member.model, member.member, **{
                            field: [getattr(member, field)[new_index[t]] if t in new_index else getattr(previous.members[name], field)[old_index[t]] for t in times]
                            for field in ("wind", "gust", "precip")})
                    clipped = EnsembleLocation(clipped.latitude, clipped.longitude, times, clipped.units, members)
                    fetched_by_hour.update({t.isoformat(): existing.get("fetched_by_hour", {}).get(t.isoformat(), existing["fetched_at"]) for t in old_hours})
            except (ValueError, TypeError, KeyError):
                pass
        data = asdict(clipped)
        data["times"] = [t.isoformat() for t in clipped.times]
        self.entries[key] = {"versions": versions, "fetched_at": min(fetched_by_hour.values(), default=now.isoformat()),
                             "fetched_by_hour": fetched_by_hour, "location": data}

    def save(self, run_id: str, now: datetime) -> None:
        if self.path is None:
            return
        cutoff = now - timedelta(days=7)
        retained = {}
        for key, value in self.entries.items():
            try:
                if cutoff <= datetime.fromisoformat(value["fetched_at"]) <= now:
                    retained[key] = value
            except (KeyError, ValueError, TypeError):
                self.invalid = True
        self.entries = retained
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA, "run_id": run_id, "parameters": self.parameters, "entries": self.entries}
        self.path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")


def parameter_signature(hourly: str, units: dict[str, str]) -> dict[str, Any]:
    return json.loads(json.dumps({"models": SOURCES, "hourly": hourly, "units": units, "parser_schema": SCHEMA,
            "fingerprint": hashlib.sha256(json.dumps([SOURCES, hourly, units], sort_keys=True).encode()).hexdigest()}))


def complete(location: EnsembleLocation | None, hours: set[datetime]) -> bool:
    """Required wind/gust/rain fields for the game/display window, per member."""
    if location is None or not hours.issubset(location.times):
        return False
    indexes = [i for i, t in enumerate(location.times) if t in hours]
    return all(getattr(member, field)[i] is not None for member in location.members.values()
               for field in ("wind", "gust", "precip") for i in indexes)
