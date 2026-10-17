"""Per-stadium game-window climate from real schedules and ERA5.

For each outdoor stadium, completed games from 2018 through the last full
season are sampled over the kickoff hour and the next two hours. Wind and
temperature are the mean of those three ERA5 hours; precipitation is their
sum. Cells are month x local kickoff window, and every cell carries N.

Open roofs contribute every completed game. Retractable roofs contribute only
games tagged open or outdoors. Domes are omitted. Unknown retractable state
is omitted. Betting totals are not part of this file.

A season is treated as complete on March 1 of the following year, after the
Super Bowl and the bowl season. On 17 Oct 2026 the window is 2018-2025.

    python -m scripts.export_stadium_climate --output data/raw/stadium_game_climate.json

Network fetches are cached under --cache-dir. --offline only reads that cache.
CFBD_API_KEY is optional; without it the college schedule comes from the ESPN
scoreboard. The key is never printed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pipeline.schedule.cfb import parse_cfbd_games
from pipeline.schedule.espn import parse_espn_scoreboard
from pipeline.schedule.nfl import parse_nflverse_games
from pipeline.stadiums.build_stadiums import USER_AGENT, scope_ids
from pipeline.stadiums.loader import DATA_DIR, load_stadium_book

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ESPN_USER_AGENT = "Mozilla/5.0 (compatible; football_weather/1.0; +https://github.com/mslade50/football_weather)"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
CFBD_GAMES = "https://api.collegefootballdata.com/games"
NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

SCHEMA_VERSION = 1
KIND = "stadium_game_window_climate"
FIRST_SEASON = 2018
MAX_BYTES = 200_000
SMALL_SAMPLE_N = 8
NFL_GAME_TYPES = {"REG", "WC", "DIV", "CON", "SB"}
EXPOSED_ROOF = {"open", "outdoors", "outdoor"}
WINDOWS = ("early", "afternoon", "prime", "late")
WINDOW_HOURS = ((0, 15), (15, 18), (18, 21), (21, 24))
WINDOW_LABELS = ("before 3pm", "3-6pm", "6-9pm", "after 9pm")
CELL_COLUMNS = (
    "month", "window", "n",
    "wind_p10", "wind_p50", "wind_p90",
    "temp_p10", "temp_p50", "temp_p90",
    "precip_p50", "precip_p90",
    "wind_ge12", "wind_ge15", "rain_ge5", "rain_ge23",
)
WIND_MPH = (12.0, 15.0)
RAIN_MM = (5.0, 23.0)
UTC = timezone.utc


@dataclass(frozen=True)
class ClimateGame:
    game_id: str
    sport: str
    season: int
    stadium_id: str
    kickoff_utc: datetime
    timezone: str
    roof_type: str


def last_full_season(today: date) -> int:
    """Season Y is complete on March 1 of Y+1."""
    return today.year - 1 if today.month >= 3 else today.year - 2


def season_span(today: Optional[date] = None, first: int = FIRST_SEASON, through: Optional[int] = None) -> list[int]:
    end = last_full_season(today or datetime.now(UTC).date()) if through is None else int(through)
    if end < first:
        raise ValueError(f"last full season {end} is before {first}")
    return list(range(first, end + 1))


def window_index(hour: int) -> int:
    if hour < 0 or hour > 23:
        raise ValueError(f"hour {hour} out of range")
    if hour < 15:
        return 0
    if hour < 18:
        return 1
    if hour < 21:
        return 2
    return 3


def hour_keys(kickoff_utc: datetime) -> list[str]:
    """Kickoff hour and the next two hours, in UTC, as YYYY-MM-DDTHH."""
    start = kickoff_utc.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [(start + timedelta(hours=offset)).strftime("%Y-%m-%dT%H") for offset in range(3)]


def percentile(values: Sequence[float], q: float) -> float:
    """Linear percentile, matching pipeline.stadiums.climatology._percentile."""
    if not values:
        raise ValueError("percentile of empty sample")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _q(value: float, places: int) -> float:
    return float(f"{value:.{places}f}")


def game_is_exposed(roof_type: Optional[str], roof_state: Optional[str]) -> bool:
    if roof_type == "open":
        return True
    if roof_type == "retractable":
        return (roof_state or "").lower() in EXPOSED_ROOF
    return False


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def hourly_index(payload: dict[str, Any], needed: Optional[set[str]] = None) -> dict[str, tuple[Optional[float], Optional[float], Optional[float]]]:
    """Map ERA5 hourly timestamps to (wind mph, temp F, precip mm)."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time") or []
    winds = hourly.get("wind_speed_10m") or []
    temps = hourly.get("temperature_2m") or []
    precs = hourly.get("precipitation") or []
    out: dict[str, tuple[Optional[float], Optional[float], Optional[float]]] = {}
    for i, raw in enumerate(times):
        key = str(raw)[:13]
        if needed is not None and key not in needed:
            continue
        wind = _finite(winds[i]) if i < len(winds) else None
        temp = _finite(temps[i]) if i < len(temps) else None
        precip = _finite(precs[i]) if i < len(precs) else None
        out[key] = (wind, temp, precip)
    return out


