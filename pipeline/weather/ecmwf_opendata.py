"""ECMWF Open Data fallback for the fields this pipeline already reads.

ECMWF Open Data (IFS and AIFS on data.ecmwf.int, with AWS and Azure mirrors)
is free to use, including commercially, under Creative Commons Attribution 4.0
(CC BY 4.0). Attribute ECMWF when the fallback supplies a forecast:

    ECMWF Open Data (CC BY 4.0). Source: European Centre for Medium-Range
    Weather Forecasts. https://www.ecmwf.int/en/forecasts/datasets/open-data

The client reads each step's ``.index``, then HTTP byte-ranges only the surface
messages the board needs (10 m u/v wind, total precipitation, 2 m temperature,
and gust when the file has it). Each message is sampled at the stadiums and
dropped before the next one is decoded; the GRIB temp file is removed after
the step. Steps are only those that bracket a game hour inside
``ECMWF_OD_MAX_LEAD_H`` (168 h). ``ECMWF_OD_CACHE_ONLY=1`` (the BetOnline
publish job) reads the sample cache and does not download. Mirrors are Google,
data.ecmwf.int, AWS, then Azure; a 503 SlowDown fails that mirror over instead
of abandoning the run. Sampled fields are cached by model run so the next
pipeline pass in the same 00z/12z cycle does not download them again. Values
are converted into the same hourly ``ParsedLocation`` /
``EnsembleLocation`` rows Open-Meteo produces (F, mph, meteorological degrees,
mm), so merge and the impact model do not change.

``eccodes`` decodes GRIB2 and is imported only when a download is decoded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import shutil
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline.weather.ecmwf_ranges import (
    AZURE_SAS_URL,
    MIRROR_URLS,
    IndexedMessages,
    coalesce_ranges,
    object_stem,
    parse_index_entries,
    select_indexed,
)
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import CONTROL, EnsembleLocation, Member
from pipeline.weather.parsers.openmeteo import ParsedLocation
from pipeline.weather.qc import plausible_precip_mm

log = logging.getLogger(__name__)

LICENSE = "CC-BY-4.0"
ATTRIBUTION = (
    "ECMWF Open Data (CC BY 4.0). Source: European Centre for Medium-Range "
    "Weather Forecasts. https://www.ecmwf.int/en/forecasts/datasets/open-data"
)
USER_AGENT = "football_weather (mckinleyslade@gmail.com)"
IFS_MODEL = "ecmwf_ifs025"
AIFS_MODEL = "ecmwf_aifs025_single"
ENS_MODEL = "ecmwf_ifs025_ensemble"
MPS_TO_MPH = 2.2369362920544
PUBLICATION_LAG_H = 8.0
STALE_INIT_H = 18.0
CYCLE_H = 6.0
DEFAULT_MAX_BYTES = 120 * 1024 * 1024
# One product (point forecast or ensemble) and the whole session. Byte ranges
# are small, but a cold CFB slate can still touch a few dozen steps. These caps
# leave the rest of the 30 minute light job room. Both are env-tunable.
FETCH_BUDGET_S = 240.0
TOTAL_BUDGET_S = 480.0
# Game hours farther out than this are left on Open-Meteo. A 222 h ensemble
# step is ~100 MB of GRIB and is past the 7-day medium-range window.
MAX_LEAD_H = 168.0
SLOWDOWN_ATTEMPTS = 3
SLOWDOWN_EVENT_LIMIT = 4
SLOWDOWN_BASE_S = 1.0
SLOWDOWN_CAP_S = 8.0
HTTP_CONNECT_TIMEOUT_S = 5.0
HTTP_READ_TIMEOUT_S = 20.0
RANGE_WORKERS = 8
SAMPLE_SCHEMA = 1
SAMPLE_KEEP_H = 36.0
_TRANSIENT_TRANSPORT = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "ChunkedEncodingError", "ProxyError",
    "ConnectError", "TimeoutException", "ReadError", "RemoteProtocolError",
    "NetworkError", "PoolTimeout",
})
# Google has been answering range requests while the AWS bucket returns
# 503 SlowDown at cycle publication. Official ECMWF is next, then AWS, then
# Azure (which needs a short-lived Planetary Computer SAS token).
MIRRORS = ("google", "ecmwf", "aws", "azure")
UNITS = {
    "temperature_2m": "°F",
    "wind_speed_10m": "mp/h",
    "wind_gusts_10m": "mp/h",
    "wind_direction_10m": "°",
    "precipitation": "mm",
}
POINT_PARAMS = ("2t", "10u", "10v", "tp")
ENS_PARAMS = ("10u", "10v", "tp")
# IFS cycle 50r1 (operational 2026-05-12) stopped publishing stream=enfo type=cf.
# The ensemble control is the high-resolution forecast (oper/fc). Perturbed
# members stay type=pf inside files named enfo-ef; the index records are still pf.
IFS_CONTROL_CUTOVER = datetime(2026, 5, 12, tzinfo=timezone.utc)
_STEP_NAME = re.compile(r"-(\d+)h-([A-Za-z0-9]+)-([A-Za-z0-9]+)\.index$")


def filename_type(kind: str, model: str) -> str:
    """GRIB filename type. IFS ``cf``/``pf`` files were renamed to ``ef``."""
    if model != "aifs-ens" and kind in ("cf", "pf"):
        return "ef"
    return kind


def control_candidates(run: datetime) -> tuple[tuple[str, str], ...]:
    """Stream/type pairs for the ensemble control, preferred pair first."""
    if aware(run) >= IFS_CONTROL_CUTOVER:
        return (("oper", "fc"), ("enfo", "cf"))
    return (("enfo", "cf"), ("oper", "fc"))


def perturbed_candidates() -> tuple[tuple[str, str], ...]:
    """Perturbed members. ``pf`` matches the index; ``ef`` is the filename fallback."""
    return (("enfo", "pf"), ("enfo", "ef"))


def parse_published_steps(names: Iterable[str], stream: str, url_type: str) -> list[int]:
    """Forecast steps whose index files are actually on the mirror."""
    found: set[int] = set()
    for name in names:
        match = _STEP_NAME.search(str(name).rsplit("/", 1)[-1])
        if match and match.group(2) == stream and match.group(3) == url_type:
            found.add(int(match.group(1)))
    return sorted(found)


def cover_with_available(requested: Sequence[int], available: Sequence[int]) -> list[int]:
    """Published steps that bracket each requested step.

    An exact step is kept. A step the index does not have is replaced by the
    nearest published steps on either side (at most 6 h away), so wind can
    still be interpolated and accumulated ``tp`` can still be turned into an
    hourly amount. ``tp`` in the open catalogue is still metres since step 0;
    ``tprate`` is a rate and is not used.
    """
    avail = sorted({int(step) for step in available})
    if not avail:
        return []
    needed: set[int] = set()
    for raw in requested:
        step = int(raw)
        if step in avail:
            needed.add(step)
            continue
        if step < avail[0] or step > avail[-1]:
            nearest = min(avail, key=lambda item: (abs(item - step), item))
            if abs(nearest - step) <= 6:
                needed.add(nearest)
            continue
        prev = max(item for item in avail if item < step)
        nxt = min(item for item in avail if item > step)
        if step - prev <= 6:
            needed.add(prev)
        if nxt - step <= 6:
            needed.add(nxt)
    return sorted(needed)


def gust_groups(steps: Sequence[int]) -> list[tuple[str, tuple[int, ...]]]:
    """Open-data gust short name changes with the step.

    Steps 1–90 h and the 6-hourly tail from 150 h are ``10fg``. Steps 93–144 h
    are ``10fg3`` (max gust since the previous post-processing). Step 0 has no
    interval to attach a gust to.
    """
    grouped: dict[str, list[int]] = {}
    for step in steps:
        if step <= 0:
            continue
        name = "10fg3" if 93 <= step <= 144 else "10fg"
        grouped.setdefault(name, []).append(int(step))
    return [(name, tuple(grouped[name])) for name in ("10fg", "10fg3") if name in grouped]


class OpenDataError(RuntimeError):
    """The open-data subset could not be retrieved or decoded."""


class OpenDataBudget(OpenDataError):
    """The fetch or session time budget ran out. Callers keep Open-Meteo."""


class OpenDataSlowDown(OpenDataError):
    """ECMWF returned 503 SlowDown and the fetch is not going to keep crawling."""


class TooLarge(OpenDataError):
    """The indexed byte ranges exceed the per-request cap."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


