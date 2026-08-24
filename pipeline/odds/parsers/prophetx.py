"""Pure parser for the ProphetX partner Market Data API (football).

Input is the raw-capture shape the transport persists::

    {"events": [<sport_event>, ...], "markets": {"<event_id>": [<market>, ...]}}

``events`` come from ``get_sport_events?tournament_id=...`` for every active
football tournament (``NFL`` -> ``nfl``; ``College Football`` -> ``cfb``;
``* Futures`` tournaments are outrights and skipped).  Each event has
``competitors[{id, name, side: home|away}]``, ``scheduled`` (UTC) and
``status`` (``not_started`` | ``live`` | ...).  ProphetX exposes no
neutral-site flag; ``odds/merge.py`` handles swapped sides.

``markets`` come from ``get_multiple_markets?event_ids=...``.  Only the
full-game markets are used, keyed by ProphetX market id:

* 219 ``Moneyline``: ``selections`` is a list of outcome groups; each group is
  the order ladder for one side (``competitor_id``, ``odds`` American, ``stake``).
* 223 ``Spread``: ``market_lines[{line (home-relative), favourite, selections}]``
  with ``selection.line`` signed per competitor.
* 225 ``Total Points``: ``market_lines`` with ``selection.name`` ``over X`` /
  ``under X`` (``outcome_id`` 12 over / 13 under).

First-half / quarter / team-total markets share ``type`` with the above and are
excluded by id + name.  Within a ladder the best (highest American) priced
order wins; ``odds`` is ``null`` where no liquidity is posted, so a side can be
missing.  ``favourite: true`` marks the exchange's main line (``is_main``).

ProphetX is an exchange, so ``prob_raw`` is the implied probability of the
best takeable price (no vig to strip; ARCH §7.3).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pipeline.contracts import GameLine

BOOK = "prophetx"

# tournament name -> sport (exact names from get_tournaments)
TOURNAMENTS: dict[str, tuple[str, ...]] = {
    "nfl": ("NFL", "NFL Preseason", "NFL Pre Season"),
    "cfb": ("College Football", "NCAAF"),
}
FOOTBALL_SPORT = "american football"

# full-game market ids / names -> contract market
MARKET_IDS: dict[int, str] = {219: "ml", 223: "spread", 225: "total"}
MARKET_NAMES: dict[str, str] = {"moneyline": "ml", "spread": "spread", "total points": "total"}

SKIP_EVENT_STATUS = frozenset({"finished", "closed", "cancelled", "canceled", "settled", "postponed"})
LIVE_EVENT_STATUS = frozenset({"live", "in_progress", "inplay"})
SKIP_MARKET_STATUS = frozenset({"inactive", "closed", "settled", "suspended"})

OUTCOME_OVER = 12
OUTCOME_UNDER = 13


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    sport: str
    tournament: str
    away: str
    home: str
    away_cid: int | None
    home_cid: int | None
    start_utc: datetime | None
    status: str
    game_id: str


def _text(value: Any) -> str:
    return str(value or "").strip()


def american_to_prob(odds: int) -> float:
    if odds < 0:
        return -odds / (-odds + 100.0)
    return 100.0 / (odds + 100.0)


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


def tournament_sport(tournament: dict) -> str | None:
    """Map a ``get_tournaments`` row to ``nfl`` / ``cfb`` (None for futures / other sports)."""
    name = _text(tournament.get("name"))
    sport = tournament.get("sport")
    sport_name = _text(sport.get("name") if isinstance(sport, dict) else sport).lower()
    if sport_name and sport_name != FOOTBALL_SPORT:
        return None
    if "futures" in name.lower() or "outright" in name.lower():
        return None
    for key, names in TOURNAMENTS.items():
        if name in names:
            return key
    return None


def event_sport(event: dict) -> str | None:
    """Sport for a ``get_sport_events`` row (via ``tournament_name``)."""
    return tournament_sport({"name": event.get("tournament_name"), "sport": event.get("sport_name")})


def parse_events(events: Iterable[dict], sport: str) -> list[RawEvent]:
    """Two-competitor football events for ``sport`` (futures/outright events skipped)."""
    out: list[RawEvent] = []
    for ev in events:
        if event_sport(ev) != sport:
            continue
        if _text(ev.get("sub_type")).lower() == "outrights" or _text(ev.get("type")).lower() == "custom":
            continue
        comps = ev.get("competitors") or []
        home = next((c for c in comps if _text(c.get("side")).lower() == "home"), None)
        away = next((c for c in comps if _text(c.get("side")).lower() == "away"), None)
        if not home or not away:
            continue
        eid = _text(ev.get("event_id") or ev.get("id"))
        if not eid:
            continue
        start = parse_start(ev.get("scheduled"))
        home_name = _text(home.get("name") or home.get("display_name"))
        away_name = _text(away.get("name") or away.get("display_name"))
        out.append(RawEvent(
            event_id=eid,
            sport=sport,
            tournament=_text(ev.get("tournament_name")),
            away=away_name,
            home=home_name,
            away_cid=_int_or_none(away.get("id")),
            home_cid=_int_or_none(home.get("id")),
            start_utc=start,
            status=_text(ev.get("status")).lower(),
            game_id=raw_game_id(sport, away_name, home_name, start),
        ))
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def market_kind(market: dict) -> str | None:
    """``ml`` / ``spread`` / ``total`` for the three full-game markets, else None."""
    mid = _int_or_none(market.get("id"))
    if mid in MARKET_IDS:
        return MARKET_IDS[mid]
    # Fall back on the exact full-game names; derivative markets (First Half,
    # 1st Quarter, "XXX: Team Total Points") never match.
    return MARKET_NAMES.get(_text(market.get("name")).lower())


def _best_odds(group: list[dict]) -> tuple[int, dict] | None:
    """Best takeable (highest American) order in a ladder; None if nothing priced."""
    best: tuple[int, dict] | None = None
    for sel in group:
        raw = sel.get("odds")
        if raw in (None, "", 0):
            continue
        try:
            odds = int(round(float(str(raw).replace("+", ""))))
        except (TypeError, ValueError):
            continue
        if odds == 0:
            continue
        if best is None or odds > best[0]:
            best = (odds, sel)
    return best


def _ml_side(sel: dict, ev: RawEvent) -> str | None:
    cid = _int_or_none(sel.get("competitor_id"))
    if cid is not None and cid == ev.home_cid:
        return "home"
    if cid is not None and cid == ev.away_cid:
        return "away"
    name = _text(sel.get("name")).lower()
    if name == ev.home.lower():
        return "home"
    if name == ev.away.lower():
        return "away"
    return None


def _ou_side(sel: dict) -> str | None:
    oid = _int_or_none(sel.get("outcome_id"))
    if oid == OUTCOME_OVER:
        return "over"
    if oid == OUTCOME_UNDER:
        return "under"
    name = _text(sel.get("name")).lower()
    if name.startswith("over"):
        return "over"
    if name.startswith("under"):
        return "under"
    return None


def _selection_groups(market_or_line: dict) -> list[list[dict]]:
    raw = market_or_line.get("selections") or []
    if not isinstance(raw, list):
        return []
    if raw and all(isinstance(g, list) for g in raw):
        return [g for g in raw if g]
    grouped: dict[str, list[dict]] = {}
    for sel in raw:
        if isinstance(sel, dict):
            grouped.setdefault(_text(sel.get("line_id") or sel.get("outcome_id") or sel.get("name")), []).append(sel)
    return list(grouped.values())


def _line_value(sel: dict, fallback: Any) -> float | None:
    for v in (sel.get("line"), fallback):
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _make(ev: RawEvent, mkt: str, side: str, odds: int, line: float | None, is_main: bool,
          sel: dict, scraped_at: datetime | None) -> GameLine:
    if line == 0:
        line = 0.0
    return GameLine(
        sport=ev.sport,
        game_id=ev.game_id,
        book=BOOK,
        market=mkt,
        side=side,
        odds=odds,
        line=line,
        prob_raw=american_to_prob(odds),
        is_main=is_main,
        source_id=f"{ev.event_id}:{_text(sel.get('line_id')) or side}",
        scraped_at=scraped_at,
    )


def parse_event_markets(
    ev: RawEvent,
    markets: Iterable[dict],
    *,
    market: str | None = None,
    include_alternates: bool = True,
    scraped_at: datetime | None = None,
) -> list[GameLine]:
    out: list[GameLine] = []
    for mk in markets:
        mkt = market_kind(mk)
        if mkt is None or (market and mkt != market):
            continue
        if _text(mk.get("status")).lower() in SKIP_MARKET_STATUS:
            continue
        if mkt == "ml":
            for group in _selection_groups(mk):
                best = _best_odds(group)
                if best is None:
                    continue
                side = _ml_side(best[1], ev)
                if side is None:
                    continue
                out.append(_make(ev, "ml", side, best[0], None, True, best[1], scraped_at))
            continue
        lines = mk.get("market_lines") or []
        # favourite marks the main line; if the feed omits it, treat every line as main
        any_fav = any(bool(ml.get("favourite")) for ml in lines)
        for ml in lines:
            is_main = bool(ml.get("favourite")) if any_fav else True
            if not include_alternates and not is_main:
                continue
            for group in _selection_groups(ml):
                best = _best_odds(group)
                if best is None:
                    continue
                sel = best[1]
                side = _ml_side(sel, ev) if mkt == "spread" else _ou_side(sel)
                if side is None:
                    continue
                line = _line_value(sel, ml.get("line"))
                if line is None:
                    continue
                out.append(_make(ev, mkt, side, best[0], line, is_main, sel, scraped_at))
    return out


def parse(
    events: Iterable[dict],
    markets_by_event: dict[str, list[dict] | None],
    sport: str,
    *,
    market: str | None = None,
    include_alternates: bool = True,
    include_live: bool = False,
    scraped_at: datetime | None = None,
) -> list[GameLine]:
    """Join events + per-event markets into GameLine rows for ``sport``.

    Events whose status is finished/closed are dropped; in-play events are
    dropped too unless ``include_live`` (their prices are live, not pre-game).
    """
    out: list[GameLine] = []
    for ev in parse_events(events, sport):
        if ev.status in SKIP_EVENT_STATUS:
            continue
        if ev.status in LIVE_EVENT_STATUS and not include_live:
            continue
        mks = markets_by_event.get(ev.event_id) or []
        out.extend(parse_event_markets(
            ev, mks, market=market, include_alternates=include_alternates, scraped_at=scraped_at,
        ))
    return out


def parse_payload(payload: dict, sport: str, **kwargs: Any) -> list[GameLine]:
    """Convenience for the ``{"events": [...], "markets": {eid: [...]}}`` raw-capture shape."""
    markets = payload.get("markets") or {}
    if not isinstance(markets, dict):
        markets = {}
    return parse(payload.get("events") or [], {str(k): v for k, v in markets.items()}, sport, **kwargs)