def sample_game(game: ClimateGame, index: dict[str, tuple[Optional[float], Optional[float], Optional[float]]]) -> Optional[dict[str, Any]]:
    rows = []
    for key in hour_keys(game.kickoff_utc):
        rec = index.get(key)
        if rec is None or any(part is None for part in rec):
            return None
        rows.append(rec)
    local = game.kickoff_utc.astimezone(ZoneInfo(game.timezone))
    return {
        "stadium_id": game.stadium_id,
        "season": game.season,
        "month": local.month,
        "window": window_index(local.hour),
        "wind": sum(row[0] for row in rows) / 3.0,
        "temp": sum(row[1] for row in rows) / 3.0,
        "precip": sum(row[2] for row in rows),
    }


def _season_month(month: int) -> int:
    return month if month >= 8 else month + 12


def aggregate(samples: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[tuple[int, int], list[dict[str, Any]]]] = {}
    for sample in samples:
        grouped.setdefault(sample["stadium_id"], {}).setdefault((sample["month"], sample["window"]), []).append(sample)
    venues: dict[str, dict[str, Any]] = {}
    for stadium_id, cells in grouped.items():
        rows = []
        seasons: set[int] = set()
        total = 0
        for (month, window), items in cells.items():
            winds = [item["wind"] for item in items]
            temps = [item["temp"] for item in items]
            precs = [item["precip"] for item in items]
            count = len(items)
            total += count
            seasons.update(int(item["season"]) for item in items)
            rows.append([
                month, window, count,
                _q(percentile(winds, 0.10), 1), _q(percentile(winds, 0.50), 1), _q(percentile(winds, 0.90), 1),
                int(round(percentile(temps, 0.10))), int(round(percentile(temps, 0.50))), int(round(percentile(temps, 0.90))),
                _q(percentile(precs, 0.50), 1), _q(percentile(precs, 0.90), 1),
                sum(1 for value in winds if value >= WIND_MPH[0]),
                sum(1 for value in winds if value >= WIND_MPH[1]),
                sum(1 for value in precs if value >= RAIN_MM[0]),
                sum(1 for value in precs if value >= RAIN_MM[1]),
            ])
        rows.sort(key=lambda row: (_season_month(row[0]), row[1]))
        venues[stadium_id] = {"n": total, "seasons": sorted(seasons), "rows": rows}
    return dict(sorted(venues.items()))


def build_payload(
    venues: dict[str, dict[str, Any]],
    seasons: Sequence[int],
    sources: dict[str, Any],
    coverage: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": generated_at,
        "seasons": [seasons[0], seasons[-1]],
        "season_years": list(seasons),
        "windows": list(WINDOWS),
        "window_hours_local": [list(pair) for pair in WINDOW_HOURS],
        "window_labels": list(WINDOW_LABELS),
        "cell_columns": list(CELL_COLUMNS),
        "thresholds": {"wind_mph": list(WIND_MPH), "rain_mm": list(RAIN_MM)},
        "small_sample_n": SMALL_SAMPLE_N,
        "units": {"wind": "mph", "temp": "F", "precip": "mm"},
        "game_window": "Three ERA5 hours: the kickoff hour and the next two. Wind and temperature are means; precipitation is the sum of the three hourly values.",
        "methodology": {
            "schedule_nfl": "nflverse games.csv regular season and playoffs, completed games with a listed kickoff.",
            "schedule_cfb": "CFBD games when a college-football data key is configured; otherwise the ESPN FBS scoreboard. Completed games only.",
            "weather": "Open-Meteo archive hourly series at the stadium coordinates (ERA5, about a 0.25 degree grid, not a field sensor).",
            "outdoor": "Open roofs include every completed game. Retractable roofs include only games tagged open or outdoors. Domes are omitted. An unknown retractable state is omitted.",
            "kickoff_window": "Local stadium time. before 3pm, 3-6pm, 6-9pm, and after 9pm.",
            "sample_size": "N is games in that month and kickoff window. Thin cells are not for ranking venues.",
            "totals": "No betting totals, prices, or win-loss records.",
        },
        "sources": sources,
        "coverage": coverage,
        "venues": venues,
    }


