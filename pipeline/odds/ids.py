"""Provisional book identity codec, independent of schedule matching.

Precision and slug style are explicit because historical captures use each of
these encodings. Team names are decoded as encoded, without undoing slugging.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

Precision = Literal["compact", "day", "minute"]
SlugStyle = Literal["raw", "hyphen", "and"]
_FORMATS = {"compact": "%Y%m%d", "day": "%Y-%m-%d", "minute": "%Y-%m-%dT%H:%M"}
_PROV_RE = re.compile(
    r"^(?P<sport>nfl|cfb):raw:(?P<stamp>\d{8}|\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?|unknown):(?P<away>[^@]*)@(?P<home>.*)$"
)
_STAMP_DAY = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")
_STAMP_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")


def slug_name(name: str, style: SlugStyle = "hyphen") -> str:
    if style == "raw":
        return name
    text = name.lower().replace("&", "and") if style == "and" else name.lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def provisional_game_id(
    sport: str, away: str, home: str, kickoff: date | datetime | None = None,
    *, precision: Precision = "minute", slug: SlugStyle = "raw", sanitize_pipes: bool = True,
) -> str:
    """Encode the book's identity, replacing odds-key separators by default.

    Kickoffs are already in the book's chosen timezone (normally UTC). The
    legacy FanDuel wrapper opts out of pipe replacement to preserve its API.
    """
    stamp = kickoff.strftime(_FORMATS[precision]) if kickoff else "unknown"
    result = f"{sport}:raw:{stamp}:{slug_name(away, slug)}@{slug_name(home, slug)}"
    return result.replace("|", " ") if sanitize_pipes else result


def parse_stamp(stamp: str) -> tuple[datetime | None, bool]:
    """Decode a stamp; date-only identities carry a noon UTC placeholder."""
    match = _STAMP_DT.match(stamp or "")
    if match:
        y, mo, d, hh, mm = (int(x) for x in match.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc), False
    match = _STAMP_DAY.match(stamp or "")
    if match:
        y, mo, d = (int(x) for x in match.groups())
        return datetime(y, mo, d, 12, tzinfo=timezone.utc), True
    return None, False


@dataclass(frozen=True)
class ProvisionalId:
    sport: str
    stamp: str
    away: str
    home: str


def parse_provisional_id(game_id: str) -> ProvisionalId | None:
    """Decode the encoded fields exactly; normalization belongs to the caller."""
    match = _PROV_RE.match(game_id or "")
    return ProvisionalId(**match.groupdict()) if match else None


def is_provisional(game_id: str) -> bool:
    return parse_provisional_id(game_id) is not None
