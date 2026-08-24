"""ESPN scoreboard client + parser (no key). Shared fallback for CFB (and NFL if needed).

Endpoint: https://site.api.espn.com/apis/site/v2/sports/football/{college-football|nfl}/scoreboard
Query: dates=YYYY&week=N[&groups=80 FBS][&seasontype=2|3]&limit=400.
Payload: events[] -> competitions[0] {date, neutralSite, venue{id, fullName, address},
competitors[{homeAway, team{location, displayName, abbreviation}}], status.type}.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pipeline.contracts import Game, make_game_id
from pipeline.stadiums.loader import slug
from utils.timeutil import ET, parse_iso

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
LEAGUE = {"cfb": "college-football", "nfl": "nfl"}


def _team_id(sport: str, team: dict[str, Any], book: Any) -> str:
    names = [team.get("displayName"), team.get("location"), team.get("abbreviation"), team.get("shortDisplayName")]
    if book is not None:
        for n in names:
            if n:
                tid = book.resolve_team(sport, n, fuzzy=False)
                if tid:
                    return tid
        for n in names[:2]:
            if n:
                tid = book.resolve_team(sport, n, fuzzy=True)
                if tid:
                    return tid
    if sport == "nfl":
        return str(team.get("abbreviation") or team.get("location") or "").lower()
    return slug(team.get("location") or team.get("displayName") or "")


def _local(kick_utc: datetime, tz_name: Optional[str]) -> datetime:
    if tz_name:
        try:
            return kick_utc.astimezone(ZoneInfo(tz_name))
        except (KeyError, ValueError):
            pass
    return kick_utc.astimezone(ET)


def parse_espn_scoreboard(payload: dict[str, Any], sport: str, season: Optional[int] = None, book: Any = None) -> list[Game]:
    games: list[Game] = []
    season_type = int(((payload.get("season") or {}).get("type")) or 2)
    default_week = int(((payload.get("week") or {}).get("number")) or 0)
    for ev in payload.get("events") or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        try:
            kick_utc = parse_iso(comp.get("date") or ev.get("date"))
        except (TypeError, ValueError):
            continue
        yr = int(season if season is not None else ((ev.get("season") or {}).get("year") or kick_utc.year))
        week = int(((ev.get("week") or {}).get("number")) or default_week)
        if season_type == 3:
            week += 15
        home = away = None
        for c in comp.get("competitors") or []:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if home is None or away is None:
            continue
        home_id = _team_id(sport, home.get("team") or {}, book)
        away_id = _team_id(sport, away.get("team") or {}, book)
        venue = comp.get("venue") or {}
        vid = str(venue.get("id")) if venue.get("id") is not None else None
        stadium_id, tz_name = vid, None
        if book is not None:
            st = book.find_stadium(vid) or book.find_stadium(venue.get("fullName"))
            if st is not None:
                stadium_id, tz_name = st.stadium_id, st.timezone
        state = ((comp.get("status") or {}).get("type") or {}).get("state") or "pre"
        status = {"pre": "scheduled", "in": "live", "post": "final"}.get(state, "scheduled")
        if comp.get("timeValid") is False:
            status = "tbd"
        games.append(
            Game(
                game_id=make_game_id(sport, yr, week, away_id, home_id),
                sport=sport,
                season=yr,
                week=week,
                kickoff_utc=kick_utc,
                kickoff_local=_local(kick_utc, tz_name),
                tz=tz_name or "America/New_York",
                home_id=home_id,
                away_id=away_id,
                stadium_id=stadium_id,
                neutral=bool(comp.get("neutralSite")),
                roof_state=None,
                status=status,
                source=f"espn:{ev.get('id', '')}",
            )
        )
    return games


def fetch_espn_scoreboard(
    sport: str,
    season: int,
    week: Optional[int] = None,
    season_type: int = 2,
    raw_dir: Optional[Path] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    import httpx

    params: dict[str, Any] = {"dates": season, "limit": 400, "seasontype": season_type}
    if week is not None:
        params["week"] = week
    if sport == "cfb":
        params["groups"] = 80
    r = httpx.get(ESPN_SCOREBOARD.format(league=LEAGUE[sport]), params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if raw_dir is not None:
        out = Path(raw_dir) / "espn"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"scoreboard_{sport}_{season}_{season_type}_{week or 'all'}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


__all__ = ["ESPN_SCOREBOARD", "LEAGUE", "fetch_espn_scoreboard", "parse_espn_scoreboard"]
