"""Pure parser: Novig GraphQL payloads -> list[GameLine].

Since 2026-09-23, captures contain ``main`` and ``alternate`` raw responses to
an allowlisted market query (``data.market`` with nested ``event``). Group membership
supplies is_consensus, which the public query does not return. The older
``data.event`` format below remains supported for historical capture replay.
The October 2026 MarketScreen_Query identifies team outcomes by competitor ID
and total outcomes by Over/Under description; legacy outcome.type still replays.

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

import re
from datetime import datetime, timezone
from typing import Any

from pipeline.contracts import GameLine, game_line
from pipeline.odds.main_select import select_main_lines

BOOK = "novig"

LEAGUE_BY_SPORT: dict[str, str] = {"nfl": "NFL", "cfb": "NCAAF"}
MARKET_BY_TYPE: dict[str, str] = {"MONEY": "ml", "SPREAD": "spread", "TOTAL": "total"}
SIDE_BY_TYPE: dict[str, str] = {"Home": "home", "Away": "away", "Over": "over", "Under": "under"}
OPEN_EVENT_STATUSES = {"OPEN_PREGAME", "OPEN_INGAME"}
_SIDE_TYPE = {side: label for label, side in SIDE_BY_TYPE.items()}
_SPREAD_OUTCOME = re.compile(r"^(?P<team>.+?)\s+(?P<signed>[+-]\d+(?:\.\d+)?)$")
_TOTAL_OUTCOME = re.compile(r"^(Over|Under)\b", re.IGNORECASE)


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


def _outcome_side(outcome: dict[str, Any], market: str, game: dict[str, Any]) -> str | None:
    """Resolve explicit side identity; never infer a side from array/index order."""
    legacy = SIDE_BY_TYPE.get(outcome.get("type") or "")
    if legacy:
        return legacy
    if market == "total":
        match = re.match(r"^(Over|Under)\b", str(outcome.get("description") or ""), re.IGNORECASE)
        return match.group(1).lower() if match else None
    competitor_id = (outcome.get("competitor") or {}).get("id")
    if competitor_id:
        for side, key in (("home", "homeTeam"), ("away", "awayTeam")):
            if competitor_id == (game.get(key) or {}).get("id"):
                return side
    return None


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
            side = _outcome_side(o, market, game)
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
                game_line(
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
                    outcome_ids={resolved: x["id"] for x in m.get("outcomes") or []
                                 if (resolved := _outcome_side(x, market, game)) in {"over", "under"}
                                 and x.get("id")},
                    scraped_at=scraped_at,
                    run_id=run_id,
                )
            )
    out = [x for x in out if x is not None]
    return out


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, 2)


def _prob(value: Any) -> float | None:
    number = _num(value)
    if number is None or not (0.0 < number < 1.0):
        return None
    return float(value) if isinstance(value, (int, float)) else number


def _first_price(levels: Any) -> float | None:
    if not isinstance(levels, list) or not levels or not isinstance(levels[0], dict):
        return None
    return _prob(levels[0].get("price"))


def _best_bids(book: dict[str, Any]) -> dict[str, float]:
    """Best bid probability per outcome. Public books use ``orders``; NBX uses ladders."""
    orders = book.get("orders")
    bids: dict[str, float] = {}
    if isinstance(orders, dict):
        for outcome_id, levels in orders.items():
            price = _first_price(levels)
            if price is not None:
                bids[str(outcome_id)] = price
        return bids
    for ladder in book.get("outcomeLadders") or []:
        if not isinstance(ladder, dict) or not ladder.get("outcomeId"):
            continue
        price = _first_price(ladder.get("bids"))
        if price is not None:
            bids[str(ladder["outcomeId"])] = price
    return bids


def taker_prices(outcome_ids: list[str], book: dict[str, Any] | None) -> dict[str, float]:
    """Price shown for an outcome is one minus the best bid on the other outcome.

    Novig's odds-screen docs define that as the taker price. A bid is an offer to
    buy one side, so it is liquidity for the opposite side. One-sided books still
    price the side that has an opposing bid.
    """
    if len(outcome_ids) != 2 or not isinstance(book, dict):
        return {}
    bids = _best_bids(book)
    out: dict[str, float] = {}
    first, second = outcome_ids
    for outcome_id, other in ((first, second), (second, first)):
        other_bid = bids.get(other)
        if other_bid is None:
            continue
        price = 1.0 - other_bid
        if 0.0 < price < 1.0:
            out[outcome_id] = price
    return out


def _page_items(pages: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages or []:
        items = page.get("items") if isinstance(page, dict) else None
        if isinstance(items, list):
            rows.extend(item for item in items if isinstance(item, dict))
    return rows


def public_game_event_ids(pages: Any) -> set[str]:
    """Event ids whose description is ``Away @ Home`` and whose status is still open."""
    ids: set[str] = set()
    for event in _page_items(pages):
        status = event.get("status")
        if status and status not in OPEN_EVENT_STATUSES:
            continue
        description = str(event.get("description") or "")
        if " @ " not in description or not event.get("eventId"):
            continue
        ids.add(str(event["eventId"]))
    return ids


def _teams(description: str) -> tuple[str, str] | None:
    away, sep, home = description.partition(" @ ")
    away, home = away.strip(), home.strip()
    if not sep or not away or not home or " @ " in home:
        return None
    return away, home


def _from_ms(value: Any) -> str | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def _public_side(
    market_type: str,
    outcome: dict[str, Any],
    strike: float | None,
    token_side: dict[str, str],
    home: str,
    away: str,
) -> str | None:
    """Side from the strike (spreads), Over/Under text (totals), or a spread token (moneylines)."""
    name = str(outcome.get("name") or "").strip()
    if market_type == "TOTAL":
        match = _TOTAL_OUTCOME.match(name)
        return match.group(1).lower() if match else None
    if market_type == "SPREAD":
        match = _SPREAD_OUTCOME.match(name)
        signed = _num(match.group("signed")) if match else None
        if signed is None or strike is None:
            return None
        if signed == strike:
            return "home"
        if signed == _num(-strike):
            return "away"
        return None
    if name.casefold() == home.casefold():
        return "home"
    if name.casefold() == away.casefold():
        return "away"
    return token_side.get(name.casefold())


def _typed_outcome(outcome_id: str, side: str, price: float, description: str) -> dict[str, Any]:
    return {"id": outcome_id, "type": _SIDE_TYPE[side], "available": price, "description": description}


def _public_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = {str(event["eventId"]): event for event in _page_items(payload.get("events")) if event.get("eventId")}
    by_event: dict[str, list[dict[str, Any]]] = {}
    for market in _page_items(payload.get("markets")):
        event_id = str(market.get("eventId") or "")
        if event_id in events:
            by_event.setdefault(event_id, []).append(market)
    books = payload.get("books") if isinstance(payload.get("books"), dict) else {}
    legacy: list[dict[str, Any]] = []
    for event_id, event in events.items():
        names = _teams(str(event.get("description") or ""))
        status = event.get("status")
        if names is None or (status and status not in OPEN_EVENT_STATUSES):
            continue
        away, home = names
        token_side: dict[str, str] = {}
        prepared: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, float]]] = []
        for market in by_event.get(event_id, []):
            market_type = market.get("marketType")
            if market.get("status") not in (None, "OPEN") or market_type not in MARKET_BY_TYPE:
                continue
            outcomes = [o for o in market.get("outcomes") or [] if isinstance(o, dict) and o.get("outcomeId")]
            ids = [str(o["outcomeId"]) for o in outcomes]
            prices = taker_prices(ids, books.get(market.get("marketId")) or books.get(str(market.get("marketId") or "")))
            strike = _num(market.get("strike"))
            if market_type == "SPREAD":
                for outcome in outcomes:
                    side = _public_side("SPREAD", outcome, strike, {}, home, away)
                    match = _SPREAD_OUTCOME.match(str(outcome.get("name") or "").strip())
                    if side and match:
                        token_side[match.group("team").casefold()] = side
            prepared.append((market, outcomes, prices))
        markets: list[dict[str, Any]] = []
        for market, outcomes, prices in prepared:
            market_type = str(market.get("marketType"))
            strike = _num(market.get("strike"))
            built = []
            for outcome in outcomes:
                side = _public_side(market_type, outcome, strike, token_side, home, away)
                price = prices.get(str(outcome.get("outcomeId")))
                if side is None or price is None:
                    continue
                built.append(_typed_outcome(str(outcome["outcomeId"]), side, price, str(outcome.get("name") or "")))
            if not built:
                continue
            markets.append({
                "id": market.get("marketId"), "type": market_type, "strike": strike,
                "is_consensus": True, "outcomes": built,
            })
        if not markets:
            continue
        legacy.append({
            "id": event_id,
            "league": event.get("league"),
            "scheduled_start": _from_ms(event.get("startsTs")),
            "game": {"homeTeam": {"id": "home", "name": home}, "awayTeam": {"id": "away", "name": away}},
            "markets": markets,
        })
    return legacy


def _nbx_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    groups = payload.get("open_markets") if isinstance(payload.get("open_markets"), dict) else {}
    for group in groups.values():
        if isinstance(group, list):
            rows.extend(row for row in group if isinstance(row, dict))
    return rows


def _nbx_books(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    groups = payload.get("event_books") if isinstance(payload.get("event_books"), dict) else {}
    for rows in groups.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                book = row.get("book") if isinstance(row.get("book"), dict) else {}
                books[str(row["id"])] = book
    return books


def _nbx_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    books = _nbx_books(payload)
    grouped: dict[str, dict[str, Any]] = {}
    for market in _nbx_rows(payload):
        event = market.get("event") if isinstance(market.get("event"), dict) else {}
        game = event.get("game") if isinstance(event.get("game"), dict) else {}
        home = game.get("homeTeam") if isinstance(game.get("homeTeam"), dict) else {}
        away = game.get("awayTeam") if isinstance(game.get("awayTeam"), dict) else {}
        if not home.get("name") or not away.get("name"):
            continue
        status = event.get("status")
        if status and status not in OPEN_EVENT_STATUSES:
            continue
        market_type = market.get("type")
        if market.get("status") not in (None, "OPEN") or market_type not in MARKET_BY_TYPE:
            continue
        event_id = str(market.get("eventId") or event.get("id") or "")
        if not event_id or not market.get("id"):
            continue
        outcomes = [o for o in market.get("outcomes") or [] if isinstance(o, dict) and o.get("id")]
        prices = taker_prices([str(o["id"]) for o in outcomes], books.get(str(market["id"])))
        built = []
        for outcome in outcomes:
            price = prices.get(str(outcome["id"]))
            if price is None:
                price = _prob(outcome.get("last"))
            if price is None:
                continue
            built.append({**outcome, "available": price})
        if not built:
            continue
        bucket = grouped.setdefault(event_id, {
            "id": event_id,
            "league": market.get("league") or game.get("league"),
            "scheduled_start": game.get("scheduledStart"),
            "game": {"homeTeam": home, "awayTeam": away},
            "markets": [],
        })
        bucket["markets"].append({
            "id": market.get("id"), "type": market_type, "strike": market.get("strike"),
            "is_consensus": True, "outcomes": built,
        })
    return list(grouped.values())


def catalog_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Legacy event shape from a public-catalog or NBX capture. Empty for GraphQL captures."""
    source = payload.get("source")
    if source == "public":
        return _public_events(payload)
    if source == "nbx":
        return _nbx_events(payload)
    return []


