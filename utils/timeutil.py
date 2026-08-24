"""ET/UTC helpers and legacy label formats.

Legacy formats (AUDIT §4): Date 'SUN 11/09', Time '01:00 PM', Timestamp
'2026-04-15T10:00:55' (naive ET ISO, seconds precision).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

DOW_ABBR = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_et() -> datetime:
    return datetime.now(ET)


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes, convert aware ones."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_et(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(ET)


def to_tz(dt: datetime, tz: Union[str, ZoneInfo]) -> datetime:
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ensure_utc(dt).astimezone(zone)


def date_label(dt: Union[datetime, date]) -> str:
    """'SUN 11/09' — local date of the supplied datetime (no tz conversion)."""
    return f"{DOW_ABBR[dt.weekday()]} {dt.month:02d}/{dt.day:02d}"


def time_label(dt: datetime) -> str:
    """'01:00 PM' — 12-hour zero-padded, no tz conversion."""
    hour12 = dt.hour % 12
    if hour12 == 0:
        hour12 = 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12:02d}:{dt.minute:02d} {ampm}"


def naive_et_iso(dt: Optional[datetime] = None) -> str:
    """Legacy Timestamp: ISO seconds, ET wall clock, no offset."""
    d = to_et(dt) if dt is not None else now_et()
    return d.replace(tzinfo=None, microsecond=0).isoformat()


def utc_iso(dt: Optional[datetime] = None) -> str:
    d = ensure_utc(dt) if dt is not None else now_utc()
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str, default_tz: Union[str, ZoneInfo, None] = None) -> datetime:
    """Parse ISO8601 (accepts trailing 'Z'); naive input gets default_tz (UTC if None)."""
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        zone = UTC if default_tz is None else (ZoneInfo(default_tz) if isinstance(default_tz, str) else default_tz)
        dt = dt.replace(tzinfo=zone)
    return dt


def run_id_for(dt: Optional[datetime] = None) -> str:
    """Compact UTC stamp used as run identifier: 20260823T141500Z."""
    d = ensure_utc(dt) if dt is not None else now_utc()
    return d.strftime("%Y%m%dT%H%M%SZ")


def hours_until(target: datetime, ref: Optional[datetime] = None) -> float:
    r = ensure_utc(ref) if ref is not None else now_utc()
    return (ensure_utc(target) - r) / timedelta(hours=1)


def et_weekday(dt: Optional[datetime] = None) -> int:
    """Monday=0 .. Sunday=6 in ET (used by CFB DOW signal thresholds)."""
    d = to_et(dt) if dt is not None else now_et()
    return d.weekday()


__all__ = [
    "UTC",
    "ET",
    "DOW_ABBR",
    "now_utc",
    "now_et",
    "ensure_utc",
    "to_et",
    "to_tz",
    "date_label",
    "time_label",
    "naive_et_iso",
    "utc_iso",
    "parse_iso",
    "run_id_for",
    "hours_until",
    "et_weekday",
]
