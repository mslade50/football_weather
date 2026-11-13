"""ET/UTC helpers and board date/time labels."""

from __future__ import annotations

from datetime import date, datetime, timezone
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


def parse_iso_utc(
    value: object, *, strip: bool = False, naive_utc: bool = True,
    strings_only: bool = False, z_suffix_only: bool = False,
) -> Optional[datetime]:
    """ISO/datetime to aware UTC, or None; never interpret numbers as epochs.

    Explicit policies preserve existing boundaries: state does not trim input;
    alert/backtest clocks trim and translate only a terminal Z; provider clocks
    accept strings only and can require an explicit timezone. parse_iso below
    remains the raising, offset-preserving parser with a configurable default TZ.
    """
    if isinstance(value, datetime) and not strings_only:
        parsed = value
    elif isinstance(value, str):
        text = value.strip() if strip else value
        if z_suffix_only:
            text = text[:-1] + '+00:00' if text.endswith('Z') else text
        else:
            text = text.replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None and not naive_utc:
        return None
    return ensure_utc(parsed)


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


__all__ = [
    "UTC",
    "ET",
    "DOW_ABBR",
    "now_utc",
    "now_et",
    "ensure_utc",
    "parse_iso_utc",
    "to_et",
    "to_tz",
    "date_label",
    "time_label",
    "utc_iso",
    "parse_iso",
    "run_id_for",
]