def slowdown_give_up_enabled() -> bool:
    """``ECMWF_OD_SLOWDOWN_GIVEUP=0`` retries the same mirror for longer.

    With mirror failover on (the default) a SlowDown still moves to the next
    mirror either way. Failover off plus this flag restores the old behaviour:
    stop the fetch on the first mirror that returns 503.
    """
    raw = os.environ.get("ECMWF_OD_SLOWDOWN_GIVEUP", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def mirror_failover_enabled() -> bool:
    """``ECMWF_OD_MIRROR_FAILOVER=0`` stops the fetch on the first 503."""
    raw = os.environ.get("ECMWF_OD_MIRROR_FAILOVER", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def slowdown_backoff(attempt: int, rng: Optional[Any] = None) -> float:
    """Equal-jitter exponential delay. ``attempt`` is 1 for the first retry."""
    window = min(SLOWDOWN_CAP_S, SLOWDOWN_BASE_S * (2 ** max(0, attempt - 1)))
    roll = random.random() if rng is None else rng()
    if roll < 0.0:
        roll = 0.0
    elif roll > 1.0:
        roll = 1.0
    return window * 0.5 + window * 0.5 * roll


def is_slowdown_response(response: Any) -> bool:
    """503 SlowDown (and 429) from an ECMWF mirror. Other statuses are not."""
    status = getattr(response, "status_code", None)
    if status == 429:
        return True
    return status == 503


def is_slowdown_error(exc: BaseException) -> bool:
    if isinstance(exc, OpenDataSlowDown):
        return True
    response = getattr(exc, "response", None)
    if response is not None and is_slowdown_response(response):
        return True
    text = str(exc)
    return "SlowDown" in text or "Slow Down" in text


def _transient_transport(exc: BaseException) -> bool:
    return type(exc).__name__ in _TRANSIENT_TRANSPORT


def fallback_mode() -> str:
    """``auto`` (default), ``force``, or ``off``. ``ECMWF_OPENDATA_FALLBACK``."""
    raw = os.environ.get("ECMWF_OPENDATA_FALLBACK", "auto").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return "off"
    if raw in ("1", "on", "force", "opendata"):
        return "force"
    return "auto"


def cache_only_enabled() -> bool:
    """``ECMWF_OD_CACHE_ONLY=1`` reads sampled fields and does not download GRIB."""
    raw = os.environ.get("ECMWF_OD_CACHE_ONLY", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def network_allowed() -> bool:
    """Unit tests never touch ECMWF unless they opt in or inject a retriever."""
    if os.environ.get("ECMWF_OPENDATA_ALLOW_NETWORK") == "1":
        return True
    return not os.environ.get("PYTEST_CURRENT_TEST")


def aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def kelvin_to_f(kelvin: float) -> float:
    return (kelvin - 273.15) * 1.8 + 32.0


def wind_from_uv(u_ms: float, v_ms: float) -> tuple[float, float]:
    """Sustained 10 m wind. ``u``/``v`` are eastward/northward m/s.

    Direction is meteorological degrees (the direction the wind comes from).
    """
    import math

    speed_mph = math.hypot(u_ms, v_ms) * MPS_TO_MPH
    direction = (270.0 - math.degrees(math.atan2(v_ms, u_ms))) % 360.0
    if direction >= 360.0 - 1e-9:
        direction = 0.0
    return speed_mph, direction


def expected_published_run(now: datetime, lag_h: float = PUBLICATION_LAG_H) -> datetime:
    """Latest 00/06/12/18 cycle that should already be on the open-data mirrors."""
    ready = aware(now) - timedelta(hours=lag_h)
    hour = (ready.hour // 6) * 6
    return ready.replace(hour=hour, minute=0, second=0, microsecond=0)


def init_is_stale(init: Optional[datetime], now: datetime, *, missing: bool) -> bool:
    """Empty ECMWF windows, an init older than 18 h, or a cycle behind the mirrors."""
    if missing:
        return True
    if init is None:
        return False
    age_h = (aware(now) - aware(init)).total_seconds() / 3600.0
    if age_h > STALE_INIT_H:
        return True
    return (expected_published_run(now) - aware(init)).total_seconds() >= CYCLE_H * 3600.0


def native_steps(model: str, run_hour: int) -> list[int]:
    """IFS is 3-hourly to 144 h (6-hourly after that on 00/12). AIFS is 6-hourly."""
    if model == "aifs-single":
        return list(range(0, 361, 6))
    steps = list(range(0, 145, 3))
    if run_hour in (0, 12):
        steps.extend(range(150, 361, 6))
    return steps


def runs_for_hours(model: str, latest: datetime, hours: Iterable[datetime]) -> list[tuple[datetime, set[datetime]]]:
    """Assign each hour to the newest cycle whose native steps reach it.

    IFS 06 and 18 UTC runs stop at 144 h. Hours past that use the previous
    00 or 12 UTC run, which continues to 360 h. AIFS cycles all reach 360 h.
    """
    latest = aware(latest)
    remaining = {aware(hour) for hour in hours}
    candidates = [latest]
    if model == "ifs" and latest.hour not in (0, 12):
        candidates.append(latest - timedelta(hours=6))
    assigned: list[tuple[datetime, set[datetime]]] = []
    for run in candidates:
        native = native_steps(model, run.hour)
        if not native:
            continue
        covered = {
            hour for hour in remaining
            if native[0] <= (hour - run).total_seconds() / 3600.0 <= native[-1]
        }
        if covered:
            assigned.append((run, covered))
            remaining -= covered
    return assigned


def canonical_param(name: str) -> str:
    """Gust is stored as ``10fg``. Open-data steps 93–144 h request it as ``10fg3``."""
    return "10fg" if name in ("10fg", "10fg3") else str(name)


def stored_params(params: Iterable[str]) -> set[str]:
    return {canonical_param(param) for param in params}


def hours_within_lead(run: datetime, hours: Iterable[datetime], max_lead_h: float) -> set[datetime]:
    """Game hours whose forecast step from ``run`` is inside ``max_lead_h``.

    A negative cap keeps every hour (``ECMWF_OD_MAX_LEAD_H=-1``).
    """
    aware_hours = {aware(hour) for hour in hours}
    if max_lead_h < 0:
        return aware_hours
    origin = aware(run)
    return {
        hour for hour in aware_hours
        if 0.0 <= (hour - origin).total_seconds() / 3600.0 <= max_lead_h
    }


def steps_for_hours(run: datetime, hours: Iterable[datetime], native: Sequence[int]) -> list[int]:
    """Native steps that bracket each hour, plus the previous step so rain can be deaccumulated."""
    ordered = list(native)
    if not ordered:
        return []
    needed: set[int] = set()
    origin = aware(run)
    for hour in hours:
        lead = (aware(hour) - origin).total_seconds() / 3600.0
        if lead < ordered[0] or lead > ordered[-1]:
            continue
        prev = ordered[0]
        for index, step in enumerate(ordered):
            if step >= lead:
                needed.add(prev)
                needed.add(step)
                if lead <= prev and index > 0:
                    needed.add(ordered[index - 1])
                break
            prev = step
    return sorted(needed)


def _lerp(left: Optional[float], right: Optional[float], s0: int, s1: int, lead: float) -> Optional[float]:
    if s0 == s1 or abs(lead - s0) < 1e-9:
        return left
    if abs(lead - s1) < 1e-9:
        return right
    if left is None or right is None:
        return None
    weight = (lead - s0) / (s1 - s0)
    return left + (right - left) * weight


def _bracket(lead: float, steps: Sequence[int]) -> Optional[tuple[int, int]]:
    prev = steps[0]
    for step in steps:
        if step >= lead:
            return prev, step
        prev = step
    return None


def precip_mm(samples: dict[int, dict[str, float]], lead: float, steps: Sequence[int]) -> Optional[float]:
    """Hourly mm from accumulated ``tp`` (metres since step 0), spread across the native interval."""
    if lead <= 0:
        return 0.0 if 0 in samples else None
    bracket = _bracket(lead, steps)
    if bracket is None:
        return None
    s0, s1 = bracket
    if lead <= s0:
        earlier = [step for step in steps if step < s0]
        if not earlier:
            return 0.0 if s0 == 0 else None
        s1 = s0
        s0 = earlier[-1]
    if s1 == s0:
        return None
    start = samples.get(s0, {}).get("tp")
    end = samples.get(s1, {}).get("tp")
    if start is None or end is None:
        return None
    hourly = max(0.0, (end - start) * 1000.0) / (s1 - s0)
    return plausible_precip_mm(hourly)


def hour_state(
    samples: dict[int, dict[str, float]], run: datetime, hour: datetime, *, gust: bool,
) -> Optional[HourlyRow]:
    """One canonical hourly row. Components are interpolated; gust is the interval maximum."""
    steps = sorted(samples)
    if not steps:
        return None
    lead = (aware(hour) - aware(run)).total_seconds() / 3600.0
    bracket = _bracket(lead, steps)
    if bracket is None or lead < steps[0] or lead > steps[-1]:
        return HourlyRow(t=aware(hour))
    s0, s1 = bracket
    left, right = samples.get(s0, {}), samples.get(s1, {})
    temp_k = _lerp(left.get("2t"), right.get("2t"), s0, s1, lead)
    u = _lerp(left.get("10u"), right.get("10u"), s0, s1, lead)
    v = _lerp(left.get("10v"), right.get("10v"), s0, s1, lead)
    speed = direction = None
    if u is not None and v is not None:
        speed, direction = wind_from_uv(u, v)
    gust_ms = None
    if gust:
        gust_step = s1 if lead > s0 else s0
        gust_ms = samples.get(gust_step, {}).get("10fg")
    return HourlyRow(
        t=aware(hour),
        temp=None if temp_k is None else kelvin_to_f(temp_k),
        wind=speed,
        gust=None if gust_ms is None else gust_ms * MPS_TO_MPH,
        dir=direction,
        precip=precip_mm(samples, lead, steps),
    )


def rows_for_hours(
    samples: dict[int, dict[str, float]], run: datetime, hours: Iterable[datetime], *, gust: bool,
) -> list[HourlyRow]:
    out = []
    for hour in sorted({aware(item) for item in hours}):
        row = hour_state(samples, run, hour, gust=gust)
        if row is not None:
            out.append(row)
    return out


@dataclass(frozen=True)
class GridField:
    """One decoded GRIB message, row-major from the first grid point."""

    param: str
    step: int
    lat0: float
    lon0: float
    dlat: float
    dlon: float
    ni: int
    nj: int
    values: Sequence[float]
    member: Optional[int] = None
    missing: Optional[float] = None

    def __post_init__(self) -> None:
        if self.ni < 1 or self.nj < 1 or len(self.values) != self.ni * self.nj:
            raise OpenDataError(f"{self.param} step {self.step} grid is {len(self.values)} values, expected {self.ni * self.nj}")


def _finite(value: Any, missing: Optional[float]) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if missing is not None and abs(number - missing) <= 1e-3 * max(1.0, abs(missing)):
        return None
    if abs(number) > 1e10:
        return None
    return number


def sample_grid(field: GridField, lat: float, lon: float) -> Optional[float]:
    """Bilinear sample. Longitudes may be -180..180 or 0..360."""
    import math

    if field.dlon == 0 or field.dlat == 0:
        return None
    west = field.lon0
    point_lon = lon
    if west >= 0 and point_lon < 0:
        point_lon += 360.0
    elif west < 0 and point_lon > 180:
        point_lon -= 360.0
    x = (point_lon - west) / field.dlon
    y = (lat - field.lat0) / field.dlat
    if abs(field.dlon * field.ni) >= 359:
        x = x % field.ni
    if x < -1e-6 or y < -1e-6 or y > field.nj - 1 + 1e-6:
        return None
    if abs(field.dlon * field.ni) < 359 and x > field.ni - 1 + 1e-6:
        return None
    i0 = int(math.floor(x)) % field.ni
    j0 = min(field.nj - 1, max(0, int(math.floor(y))))
    i1 = (i0 + 1) % field.ni if abs(field.dlon * field.ni) >= 359 else min(field.ni - 1, i0 + 1)
    j1 = min(field.nj - 1, j0 + 1)
    wx = x - math.floor(x)
    wy = min(1.0, max(0.0, y - j0))
    corners = (
        (i0, j0, (1.0 - wx) * (1.0 - wy)),
        (i1, j0, wx * (1.0 - wy)),
        (i0, j1, (1.0 - wx) * wy),
        (i1, j1, wx * wy),
    )
    total = weight = 0.0
    for i, j, w in corners:
        if w <= 0:
            continue
        number = _finite(field.values[j * field.ni + i], field.missing)
        if number is None:
            continue
        total += number * w
        weight += w
    if weight <= 0:
        return None
    return total / weight


@dataclass(frozen=True)
class Request:
    model: str
    run: datetime
    stream: str
    kind: str
    steps: tuple[int, ...]
    params: tuple[str, ...]
    numbers: tuple[int, ...] = ()
    # ``control`` labels oper/fc (or legacy enfo/cf) as the ensemble control.
    # ``perturbed`` labels enfo/pf members. Empty for deterministic point fields.
    role: str = ""

    @property
    def key(self) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "run": aware(self.run).strftime("%Y%m%d%H"),
                "stream": self.stream,
                "kind": self.kind,
                "steps": list(self.steps),
                "params": list(self.params),
                "numbers": list(self.numbers),
                "role": self.role,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def cache_name(self) -> str:
        stamp = aware(self.run).strftime("%Y%m%d%H")
        return f"{self.model}-{stamp}-{self.stream}-{self.kind}-{self.key}.grib2"


def member_label(request: Request, grid: GridField) -> Optional[str]:
    """Open-Meteo member id for one decoded field. Deterministic rows stay unlabeled."""
    if request.role == "control" or request.kind == "cf":
        return CONTROL
    if grid.member is None or int(grid.member) <= 0:
        return None
    return f"member{int(grid.member):02d}"


def prune_sample_cache(cache_dir: Path, now: datetime, *, keep_hours: float = SAMPLE_KEEP_H) -> None:
    """Drop sampled fields from model runs older than ``keep_hours``."""
    root = cache_dir / "samples"
    if not root.is_dir():
        return
    raw = os.environ.get("ECMWF_OD_SAMPLE_KEEP_H", "").strip()
    if raw:
        try:
            keep_hours = float(raw)
        except ValueError:
            pass
    cutoff = aware(now) - timedelta(hours=keep_hours)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            stamp = datetime.strptime(child.name, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def _point_key(point: tuple[float, float]) -> str:
    return f"{point[0]:.4f},{point[1]:.4f}"


def _part_bytes(result: Any) -> int:
    total = 0
    for item in getattr(result, "urls", ()) or ():
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], tuple):
            total += sum(int(part[1]) for part in item[1] if isinstance(part, tuple) and len(part) >= 2)
    return total


def decode_grib(path: Path) -> Iterator[GridField]:
    """Yield one field per message. Requires the ``eccodes`` package."""
    try:
        from eccodes import codes_get, codes_get_string, codes_get_values, codes_grib_new_from_file, codes_release
    except ImportError as exc:
        raise OpenDataError("eccodes is not installed; GRIB2 fallback cannot decode") from exc

    def key(gid: int, name: str, default: Any = None) -> Any:
        try:
            return codes_get(gid, name)
        except Exception:  # noqa: BLE001 - optional GRIB keys differ by message
            return default

    with path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                name = str(codes_get_string(gid, "shortName"))
                if name in ("10fg", "10fg3"):
                    name = "10fg"
                step = int(key(gid, "endStep", key(gid, "step", 0)) or 0)
                ni = int(key(gid, "Ni"))
                nj = int(key(gid, "Nj"))
                dlon = abs(float(key(gid, "iDirectionIncrementInDegrees")))
                dlat = abs(float(key(gid, "jDirectionIncrementInDegrees")))
                if int(key(gid, "iScansNegatively", 0) or 0):
                    dlon = -dlon
                if not int(key(gid, "jScansPositively", 0) or 0):
                    dlat = -dlat
                kind = str(key(gid, "marsType", "") or "")
                member = None
                if kind == "pf":
                    member = int(key(gid, "perturbationNumber"))
                elif kind == "cf":
                    member = 0
                values = codes_get_values(gid)
                missing = key(gid, "missingValue")
                yield GridField(
                    param=name, step=step,
                    lat0=float(key(gid, "latitudeOfFirstGridPointInDegrees")),
                    lon0=float(key(gid, "longitudeOfFirstGridPointInDegrees")),
                    dlat=dlat, dlon=dlon, ni=ni, nj=nj, values=values,
                    member=member, missing=None if missing is None else float(missing),
                )
            finally:
                codes_release(gid)


def _client(source: str, model: str, owner: Optional[OpenDataSession] = None) -> Any:
    from ecmwf.opendata import Client

    # maximum_retries=1: multiurl must not sleep 2s per byte-range on 503.
    # SlowDown backoff lives in OpenDataSession.guarded_http, which also
    # enforces the fetch budget. A 120s read timeout is what let one file
    # crawl until the workflow was cancelled.
    client = Client(
        source=source, model=model, resol="0p25",
        maximum_retries=1, retry_after=1, use_server_retry_after=False,
    )
    client.session.headers["User-Agent"] = USER_AGENT
    original = client.session.request

    def request(method: str, url: str, **kwargs: Any) -> Any:
        if owner is None:
            kwargs["timeout"] = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)
            return original(method, url, **kwargs)
        return owner.guarded_http(original, method, url, **kwargs)

    client.session.request = request
    return client


@dataclass
class OpenDataSession:
    """One process-wide download cache. The same model run is decoded once."""

    cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("ECMWF_OD_CACHE", "data/cache/ecmwf")))
    retriever: Optional[Any] = None
    decoder: Any = decode_grib
    max_bytes: int = field(default_factory=lambda: int(os.environ.get("ECMWF_OD_MAX_BYTES", str(DEFAULT_MAX_BYTES))))
    sources: tuple[str, ...] = field(default_factory=lambda: tuple(
        part.strip() for part in os.environ.get("ECMWF_OD_SOURCES", ",".join(MIRRORS)).split(",") if part.strip()
    ))
    fixed_run: Optional[datetime] = None
    # Test hook: ``(Request) -> steps`` that are really in the index. ``None`` lists the mirror.
    step_catalog: Optional[Any] = None
    downloaded_bytes: int = 0
    seconds: float = 0.0
    mirror: Optional[str] = None
    runs: dict[str, datetime] = field(default_factory=dict)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    # Tests inject these. Production uses time.monotonic, time.sleep, and random.random.
    clock: Optional[Any] = None
    sleeper: Optional[Any] = None
    jitter: Optional[Any] = None
    fetch_budget_s: float = field(default_factory=lambda: _env_float("ECMWF_OD_FETCH_BUDGET_S", FETCH_BUDGET_S))
    total_budget_s: float = field(default_factory=lambda: _env_float("ECMWF_OD_TOTAL_BUDGET_S", TOTAL_BUDGET_S))
    max_lead_h: float = field(default_factory=lambda: _env_float("ECMWF_OD_MAX_LEAD_H", MAX_LEAD_H))
    # The BetOnline publish job reuses samples. A miss keeps Open-Meteo.
    cache_only: bool = field(default_factory=cache_only_enabled)
    cache_note: Optional[str] = None
    slowdown_attempts: int = field(default_factory=lambda: _env_int("ECMWF_OD_SLOWDOWN_ATTEMPTS", SLOWDOWN_ATTEMPTS, 1, 6))
    slowdown_event_limit: int = field(default_factory=lambda: _env_int("ECMWF_OD_SLOWDOWN_LIMIT", SLOWDOWN_EVENT_LIMIT, 1, 20))
    give_up_on_slowdown: bool = field(default_factory=slowdown_give_up_enabled)
    mirror_failover: bool = field(default_factory=mirror_failover_enabled)
    # Tests inject ``(method, url, headers) -> response``. Production uses httpx.
    transport: Optional[Any] = None
    range_workers: int = field(default_factory=lambda: _env_int("ECMWF_OD_RANGE_WORKERS", RANGE_WORKERS, 1, 32))
    stopped_early: Optional[str] = None
    _samples: dict[str, dict[tuple[float, float], dict[int, dict[str, float]]]] = field(default_factory=dict)
    _members: dict[str, dict[tuple[float, float], dict[str, dict[int, dict[str, float]]]]] = field(default_factory=dict)
    _active_s: float = field(default=0.0, init=False, repr=False)
    _fetch_started: Optional[float] = field(default=None, init=False, repr=False)
    _abandoned: Optional[str] = field(default=None, init=False, repr=False)
    _slowdown_events: int = field(default=0, init=False, repr=False)
    _index_cache: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _http: Any = field(default=None, init=False, repr=False)
    _lock: Optional[threading.Lock] = field(default=None, init=False, repr=False)
    _azure_sas: Optional[str] = field(default=None, init=False, repr=False)

    def _now(self) -> float:
        return time.monotonic() if self.clock is None else self.clock()

    def _sleep(self, seconds: float) -> None:
        (time.sleep if self.sleeper is None else self.sleeper)(seconds)

    def _roll(self) -> float:
        return random.random() if self.jitter is None else self.jitter()

    def begin_fetch(self) -> None:
        """Start the per-fetch clock. Idle time outside a fetch is not charged."""
        self.end_fetch()
        self._fetch_started = self._now()
        self._slowdown_events = 0
        self._abandoned = None
        self.stopped_early = None
        self.cache_note = None
        try:
            prune_sample_cache(self.cache_dir, datetime.now(timezone.utc))
            self._purge_grib()
        except OSError:
            log.info("ECMWF sample cache prune skipped")

    def end_fetch(self) -> None:
        """Stop charging the session budget. Odds scraping between sports is idle."""
        if self._fetch_started is None:
            return
        self._active_s += max(0.0, self._now() - self._fetch_started)
        self._fetch_started = None

    def _active_elapsed(self) -> float:
        extra = 0.0
        if self._fetch_started is not None:
            extra = max(0.0, self._now() - self._fetch_started)
        return self._active_s + extra

    def _remaining(self) -> float:
        now = self._now()
        limits: list[float] = []
        if self.total_budget_s >= 0:
            limits.append(self.total_budget_s - self._active_elapsed())
        if self._fetch_started is not None and self.fetch_budget_s >= 0:
            limits.append(self.fetch_budget_s - max(0.0, now - self._fetch_started))
        if not limits:
            return HTTP_READ_TIMEOUT_S
        return max(0.0, min(limits))

    def check_budget(self) -> None:
        """Raise when this fetch should stop and the caller should keep Open-Meteo."""
        now = self._now()
        if self._fetch_started is None:
            self._fetch_started = now
        if self.total_budget_s >= 0 and self._active_elapsed() >= self.total_budget_s:
            self._abandoned = "budget"
            raise OpenDataBudget(
                f"ECMWF open-data total budget of {self.total_budget_s:.0f}s exceeded"
            )
        if self.fetch_budget_s >= 0 and now - self._fetch_started >= self.fetch_budget_s:
            self._abandoned = "budget"
            raise OpenDataBudget(
                f"ECMWF open-data fetch budget of {self.fetch_budget_s:.0f}s exceeded"
            )
        if self._abandoned == "slowdown":
            raise OpenDataSlowDown("ECMWF open data gave up after 503 SlowDown")
        if self._abandoned == "budget":
            raise OpenDataBudget("ECMWF open-data budget already exceeded")

    def _state_lock(self) -> threading.Lock:
        if self._lock is None:
            self._lock = threading.Lock()
        return self._lock

    def _note_slowdown(self, url: str) -> None:
        self._slowdown_events += 1
        # Failover moves on to the next mirror. Poisoning the session here is
        # what made one AWS 503 throw away Azure, Google, and data.ecmwf.int.
        if self.mirror_failover:
            log.warning(
                "ECMWF 503 SlowDown on %s (%d this fetch); trying another mirror",
                url, self._slowdown_events,
            )
            return
        if self.give_up_on_slowdown and self._slowdown_events >= self.slowdown_event_limit:
            self._abandoned = "slowdown"
            log.warning(
                "ECMWF 503 SlowDown on %s (%d events); giving up this fetch and keeping other models",
                url, self._slowdown_events,
            )
            raise OpenDataSlowDown(f"503 SlowDown for {url}")

    def guarded_http(self, original: Any, method: str, url: str, **kwargs: Any) -> Any:
        """One HTTP call with a short timeout, SlowDown backoff, and the fetch budget.

        A returned 503 is not a reason to keep reading the rest of a multi-gigabyte
        GRIB. The attempt cap ends this URL; mirror failover then tries the next host.
        """
        lock = self._state_lock()
        with lock:
            self.check_budget()
            remaining = self._remaining()
            read = min(HTTP_READ_TIMEOUT_S, max(1.0, remaining))
        kwargs["timeout"] = (min(HTTP_CONNECT_TIMEOUT_S, read), read)
        attempts = max(1, self.slowdown_attempts)
        for attempt in range(1, attempts + 1):
            with lock:
                self.check_budget()
            try:
                response = original(method, url, **kwargs)
            except OpenDataBudget:
                raise
            except OpenDataSlowDown:
                raise
            except Exception as exc:  # noqa: BLE001 - transport blips only
                if not _transient_transport(exc) or attempt >= attempts:
                    raise
                delay = slowdown_backoff(attempt, self._roll)
                log.warning(
                    "ECMWF %s %s (%s); backing off %.2fs (attempt %d/%d)",
                    method, url, type(exc).__name__, delay, attempt, attempts,
                )
                with lock:
                    if delay >= self._remaining():
                        self._abandoned = "budget"
                        raise OpenDataBudget("ECMWF open-data budget exceeded during transport backoff") from exc
                self._sleep(delay)
                continue
            if not is_slowdown_response(response):
                return response
            with lock:
                self._note_slowdown(url)
            if attempt >= attempts:
                if self.give_up_on_slowdown and not self.mirror_failover:
                    self._abandoned = "slowdown"
                log.warning(
                    "ECMWF 503 SlowDown persisted for %s after %d attempts; falling back",
                    url, attempts,
                )
                raise OpenDataSlowDown(f"503 SlowDown for {url}")
            delay = slowdown_backoff(attempt, self._roll)
            log.warning(
                "ECMWF 503 SlowDown for %s; backing off %.2fs (attempt %d/%d)",
                url, delay, attempt, attempts,
            )
            with lock:
                if delay >= self._remaining():
                    self._abandoned = "budget"
                    raise OpenDataBudget("ECMWF open-data budget exceeded during SlowDown backoff")
            self._sleep(delay)
        raise OpenDataSlowDown(f"503 SlowDown for {url}")

    def can_fetch(self) -> bool:
        return self.retriever is not None or network_allowed()

    def run_for(self, model: str) -> datetime:
        if model in self.runs:
            return self.runs[model]
        if self.fixed_run is not None:
            found = aware(self.fixed_run)
            self.runs[model] = found
            return found
        if not network_allowed():
            raise OpenDataError("ECMWF open-data network is disabled")
        candidate = expected_published_run(datetime.now(timezone.utc))
        last: Optional[Exception] = None
        for back in range(4):
            run = candidate - timedelta(hours=6 * back)
            stem = object_stem(model, run, "oper", "fc", 0) + ".index"
            for source in self.sources:
                if source not in MIRROR_URLS:
                    continue
                try:
                    self.check_budget()
                    response = self._exchange("HEAD", self._object_url(source, stem))
                except OpenDataBudget:
                    raise
                except OpenDataSlowDown as exc:
                    last = exc
                    if not self.mirror_failover or self._abandoned == "slowdown":
                        raise
                    continue
                except Exception as exc:  # noqa: BLE001 - next mirror or previous cycle
                    last = exc
                    continue
                if getattr(response, "status_code", None) == 200:
                    self.mirror = source
                    self.runs[model] = aware(run)
                    log.info(
                        "ECMWF open-data %s run %s via %s",
                        model, aware(run).strftime("%Y-%m-%d %HZ"), source,
                    )
                    return self.runs[model]
        raise OpenDataError(f"no published {model} run ({last})")

    def openmeteo_init(self) -> Optional[datetime]:
        """Best-effort Open-Meteo ECMWF initialization. Metadata is outside the forecast quota."""
        cached = getattr(self, "_openmeteo_init", "missing")
        if cached != "missing":
            return cached
        self._openmeteo_init: Optional[datetime] = None
        if not network_allowed():
            return None
        try:
            import httpx
        except ImportError:
            return None
        urls = (
            "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json",
            "https://ensemble-api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json",
        )
        for url in urls:
            try:
                response = httpx.get(url, timeout=10.0, headers={"User-Agent": USER_AGENT})
                if response.status_code != 200:
                    continue
                stamp = int(response.json()["last_run_initialisation_time"])
                self._openmeteo_init = datetime.fromtimestamp(stamp, tz=timezone.utc)
                return self._openmeteo_init
            except Exception:  # noqa: BLE001 - staleness is optional
                continue
        return None

    def _purge_grib(self) -> None:
        """Drop temp GRIB left by a killed run. Sampled JSON is the cache."""
        grib = self.cache_dir / "grib"
        if grib.exists():
            shutil.rmtree(grib, ignore_errors=True)
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.glob("*.grib2"):
            path.unlink(missing_ok=True)
        for path in self.cache_dir.glob("*.grib2.part"):
            path.unlink(missing_ok=True)

    def _discard_grib(self, request: Request, path: Optional[Path]) -> None:
        targets: list[Path] = []
        if path is not None:
            targets.append(path)
            targets.append(Path(str(path) + ".part"))
        for step in request.steps:
            targets.append(self._step_grib_path(request, int(step)))
        for target in targets:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                log.info("ECMWF temp GRIB cleanup skipped for %s", target)

    def _http_client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        return self._http

    def _azure_sas_token(self) -> Optional[str]:
        if self._azure_sas is not None:
            return self._azure_sas or None
        if self.transport is not None:
            self._azure_sas = ""
            return None
        try:
            import httpx

            response = httpx.get(AZURE_SAS_URL, timeout=10.0, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            token = str(response.json().get("token") or "")
        except Exception as exc:  # noqa: BLE001 - Azure is one of four mirrors
            log.warning("ECMWF Azure SAS token unavailable: %s", exc)
            token = ""
        self._azure_sas = token
        return token or None

    def _object_url(self, source: str, path: str) -> str:
        base = MIRROR_URLS.get(source)
        if not base:
            raise OpenDataError(f"unknown ECMWF mirror {source}")
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        if source == "azure":
            token = self._azure_sas_token()
            if not token:
                raise OpenDataError("azure SAS token unavailable")
            url = f"{url}?{token}"
        return url

    def _exchange(self, method: str, url: str, extra_headers: Optional[dict[str, str]] = None) -> Any:
        headers = {"User-Agent": USER_AGENT}
        if extra_headers:
            headers.update(extra_headers)

        def original(call_method: str, call_url: str, **kwargs: Any) -> Any:
            if self.transport is not None:
                return self.transport(call_method, call_url, headers)
            return self._http_client().request(
                call_method, call_url, headers=headers, timeout=kwargs.get("timeout"),
            )

        return self.guarded_http(original, method, url)

    def _get_index_text(self, source: str, path: str) -> str:
        key = (source, path)
        cached = self._index_cache.get(key)
        if cached is not None:
            return cached
        response = self._exchange("GET", self._object_url(source, path))
        status = getattr(response, "status_code", None)
        if status != 200:
            raise OpenDataError(f"HTTP {status} for {path} via {source}")
        text = bytes(response.content).decode("utf-8")
        self._index_cache[key] = text
        return text

    def _get_ranges(self, source: str, path: str, spans: Sequence[tuple[int, int]]) -> bytes:
        merged = coalesce_ranges(spans, 0)
        parts: list[Optional[bytes]] = [None] * len(merged)

        def fetch(index: int, start: int, length: int) -> None:
            header = {"Range": f"bytes={start}-{start + length - 1}"}
            response = self._exchange("GET", self._object_url(source, path), header)
            status = getattr(response, "status_code", None)
            body = bytes(getattr(response, "content", b"") or b"")
            if status not in (200, 206):
                raise OpenDataError(f"HTTP {status} for {path} via {source}")
            if len(body) != length or not body.startswith(b"GRIB"):
                raise OpenDataError(
                    f"{source} returned {len(body)} bytes for a {length}-byte GRIB range of {path}"
                )
            parts[index] = body

        workers = max(1, min(self.range_workers, len(merged)))
        if workers == 1:
            for index, (start, length) in enumerate(merged):
                fetch(index, start, length)
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch, index, start, length) for index, (start, length) in enumerate(merged)]
                for future in futures:
                    future.result()
        return b"".join(part for part in parts if part is not None)

    def _step_grib_path(self, request: Request, step: int) -> Path:
        stamp = aware(request.run).strftime("%Y%m%d%H")
        token = hashlib.sha256(
            json.dumps(
                {"p": list(request.params), "n": list(request.numbers), "role": request.role},
                sort_keys=True,
            ).encode(),
        ).hexdigest()[:12]
        return self.cache_dir / "grib" / stamp / f"{request.model}-{request.stream}-{request.kind}-{int(step)}-{token}.grib2"

    def _fetch_step(self, request: Request, step: int) -> tuple[bytes, str, IndexedMessages]:
        file_type = filename_type(request.kind, request.model)
        stem = object_stem(request.model, aware(request.run), request.stream, file_type, step)
        last: Optional[Exception] = None
        for source in self.sources:
            if source not in MIRROR_URLS:
                continue
            try:
                self.check_budget()
                text = self._get_index_text(source, stem + ".index")
                indexed = select_indexed(parse_index_entries(text), request.params, request.numbers, step)
                if indexed.messages == 0:
                    raise OpenDataError(
                        f"no index entries for {request.stream}/{request.kind} "
                        f"{list(request.params)} step {step}"
                    )
                if indexed.nbytes > self.max_bytes:
                    raise TooLarge(f"{indexed.nbytes} bytes exceeds {self.max_bytes}")
                started = time.perf_counter()
                payload = self._get_ranges(source, stem + ".grib2", indexed.spans)
                self.seconds += time.perf_counter() - started
                member_count = len(indexed.members) or (1 if request.role == "control" else 0)
                log.info(
                    "ECMWF open-data %s %s/%s step %sh via %s: %d messages, %.2f MB, members=%d",
                    request.model, request.stream, request.kind, step, source,
                    indexed.messages, indexed.nbytes / 1_000_000, member_count,
                )
                return payload, source, indexed
            except TooLarge:
                raise
            except OpenDataBudget:
                raise
            except OpenDataSlowDown as exc:
                last = exc
                log.warning("ECMWF open data %s step %s via %s hit 503 SlowDown: %s", request.model, step, source, exc)
                if not self.mirror_failover or self._abandoned == "slowdown":
                    self._abandoned = "slowdown"
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 - next mirror
                if is_slowdown_error(exc):
                    last = exc
                    log.warning("ECMWF open data %s step %s via %s hit 503 SlowDown: %s", request.model, step, source, exc)
                    if not self.mirror_failover or self._abandoned == "slowdown":
                        self._abandoned = "slowdown"
                        raise OpenDataSlowDown(str(exc)) from exc
                    continue
                last = exc
                log.warning("ECMWF open data %s step %s via %s failed: %s", request.model, step, source, exc)
        if isinstance(last, OpenDataSlowDown):
            # Every configured mirror already returned 503. Later steps must not
            # walk that list again inside this fetch.
            self._abandoned = "slowdown"
            raise last
        if isinstance(last, (OpenDataBudget, TooLarge)):
            raise last
        raise OpenDataError(f"{request.model} {request.kind} {request.params} step {step} unavailable: {last}")

    def _download(self, request: Request) -> Path:
        """Write one step at a time. The caller deletes the file after sampling."""
        path = self.cache_dir / request.cache_name()
        if path.exists() and path.stat().st_size > 0:
            return path
        if self.retriever is not None:
            raise OpenDataError("injected retriever did not supply this request")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        written = 0
        try:
            with tmp.open("wb") as handle:
                for step in request.steps:
                    step_path = self._step_grib_path(request, int(step))
                    if step_path.exists() and step_path.stat().st_size > 0:
                        payload = step_path.read_bytes()
                        self.manifests.append({
                            "license": LICENSE, "attribution": ATTRIBUTION, "mirror": self.mirror or "cache",
                            "model": request.model, "run": aware(request.run).isoformat(),
                            "stream": request.stream, "type": request.kind,
                            "steps": [int(step)], "params": list(request.params),
                            "numbers": list(request.numbers), "bytes": 0, "cached": True,
                        })
                    else:
                        self.check_budget()
                        payload, source, indexed = self._fetch_step(request, int(step))
                        step_path.parent.mkdir(parents=True, exist_ok=True)
                        step_path.write_bytes(payload)
                        self.downloaded_bytes += len(payload)
                        self.mirror = source
                        self.manifests.append({
                            "license": LICENSE, "attribution": ATTRIBUTION, "mirror": source,
                            "model": request.model, "run": aware(request.run).isoformat(),
                            "stream": request.stream, "type": request.kind,
                            "steps": [int(step)], "params": list(request.params),
                            "numbers": list(request.numbers), "bytes": len(payload),
                            "indexed_bytes": indexed.nbytes, "messages": indexed.messages,
                            "members": list(indexed.members), "cached": False,
                        })
                    handle.write(payload)
                    written += len(payload)
                    del payload
            if written == 0:
                raise OpenDataError(f"{request.model} {request.kind} produced no GRIB messages")
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise  # noqa: BLE001 - keep the part file from surviving a failed fetch
        return path

    def _decode_download(self, request: Request) -> list[GridField]:
        return list(self.decoder(self._download(request)))

    def _fields(self, request: Request) -> list[GridField]:
        if self.retriever is not None:
            started = time.perf_counter()
            fields = list(self.retriever(request))
            self.seconds += time.perf_counter() - started
            self.manifests.append({
                "license": LICENSE, "attribution": ATTRIBUTION, "mirror": "retriever",
                "model": request.model, "run": aware(request.run).isoformat(),
                "stream": request.stream, "type": request.kind,
                "steps": list(request.steps), "params": list(request.params),
                "numbers": list(request.numbers), "bytes": 0, "cached": False,
            })
            return fields
        if len(request.steps) > 1:
            fields: list[GridField] = []
            for step in request.steps:
                one = replace(request, steps=(int(step),))
                try:
                    fields.extend(self._fields(one))
                except OpenDataBudget as exc:
                    self.stopped_early = str(exc)
                    log.warning(
                        "ECMWF open-data budget reached at step %s; keeping %d decoded fields",
                        step, len(fields),
                    )
                    break
                except OpenDataSlowDown as exc:
                    self.stopped_early = str(exc)
                    self._abandoned = "slowdown"
                    log.warning(
                        "ECMWF open-data SlowDown at step %s after the mirrors failed; keeping %d decoded fields",
                        step, len(fields),
                    )
                    break
            return fields
        if len(request.numbers) > 1:
            try:
                return self._decode_download(request)
            except TooLarge:
                mid = len(request.numbers) // 2
                left = self._fields(replace(request, numbers=request.numbers[:mid]))
                try:
                    right = self._fields(replace(request, numbers=request.numbers[mid:]))
                except OpenDataBudget as exc:
                    self.stopped_early = str(exc)
                    log.warning("ECMWF open-data budget reached while splitting members; keeping the first half")
                    return left
                return left + right
        return self._decode_download(request)

    def _sample_path(self, request: Request, step: int) -> Path:
        stamp = aware(request.run).strftime("%Y%m%d%H")
        params = "-".join(request.params)
        role = request.role or "det"
        return self.cache_dir / "samples" / stamp / f"{request.stream}-{request.kind}-{role}-{int(step)}-{params}.json"

    def _entry_complete(self, request: Request, entry: dict[str, Any]) -> bool:
        params = stored_params(request.params)

        def covered(row: Any) -> bool:
            return isinstance(row, dict) and params <= set(row)

        if request.role == "perturbed":
            labels = [key for key in entry if str(key).startswith("member")]
            return len(labels) >= 50 and all(covered(entry[key]) for key in labels)
        if request.role == "control":
            return covered(entry.get("control"))
        return covered(entry.get("") if "" in entry else entry.get("det"))

    def _read_step_points(self, request: Request, step: int) -> Optional[dict[str, Any]]:
        path = self._sample_path(request, int(step))
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("schema") != SAMPLE_SCHEMA or not data.get("complete"):
            return None
        pts = data.get("points")
        return pts if isinstance(pts, dict) else None

    def _step_covers(self, request: Request, pts: dict[str, Any], points: Sequence[tuple[float, float]]) -> bool:
        for point in points:
            entry = pts.get(_point_key(point))
            if not isinstance(entry, dict) or not self._entry_complete(request, entry):
                return False
        return True

    def _absorb_step(
        self, request: Request, step: int, pts: dict[str, Any], points: Sequence[tuple[float, float]],
    ) -> None:
        bucket = self._samples.setdefault(request.key, {})
        member_bucket = self._members.setdefault(request.key, {})
        wanted = stored_params(request.params)
        for point in points:
            entry = pts[_point_key(point)]
            for label, values in entry.items():
                if not isinstance(values, dict):
                    continue
                clean: dict[str, float] = {}
                for param, raw in values.items():
                    canon = canonical_param(str(param))
                    if canon not in wanted:
                        continue
                    try:
                        clean[canon] = float(raw)
                    except (TypeError, ValueError):
                        continue
                if not clean:
                    continue
                if label in ("", "det"):
                    bucket.setdefault(point, {}).setdefault(step, {}).update(clean)
                else:
                    member_bucket.setdefault(point, {}).setdefault(str(label), {}).setdefault(step, {}).update(clean)
                    if request.role != "perturbed":
                        bucket.setdefault(point, {}).setdefault(step, {}).update(clean)

    def _load_samples(self, request: Request, points: Sequence[tuple[float, float]]) -> bool:
        loaded: list[tuple[int, dict[str, Any]]] = []
        for step in request.steps:
            pts = self._read_step_points(request, int(step))
            if pts is None or not self._step_covers(request, pts, points):
                return False
            loaded.append((int(step), pts))
        for step, pts in loaded:
            self._absorb_step(request, step, pts, points)
        log.info(
            "ECMWF open-data cache hit %s %s/%s steps %s for %d locations (run %s)",
            request.model, request.stream, request.kind, list(request.steps), len(points),
            aware(request.run).strftime("%Y-%m-%d %HZ"),
        )
        return True

    def _load_present_steps(self, request: Request, points: Sequence[tuple[float, float]]) -> list[int]:
        """Load cached steps. Return the steps that still need a download."""
        pending: list[int] = []
        hit: list[int] = []
        for step in request.steps:
            pts = self._read_step_points(request, int(step))
            if pts is None or not self._step_covers(request, pts, points):
                pending.append(int(step))
                continue
            self._absorb_step(request, int(step), pts, points)
            hit.append(int(step))
        if hit:
            log.info(
                "ECMWF open-data cache hit %s %s/%s steps %s; still missing %s",
                request.model, request.stream, request.kind, hit, pending,
            )
        return pending

    def _store_samples(self, request: Request, points: Sequence[tuple[float, float]]) -> None:
        if self.stopped_early:
            return
        bucket = self._samples.get(request.key, {})
        members = self._members.get(request.key, {})
        wanted = stored_params(request.params)
        for step in request.steps:
            path = self._sample_path(request, int(step))
            existing: dict[str, Any] = {}
            if path.exists():
                try:
                    prior = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(prior.get("points"), dict):
                        existing = prior["points"]
                except (OSError, ValueError):
                    existing = {}
            for point in points:
                entry: dict[str, dict[str, float]] = {}
                values = bucket.get(point, {}).get(int(step)) or {}
                bare = {param: values[param] for param in wanted if param in values}
                if bare:
                    entry[""] = bare
                for label, steps in members.get(point, {}).items():
                    row = steps.get(int(step)) or {}
                    kept = {param: row[param] for param in wanted if param in row}
                    if kept:
                        entry[str(label)] = kept
                if not entry or not self._entry_complete(request, entry):
                    continue
                existing[_point_key(point)] = entry
            if not existing:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"schema": SAMPLE_SCHEMA, "complete": True, "points": existing}, allow_nan=False),
                encoding="utf-8",
            )

    def _sample_field(
        self,
        request: Request,
        grid: GridField,
        points: Sequence[tuple[float, float]],
        bucket: dict[tuple[float, float], dict[int, dict[str, float]]],
        member_bucket: dict[tuple[float, float], dict[str, dict[int, dict[str, float]]]],
    ) -> None:
        param = canonical_param(grid.param)
        label = member_label(request, grid)
        for point in points:
            value = sample_grid(grid, point[0], point[1])
            if value is None:
                continue
            bucket.setdefault(point, {}).setdefault(grid.step, {})[param] = value
            if label is not None:
                member_bucket.setdefault(point, {}).setdefault(label, {}).setdefault(grid.step, {})[param] = value

    def _stream_step(
        self,
        request: Request,
        points: Sequence[tuple[float, float]],
        bucket: dict[tuple[float, float], dict[int, dict[str, float]]],
        member_bucket: dict[tuple[float, float], dict[str, dict[int, dict[str, float]]]],
    ) -> None:
        """Decode one step, sample it, and delete the GRIB before the next step."""
        try:
            path = self._download(request)
        except TooLarge:
            if len(request.numbers) <= 1:
                raise
            mid = len(request.numbers) // 2
            self._stream_step(replace(request, numbers=request.numbers[:mid]), points, bucket, member_bucket)
            self._stream_step(replace(request, numbers=request.numbers[mid:]), points, bucket, member_bucket)
            return
        try:
            for grid in self.decoder(path):
                self._sample_field(request, grid, points, bucket, member_bucket)
                del grid
        finally:
            self._discard_grib(request, path)

    def _ensure(self, request: Request, points: Sequence[tuple[float, float]]) -> None:
        bucket = self._samples.setdefault(request.key, {})
        missing = [point for point in points if point not in bucket]
        if not missing:
            return
        if self._load_samples(request, missing):
            for point in missing:
                bucket.setdefault(point, {})
            return
        pending = self._load_present_steps(request, missing)
        member_bucket = self._members.setdefault(request.key, {})
        if not pending:
            for point in missing:
                bucket.setdefault(point, {})
            return
        if self.cache_only:
            self.cache_note = "open-data cache miss; network fetch skipped"
            log.info(
                "ECMWF open-data cache-only miss %s %s/%s steps %s; not downloading",
                request.model, request.stream, request.kind, pending,
            )
            for point in missing:
                bucket.setdefault(point, {})
            return
        if self.retriever is not None:
            for grid in self._fields(request):
                self._sample_field(request, grid, missing, bucket, member_bucket)
            for point in missing:
                bucket.setdefault(point, {})
            self._store_samples(request, missing)
            return
        for step in pending:
            one = replace(request, steps=(int(step),))
            try:
                self._stream_step(one, missing, bucket, member_bucket)
            except OpenDataBudget as exc:
                self.stopped_early = str(exc)
                log.warning(
                    "ECMWF open-data budget reached at step %s; keeping fields decoded so far",
                    step,
                )
                break
            except OpenDataSlowDown as exc:
                self.stopped_early = str(exc)
                self._abandoned = "slowdown"
                log.warning(
                    "ECMWF open-data SlowDown at step %s after the mirrors failed; keeping fields decoded so far",
                    step,
                )
                break
            self._store_samples(request, missing)
        for point in missing:
            bucket.setdefault(point, {})

    def _plan(self, model: str, hours: set[datetime]) -> list[tuple[datetime, set[datetime]]]:
        latest = self.run_for(model)
        plan = runs_for_hours(model, latest, hours)
        for run, _covered in plan:
            if aware(run) != aware(latest):
                self.runs[f"{model}-extended"] = aware(run)
        return plan

    def point_forecasts(
        self, points: Sequence[tuple[float, float]], hours_by_point: dict[tuple[float, float], set[datetime]],
    ) -> dict[tuple[float, float], ParsedLocation]:
        if not points:
            return {}
        self.begin_fetch()
        try:
            return self._point_forecasts(points, hours_by_point)
        finally:
            self.end_fetch()

    def _point_forecasts(
        self, points: Sequence[tuple[float, float]], hours_by_point: dict[tuple[float, float], set[datetime]],
    ) -> dict[tuple[float, float], ParsedLocation]:
        union = {hour for point in points for hour in hours_by_point.get(point, ())}
        out: dict[tuple[float, float], ParsedLocation] = {}
        products = (
            ("ifs", IFS_MODEL, True),
            ("aifs-single", AIFS_MODEL, False),
        )
        for model, public_name, want_gust in products:
            try:
                by_point: dict[tuple[float, float], dict[datetime, HourlyRow]] = {}
                for run, covered in self._plan(model, union):
                    covered = self._clip_hours(run, covered)
                    steps = tuple(steps_for_hours(run, covered, native_steps(model, aware(run).hour)))
                    if not steps:
                        continue
                    request = Request(model, run, "oper", "fc", steps, POINT_PARAMS)
                    self._ensure(request, points)
                    gust_samples: dict[tuple[float, float], dict[int, dict[str, float]]] = {}
                    if want_gust:
                        for name, gust_steps in gust_groups(steps):
                            try:
                                gust_request = Request(model, run, "oper", "fc", gust_steps, (name,))
                                self._ensure(gust_request, points)
                            except OpenDataError as exc:
                                log.warning("ECMWF gust subset skipped: %s", exc)
                                continue
                            for point, samples in self._samples.get(gust_request.key, {}).items():
                                slot = gust_samples.setdefault(point, {})
                                for step, values in samples.items():
                                    slot.setdefault(step, {}).update(values)
                    base = self._samples.get(request.key, {})
                    for point in points:
                        point_hours = covered & {aware(hour) for hour in hours_by_point.get(point, ())}
                        if not point_hours:
                            continue
                        merged: dict[int, dict[str, float]] = {}
                        for step, values in base.get(point, {}).items():
                            merged[step] = dict(values)
                        for step, values in gust_samples.get(point, {}).items():
                            merged.setdefault(step, {}).update(values)
                        for row in rows_for_hours(merged, run, point_hours, gust=want_gust):
                            if row.temp is None and row.wind is None:
                                continue
                            by_point.setdefault(point, {})[row.t] = row
                for point, timed in by_point.items():
                    series = [timed[stamp] for stamp in sorted(timed)]
                    current = out.get(point) or ParsedLocation(latitude=point[0], longitude=point[1], units=dict(UNITS))
                    current.models[public_name] = series
                    out[point] = current
            except OpenDataError as exc:
                log.warning("ECMWF %s point fallback skipped: %s", model, exc)
                if model == "ifs":
                    raise
        return out

    def published_steps(self, request: Request) -> Optional[list[int]]:
        """Steps the mirror actually published for this request's stream and filename type."""
        if self.step_catalog is not None:
            found = self.step_catalog(request)
            return None if found is None else sorted(int(step) for step in found)
        if not network_allowed():
            return None
        run = aware(request.run)
        prefix = f"{run:%Y%m%d}/{run:%H}z/{request.model}/0p25/{request.stream}/"
        names = self._list_index_names(prefix)
        if not names:
            return None
        direct = parse_published_steps(names, request.stream, request.kind)
        if direct:
            return direct
        renamed = filename_type(request.kind, request.model)
        if renamed != request.kind:
            renamed_steps = parse_published_steps(names, request.stream, renamed)
            if renamed_steps:
                return renamed_steps
        return []

    def _list_index_names(self, prefix: str) -> list[str]:
        """Index object names under ``prefix``. AWS is the layout source; other mirrors mirror it."""
        import xml.etree.ElementTree as ET

        import httpx

        bases = {
            "aws": "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
            "google": "https://storage.googleapis.com/ecmwf-open-data",
        }
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for source in self.sources:
            base = bases.get(source)
            if base is None:
                continue
            found: list[str] = []
            token: Optional[str] = None
            try:
                self.check_budget()
                timeout = min(20.0, max(1.0, self._remaining()))
                with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
                    while True:
                        self.check_budget()
                        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
                        if token:
                            params["continuation-token"] = token
                        response = client.get(base, params=params)
                        if is_slowdown_response(response):
                            self._note_slowdown(base)
                            delay = slowdown_backoff(1, self._roll)
                            log.warning("ECMWF 503 SlowDown listing %s; backing off %.2fs", base, delay)
                            if delay >= self._remaining():
                                self._abandoned = "budget"
                                raise OpenDataBudget("ECMWF open-data budget exceeded during index SlowDown")
                            self._sleep(delay)
                            self.check_budget()
                            response = client.get(base, params=params)
                            if is_slowdown_response(response):
                                self._note_slowdown(base)
                                if self.give_up_on_slowdown:
                                    self._abandoned = "slowdown"
                                raise OpenDataSlowDown(f"503 SlowDown listing {base}")
                        response.raise_for_status()
                        root = ET.fromstring(response.content)
                        for el in root.findall("s:Contents/s:Key", ns):
                            if el.text and el.text.endswith(".index"):
                                found.append(el.text)
                        nxt = root.find("s:NextContinuationToken", ns)
                        if nxt is None or not nxt.text:
                            break
                        token = nxt.text
            except OpenDataBudget:
                raise
            except OpenDataSlowDown:
                if self._abandoned == "slowdown":
                    raise
                log.info("ECMWF open data step list via %s hit 503 SlowDown", source)
                continue
            except Exception as exc:  # noqa: BLE001 - the next mirror, or no catalog
                log.info("ECMWF open data step list via %s failed: %s", source, exc)
                continue
            if found:
                return found
        return []

    def _nearest_request(self, request: Request) -> Optional[Request]:
        available = self.published_steps(request)
        if available is None and network_allowed() and self.step_catalog is None:
            # The index listing itself failed. 6 h is the other open-data cadence.
            available = list(range(0, 361, 6))
        if not available:
            return None
        snapped = tuple(cover_with_available(request.steps, available))
        if not snapped or snapped == tuple(request.steps):
            return None
        return replace(request, steps=snapped)

    def _has_samples(self, request: Request, points: Sequence[tuple[float, float]]) -> bool:
        bucket = self._samples.get(request.key, {})
        members = self._members.get(request.key, {})
        return any(bucket.get(point) or members.get(point) for point in points)

    def _accept_cached(self, request: Request, points: Sequence[tuple[float, float]]) -> Optional[Request]:
        """A cache-only miss is not a loaded product. Nearest-step lookup would hit the network."""
        if self.cache_only and not self._has_samples(request, points):
            return None
        return request

    def _ensure_available(self, request: Request, points: Sequence[tuple[float, float]]) -> Optional[Request]:
        """Fetch ``request``. On a missing index entry, retry the nearest published steps."""
        try:
            self._ensure(request, points)
        except OpenDataBudget:
            raise
        except OpenDataSlowDown:
            return None
        except OpenDataError as exc:
            first = exc
        else:
            return self._accept_cached(request, points)
        alt = self._nearest_request(request)
        if alt is None:
            log.warning(
                "ECMWF open data has no %s/%s index entries for %s at steps %s: %s",
                request.stream, request.kind, ",".join(request.params), list(request.steps), first,
            )
            return None
        log.warning(
            "ECMWF open data %s/%s steps %s are not in the index; using nearest published steps %s",
            request.stream, request.kind, list(request.steps), list(alt.steps),
        )
        try:
            self._ensure(alt, points)
        except OpenDataBudget:
            raise
        except OpenDataSlowDown:
            return None
        except OpenDataError as exc:
            log.warning(
                "ECMWF open data %s/%s still has no index entries for %s at nearest steps %s: %s",
                alt.stream, alt.kind, ",".join(alt.params), list(alt.steps), exc,
            )
            return None
        return self._accept_cached(alt, points)

    def _load_member_product(
        self,
        run: datetime,
        candidates: Sequence[tuple[str, str]],
        steps: Sequence[int],
        params: tuple[str, ...],
        numbers: tuple[int, ...],
        points: Sequence[tuple[float, float]],
        role: str,
    ) -> Optional[Request]:
        for stream, kind in candidates:
            request = Request("ifs", run, stream, kind, tuple(steps), params, numbers, role)
            loaded = self._ensure_available(request, points)
            if loaded is not None:
                return loaded
        log.warning(
            "ECMWF open data ensemble %s unavailable for run %s (%s); no %s index entries at steps %s",
            role, aware(run).strftime("%Y%m%d%H"),
            ", ".join(f"{stream}/{kind}" for stream, kind in candidates),
            ",".join(params), list(steps),
        )
        return None

    def _clip_hours(self, run: datetime, hours: set[datetime]) -> set[datetime]:
        clipped = hours_within_lead(run, hours, self.max_lead_h)
        skipped = len(hours) - len(clipped)
        if skipped:
            log.info(
                "ECMWF open-data skipping %d hours beyond %.0fh from %s",
                skipped, self.max_lead_h, aware(run).strftime("%Y-%m-%d %HZ"),
            )
        return clipped

    def ensemble_forecasts(
        self, points: Sequence[tuple[float, float]], hours_by_point: dict[tuple[float, float], set[datetime]],
    ) -> dict[tuple[float, float], EnsembleLocation]:
        if not points:
            return {}
        self.begin_fetch()
        try:
            return self._ensemble_forecasts(points, hours_by_point)
        finally:
            self.end_fetch()

    def _ensemble_forecasts(
        self, points: Sequence[tuple[float, float]], hours_by_point: dict[tuple[float, float], set[datetime]],
    ) -> dict[tuple[float, float], EnsembleLocation]:
        union = {hour for point in points for hour in hours_by_point.get(point, ())}
        plan = self._plan("ifs", union)
        if not plan:
            return {}
        collected: dict[tuple[float, float], dict[str, dict[datetime, HourlyRow]]] = {}
        for run, covered in plan:
            covered = self._clip_hours(run, covered)
            steps = tuple(steps_for_hours(run, covered, native_steps("ifs", aware(run).hour)))
            if not steps:
                continue
            control = self._load_member_product(
                run, control_candidates(run), steps, ENS_PARAMS, (), points, "control",
            )
            perturbed = self._load_member_product(
                run, perturbed_candidates(), steps, ENS_PARAMS, tuple(range(1, 51)), points, "perturbed",
            )
            if control is None and perturbed is None:
                log.warning(
                    "ECMWF open data ensemble found no control or perturbed fields for %s "
                    "(10u/10v/tp index entries missing at steps %s)",
                    aware(run).strftime("%Y%m%d%H"), list(steps),
                )
                continue
            gust_keys: list[str] = []
            for base in (control, perturbed):
                if base is None:
                    continue
                for name, gust_steps in gust_groups(base.steps):
                    gust_request = Request(
                        "ifs", run, base.stream, base.kind, gust_steps, (name,), base.numbers, base.role,
                    )
                    loaded = self._ensure_available(gust_request, points)
                    if loaded is None:
                        continue
                    gust_keys.append(loaded.key)
            for point in points:
                by_member: dict[str, dict[int, dict[str, float]]] = {}
                for request in (control, perturbed):
                    if request is None:
                        continue
                    for label, samples in self._members.get(request.key, {}).get(point, {}).items():
                        slot = by_member.setdefault(label, {})
                        for step, values in samples.items():
                            slot.setdefault(step, {}).update(values)
                for key in gust_keys:
                    for label, samples in self._members.get(key, {}).get(point, {}).items():
                        slot = by_member.setdefault(label, {})
                        for step, values in samples.items():
                            slot.setdefault(step, {}).update(values)
                point_hours = covered & {aware(hour) for hour in hours_by_point.get(point, ())}
                for label, samples in by_member.items():
                    timed = collected.setdefault(point, {}).setdefault(label, {})
                    for hour in point_hours:
                        row = hour_state(samples, run, hour, gust=True)
                        timed[aware(hour)] = row or HourlyRow(t=aware(hour))
        out = {}
        for point, labels in collected.items():
            times = sorted({aware(hour) for hour in hours_by_point.get(point, ())})
            members = {}
            for label, timed in sorted(labels.items()):
                series = [timed.get(hour) or HourlyRow(t=hour) for hour in times]
                members[f"{ENS_MODEL}:{label}"] = Member(
                    model=ENS_MODEL, member=label,
                    wind=[row.wind for row in series], gust=[row.gust for row in series],
                    precip=[row.precip for row in series],
                )
            if members:
                out[point] = EnsembleLocation(point[0], point[1], times, dict(UNITS), members)
        run = self.runs.get("ifs")
        run_text = run.isoformat() if run is not None else "unknown"
        for point, location in out.items():
            log.info(
                "ECMWF ensemble %.4f,%.4f source=ecmwf-opendata run=%s mirror=%s members=%d",
                point[0], point[1], run_text, self.mirror or "cache", location.n_members(),
            )
        if out:
            log.info(
                "ECMWF ensemble open-data summary run=%s mirror=%s locations=%d member_counts=%s",
                run_text, self.mirror or "cache", len(out), [item.n_members() for item in out.values()],
            )
        return out


