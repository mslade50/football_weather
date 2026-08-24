"""Pure parser for the FanDuel ``content-managed-page`` payload (ARCH §8).

Input is the JSON envelope returned by
``sbapi.az.sportsbook.fanduel.com/api/content-managed-page?page=CUSTOM&customPageId={nfl,ncaaf}``
(or the ``competition-page`` fallback — same ``attachments`` shape):

    attachments.events[eventId]       {name 'Away @ Home', openDate, competitionId}
    attachments.competitions[cid]     {name 'NFL' | 'NFL Preseason' | 'NCAA Football Games' | 'NCAA FCS'}
    attachments.markets[marketId]     {eventId, marketType, marketStatus, runners[]}
    runner                            {runnerName, handicap, result.type HOME|AWAY|OVER|UNDER,
                                       runnerStatus, winRunnerOdds.americanDisplayOdds.americanOdds}

Game markets: ``MONEY_LINE``, ``MATCH_HANDICAP_(2-WAY)``, ``TOTAL_POINTS_(OVER/UNDER)``.
Spread ``handicap`` is already side-relative (+2.5 on the away runner, -2.5 on
the home runner); ``GameLine.line`` keeps it side-relative. Totals carry the
same ``handicap`` on both runners.

``game_id`` is provisional (``{sport}:raw:{YYYY-MM-DD}:{Away}@{Home}`` with the
raw FanDuel team names) — ``odds/merge.py`` resolves it against the schedule.
``source_id`` is ``fanduel:{eventId}``. FanDuel exposes no neutral-site flag;
the away/home order is whatever the event name says.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from pipeline.contracts import GameLine

logger = logging.getLogger(__name__)

BOOK = "fanduel"

MARKET_TYPES: dict[str, str] = {
    "MONEY_LINE": "ml",
    "MATCH_HANDICAP_(2-WAY)": "spread",
    "TOTAL_POINTS_(OVER/UNDER)": "total",
}
SIDE_BY_RESULT: dict[str, str] = {"HOME": "home", "AWAY": "away", "OVER": "over", "UNDER": "under"}

# Competition ids seen on the custom pages (kept for the competition-page fallback).
COMPETITIONS: dict[str, dict[str, str]] = {
    "nfl": {"12282733": "NFL", "11432305": "NFL Preseason"},
    "cfb": {"12529073": "NCAA Football Games", "12623176": "NCAA FCS"},
}
# Competition names that carry game markets for each sport.
GAME_COMPETITIONS: dict[str, tuple[str, ...]] = {
    "nfl": ("NFL", "NFL Preseason"),
    "cfb": ("NCAA Football Games", "NCAA FCS"),
}


@dataclass(frozen=True)
class FanDuelEvent:
    event_id: str
    away: str
    home: str
    kickoff_utc: datetime
    competition: str


def raw_game_id(away: str, home: str, kickoff_utc: datetime, sport: str) -> str:
    day = kickoff_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{sport}:raw:{day}:{away}@{home}"


def parse_open_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def split_event_name(name: str) -> tuple[str, str] | None:
    """'Away @ Home' -> (away, home); None when the name is not a game."""
    if " @ " not in name:
        return None
    away, home = name.split(" @ ", 1)
    away, home = away.strip(), home.strip()
    if not away or not home:
        return None
    return away, home


def runner_odds(runner: dict) -> int | None:
    odds = (runner.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}
    val = odds.get("americanOdds")
    if val is None:
        val = odds.get("americanOddsInt")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def iter_events(payload: dict, sport: str) -> Iterable[FanDuelEvent]:
    att = payload.get("attachments") or payload
    comps = att.get("competitions") or {}
    allowed = GAME_COMPETITIONS[sport]
    for eid, ev in (att.get("events") or {}).items():
        comp_name = (comps.get(str(ev.get("competitionId")), {}) or {}).get("name", "")
        if comp_name not in allowed:
            continue
        teams = split_event_name(ev.get("name") or "")
        if not teams:
            continue
        try:
            kickoff = parse_open_date(ev["openDate"])
        except (KeyError, ValueError, TypeError):
            logger.warning(f"[fanduel] event {eid} bad openDate {ev.get('openDate')!r}")
            continue
        yield FanDuelEvent(event_id=str(eid), away=teams[0], home=teams[1], kickoff_utc=kickoff, competition=comp_name)


def parse_market(
    mkt: dict,
    event: FanDuelEvent,
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    market = MARKET_TYPES.get(mkt.get("marketType") or "")
    if market is None:
        return []
    if (mkt.get("marketStatus") or "OPEN") != "OPEN":
        return []
    out: list[GameLine] = []
    game_id = raw_game_id(event.away, event.home, event.kickoff_utc, sport)
    for runner in mkt.get("runners") or []:
        if (runner.get("runnerStatus") or "ACTIVE") != "ACTIVE":
            continue
        side = SIDE_BY_RESULT.get(((runner.get("result") or {}).get("type") or "").upper())
        if side is None:
            continue
        odds = runner_odds(runner)
        if odds is None or odds == 0:
            continue
        line: float | None = None
        if market != "ml":
            handicap = runner.get("handicap")
            if handicap is None:
                continue
            line = float(handicap)
        out.append(GameLine(
            sport=sport, game_id=game_id, book=BOOK, market=market, side=side,
            odds=odds, line=line, is_main=True,
            source_id=f"{BOOK}:{event.event_id}", scraped_at=scraped_at, run_id=run_id,
        ))
    return out


def parse(
    payload: dict,
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
    market: str | None = None,
) -> list[GameLine]:
    """Raw FanDuel envelope -> GameLine rows for ``sport`` (``nfl`` | ``cfb``)."""
    att = payload.get("attachments") or payload
    events = {ev.event_id: ev for ev in iter_events(payload, sport)}
    out: list[GameLine] = []
    for mkt in (att.get("markets") or {}).values():
        ev = events.get(str(mkt.get("eventId")))
        if ev is None:
            continue
        rows = parse_market(mkt, ev, sport, scraped_at=scraped_at, run_id=run_id)
        if market:
            rows = [r for r in rows if r.market == market]
        out.extend(rows)
    return out


def events_by_game_id(payload: dict, sport: str) -> dict[str, FanDuelEvent]:
    """Provisional game_id -> event metadata (kickoff, raw names) for merge."""
    return {raw_game_id(ev.away, ev.home, ev.kickoff_utc, sport): ev for ev in iter_events(payload, sport)}


__all__ = [
    "BOOK",
    "COMPETITIONS",
    "GAME_COMPETITIONS",
    "MARKET_TYPES",
    "FanDuelEvent",
    "events_by_game_id",
    "iter_events",
    "parse",
    "parse_market",
    "raw_game_id",
    "runner_odds",
    "split_event_name",
]
