"""Pure parser: Novig GraphQL ``event`` payload -> list[GameLine].

Payload shape (Hasura, see ``pipeline/odds/novig.py`` for the query)::

    {"data": {"event": [{
        "id", "description": "Away @ Home", "league": "NFL"|"NCAAF",
        "status", "scheduled_start": "2026-08-24T00:00:00+00:00",
        "game": {"homeTeam": {"name", "symbol"}, "awayTeam": {...}},
        "markets": [{
            "id", "type": "MONEY"|"SPREAD"|"TOTAL", "strike", "is_consensus",
            "outcomes": [{"index", "type": "Home"|"Away"|"Over"|"Under",
                          "description", "available", "last"}]
        }]
    }]}}

Conventions locked from the 2026-08-23 live capture:

* ``strike`` on SPREAD is **home-relative** (``TEN +6.5`` home <-> strike 6.5).
* ``strike`` on TOTAL is the total; ``MONEY`` strike is 0.
* ``is_consensus`` marks the main line; every other strike is an alternate
  (``is_main=False``).
* ``available`` is the best offered probability for that side; ``null`` means
  no liquidity -> the outcome is skipped (``last`` is only a trade print).

``game_id`` is provisional until ``odds/merge.py`` resolves team ids against
the schedule: ``"{sport}:raw:{kickoff_utc_iso}:{away}@{home}"`` (no ``|``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pipeline.contracts import GameLine

BOOK = "novig"

LEAGUE_BY_SPORT: dict[str, str] = {"nfl": "NFL", "cfb": "NCAAF"}
MARKET_BY_TYPE: dict[str, str] = {"MONEY": "ml", "SPREAD": "spread", "TOTAL": "total"}
SIDE_BY_TYPE: dict[str, str] = {"Home": "home", "Away": "away", "Over": "over", "Under": "under"}


def prob_to_american(price: float) -> int:
    """Decimal probability (0-1) -> American odds; 0 when out of range."""
    if price <= 0 or price >= 1:
        return 0
    if price >= 0.5:
        return int(round(-100 * price / (1 - price)))
    return int(round(100 * (1 - price) / price))


def parse_kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def provisional_game_id(sport: str, kickoff: datetime | None, away: str, home: str) -> str:
    stamp = kickoff.strftime("%Y-%m-%dT%H:%M") if kickoff else "unknown"
    return f"{sport}:raw:{stamp}:{away}@{home}".replace("|", " ")


def _side_line(market: str, side: str, strike: float | None) -> float | None:
    """Line for a side. Spread strike is home-relative; away gets the negation."""
    if market == "ml":
        return None
    if strike is None:
        return None
    if market == "spread":
        return float(strike) if side == "home" else -float(strike)
    return float(strike)


def parse_event(
    event: dict[str, Any],
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    game = event.get("game") or {}
    home = (game.get("homeTeam") or {}).get("name") or ""
    away = (game.get("awayTeam") or {}).get("name") or ""
    if not home or not away:
        return []
    kickoff = parse_kickoff(event.get("scheduled_start"))
    game_id = provisional_game_id(sport, kickoff, away, home)

    out: list[GameLine] = []
    for m in event.get("markets") or []:
        market = MARKET_BY_TYPE.get(m.get("type") or "")
        if market is None:
            continue
        strike = m.get("strike")
        is_main = bool(m.get("is_consensus"))
        for o in m.get("outcomes") or []:
            side = SIDE_BY_TYPE.get(o.get("type") or "")
            if side is None:
                continue
            avail = o.get("available")
            if avail is None or not (0.0 < float(avail) < 1.0):
                continue
            prob = float(avail)
            line = _side_line(market, side, strike)
            if market != "ml" and line is None:
                continue
            # -0.0 is ugly in outputs and breaks equality with 0.0 in some sinks.
            if line == 0:
                line = 0.0
            out.append(
                GameLine(
                    sport=sport,
                    game_id=game_id,
                    book=BOOK,
                    market=market,
                    side=side,
                    odds=prob_to_american(prob),
                    line=line,
                    prob_raw=prob,
                    is_main=is_main,
                    source_id=f"{event.get('id')}:{m.get('id')}",
                    scraped_at=scraped_at,
                    run_id=run_id,
                )
            )
    return out


def parse(
    payload: dict[str, Any],
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    """Convert a raw Novig GraphQL response into GameLine rows for ``sport``."""
    league = LEAGUE_BY_SPORT[sport]
    events = ((payload.get("data") or {}).get("event")) or []
    lines: list[GameLine] = []
    for e in events:
        if (e.get("league") or league) != league:
            continue
        lines.extend(parse_event(e, sport, scraped_at=scraped_at, run_id=run_id))
    return lines


__all__ = ["BOOK", "LEAGUE_BY_SPORT", "parse", "parse_event", "prob_to_american", "provisional_game_id"]