def validate_payload(payload: dict[str, Any], seasons: Sequence[int]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        errors.append("schema")
    if payload.get("cell_columns") != list(CELL_COLUMNS):
        errors.append("columns")
    if payload.get("windows") != list(WINDOWS):
        errors.append("windows")
    allowed = set(seasons)
    forbidden = {"rank", "wins", "losses", "pushes", "roi", "odds", "total", "game_id"}
    if forbidden & set(payload.get("cell_columns") or []):
        errors.append("forbidden column")

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if forbidden & set(obj):
                errors.append("forbidden key")
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            for value in obj:
                walk(value)

    walk(payload)
    venues = payload.get("venues") or {}
    if not isinstance(venues, dict) or not venues:
        errors.append("no venues")
    seen_pairs: set[tuple[str, int, int]] = set()
    for stadium_id, venue in venues.items():
        rows = venue.get("rows") or []
        if not rows:
            errors.append(f"{stadium_id} empty")
            continue
        total = 0
        previous = (-1, -1)
        for row in rows:
            if len(row) != len(CELL_COLUMNS):
                errors.append(f"{stadium_id} width")
                break
            month, window, count = row[0], row[1], row[2]
            if not isinstance(count, int) or count < 1:
                errors.append(f"{stadium_id} n")
            if not isinstance(month, int) or not 1 <= month <= 12 or not isinstance(window, int) or not 0 <= window <= 3:
                errors.append(f"{stadium_id} bucket")
            key = (stadium_id, month, window)
            if key in seen_pairs:
                errors.append(f"{stadium_id} duplicate")
            seen_pairs.add(key)
            order = (_season_month(month), window)
            if order < previous:
                errors.append(f"{stadium_id} order")
            previous = order
            counts = row[11:15]
            if any((not isinstance(item, int) or item < 0 or item > count) for item in counts):
                errors.append(f"{stadium_id} counts")
            wind = row[3:6]
            temp = row[6:9]
            precip = row[9:11]
            if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in (*wind, *temp, *precip)):
                errors.append(f"{stadium_id} percentile type")
            if not (wind[0] <= wind[1] + 0.15 and wind[1] <= wind[2] + 0.15):
                errors.append(f"{stadium_id} wind order")
            if not (temp[0] <= temp[1] + 1 and temp[1] <= temp[2] + 1):
                errors.append(f"{stadium_id} temp order")
            if not (precip[0] <= precip[1] + 0.15):
                errors.append(f"{stadium_id} precip order")
            total += count
        if venue.get("n") != total:
            errors.append(f"{stadium_id} n sum")
        if any(year not in allowed for year in venue.get("seasons") or []):
            errors.append(f"{stadium_id} season")
    return errors


def encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False, ensure_ascii=True) + "\n"


