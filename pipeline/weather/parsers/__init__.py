"""Pure parsers: raw weather payloads -> hourly rows. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class HourlyRow:
    """One hourly sample in canonical units: F, mph, degrees, mm, PoP as 0-100 percent."""

    t: datetime
    temp: Optional[float] = None
    wind: Optional[float] = None
    gust: Optional[float] = None
    dir: Optional[float] = None
    precip: Optional[float] = None
    pop: Optional[float] = None


__all__ = ["HourlyRow"]
