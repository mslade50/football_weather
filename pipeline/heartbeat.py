"""Shared, publication-time verdict for the Worker scheduler heartbeat."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.model.config import HEARTBEAT_STALE_HOURS
from utils.timeutil import ensure_utc, parse_iso, utc_iso


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(parse_iso(value.replace(" UTC", "Z").replace(" ", "T")))
    except (ValueError, OverflowError):
        return None


def assess(raw: Any, now: datetime, previous: Any = None, *, readable: bool = True) -> dict[str, Any]:
    """Unknown clocks become stale only after persisted unknown observations >20h.

    A failed object read is not evidence of a missing tick: it breaks the unknown
    observation window. Future clocks are unknown too. A valid tick resets it.
    """
    now = ensure_utc(now)
    previous = previous if isinstance(previous, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    tick = timestamp(raw.get("ts") or raw.get("last") or raw.get("at")) if readable else None
    if tick is not None and tick > now:
        tick = None
    since = None
    age = None
    status = "unknown"
    reason = "Heartbeat could not be read" if not readable else "No usable heartbeat timestamp"
    if tick is not None:
        age = (now - tick).total_seconds() / 3600
        status = "stale" if age > HEARTBEAT_STALE_HOURS else "fresh"
        reason = f"Last Worker tick: {utc_iso(tick)}"
    elif readable:
        since = timestamp(previous.get("unknown_since"))
        if since is None or since > now:
            since = now
        if (now - since).total_seconds() > HEARTBEAT_STALE_HOURS * 3600:
            status = "stale"
            reason = f"No usable heartbeat observed since {utc_iso(since)}"
    return {
        "status": status, "ts": utc_iso(tick) if tick else None,
        "age_hours": age, "threshold_hours": HEARTBEAT_STALE_HOURS,
        "unknown_since": utc_iso(since) if since else None,
        "evaluated_at": utc_iso(now), "reason": reason,
    }