def window_missing(location: Optional[ParsedLocation], hours: set[datetime]) -> bool:
    rows = {}
    if location is not None:
        rows = {row.t: row for row in location.models.get(IFS_MODEL, ())}
    return any(hour not in rows or rows[hour].temp is None or rows[hour].wind is None for hour in hours)


def compare_points(
    points: Sequence[tuple[float, float]], start: datetime, end: datetime, *, cache_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Side-by-side Open-Meteo ECMWF and this fallback for a few coordinates."""
    import httpx

    os.environ["ECMWF_OPENDATA_ALLOW_NETWORK"] = "1"
    hours = []
    cursor = aware(start).replace(minute=0, second=0, microsecond=0)
    stop = aware(end)
    while cursor <= stop:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "models": f"{IFS_MODEL},{AIFS_MODEL}",
        "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "mm",
        "timezone": "UTC",
        "start_hour": hours[0].strftime("%Y-%m-%dT%H:00"),
        "end_hour": hours[-1].strftime("%Y-%m-%dT%H:00"),
    }
    started = time.perf_counter()
    response = httpx.get(
        "https://api.open-meteo.com/v1/forecast", params=params, timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    payload = response.json()
    from pipeline.weather.parsers.openmeteo import parse_forecast

    parsed = parse_forecast(payload if isinstance(payload, list) else payload)
    session = OpenDataSession(cache_dir=cache_dir or Path("data/cache/ecmwf"))
    by_point = {point: set(hours) for point in points}
    fallback = session.point_forecasts(points, by_point)
    elapsed = time.perf_counter() - started

    def index(location: Optional[ParsedLocation], model: str) -> dict[datetime, HourlyRow]:
        if location is None:
            return {}
        return {row.t: row for row in location.models.get(model, ())}

    games = []
    for point, remote in zip(points, parsed, strict=True):
        local = fallback.get(point)
        models = {}
        for model in (IFS_MODEL, AIFS_MODEL):
            left, right = index(remote, model), index(local, model)
            shared = sorted(set(left) & set(right))
            deltas = {}
            for name in ("temp", "wind", "gust", "dir", "precip"):
                pairs = [(getattr(left[t], name), getattr(right[t], name)) for t in shared]
                pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
                deltas[name] = None if not pairs else round(sum(abs(a - b) for a, b in pairs) / len(pairs), 3)
            models[model] = {"hours": len(shared), "mean_abs": deltas}
        games.append({"lat": point[0], "lon": point[1], "models": models})
    return {
        "license": LICENSE, "attribution": ATTRIBUTION,
        "seconds": round(elapsed, 3), "downloaded_bytes": session.downloaded_bytes,
        "runs": {name: stamp.isoformat() for name, stamp in session.runs.items()},
        "mirror": session.mirror, "games": games,
    }


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare ECMWF Open Data with Open-Meteo ECMWF")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--points", help="lat,lon pairs separated by semicolons")
    parser.add_argument("--hours-ahead", type=float, default=72.0)
    parser.add_argument("--window-hours", type=float, default=6.0)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/ecmwf"))
    args = parser.parse_args(argv)
    if args.points:
        points = []
        for item in args.points.split(";"):
            lat, lon = item.split(",")
            points.append((float(lat), float(lon)))
    else:
        points = [(39.2780, -76.6227), (42.0909, -80.0781), (41.8623, -87.6167)]
    start = datetime.now(timezone.utc) + timedelta(hours=args.hours_ahead)
    end = start + timedelta(hours=args.window_hours)
    report = compare_points(points, start, end, cache_dir=args.cache_dir)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "ATTRIBUTION",
    "AIFS_MODEL",
    "ENS_MODEL",
    "IFS_MODEL",
    "LICENSE",
    "GridField",
    "OpenDataError",
    "OpenDataSession",
    "Request",
    "TooLarge",
    "compare_points",
    "control_candidates",
    "cover_with_available",
    "decode_grib",
    "expected_published_run",
    "fallback_mode",
    "filename_type",
    "gust_groups",
    "hour_state",
    "init_is_stale",
    "kelvin_to_f",
    "member_label",
    "mirror_failover_enabled",
    "prune_sample_cache",
    "native_steps",
    "network_allowed",
    "parse_published_steps",
    "perturbed_candidates",
    "runs_for_hours",
    "precip_mm",
    "rows_for_hours",
    "sample_grid",
    "steps_for_hours",
    "wind_from_uv",
    "window_missing",
]