def parse(
    payload: dict[str, Any],
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    """Convert a raw Novig capture into GameLine rows for ``sport``.

    GraphQL captures (``data.event`` or ``main``/``alternate``) replay unchanged.
    ``source=public`` and ``source=nbx`` captures come from the REST catalog. The
    REST feed has no consensus flag, so every priced strike starts as a candidate
    and ``select_main_lines`` keeps the single pair nearest a 50 cent price.
    """
    league = LEAGUE_BY_SPORT[sport]
    if payload.get("source") in ("public", "nbx"):
        events = catalog_events(payload)
        collapse_main = True
    else:
        collapse_main = False
        events = ((payload.get("data") or {}).get("event")) or []
        if "main" in payload or "alternate" in payload:
            events = []
            for group in ("main", "alternate"):
                response = payload.get(group) or {}
                for market in (response.get("data") or {}).get("market") or []:
                    # Copy only the enclosing dicts so captured responses stay raw.
                    event = market.get("event") or {}
                    events.append({**event, "markets": [{**market, "is_consensus": group == "main"}]})
    lines: list[GameLine] = []
    for e in events:
        if (e.get("league") or league) != league:
            continue
        lines.extend(parse_event(e, sport, scraped_at=scraped_at, run_id=run_id))
    if collapse_main:
        lines = select_main_lines(lines)
    return lines


__all__ = [
    "BOOK", "LEAGUE_BY_SPORT", "OPEN_EVENT_STATUSES", "catalog_events", "parse", "parse_event",
    "prob_to_american", "provisional_game_id", "public_game_event_ids", "taker_prices",
]