def _cache_path(cache_dir: Path, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return cache_dir / f"{safe}.json"


def _read_cache(path: Path) -> Optional[bytes]:
    if path.exists():
        return path.read_bytes()
    return None


def _write_cache(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _get_bytes(client: Any, url: str, params: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None, timeout: float = 120.0) -> bytes:
    import httpx

    last: Exception | None = None
    for attempt in range(6):
        try:
            response = client.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep((15.0 if response.status_code in (403, 429) else 4.0) * (attempt + 1))
                last = RuntimeError(f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return response.content
        except httpx.TransportError as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {type(last).__name__}" if last else "fetch failed")


def _cached_bytes(cache_dir: Optional[Path], name: str, client: Any, url: str, params: Optional[dict[str, Any]] = None,
                  headers: Optional[dict[str, str]] = None, offline: bool = False, timeout: float = 120.0) -> bytes:
    path = _cache_path(cache_dir, name) if cache_dir is not None else None
    if path is not None:
        cached = _read_cache(path)
        if cached is not None:
            return cached
    if offline:
        raise RuntimeError(f"offline: missing {name}")
    body = _get_bytes(client, url, params=params, headers=headers, timeout=timeout)
    if path is not None:
        _write_cache(path, body)
    return body


def outdoor_candidates(data_dir: Path) -> dict[str, dict[str, Any]]:
    book = load_stadium_book(data_dir)
    with (data_dir / "stadiums.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    ids, _wanted = scope_ids(rows, book)
    out: dict[str, dict[str, Any]] = {}
    for stadium_id in sorted(ids):
        stadium = book.stadiums.get(stadium_id)
        if stadium is None or stadium.roof_type not in ("open", "retractable"):
            continue
        if stadium.lat is None or stadium.lon is None or not stadium.timezone:
            continue
        out[stadium_id] = {
            "stadium_id": stadium_id,
            "name": stadium.name,
            "roof_type": stadium.roof_type,
            "timezone": stadium.timezone,
            "lat": stadium.lat,
            "lon": stadium.lon,
        }
    return out


def _nfl_rows(csv_text: str) -> dict[str, dict[str, str]]:
    return {row["game_id"]: row for row in csv.DictReader(csv_text.splitlines()) if row.get("game_id")}


def select_nfl_games(csv_text: str, book: Any, seasons: Sequence[int], candidates: dict[str, dict[str, Any]]) -> tuple[list[ClimateGame], Counter]:
    rows = _nfl_rows(csv_text)
    parsed = []
    for season in seasons:
        parsed.extend(parse_nflverse_games(csv_text, season, book=book, include_final=True))
    kept: list[ClimateGame] = []
    stats: Counter = Counter()
    seen: set[tuple[str, datetime]] = set()
    for game in parsed:
        source_id = game.source.split(":", 1)[1] if ":" in game.source else ""
        row = rows.get(source_id)
        if row is None:
            stats["missing_row"] += 1
            continue
        if (row.get("game_type") or "") not in NFL_GAME_TYPES:
            stats["other_game_type"] += 1
            continue
        if not (row.get("gametime") or "").strip() or game.status == "tbd":
            stats["tbd"] += 1
            continue
        if game.status != "final":
            stats["not_final"] += 1
            continue
        candidate = candidates.get(game.stadium_id or "")
        if candidate is None:
            stats["not_outdoor_candidate"] += 1
            continue
        if not game_is_exposed(candidate["roof_type"], game.roof_state):
            stats["not_exposed"] += 1
            continue
        slot = (candidate["stadium_id"], game.kickoff_utc.replace(second=0, microsecond=0))
        if slot in seen:
            stats["duplicate"] += 1
            continue
        seen.add(slot)
        kept.append(ClimateGame(
            game.game_id, "nfl", int(game.season), candidate["stadium_id"], game.kickoff_utc,
            candidate["timezone"], candidate["roof_type"],
        ))
    stats["kept"] = len(kept)
    return kept, stats


def select_cfb_games(games: Iterable[Any], candidates: dict[str, dict[str, Any]]) -> tuple[list[ClimateGame], Counter]:
    kept: list[ClimateGame] = []
    stats: Counter = Counter()
    seen: set[tuple[str, datetime]] = set()
    seen_source: set[str] = set()
    for game in games:
        if game.status == "tbd":
            stats["tbd"] += 1
            continue
        if game.status != "final":
            stats["not_final"] += 1
            continue
        if not game.stadium_id:
            stats["unresolved"] += 1
            continue
        if game.source and game.source in seen_source:
            stats["duplicate"] += 1
            continue
        candidate = candidates.get(game.stadium_id)
        if candidate is None:
            stats["not_outdoor_candidate"] += 1
            continue
        if not game_is_exposed(candidate["roof_type"], game.roof_state):
            stats["not_exposed"] += 1
            continue
        slot = (candidate["stadium_id"], game.kickoff_utc.replace(second=0, microsecond=0))
        if slot in seen:
            stats["duplicate"] += 1
            continue
        if game.source:
            seen_source.add(game.source)
        seen.add(slot)
        kept.append(ClimateGame(
            game.game_id, "cfb", int(game.season), candidate["stadium_id"], game.kickoff_utc,
            candidate["timezone"], candidate["roof_type"],
        ))
    stats["kept"] = len(kept)
    return kept, stats


def _client(user_agent: str = USER_AGENT) -> Any:
    import httpx

    return httpx.Client(timeout=120.0, headers={"User-Agent": user_agent}, follow_redirects=True)


def fetch_cfb_games(seasons: Sequence[int], book: Any, cache_dir: Optional[Path], offline: bool, log: Any) -> tuple[list[Any], dict[str, Any]]:
    """CFBD when a key is set and the request works; otherwise ESPN. The key is not logged."""
    key = os.environ.get("CFBD_API_KEY", "").strip()
    bodies: list[bytes] = []
    provider = "espn"
    parsed: list[Any] = []
    with _client() as client:
        if key:
            try:
                headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
                for season in seasons:
                    for season_type in ("regular", "postseason"):
                        name = f"cfbd_games_{season}_{season_type}"
                        body = _cached_bytes(
                            cache_dir, name, client, CFBD_GAMES,
                            params={"year": season, "seasonType": season_type, "division": "fbs", "classification": "fbs"},
                            headers=headers, offline=offline,
                        )
                        bodies.append(body)
                        parsed.extend(parse_cfbd_games(json.loads(body), season, book=book, include_final=True))
                provider = "cfbd"
            except Exception as exc:  # noqa: BLE001
                log(f"CFBD unavailable ({type(exc).__name__}); using ESPN scoreboard")
                parsed = []
                bodies = []
        if provider != "cfbd":
            jobs = [(season, season_type, week) for season in seasons for season_type, weeks in ((2, range(1, 18)), (3, range(1, 7))) for week in weeks]
            def one(job: tuple[int, int, int]) -> tuple[tuple[int, int, int], bytes]:
                season, season_type, week = job
                name = f"espn_cfb_{season}_{season_type}_{week}"
                # A fresh client per call keeps the pool thread-safe across workers.
                with _client(ESPN_USER_AGENT) as local:
                    body = _cached_bytes(
                        cache_dir, name, local, ESPN_SCOREBOARD,
                        params={"dates": season, "week": week, "seasontype": season_type, "groups": 80, "limit": 400},
                        offline=offline,
                    )
                return job, body

            found: dict[tuple[int, int, int], bytes] = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(one, job) for job in jobs]
                for future in as_completed(futures):
                    job, body = future.result()
                    found[job] = body
            for job in jobs:
                body = found[job]
                bodies.append(body)
                season, _season_type, _week = job
                parsed.extend(parse_espn_scoreboard(json.loads(body), "cfb", season=season, book=book))
    digest = hashlib.sha256()
    for body in bodies:
        digest.update(hashlib.sha256(body).digest())
    return parsed, {"provider": provider, "requests": len(bodies), "sha256": digest.hexdigest(), "parsed": len(parsed)}


def fetch_and_sample(candidates: dict[str, dict[str, Any]], games: Sequence[ClimateGame], start: str, end: str,
                     cache_dir: Optional[Path], offline: bool, workers: int, log: Any) -> tuple[list[dict[str, Any]], int]:
    """Fetch one ERA5 series per stadium and reduce it before keeping the next."""
    by_stadium: dict[str, list[ClimateGame]] = {}
    for game in games:
        by_stadium.setdefault(game.stadium_id, []).append(game)
    ids = sorted(by_stadium)

    def one(stadium_id: str) -> tuple[list[dict[str, Any]], int]:
        stadium = candidates[stadium_id]
        name = f"era5_{start}_{end}_{stadium_id}"
        params = {
            "latitude": f"{stadium['lat']:.5f}",
            "longitude": f"{stadium['lon']:.5f}",
            "start_date": start,
            "end_date": end,
            "hourly": "temperature_2m,precipitation,wind_speed_10m",
            "wind_speed_unit": "mph",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        }
        with _client() as client:
            body = _cached_bytes(cache_dir, name, client, ARCHIVE_URL, params=params, offline=offline, timeout=180.0)
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
            raise RuntimeError(f"{stadium_id} archive payload has no hourly block")
        stadium_games = by_stadium[stadium_id]
        needed: set[str] = set()
        for game in stadium_games:
            needed.update(hour_keys(game.kickoff_utc))
        index = hourly_index(payload, needed)
        samples: list[dict[str, Any]] = []
        dropped = 0
        for game in stadium_games:
            sample = sample_game(game, index)
            if sample is None:
                dropped += 1
            else:
                samples.append(sample)
        return samples, dropped

    samples: list[dict[str, Any]] = []
    dropped = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, stadium_id): stadium_id for stadium_id in ids}
        done = 0
        for future in as_completed(futures):
            stadium_id = futures[future]
            done += 1
            try:
                stadium_samples, stadium_dropped = future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append(stadium_id)
                log(f"era5 {done}/{len(ids)} {stadium_id} failed ({type(exc).__name__})")
                continue
            samples.extend(stadium_samples)
            dropped += stadium_dropped
            if done % 20 == 0 or done == len(ids):
                log(f"era5 {done}/{len(ids)}")
    if failures:
        raise RuntimeError("ERA5 missing for " + ", ".join(sorted(failures)))
    return samples, dropped


def archive_bounds(games: Sequence[ClimateGame], pad_days: int = 2) -> tuple[str, str]:
    start = min(game.kickoff_utc for game in games) - timedelta(days=pad_days)
    end = max(game.kickoff_utc for game in games) + timedelta(days=pad_days)
    return start.date().isoformat(), end.date().isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_climate(data_dir: Path, output: Path, cache_dir: Optional[Path], offline: bool, seasons: Sequence[int], workers: int, log: Any = print) -> dict[str, Any]:
    book = load_stadium_book(data_dir)
    candidates = outdoor_candidates(data_dir)
    log(f"outdoor candidates {len(candidates)} seasons {seasons[0]}-{seasons[-1]}")
    with _client() as client:
        nfl_bytes = _cached_bytes(cache_dir, "nflverse_games", client, NFLVERSE_GAMES_URL, offline=offline)
    nfl_text = nfl_bytes.decode("utf-8")
    nfl_games, nfl_stats = select_nfl_games(nfl_text, book, seasons, candidates)
    cfb_parsed, cfb_meta = fetch_cfb_games(seasons, book, cache_dir, offline, log)
    cfb_games, cfb_stats = select_cfb_games(cfb_parsed, candidates)
    games = nfl_games + cfb_games
    if not games:
        raise RuntimeError("no outdoor games selected")
    start, end = archive_bounds(games)
    stadium_ids = sorted({game.stadium_id for game in games})
    log(f"games nfl {len(nfl_games)} cfb {len(cfb_games)} via {cfb_meta['provider']}; era5 {start}..{end} for {len(stadium_ids)} stadiums")
    samples, dropped = fetch_and_sample(candidates, games, start, end, cache_dir, offline, workers, log)
    if dropped:
        log(f"dropped {dropped} games with an incomplete ERA5 hour")
    if dropped > max(25, int(0.02 * len(games))):
        raise RuntimeError(f"{dropped} games missing ERA5 hours")
    venues = aggregate(samples)
    coverage = {
        "games": len(samples),
        "dropped_incomplete_hours": dropped,
        "nfl_games": len(nfl_games),
        "cfb_games": len(cfb_games),
        "venues": len(venues),
        "candidates": len(candidates),
        "nfl_skipped": {key: value for key, value in sorted(nfl_stats.items()) if key != "kept"},
        "cfb_skipped": {key: value for key, value in sorted(cfb_stats.items()) if key != "kept"},
        "cfb_provider": cfb_meta["provider"],
    }
    sources = {
        "nfl": {"url": NFLVERSE_GAMES_URL, "sha256": hashlib.sha256(nfl_bytes).hexdigest(), "games": len(nfl_games)},
        "cfb": {"provider": cfb_meta["provider"], "requests": cfb_meta["requests"], "sha256": cfb_meta["sha256"], "games": len(cfb_games)},
        "weather": {"url": ARCHIVE_URL, "start": start, "end": end, "stadiums": len(stadium_ids),
                    "variables": ["wind_speed_10m", "temperature_2m", "precipitation"],
                    "units": {"wind": "mph", "temp": "fahrenheit", "precip": "mm"}},
        "stadiums_csv_sha256": _sha256(data_dir / "stadiums.csv"),
    }
    payload = build_payload(venues, seasons, sources, coverage, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    errors = validate_payload(payload, seasons)
    if errors:
        raise RuntimeError("climate payload failed validation: " + ", ".join(errors[:12]))
    encoded = encode(payload)
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise RuntimeError(f"climate JSON is {len(encoded.encode('utf-8'))} bytes, over the {MAX_BYTES} cap")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(output)
    log(f"wrote {output} venues={len(venues)} games={len(samples)} bytes={len(encoded.encode('utf-8'))}")
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export per-stadium game-window climate JSON")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "raw" / "stadium_game_climate.json")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--first-season", type=int, default=FIRST_SEASON)
    parser.add_argument("--through-season", type=int, default=None)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    seasons = season_span(first=args.first_season, through=args.through_season)
    export_climate(args.data_dir, args.output, args.cache_dir, args.offline, seasons, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
