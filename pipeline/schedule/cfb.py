"""CFB schedule from CFBD `/games` (Bearer CFBD_API_KEY) with ESPN scoreboard fallback.

CFBD payload fields used: id, season, week, seasonType ('regular'|'postseason'),
startDate (ISO, UTC), startTimeTBD, neutralSite, venueId, venue, homeTeam, awayTeam,
homeClassification, awayClassification, homePoints. `/calendar?year=` gives week
windows (firstGameStart/lastGameStart) used by `current_week`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pipeline.contracts import Game, make_game_id
from pipeline.schedule.espn import fetch_espn_scoreboard, parse_espn_scoreboard
from pipeline.stadiums.loader import slug
from utils.timeutil import ET, ensure_utc, now_utc, parse_iso

CFBD_BASE = "https://api.collegefootballdata.com"
POSTSEASON_WEEK_OFFSET = 15


def _headers(api_key: Optional[str]) -> dict[str, str]:
    key = api_key or os.environ.get("CFBD_API_KEY", "")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, params: dict[str, Any], api_key: Optional[str], timeout: float = 30.0) -> Any:
    import httpx

    r = httpx.get(f"{CFBD_BASE}{path}", params=params, headers=_headers(api_key), timeout=timeout)
    r.raise_for_status()
    return r.json()


def _team_id(sport: str, name: Optional[str], book: Any) -> str:
    if not name:
        return ""
    if book is not None:
        tid = book.resolve_team(sport, name, fuzzy=False)
        if tid:
            return tid
    return slug(name)


def _local(kick_utc: datetime, tz_name: Optional[str]) -> datetime:
    if tz_name:
        try:
            return kick_utc.astimezone(ZoneInfo(tz_name))
        except (KeyError, ValueError):
            pass
    return kick_utc.astimezone(ET)


def cfb_week(raw_week: Any, season_type: Optional[str]) -> int:
    w = int(raw_week or 0)
    if (season_type or "regular").lower() == "postseason":
        return w + POSTSEASON_WEEK_OFFSET
    return w


def parse_cfbd_games(
    payload: list[dict[str, Any]],
    season: Optional[int] = None,
    book: Any = None,
    fbs_only: bool = True,   # games with at least one FBS side (FBS-vs-FCS kept for the legacy Other sheet)
    include_final: bool = False,
) -> list[Game]:
    games: list[Game] = []
    for g in payload or []:
        start = g.get("startDate") or g.get("start_date")
        if not start:
            continue
        try:
            kick_utc = parse_iso(start)
        except (TypeError, ValueError):
            continue
        yr = int(season if season is not None else (g.get("season") or kick_utc.year))
        if season is not None and g.get("season") not in (None, yr):
            continue
        home_cls = (g.get("homeClassification") or g.get("home_classification") or "").lower()
        away_cls = (g.get("awayClassification") or g.get("away_classification") or "").lower()
        if fbs_only and "fbs" not in (home_cls, away_cls):
            continue
        final = g.get("homePoints") is not None or g.get("home_points") is not None or bool(g.get("completed"))
        if final and not include_final:
            continue
        week = cfb_week(g.get("week"), g.get("seasonType") or g.get("season_type"))
        home_name = g.get("homeTeam") or g.get("home_team")
        away_name = g.get("awayTeam") or g.get("away_team")
        home_id = _team_id("cfb", home_name, book)
        away_id = _team_id("cfb", away_name, book)
        if not home_id or not away_id:
            continue
        vid = g.get("venueId") if g.get("venueId") is not None else g.get("venue_id")
        stadium_id = str(vid) if vid is not None else None
        tz_name = None
        if book is not None:
            st = book.find_stadium(stadium_id) or book.find_stadium(g.get("venue"))
            if st is not None:
                stadium_id, tz_name = st.stadium_id, st.timezone
        tbd = bool(g.get("startTimeTBD") or g.get("start_time_tbd"))
        games.append(
            Game(
                game_id=make_game_id("cfb", yr, week, away_id, home_id),
                sport="cfb",
                season=yr,
                week=week,
                kickoff_utc=kick_utc,
                kickoff_local=_local(kick_utc, tz_name),
                tz=tz_name or "America/New_York",
                home_id=home_id,
                away_id=away_id,
                stadium_id=stadium_id,
                neutral=bool(g.get("neutralSite") or g.get("neutral_site")),
                roof_state=None,
                status="final" if final else ("tbd" if tbd else "scheduled"),
                source=f"cfbd:{g.get('id', '')}",
            )
        )
    return games


def current_week(calendar: list[dict[str, Any]], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """First /calendar entry whose window has not ended (lastGameStart >= now)."""
    ref = ensure_utc(now) if now is not None else now_utc()
    for entry in sorted(calendar or [], key=lambda e: str(e.get("firstGameStart") or e.get("first_game_start") or "")):
        last = entry.get("lastGameStart") or entry.get("last_game_start")
        if not last:
            continue
        try:
            if parse_iso(last) >= ref:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def fetch_cfbd_calendar(season: int, api_key: Optional[str] = None, raw_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    payload = _get("/calendar", {"year": season}, api_key)
    _capture(raw_dir, f"calendar_{season}.json", payload)
    return payload


def fetch_cfbd_games(
    season: int,
    week: Optional[int] = None,
    season_type: str = "both",
    division: str = "fbs",
    api_key: Optional[str] = None,
    raw_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    # CFBD API v2 renamed the filter ``division`` -> ``classification``; send both so the
    # server-side FBS filter applies (v1 ignored ``classification``, v2 ignores ``division``).
    params: dict[str, Any] = {"year": season, "seasonType": season_type, "division": division,
                              "classification": division}
    if week is not None:
        params["week"] = week
    payload = _get("/games", params, api_key)
    _capture(raw_dir, f"games_{season}_{season_type}_{week or 'all'}.json", payload)
    return payload


def _capture(raw_dir: Optional[Path], name: str, payload: Any) -> None:
    if raw_dir is None:
        return
    out = Path(raw_dir) / "cfbd"
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(payload), encoding="utf-8")


def fetch_cfb_schedule(
    season: int,
    week: Optional[int] = None,
    book: Any = None,
    api_key: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    ctx: Any = None,
    include_final: bool = False,
) -> list[Game]:
    """CFBD when a key is available, else ESPN scoreboard (Degradation recorded on ctx)."""
    key = api_key or os.environ.get("CFBD_API_KEY")
    if key:
        try:
            payload = fetch_cfbd_games(season, week=week, api_key=key, raw_dir=raw_dir)
            return parse_cfbd_games(payload, season, book=book, include_final=include_final)
        except Exception as exc:  # noqa: BLE001
            if ctx is not None and hasattr(ctx, "degrade"):
                ctx.degrade("schedule.cfb", f"CFBD failed ({exc}); using ESPN", "warn")
    elif ctx is not None and hasattr(ctx, "degrade"):
        ctx.degrade("schedule.cfb", "CFBD_API_KEY missing; using ESPN scoreboard", "warn")
    games: list[Game] = []
    for season_type in (2, 3):
        try:
            payload = fetch_espn_scoreboard("cfb", season, week=week, season_type=season_type, raw_dir=raw_dir)
        except Exception as exc:  # noqa: BLE001
            if ctx is not None and hasattr(ctx, "degrade"):
                ctx.degrade("schedule.cfb", f"ESPN scoreboard failed (type {season_type}): {exc}", "error")
            continue
        games.extend(parse_espn_scoreboard(payload, "cfb", season=season, book=book))
    return [g for g in games if include_final or g.status != "final"]


__all__ = [
    "CFBD_BASE",
    "POSTSEASON_WEEK_OFFSET",
    "cfb_week",
    "current_week",
    "fetch_cfb_schedule",
    "fetch_cfbd_calendar",
    "fetch_cfbd_games",
    "parse_cfbd_games",
]
