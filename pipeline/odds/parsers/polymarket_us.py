"""Public Polymarket US football BBOs; full-game markets only.

Buy long at the ask; buy the opposite outcome at 1 - bid. Last trades and
marketSides.price are not executable prices. Fees follow docs.polymarket.us/fees.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

from pipeline.contracts import GameLine, game_line
from pipeline.model.fair import prob_to_american
from pipeline.odds.ids import provisional_game_id as build_game_id
from pipeline.odds.parsers.common import MAX_QUOTE_WIDTH, parse_utc
from pipeline.odds.parsers.common import finite_number as number

BOOK = "polymarket_us"
TYPES = {"football_team_full_game_total": "total", "football_team_full_game_spread": "spread",
         "football_team_full_game_winner": "ml"}


def parse(payload: dict, sport: str, *, scraped_at: datetime, run_id: str | None = None) -> list[GameLine]:
    if sport not in ("nfl", "cfb"):
        raise ValueError(f"unknown sport {sport}")
    out = []
    for event in payload.get("events", []):
        if not event.get("active") or event.get("closed") or event.get("live") or event.get("ended"):
            continue
        if not str(event.get("slug", "")).startswith(sport + "-"):
            continue
        try:
            kickoff = parse_utc(event["startTime"], naive_utc=False)
        except (KeyError, TypeError, ValueError):
            continue
        if kickoff is None or kickoff <= scraped_at:
            continue
        # Full-game winner sides carry explicit home/away identity; never infer
        # home from display order or a total market's generic over/under sides.
        teams = {}
        for market in event.get("markets", []):
            if market.get("sportsMarketType") == "football_team_full_game_winner":
                for side in market.get("marketSides", []):
                    team = side.get("team") or {}
                    if team.get("ordering") in ("home", "away") and team.get("name"):
                        teams[team["ordering"]] = team["name"]
        if len(teams) != 2:
            continue
        gid = build_game_id(sport, teams["away"], teams["home"], kickoff.astimezone(timezone.utc), slug="and")
        rows = []
        for market in event.get("markets", []):
            kind = TYPES.get(market.get("sportsMarketType"))
            if (kind is None or not market.get("active") or market.get("closed")
                    or market.get("ep3Status") != "OPEN" or market.get("hidden")):
                continue
            bid = number((market.get("bestBidQuote") or {}).get("value"))
            ask = number((market.get("bestAskQuote") or {}).get("value"))
            fee = number(market.get("feeCoefficient"))
            if bid is None or ask is None or not 0 < bid <= ask < 1 or ask - bid > MAX_QUOTE_WIDTH:
                continue
            if fee is None or not 0 <= fee <= 1:
                continue  # unknown fees must not look like a free execution
            line = number(market.get("line")) if kind != "ml" else None
            # These exchange contracts have binary settlement. Do not pretend an
            # integer strike refunds the stake like a sportsbook push.
            if kind != "ml" and (line is None or not math.isclose(line % 1, .5)):
                continue
            for side in market.get("marketSides", []):
                long = side.get("long")
                if not isinstance(long, bool) or side.get("tradable") is not True:
                    continue
                if kind == "total":
                    outcome = str(side.get("description", "")).lower()
                    if outcome != ("over" if long else "under"):
                        continue
                    points = line
                else:
                    outcome = (side.get("team") or {}).get("ordering")
                    if outcome not in teams:
                        continue
                    points = (line if long else -line) if kind == "spread" else None
                price = ask if long else 1 - bid
                cost = price + fee * price * (1 - price)
                if not 0 < cost < 1:
                    continue
                mid = (bid + ask) / 2
                ln_ = (game_line(sport=sport, game_id=gid, book=BOOK, market=kind, side=outcome,
                                     line=points, odds=prob_to_american(cost), prob_raw=mid if long else 1-mid,
                                     is_main=False, source_id=market.get("slug"), scraped_at=scraped_at, run_id=run_id))
                if ln_ is not None:
                    rows.append(ln_)
        # Main rung = balanced midpoint, with both sides on that same contract.
        for kind in ("total", "spread", "ml"):
            group = [r for r in rows if r.market == kind]
            if group:
                main = min(group, key=lambda r: (abs(r.prob_raw - .5), r.source_id or "")).source_id
                out.extend(replace(r, is_main=r.source_id == main) for r in group)
    return out
