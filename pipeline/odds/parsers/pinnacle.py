"""Pure parser for the Pinnacle guest feed (sport 15 = Football).

Input is the two sport-level payloads the transport fetches:
  * ``/sports/15/matchups?withSpecials=false``  -> list of matchup dicts
  * ``/sports/15/markets/straight?primaryOnly=false&withSpecials=false`` -> list of market dicts

A matchup has ``participants[{alignment: home|away, name}]``, ``league.name``
(``NFL`` / ``NFL Pre Season`` / ``NCAA``) and ``startTime`` (UTC).  Markets are
joined by ``matchupId``; ``type`` is ``moneyline`` / ``spread`` / ``total`` /
``team_total``; ``period`` 0 is full game; ``isAlternate`` False marks the main
line; ``prices[{designation, points, price}]`` carry American odds.

Scrapers do not know the schedule, so ``game_id`` is provisional
(``raw_game_id``); ``odds/merge.py`` resolves it against the schedule Game.
Pinnacle exposes no neutral-site flag; merge handles swapped sides.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pipeline.contracts import GameLine

BOOK = "pinnacle"

# league.name substrings per sport
LEAGUES: dict[str, tuple[str, ...]] = {
    "nfl": ("NFL", "NFL Pre Season"),
    "cfb": ("NCAA",),
}

MARKET_TYPES: dict[str, str] = {
    "moneyline": "ml",
    "spread": "spread",
    "total": "total",
}


@dataclass(frozen=True)
class RawEvent:
    matchup_id: int
    sport: str
    league: str
    away: str
    home: str
    start_utc: datetime | None
    game_id: str


def parse_start(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def raw_game_id(sport: str, away: str, home: str, start_utc: datetime | None) -> str:
    """Provisional id, same shape as the other parsers: ``{sport}:raw:{kickoff}:{away}@{home}``."""
    stamp = start_utc.strftime("%Y-%m-%dT%H:%M") if start_utc else "unknown"
    return f"{sport}:raw:{stamp}:{away}@{home}".replace("|", " ")


def league_sport(league_name: str) -> str | None:
    for sport, names in LEAGUES.items():
        if league_name in names:
            return sport
    return None


def parse_events(matchups: Iterable[dict], sport: str) -> list[RawEvent]:
    """Pre-game full-game matchups for ``sport`` (live/child matchups skipped)."""
    events: list[RawEvent] = []
    for m in matchups:
        if m.get("type") != "matchup" or m.get("parentId") or m.get("isLive"):
            continue
        league = (m.get("league") or {}).get("name", "")
        if league_sport(league) != sport:
            continue
        parts = m.get("participants") or []
        home = next((p.get("name") for p in parts if p.get("alignment") == "home"), None)
        away = next((p.get("name") for p in parts if p.get("alignment") == "away"), None)
        if not home or not away:
            continue
        start = parse_start(m.get("startTime"))
        events.append(RawEvent(
            matchup_id=int(m["id"]),
            sport=sport,
            league=league,
            away=away,
            home=home,
            start_utc=start,
            game_id=raw_game_id(sport, away, home, start),
        ))
    return events


def _price_line(market: dict, price: dict, event: RawEvent, mkt: str, scraped_at: datetime | None) -> GameLine | None:
    side = price.get("designation")
    odds = price.get("price")
    if side not in ("home", "away", "over", "under") or not isinstance(odds, int):
        return None
    line: float | None = None
    if mkt != "ml":
        pts = price.get("points")
        if pts is None:
            return None
        line = float(pts)
    return GameLine(
        sport=event.sport,
        game_id=event.game_id,
        book=BOOK,
        market=mkt,
        side=side,
        odds=odds,
        line=line,
        is_main=not bool(market.get("isAlternate")),
        source_id=str(event.matchup_id),
        scraped_at=scraped_at,
    )


def parse(
    matchups: Iterable[dict],
    markets: Iterable[dict],
    sport: str,
    *,
    market: str | None = None,
    include_alternates: bool = True,
    scraped_at: datetime | None = None,
) -> list[GameLine]:
    """Join matchups + straight markets into GameLine rows for ``sport``."""
    events = {ev.matchup_id: ev for ev in parse_events(matchups, sport)}
    lines: list[GameLine] = []
    for mk in markets:
        ev = events.get(mk.get("matchupId"))
        if ev is None:
            continue
        mkt = MARKET_TYPES.get(mk.get("type", ""))
        if mkt is None or mk.get("period", 0) != 0 or mk.get("status") == "closed":
            continue
        if market and mkt != market:
            continue
        if not include_alternates and mk.get("isAlternate"):
            continue
        for price in mk.get("prices") or []:
            ln = _price_line(mk, price, ev, mkt, scraped_at)
            if ln is not None:
                lines.append(ln)
    return lines


def parse_payload(payload: dict, sport: str, **kwargs: Any) -> list[GameLine]:
    """Convenience for the ``{"matchups": [...], "markets": [...]}`` raw-capture shape."""
    return parse(payload.get("matchups") or [], payload.get("markets") or [], sport, **kwargs)
