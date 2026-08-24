"""NFL schedule from nflverse games.csv.

Columns used: season, game_type, week, gameday, gametime (ET, may be blank),
away_team, home_team, location ('Home'|'Neutral'), roof, surface, stadium_id,
stadium, home_score. Parsing is pure (`parse_nflverse_games`); `fetch_nfl_schedule`
does the httpx GET and captures the raw CSV.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pipeline.contracts import ROOF_STATES, Game, make_game_id
from utils.timeutil import ET, UTC

NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
DEFAULT_TBD_TIME = "13:00"
POST_WEEK = {"WC": 19, "DIV": 20, "CON": 21, "SB": 22}


def _week(row: dict[str, str]) -> int:
    gt = (row.get("game_type") or "REG").strip()
    if gt in POST_WEEK:
        return POST_WEEK[gt]
    return int(float(row.get("week") or 0))


def _kickoff_et(row: dict[str, str]) -> Optional[datetime]:
    day = (row.get("gameday") or "").strip()
    if not day:
        return None
    tm = (row.get("gametime") or "").strip() or DEFAULT_TBD_TIME
    try:
        return datetime.strptime(f"{day} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        return None


def _local(kick_utc: datetime, tz_name: Optional[str]) -> datetime:
    if tz_name:
        try:
            return kick_utc.astimezone(ZoneInfo(tz_name))
        except (KeyError, ValueError):
            pass
    return kick_utc.astimezone(ET)


def parse_nflverse_games(
    payload: str,
    season: int,
    book: Any = None,
    weeks: Optional[Iterable[int]] = None,
    include_final: bool = False,
) -> list[Game]:
    """games.csv text -> Game list for `season` (all game types).

    `book` (optional StadiumBook) maps nflverse stadium_id -> canonical stadium_id and
    provides the venue timezone for kickoff_local; without it stadium_id stays the
    nflverse id and local time is ET.
    """
    reader = csv.DictReader(io.StringIO(payload))
    wanted = set(int(w) for w in weeks) if weeks is not None else None
    games: list[Game] = []
    for row in reader:
        try:
            if int(float(row.get("season") or 0)) != int(season):
                continue
        except ValueError:
            continue
        week = _week(row)
        if wanted is not None and week not in wanted:
            continue
        kick_et = _kickoff_et(row)
        if kick_et is None:
            continue
        home = (row.get("home_team") or "").strip().lower()
        away = (row.get("away_team") or "").strip().lower()
        if not home or not away:
            continue
        final = bool((row.get("home_score") or "").strip())
        if final and not include_final:
            continue
        nv_sid = (row.get("stadium_id") or "").strip() or None
        stadium_id, tz_name = nv_sid, None
        if book is not None and nv_sid:
            st = book.find_stadium(nv_sid)
            if st is not None:
                stadium_id, tz_name = st.stadium_id, st.timezone
        kick_utc = kick_et.astimezone(UTC)
        roof = (row.get("roof") or "").strip().lower() or None
        games.append(
            Game(
                game_id=make_game_id("nfl", int(season), week, away, home),
                sport="nfl",
                season=int(season),
                week=week,
                kickoff_utc=kick_utc,
                kickoff_local=_local(kick_utc, tz_name),
                tz=tz_name or "America/New_York",
                home_id=home,
                away_id=away,
                stadium_id=stadium_id,
                neutral=(row.get("location") or "").strip().lower() == "neutral",
                roof_state=roof if roof in ROOF_STATES else None,
                status="final" if final else ("tbd" if not (row.get("gametime") or "").strip() else "scheduled"),
                source=f"nflverse:{row.get('game_id', '')}",
            )
        )
    return games


def fetch_nflverse_csv(raw_dir: Optional[Path] = None, url: str = NFLVERSE_GAMES_URL, timeout: float = 60.0) -> str:
    import httpx

    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    text = r.text
    if raw_dir is not None:
        out = Path(raw_dir) / "nflverse"
        out.mkdir(parents=True, exist_ok=True)
        (out / "games.csv").write_text(text, encoding="utf-8")
    return text


def fetch_nfl_schedule(
    season: int,
    book: Any = None,
    raw_dir: Optional[Path] = None,
    weeks: Optional[Iterable[int]] = None,
    include_final: bool = False,
) -> list[Game]:
    text = fetch_nflverse_csv(raw_dir=raw_dir)
    return parse_nflverse_games(text, season, book=book, weeks=weeks, include_final=include_final)


__all__ = ["NFLVERSE_GAMES_URL", "POST_WEEK", "fetch_nfl_schedule", "fetch_nflverse_csv", "parse_nflverse_games"]
