"""Prepare a reproducible 2024–25 directional backtest input archive.

The source forecasts are recovered from the distinct historical Git blobs of
``nfl_weather.csv`` and ``cfb_weather.xlsx``. Schedules/results come from public
ESPN (CFB) and nflverse (NFL). ERA5 actuals are optional and independently
fetched for every outdoor graded game, not only high-wind forecasts.

Example::

    python -m scripts.prepare_directional_archive --fetch-actuals \
        --output data/backtest/directional_inputs.parquet

The actual-weather window intentionally matches ``pipeline.backtest._window``:
the kickoff hour rounded down through the next two hourly observations (three
points). The output is an input dataset; it does not change legacy backtest
windowing or grade missing observations as zero.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from pipeline.backtest import _window
from pipeline.schedule.espn import fetch_espn_scoreboard, parse_espn_scoreboard
from pipeline.schedule.nfl import NFLVERSE_GAMES_URL, parse_nflverse_games
from pipeline.stadiums.loader import load_stadium_book, normalize_alias
from pipeline.weather.merge import vector_mean_deg
from scripts import _git_history
from scripts._git_history import Snapshot
from utils.timeutil import ET, UTC, parse_iso, utc_iso

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_REF = "origin/backtest/historical-replay"
NFL_ARCHIVE = "nfl_weather.csv"
CFB_ARCHIVE = "cfb_weather.xlsx"
ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"
SEASONS = (2024, 2025)
FIELDS = (
    "game_id", "sport", "season", "stadium_id", "kickoff_utc", "forecast_at",
    "source_timestamp", "source_commit_at",
    "lead_fc", "wind_fc", "temp_fc", "rain_fc", "wind_dir_fc",
    "wind_dir_deg_fc", "total_close", "close_at", "close_lead_h", "closing_total_source",
    "total_open", "spread_open",
    "actual_total", "wind_act", "temp_act", "rain_act", "wind_dir_deg_act",
    "src_forecast", "src_actual", "actual_source", "opening_spread_source", "venue_source",
    "orientation_deg", "roof_state", "lat", "lon",
)
USER_AGENT = "football-weather-directional-backtest/1.0"


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _compass(value: Any) -> float | None:
    if not isinstance(value, str):
        return _number(value)
    labels = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    token = value.strip().upper()
    return (labels.index(token) * 22.5) % 360 if token in labels else None


def _parse_archive_date(row: dict[str, Any], stamp: datetime) -> tuple[int, int] | None:
    raw = str(row.get("Date") or "")
    m = re.search(r"(\d{1,2})/(\d{1,2})", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    # A few exports use an ISO date instead of the legacy weekday label.
    try:
        d = datetime.fromisoformat(raw.strip()).date()
        return d.month, d.day
    except ValueError:
        return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=ET) if value.tzinfo is None else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=ET) if d.tzinfo is None else d


def _available_at(snapshot_time: datetime, first_commit_time: str) -> datetime:
    """Earliest defensible availability is the later file time or Git commit."""
    committed = datetime.fromisoformat(first_commit_time)
    committed = committed.replace(tzinfo=UTC) if committed.tzinfo is None else committed.astimezone(UTC)
    return max(snapshot_time.astimezone(UTC), committed)


def _closing_quote(sport: str, archived_total: float | None, lead_h: float,
                   nflverse_total: float | None, kickoff: datetime, available_at: datetime) -> tuple[float | None, str | None, float | None, str | None]:
    if sport == "nfl" and nflverse_total is not None:
        return nflverse_total, utc_iso(kickoff), 0.0, "nflverse_total_line"
    if 0 < lead_h <= 6 and archived_total is not None:
        return archived_total, utc_iso(available_at), round(lead_h, 3), "last_archived_quote_within_6h"
    return None, None, None, None


def _history_snapshots(path: str) -> list[Snapshot]:
    """Read full archive history from the retained replay ref, not fixture copies."""
    out = _git_history.git("log", ARCHIVE_REF, "--format=%H %cI", "--raw", "--no-abbrev", "--", path)
    assert isinstance(out, str)
    snaps: list[Snapshot] = []
    sha = ""
    committed = datetime.min
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith(":"):
            meta = line.split("\t", 1)[0].split()
            blob = meta[3]
            if blob.strip("0"):
                snaps.append(Snapshot(sha=sha, date=committed, blob=blob, path=path))
        else:
            sha, iso = line.split(" ", 1)
            committed = datetime.fromisoformat(iso.strip())
    snaps.reverse()
    return snaps


def _read_blobs(path: str) -> list[tuple[Snapshot, list[dict[str, Any]]]]:
    """Read each distinct Git content blob once, retaining its first commit date."""
    seen: set[str] = set()
    out: list[tuple[Snapshot, list[dict[str, Any]]]] = []
    snaps = _history_snapshots(path)
    materialised = _git_history.materialise(snaps, Path(path).suffix)
    for snap in snaps:
        blob_path = materialised[snap.blob]
        if snap.blob in seen:
            continue
        seen.add(snap.blob)
        if path.endswith(".csv"):
            frame = pd.read_csv(blob_path)
        else:
            frame = pd.read_excel(blob_path, sheet_name=None)
            frame = pd.concat([v.assign(_sheet=k) for k, v in frame.items()], ignore_index=True)
        out.append((snap, frame.where(pd.notna(frame), None).to_dict(orient="records")))
    return out


def _fetch_schedules(cache: Path, seasons: tuple[int, ...], offline: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cache.mkdir(parents=True, exist_ok=True)
    book = load_stadium_book()
    schedule: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    # nflverse rows retain both venue IDs and final scores; keep raw schedule in cache.
    nfl_path = cache / "nflverse-games.csv"
    if not nfl_path.exists():
        if offline:
            raise FileNotFoundError(f"offline schedule cache missing: {nfl_path}")
        with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(NFLVERSE_GAMES_URL)
            response.raise_for_status()
        nfl_path.write_text(response.text, encoding="utf-8")
    nfl_rows = list(csv.DictReader(nfl_path.open(encoding="utf-8", newline="")))
    for row in nfl_rows:
        try:
            season = int(float(row.get("season") or 0))
        except ValueError:
            continue
        if season not in seasons:
            continue
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(row.keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
        parsed = parse_nflverse_games(
            buf.getvalue(),
            season, book=book, include_final=True,
        )
        # CSV reconstruction above can be lossy for commas. The robust per-row
        # fields needed below are preserved directly from DictReader instead.
        if not parsed:
            continue
        game = parsed[0]
        try:
            hs, aws = _number(row.get("home_score")), _number(row.get("away_score"))
            total = hs + aws if hs is not None and aws is not None else None
        except (TypeError, ValueError):
            total = None
        stadium = book.stadiums.get(game.stadium_id or "") if row.get("stadium_id") and game.stadium_id else None
        location = str(row.get("location") or "").strip().lower()
        schedule.append({"game": game, "total": total, "total_line": _number(row.get("total_line")),
                         "raw_date": str(row.get("gameday") or ""),
                         "venue_id": str(row.get("stadium_id") or ""), "venue_name": str(row.get("stadium") or ""),
                         "roof_raw": str(row.get("roof") or "").lower(),
                         "neutral_status": False if location == "home" else (True if location == "neutral" else None),
                         "stadium": stadium, "event_id": str(row.get("game_id") or "")})
        counts["nfl_schedule"] += 1
    for season in seasons:
        for season_type, weeks in ((2, range(0, 18)), (3, range(1, 6))):
            for week in weeks:
                dest = cache / f"espn-cfb-{season}-{season_type}-{week}.json"
                if dest.exists():
                    payload = json.loads(dest.read_text(encoding="utf-8"))
                elif offline:
                    continue
                else:
                    try:
                        payload = fetch_espn_scoreboard("cfb", season, week, season_type, timeout=30)
                        dest.write_text(json.dumps(payload), encoding="utf-8")
                    except httpx.HTTPError:
                        counts["espn_fetch_errors"] += 1
                        continue
                    time.sleep(0.12)
                games = parse_espn_scoreboard(payload, "cfb", season, book)
                by_id = {g.source.removeprefix("espn:"): g for g in games}
                for event in payload.get("events") or []:
                    comp = (event.get("competitions") or [{}])[0]
                    game = by_id.get(str(event.get("id") or ""))
                    if game is None:
                        continue
                    scores: dict[str, float] = {}
                    for c in comp.get("competitors") or []:
                        if c.get("score") is not None:
                            scores[str(c.get("homeAway"))] = _number(c.get("score"))
                    total = scores.get("home") + scores.get("away") if scores.get("home") is not None and scores.get("away") is not None else None
                    venue = comp.get("venue") or {}
                    venue_id = str(venue.get("id") or "")
                    stadium_id = book.espn_venue_index.get(venue_id) if venue_id else None
                    stadium = book.stadiums.get(stadium_id or "")
                    indoor = (venue.get("indoor") if isinstance(venue.get("indoor"), bool) else None)
                    roof = "dome" if indoor is True else ("outdoors" if indoor is False else None)
                    schedule.append({"game": game, "total": total, "raw_date": game.kickoff_utc.date().isoformat(),
                                     "venue_id": venue_id, "roof_raw": roof or "",
                                     "venue_name": str(venue.get("fullName") or ""),
                                     "neutral_status": comp.get("neutralSite") if isinstance(comp.get("neutralSite"), bool) else None,
                                     "stadium": stadium,
                                     "event_id": str(event.get("id") or "")})
                    counts["cfb_schedule"] += 1
    # ESPN week queries overlap around week 0 / postseason. Collapse byte-level
    # repeats of the same source event, but preserve separate source event IDs so
    # genuinely conflicting duplicate fixtures remain ambiguous and fail closed.
    unique: dict[tuple[str, str, datetime, str], dict[str, Any]] = {}
    for item in schedule:
        game = item["game"]
        key = (game.sport, game.game_id, game.kickoff_utc, str(item.get("event_id") or game.source))
        unique.setdefault(key, item)
    counts["duplicate_schedule_rows"] = len(schedule) - len(unique)
    schedule = list(unique.values())
    counts["schedule_total"] = len(schedule)
    return schedule, dict(counts)


def _candidate_for(row: dict[str, Any], stamp: datetime, schedule: list[dict[str, Any]], sport: str, counts: Counter[str], book: Any) -> dict[str, Any] | None:
    teams = str(row.get("Game") or "").split("@")
    if len(teams) != 2:
        teams = str(row.get("Game") or "").split(" vs ")
    if len(teams) != 2:
        counts["bad_game_text"] += 1
        return None
    away = book.resolve_team(sport, teams[0].strip(), fuzzy=False)
    home = book.resolve_team(sport, teams[1].strip(), fuzzy=False)
    if not away or not home:
        counts["unresolved_teams"] += 1
        return None
    md = _parse_archive_date(row, stamp)
    if md is None:
        counts["bad_archive_date"] += 1
        return None
    # Archive dates omit the year. Forecast timestamp and kickoff jointly resolve it.
    viable = []
    for item in schedule:
        game = item["game"]
        if game.sport != sport or game.away_id != away or game.home_id != home:
            continue
        kick = game.kickoff_utc
        local_kick = kick.astimezone(ET)
        if (local_kick.month, local_kick.day) != md:
            continue
        lead = (kick - stamp.astimezone(UTC)).total_seconds() / 3600
        if 0 <= lead <= 168:
            viable.append((lead, item))
    if not viable:
        counts["schedule_unmatched"] += 1
        return None
    viable.sort(key=lambda x: x[0])
    if len(viable) > 1 and abs(viable[0][0] - viable[1][0]) < 1e-6:
        counts["ambiguous_schedule"] += 1
        return None
    return viable[0][1]


def _coordinate_stadium(value: Any, book: Any, espn_venue_name: str) -> Any:
    """Return a venue-name-corrobated stadium within 200m of the archived point."""
    if not isinstance(value, str) or "," not in value:
        return None
    try:
        lat, lon = (float(part.strip()) for part in value.split(",", 1))
    except ValueError:
        return None
    stadium = book.find_stadium(espn_venue_name)
    if stadium is None and espn_venue_name.strip():
        venue_key = normalize_alias(espn_venue_name)
        candidates = []
        for candidate in book.stadiums.values():
            names = [candidate.name, *(candidate.aliases or [])]
            if any((key := normalize_alias(name)) and (key.startswith(venue_key) or venue_key.startswith(key)) for name in names):
                candidates.append(candidate)
        if len(candidates) == 1:
            stadium = candidates[0]
    if stadium is None or stadium.lat is None or stadium.lon is None:
        return None
    p1, p2 = math.radians(lat), math.radians(float(stadium.lat))
    dlat = p2 - p1
    dlon = math.radians(float(stadium.lon) - lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    distance = 6371000 * 2 * math.asin(min(1, math.sqrt(a)))
    return stadium if distance <= 200 else None


def _archives(seasons: tuple[int, ...], cache_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tip = str(_git_history.git("rev-parse", ARCHIVE_REF)).strip()
    cache_file = cache_dir / f"normalized-archive-{tip[:12]}-{'-'.join(map(str, seasons))}.json.gz"
    if cache_file.exists():
        try:
            with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("archive_tip") == tip and cached.get("seasons") == list(seasons):
                snapshots = cached["snapshots"]
                for snapshot in snapshots:
                    snapshot["stamp"] = _timestamp(snapshot["stamp"])
                return snapshots, {"archive_snapshots": len(snapshots), "archive_cache_hit": 1}
        except (OSError, json.JSONDecodeError):
            pass
    counts: Counter[str] = Counter()
    snapshots: list[dict[str, Any]] = []
    for path, sport in ((NFL_ARCHIVE, "nfl"), (CFB_ARCHIVE, "cfb")):
        for snap, rows in _read_blobs(path):
            for row in rows:
                stamp = _timestamp(row.get("Timestamp"))
                if stamp is None:
                    counts["bad_timestamp"] += 1
                    continue
                # Distinct historical rows are dated by their game kickoff. The
                # timestamp is used only to identify season and forecast lead.
                if stamp.year not in seasons and stamp.year - 1 not in seasons:
                    continue
                snapshots.append({"sport": sport, "row": row, "stamp": stamp, "commit": snap.date.isoformat(),
                                  "commit_sha": snap.sha, "blob": snap.blob})
    counts["archive_snapshots"] = len(snapshots)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
        json.dump({"archive_tip": tip, "seasons": list(seasons), "snapshots": snapshots}, fh, default=str)
    counts["archive_cache_hit"] = 0
    return snapshots, dict(counts)


def _prepare_rows(seasons: tuple[int, ...], schedule: list[dict[str, Any]], cache_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    archives, counts0 = _archives(seasons, cache_dir)
    counts: Counter[str] = Counter(counts0)
    book = load_stadium_book()
    per_game: dict[str, dict[str, Any]] = {}
    for snap in archives:
        src, stamp, row = snap["sport"], snap["stamp"], snap["row"]
        # A timestamp inside a file is not enough to prove availability: the
        # content first became public at its Git commit. Use the later time.
        available_at = _available_at(stamp, snap["commit"])
        item = _candidate_for(row, available_at, schedule, src, counts, book)
        if item is None:
            continue
        game = item["game"]
        season = int(game.season)
        if season not in seasons:
            continue
        kick = game.kickoff_utc
        lead = (kick - available_at).total_seconds() / 3600
        if not (0 <= lead <= 168):
            continue
        stadium = item.get("stadium")
        sid = game.stadium_id if stadium is not None else None
        if not sid:
            counts["missing_exact_venue"] += 1
        if item.get("neutral_status") is not False:
            counts["neutral_sites_excluded"] += 1
            continue
        roof_raw = str(item.get("roof_raw") or "").lower()
        if roof_raw in ("outdoors", "open", "outdoor", "open air"):
            roof_state = "outdoors"
        elif roof_raw in ("closed", "dome", "indoor", "retractable"):
            roof_state = "dome"
        elif stadium is not None and str(stadium.roof_type or "").lower() in ("open", "outdoor", "open-air"):
            roof_state = "outdoors"
        elif stadium is not None and str(stadium.roof_type or "").lower() in ("dome", "closed"):
            roof_state = "dome"
        else:
            roof_state = None
        # ESPN ID is the only accepted CFB venue match; nflverse IDs are exact.
        exact_espn_sid = book.espn_venue_index.get(item.get("venue_id") or "")
        if src == "cfb" and (not exact_espn_sid or exact_espn_sid != sid):
            fallback = _coordinate_stadium(row.get("game_loc"), book, item.get("venue_name") or "")
            if fallback is not None:
                stadium, sid = fallback, fallback.stadium_id
                venue_source = "ESPN venue name + archived coordinates within 200m"
                counts["coordinate_venue_fallback"] += 1
            else:
                stadium = None
                sid = None
                venue_source = None
                counts["venue_id_mismatch"] += 1
        else:
            venue_source = "ESPN venue ID"
        exact_nfl_sid = book.nflverse_index.get(item.get("venue_id") or "")
        if src == "nfl" and (not exact_nfl_sid or exact_nfl_sid != sid):
            fallback = _coordinate_stadium(row.get("game_loc"), book, item.get("venue_name") or "")
            if fallback is not None:
                stadium, sid = fallback, fallback.stadium_id
                venue_source = "nflverse venue name + archived coordinates within 200m"
                counts["coordinate_venue_fallback"] += 1
            else:
                stadium = None
                sid = None
                venue_source = None
                counts["venue_id_mismatch"] += 1
        elif src == "nfl":
            venue_source = "nflverse stadium ID"
        key = game.game_id
        old = per_game.get(key)
        if old and parse_iso(old["forecast_at"]) >= available_at:
            continue
        if old is None:
            counts["matched_games"] += 1
        total_close_key = "Total_now" if src == "nfl" else "FD_now"
        total_open_key = "Total_open" if src == "nfl" else "Fd_open"
        spread_open_key = "Spread_open" if src == "nfl" else "Open"
        winddir = row.get("wind_dir_fg")
        winddir_deg = _compass(winddir)
        archived_close = _number(row.get(total_close_key))
        # Historical snapshots are only a closing proxy when observed within six
        # hours. nflverse's final total_line is a separate closing source.
        total_close, close_at, close_lead, close_source = _closing_quote(
            src, archived_close, lead, item.get("total_line"), kick, available_at,
        )
        per_game[key] = {
            "game_id": key, "sport": src, "season": season, "stadium_id": sid,
            "kickoff_utc": utc_iso(kick), "forecast_at": utc_iso(available_at),
            "source_timestamp": utc_iso(stamp.astimezone(UTC)), "source_commit_at": snap["commit"],
            "lead_fc": round(lead, 3), "wind_fc": _number(row.get("wind_fg")),
            "temp_fc": _number(row.get("temp_fg")), "rain_fc": _number(row.get("rain_fg")),
            "wind_dir_fc": winddir, "wind_dir_deg_fc": winddir_deg,
            "total_close": total_close, "close_at": close_at, "close_lead_h": close_lead,
            "closing_total_source": close_source, "total_open": _number(row.get(total_open_key)),
            "spread_open": _number(row.get(spread_open_key)), "actual_total": item.get("total"),
            "wind_act": None, "temp_act": None, "rain_act": None, "wind_dir_deg_act": None,
            "src_forecast": f"git:{snap['commit_sha']}:{snap['blob']}",
            "src_actual": None,
            "actual_source": None,
            "opening_spread_source": f"legacy_{spread_open_key}",
            "venue_source": venue_source,
            "orientation_deg": _number(stadium.orientation_deg) if stadium else None,
            "roof_state": roof_state, "lat": _number(stadium.lat) if stadium else None,
            "lon": _number(stadium.lon) if stadium else None,
        }
    rows = list(per_game.values())
    counts["output_games"] = len(rows)
    counts["graded_games"] = sum(r["actual_total"] is not None for r in rows)
    counts["forecast_direction_games"] = sum(r["wind_dir_deg_fc"] is not None for r in rows)
    counts["eligible_outdoor_games"] = sum(r["actual_total"] is not None and r["roof_state"] in ("outdoors", "open") and r["lat"] is not None and r["lon"] is not None for r in rows)
    counts["venue_resolved_games"] = sum(bool(r["stadium_id"]) for r in rows)
    counts["close_resolved_games"] = sum(r["total_close"] is not None for r in rows)
    return rows, dict(counts)


def _fetch_actuals(rows: list[dict[str, Any]], cache: Path, budget_seconds: float) -> dict[str, int]:
    """Fetch/continue ERA5 actuals. Only successful windows are cached; 429 stops."""
    cache.mkdir(parents=True, exist_ok=True)
    cached_path = cache / "era5_actuals.json"
    try:
        cached = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        cached = {}
    pending = [r for r in rows if r["actual_total"] is not None and r["roof_state"] in ("outdoors", "open") and r["lat"] is not None and r["lon"] is not None and r["game_id"] not in cached]
    deadline = time.monotonic() + max(0, budget_seconds)
    stats: Counter[str] = Counter(total_candidates=len(pending))
    # Batch up to 20 distinct venues per API request. Request date range is the
    # kickoff-hour window's UTC date(s); each game's own 3-hour window is applied
    # after the shared hourly response arrives.
    groups: dict[tuple[str, str], dict[tuple[float, float], list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in pending:
        kick = parse_iso(r["kickoff_utc"])
        start, end = _window(kick)
        date_range = (start.date().isoformat(), end.date().isoformat())
        coord = (round(float(r["lat"]), 4), round(float(r["lon"]), 4))
        groups[date_range][coord].append(r)
    with httpx.Client(timeout=35, headers={"User-Agent": USER_AGENT}) as client:
        for (start_day, end_day), coordinates in groups.items():
            coords = list(coordinates)
            for offset in range(0, len(coords), 20):
                if time.monotonic() >= deadline:
                    stats["budget_exhausted"] = 1
                    break
                batch = coords[offset:offset + 20]
                params = {"latitude": ",".join(str(c[0]) for c in batch),
                          "longitude": ",".join(str(c[1]) for c in batch),
                          "start_date": start_day, "end_date": end_day,
                          "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,precipitation",
                          "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                          "precipitation_unit": "mm", "timezone": "UTC", "models": "era5"}
                try:
                    response = client.get(ERA5_URL, params=params)
                    if response.status_code == 429:
                        stats["rate_limited"] = 1
                        break
                    response.raise_for_status()
                    payload = response.json()
                    locations = payload if isinstance(payload, list) else [payload]
                    if len(locations) != len(batch):
                        stats["response_location_mismatch"] += 1
                        continue
                    for coord, location in zip(batch, locations, strict=True):
                        hourly = (location or {}).get("hourly") or {}
                        times = hourly.get("time") or []
                        for r in coordinates[coord]:
                            start, end = _window(parse_iso(r["kickoff_utc"]))
                            idx = [i for i, value in enumerate(times) if start <= parse_iso(str(value), default_tz=UTC) <= end]
                            def values(var: str, hourly: dict[str, Any] = hourly, idx: list[int] = idx) -> list[float]:
                                vals = [_number((hourly.get(var) or [])[i]) for i in idx if i < len(hourly.get(var) or [])]
                                return [v for v in vals if v is not None]
                            def mean(var: str) -> float | None:
                                vals = values(var)
                                return sum(vals) / 3 if len(vals) == 3 else None
                            precip = values("precipitation")
                            dirs = [_number((hourly.get("wind_direction_10m") or [])[i]) for i in idx if i < len(hourly.get("wind_direction_10m") or [])]
                            direction = vector_mean_deg([v for v in dirs if v is not None]) if len([v for v in dirs if v is not None]) == 3 else None
                            if len(idx) != 3:
                                stats["empty_windows"] += 1
                                continue
                            wind_actual = mean("wind_speed_10m")
                            if wind_actual is None or direction is None:
                                stats["required_actual_missing"] += 1
                                continue
                            cached[r["game_id"]] = {"wind_act": wind_actual, "temp_act": mean("temperature_2m"),
                                                     "rain_act": sum(precip) if len(precip) == 3 else None, "wind_dir_deg_act": direction,
                                                     "source": "ERA5", "window_start_utc": utc_iso(start), "window_end_utc": utc_iso(end),
                                                     "samples": len(idx)}
                            stats["games_fetched"] += 1
                    cached_path.write_text(json.dumps(cached, indent=2), encoding="utf-8")
                except httpx.HTTPError:
                    stats["fetch_errors"] += 1
                    # Leave missing values empty and continue to other independent dates.
                time.sleep(0.15)
            if stats.get("budget_exhausted") or stats.get("rate_limited"):
                break
    for row in rows:
        actual = cached.get(row["game_id"])
        if actual:
            row.update({k: actual.get(k) for k in ("wind_act", "temp_act", "rain_act", "wind_dir_deg_act")})
            row["src_actual"] = "ERA5"
            row["actual_source"] = "ERA5"
    stats["cached_actuals"] = sum(r["wind_dir_deg_act"] is not None for r in rows)
    stats["missing_actuals"] = sum(
        r["actual_total"] is not None and r["roof_state"] in ("outdoors", "open")
        and r["lat"] is not None and r["lon"] is not None for r in rows
    ) - stats["cached_actuals"]
    return dict(stats)


def build(args: argparse.Namespace) -> tuple[Path, Path]:
    seasons = tuple(int(v) for v in args.seasons.split(",") if v.strip())
    cache = Path(args.cache_dir)
    schedule, schedule_counts = _fetch_schedules(cache, seasons, args.offline)
    rows, counts = _prepare_rows(seasons, schedule, cache)
    actual_counts: dict[str, int] = {}
    if args.fetch_actuals:
        actual_counts = _fetch_actuals(rows, cache, args.network_budget_seconds)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=FIELDS).to_parquet(output, index=False)
    manifest = output.with_suffix(output.suffix + ".manifest.json")
    archive_tip = _git_history.git("rev-parse", ARCHIVE_REF)
    body = {"dataset": output.name, "generated_at_utc": utc_iso(datetime.now(UTC)),
            "archive_ref": ARCHIVE_REF, "archive_tip": str(archive_tip).strip(),
            "seasons": list(seasons), "source_archives": {"nfl": NFL_ARCHIVE, "cfb": CFB_ARCHIVE},
            "schedule_sources": {"nfl": "nflverse games.csv", "cfb": "ESPN scoreboard"},
            "actual_source": "Open-Meteo ERA5 Archive API; actual weather fetched for every outdoor graded candidate independent of forecast wind",
            "actual_window": "kickoff hour floor through +2 hours, three hourly points; same as pipeline.backtest._window",
            "counts": {**schedule_counts, **counts, **actual_counts},
            "coverage_by_sport_season": _coverage_by_sport_season(rows), "columns": list(FIELDS),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    manifest.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return output, manifest


def _coverage_by_sport_season(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    for sport in ("cfb", "nfl"):
        for season in SEASONS:
            group = [r for r in rows if r["sport"] == sport and r["season"] == season]
            coverage[f"{sport}_{season}"] = {
                "games": len(group),
                "graded": sum(r["actual_total"] is not None for r in group),
                "forecast_direction": sum(r["wind_dir_deg_fc"] is not None for r in group),
                "venue": sum(bool(r["stadium_id"]) for r in group),
                "closing_total": sum(r["total_close"] is not None for r in group),
                "actual_direction": sum(r["wind_dir_deg_act"] is not None for r in group),
            }
    return coverage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2024,2025")
    parser.add_argument("--output", default="data/backtest/directional_inputs.parquet")
    parser.add_argument("--cache-dir", default="data/backtest/directional_archive_cache")
    parser.add_argument("--offline", action="store_true", help="Use only previously downloaded schedule files.")
    parser.add_argument("--fetch-actuals", action="store_true", help="Fetch actual ERA5 windows for all outdoor graded games.")
    parser.add_argument("--network-budget-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        output, manifest = build(args)
    except Exception as exc:  # surface useful CLI diagnostics; no silent partial dataset
        print(f"directional archive preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "manifest": str(manifest)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
