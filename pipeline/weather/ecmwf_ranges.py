"""Index selection for ECMWF Open Data GRIB byte ranges.

ECMWF publishes one GRIB per forecast step. An ensemble step file is several
gigabytes because it carries every level and parameter for every member. The
``.index`` sidecar is JSON lines with ``_offset`` and ``_length``. Selecting
10 m wind, precipitation, and 2 m temperature keeps a few dozen megabytes and
one download serves every stadium.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

MIRROR_URLS = {
    "google": "https://storage.googleapis.com/ecmwf-open-data",
    "ecmwf": "https://data.ecmwf.int/forecasts",
    "aws": "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "azure": "https://ai4edataeuwest.blob.core.windows.net/ecmwf",
}
AZURE_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/ai4edataeuwest/ecmwf"
# Cloud mirrors answer a single Range. Adjacent messages can share one request;
# a gap would splice foreign bytes into the GRIB and break the decoder.
RANGE_GAP = 0


@dataclass(frozen=True)
class IndexedMessages:
    """Byte ranges for the parameters this pipeline actually samples."""

    spans: tuple[tuple[int, int], ...]
    messages: int
    nbytes: int
    members: tuple[int, ...]


def object_stem(model: str, run: datetime, stream: str, file_type: str, step: int) -> str:
    """Object path without ``.index`` / ``.grib2``. ``file_type`` is the filename type."""
    if run.tzinfo is None:
        stamp = run
    else:
        stamp = run.astimezone(run.tzinfo)
    # Callers pass an aware UTC run. Format from that clock, not a local shift.
    return (
        f"{stamp:%Y%m%d}/{stamp:%H}z/{model}/0p25/{stream}/"
        f"{stamp:%Y%m%d%H%M%S}-{int(step)}h-{stream}-{file_type}"
    )


def parse_index_entries(payload: str) -> list[dict]:
    """One JSON object per line. Blank lines are ignored."""
    rows = []
    for line in payload.splitlines():
        text = line.strip()
        if not text:
            continue
        row = json.loads(text)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _number(row: dict) -> Optional[int]:
    raw = row.get("number")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def select_indexed(
    rows: Iterable[dict],
    params: Sequence[str],
    numbers: Sequence[int] = (),
    step: Optional[int] = None,
) -> IndexedMessages:
    """Surface messages for ``params``.

    An empty ``numbers`` sequence is the deterministic / control file (no
    perturbation number, or number 0). A non-empty sequence keeps those
    perturbed members only.
    """
    wanted = {str(param) for param in params}
    wanted_numbers = {int(number) for number in numbers}
    deterministic = not wanted_numbers
    spans: list[tuple[int, int]] = []
    found: set[int] = set()
    for row in rows:
        if str(row.get("param")) not in wanted:
            continue
        if row.get("levtype") not in (None, "sfc"):
            continue
        if step is not None and row.get("step") not in (None, "", str(int(step)), int(step)):
            continue
        number = _number(row)
        if deterministic:
            if number not in (None, 0):
                continue
        elif number not in wanted_numbers:
            continue
        try:
            offset = int(row["_offset"])
            length = int(row["_length"])
        except (KeyError, TypeError, ValueError):
            continue
        if length <= 0 or offset < 0:
            continue
        spans.append((offset, length))
        if number:
            found.add(number)
    spans.sort()
    return IndexedMessages(
        spans=tuple(spans),
        messages=len(spans),
        nbytes=sum(length for _, length in spans),
        members=tuple(sorted(found)),
    )


def coalesce_ranges(spans: Sequence[tuple[int, int]], gap: int = RANGE_GAP) -> list[tuple[int, int]]:
    """Merge ranges that touch or overlap. ``gap`` is extra bytes allowed between them."""
    if not spans:
        return []
    ordered = sorted((int(start), int(length)) for start, length in spans)
    merged = [ordered[0]]
    for start, length in ordered[1:]:
        prev_start, prev_length = merged[-1]
        prev_end = prev_start + prev_length
        if start <= prev_end + max(0, gap):
            end = max(prev_end, start + length)
            merged[-1] = (prev_start, end - prev_start)
        else:
            merged.append((start, length))
    return merged
