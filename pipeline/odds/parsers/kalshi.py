"""Pure parser: Kalshi ``/events?with_nested_markets=true`` payloads -> list[GameLine].

Payload handed in by ``pipeline/odds/kalshi.py`` (and the fixtures)::

    {"KXNFLGAME":   [event, ...],   # winner  (one yes/no market per team)
     "KXNFLSPREAD": [event, ...],   # spread  ladder: "<Team> wins by over X.5 points?"
     "KXNFLTOTAL":  [event, ...]}   # total   ladder: "Will there be over X.5 points scored?"

Event shape (2026-08-23 live capture)::

    {"event_ticker": "KXNFLSPREAD-26SEP13DALNYG", "series_ticker": "KXNFLSPREAD",
     "title": "Dallas vs New York: Spread", "sub_title": "DAL vs NYG (Sep 13)",
     "product_metadata": {"competition": "Pro Football", "competition_scope": "Spread"},
     "markets": [{"ticker": "KXNFLSPREAD-26SEP13DALNYG-DAL3", "status": "active",
                  "floor_strike": 2.5, "yes_sub_title": "Dallas wins by over 2.5 points",
                  "yes_bid_dollars": "0.5100", "yes_ask_dollars": "0.5200",
                  "no_bid_dollars": "0.4800", "no_ask_dollars": "0.4900", ...}]}

Conventions locked from that capture:

* Event ticker ``{SERIES}-{YY}{MON}{DD}{AWAY}{HOME}``; the abbreviations are split
  using ``sub_title`` (``"DAL vs NYG (Sep 13)"``) because the concatenation is
  ambiguous. ``title`` carries the display names (``"Dallas vs New York"``).
  Kalshi has no neutral-site flag; the first team is treated as the away side and
  ``odds/merge.py`` tries the swapped orientation.
* Market ticker suffix: winner ``-{ABBR}``, spread ``-{ABBR}{N}`` with
  ``floor_strike = N - 0.5`` (``"<Team> wins by over X.5"``), total ``-{N}`` with
  ``floor_strike = N - 0.5``. Only ``*_dollars`` price fields are used; the legacy
  integer-cent fields are null.
* Each yes/no market yields two ``GameLine`` rows (Yes side and No side). Spread
  lines are per side: home ``-X.5`` when the home team's market, away ``+X.5``
  for the complementary No side, and vice versa.
* ``prob_raw`` = bid/ask midpoint (fee-free exchange probability, what §7.3 uses
  directly for exchanges). ``odds`` = American odds of the executable cost to
  buy that side at the ask *including* the 0.07·P(1−P) taker fee (golf
  ``_effective_price``).
* Main line (``is_main``): the ladder rung whose home / over midpoint is nearest
  0.5 (no consensus is available at parse time). Ties -> smaller |line|; when
  no rung is within ``MAIN_TOL`` of 0.5 the ladder is too sparse and nothing is
  marked main. Everything else is an alternate (``is_main=False``). Winner rows
  are main.
* Markets are skipped when not ``active``, when either side is unquoted
  (bid 0 / ask 1), or when the bid-ask width exceeds ``MAX_WIDTH`` (illiquid
  CFB ladders show 0.08 / 0.60 quotes that would poison the main-line pick).

``game_id`` is provisional until ``odds/merge.py`` resolves team ids:
``"{sport}:raw:{YYYY-MM-DD}:{AWAY}@{HOME}"`` using Kalshi abbreviations (no ``|``).
Use ``event_teams`` to recover display names / abbreviations per ``game_id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from pipeline.contracts import GameLine

BOOK = "kalshi"

SERIES_BY_SPORT: dict[str, dict[str, str]] = {
    "nfl": {"ml": "KXNFLGAME", "spread": "KXNFLSPREAD", "total": "KXNFLTOTAL"},
    "cfb": {"ml": "KXNCAAFGAME", "spread": "KXNCAAFSPREAD", "total": "KXNCAAFTOTAL"},
}
MARKET_BY_SERIES: dict[str, str] = {s: m for per in SERIES_BY_SPORT.values() for m, s in per.items()}

TAKER_FEE_RATE = 0.07
MAX_WIDTH = 0.25   # max yes bid/ask width for a market to be priced
MAIN_TOL = 0.15    # main rung must have its home/over midpoint within 0.5 +/- MAIN_TOL

_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_EVENT_RE = re.compile(r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<teams>[A-Z0-9]+)$")
_SUB_RE = re.compile(r"^\s*(?P<away>[A-Z0-9&'.-]+)\s+vs\.?\s+(?P<home>[A-Z0-9&'.-]+)", re.I)
_SPREAD_SUFFIX_RE = re.compile(r"^(?P<abbr>[A-Z]+?)(?P<n>\d+)$")


# ── price helpers (copied from golf_scraping/scrapers/kalshi.py) ─────────────
def dollar_to_american(price: float) -> int:
    """Kalshi dollar price (0-1 probability) -> American odds; 0 when unpriceable."""
    if price <= 0 or price >= 1:
        return 0
    if price >= 0.5:
        return int(round(-100 * price / (1 - price)))
    return int(round(100 * (1 - price) / price))


def taker_fee(price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return TAKER_FEE_RATE * price * (1 - price)


def effective_price(ask: float) -> float:
    """Cost to buy at the ask including the taker fee, capped at 0.99."""
    if ask <= 0 or ask >= 1:
        return ask
    return min(ask + taker_fee(ask), 0.99)


def _dollars(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quote(market: dict[str, Any]) -> tuple[float, float] | None:
    """(yes_bid, yes_ask) when the market is active and two-sided within MAX_WIDTH."""
    if (market.get("status") or "active") != "active":
        return None
    bid = _dollars(market.get("yes_bid_dollars"))
    ask = _dollars(market.get("yes_ask_dollars"))
    if bid is None or ask is None:
        return None
    if bid <= 0.0 or ask >= 1.0 or ask <= bid:
        return None
    if ask - bid > MAX_WIDTH:
        return None
    return bid, ask


# ── event identity ───────────────────────────────────────────────────────────
def parse_event_ticker(event_ticker: str) -> tuple[str, date, str] | None:
    """``KXNFLGAME-26SEP13DALNYG`` -> (series, date, 'DALNYG')."""
    m = _EVENT_RE.match(event_ticker or "")
    if not m or m.group("mon") not in _MONTHS:
        return None
    d = date(2000 + int(m.group("yy")), _MONTHS[m.group("mon")], int(m.group("dd")))
    return m.group("series"), d, m.group("teams")


def split_abbrs(event: dict[str, Any], teams: str) -> tuple[str, str] | None:
    """Away/home abbreviations: ``sub_title`` 'DAL vs NYG (Sep 13)' preferred; fall back
    to splitting the concatenated ticker on a market suffix."""
    m = _SUB_RE.match(event.get("sub_title") or "")
    if m:
        away, home = m.group("away").upper(), m.group("home").upper()
        if away + home == teams:
            return away, home
    for mk in event.get("markets") or []:
        suffix = (mk.get("ticker") or "").rsplit("-", 1)[-1]
        sm = _SPREAD_SUFFIX_RE.match(suffix)
        abbr = sm.group("abbr") if sm else suffix
        if abbr and teams.endswith(abbr) and len(abbr) < len(teams):
            return teams[: -len(abbr)], abbr
        if abbr and teams.startswith(abbr) and len(abbr) < len(teams):
            return abbr, teams[len(abbr):]
    return None


def split_names(title: str) -> tuple[str, str]:
    base = re.split(r":\s", title or "", maxsplit=1)[0]
    parts = re.split(r"\s+vs\.?\s+", base, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return base.strip(), ""
    return parts[0].strip(), parts[1].strip()


def provisional_game_id(sport: str, day: date, away: str, home: str) -> str:
    return f"{sport}:raw:{day.isoformat()}:{away}@{home}".replace("|", " ")


def event_identity(event: dict[str, Any], sport: str) -> dict[str, Any] | None:
    parsed = parse_event_ticker(event.get("event_ticker") or "")
    if parsed is None:
        return None
    series, day, teams = parsed
    abbrs = split_abbrs(event, teams)
    if abbrs is None:
        return None
    away, home = abbrs
    away_name, home_name = split_names(event.get("title") or "")
    return {
        "game_id": provisional_game_id(sport, day, away, home),
        "series": series,
        "date": day,
        "away": away,
        "home": home,
        "away_name": away_name,
        "home_name": home_name,
    }


def event_teams(payload: dict[str, Iterable[dict[str, Any]]], sport: str) -> dict[str, dict[str, Any]]:
    """{game_id: {away, home, away_name, home_name, date}} across every series."""
    out: dict[str, dict[str, Any]] = {}
    for events in payload.values():
        for ev in events or []:
            ident = event_identity(ev, sport)
            if ident and ident["game_id"] not in out:
                out[ident["game_id"]] = {k: ident[k] for k in ("away", "home", "away_name", "home_name", "date")}
    return out


# ── market -> rows ───────────────────────────────────────────────────────────
def _team_side(suffix_abbr: str, away: str, home: str) -> str | None:
    if suffix_abbr == home:
        return "home"
    if suffix_abbr == away:
        return "away"
    return None


def _pair(
    sport: str,
    game_id: str,
    market: str,
    yes_side: str,
    no_side: str,
    yes_line: float | None,
    no_line: float | None,
    bid: float,
    ask: float,
    ticker: str,
    is_main: bool,
    scraped_at: datetime | None,
    run_id: str | None,
) -> list[GameLine]:
    mid = (bid + ask) / 2.0
    yes_odds = dollar_to_american(effective_price(ask))
    no_odds = dollar_to_american(effective_price(1.0 - bid))
    rows: list[GameLine] = []
    if yes_odds:
        rows.append(GameLine(sport=sport, game_id=game_id, book=BOOK, market=market, side=yes_side, odds=yes_odds,
                             line=yes_line, prob_raw=mid, is_main=is_main, source_id=ticker,
                             scraped_at=scraped_at, run_id=run_id))
    if no_odds:
        rows.append(GameLine(sport=sport, game_id=game_id, book=BOOK, market=market, side=no_side, odds=no_odds,
                             line=no_line, prob_raw=1.0 - mid, is_main=is_main, source_id=ticker,
                             scraped_at=scraped_at, run_id=run_id))
    return rows


def _pick_main(candidates: list[tuple[float, float]]) -> float | None:
    """candidates: (line, home_or_over_mid_prob) -> line nearest 0.5, ties -> smaller |line|."""
    if not candidates:
        return None
    best = min(candidates, key=lambda c: (abs(c[1] - 0.5), abs(c[0])))
    if abs(best[1] - 0.5) > MAIN_TOL:
        return None
    return best[0]


def parse_winner_event(event: dict[str, Any], sport: str, scraped_at: datetime | None, run_id: str | None) -> list[GameLine]:
    ident = event_identity(event, sport)
    if ident is None:
        return []
    away, home, game_id = ident["away"], ident["home"], ident["game_id"]
    out: list[GameLine] = []
    for mk in event.get("markets") or []:
        q = quote(mk)
        if q is None:
            continue
        ticker = mk.get("ticker") or ""
        side = _team_side(ticker.rsplit("-", 1)[-1], away, home)
        if side is None:
            continue
        # Each team has its own market; emit only the Yes side so the two
        # markets do not double-count (the No of TEN == the Yes of SEA).
        bid, ask = q
        odds = dollar_to_american(effective_price(ask))
        if not odds:
            continue
        out.append(GameLine(sport=sport, game_id=game_id, book=BOOK, market="ml", side=side, odds=odds,
                            line=None, prob_raw=(bid + ask) / 2.0, is_main=True, source_id=ticker,
                            scraped_at=scraped_at, run_id=run_id))
    return out


def parse_spread_event(event: dict[str, Any], sport: str, scraped_at: datetime | None, run_id: str | None) -> list[GameLine]:
    ident = event_identity(event, sport)
    if ident is None:
        return []
    away, home, game_id = ident["away"], ident["home"], ident["game_id"]
    priced: list[tuple[float, str, float, float, str]] = []  # (home_line, yes_side, bid, ask, ticker)
    for mk in event.get("markets") or []:
        q = quote(mk)
        strike = _dollars(mk.get("floor_strike"))
        if q is None or strike is None:
            continue
        ticker = mk.get("ticker") or ""
        sm = _SPREAD_SUFFIX_RE.match(ticker.rsplit("-", 1)[-1])
        if not sm:
            continue
        side = _team_side(sm.group("abbr"), away, home)
        if side is None:
            continue
        home_line = -strike if side == "home" else strike
        priced.append((home_line, side, q[0], q[1], ticker))
    # home-relative midpoint for main-line selection
    candidates = [(hl, (b + a) / 2.0 if s == "home" else 1.0 - (b + a) / 2.0) for hl, s, b, a, _ in priced]
    main_line = _pick_main(candidates)
    out: list[GameLine] = []
    for home_line, side, bid, ask, ticker in priced:
        other = "away" if side == "home" else "home"
        # Yes side: "<team> wins by over X.5" == that team -X.5; No side: other team +X.5
        strike = abs(home_line)
        out.extend(_pair(sport, game_id, "spread", side, other, -strike, strike, bid, ask, ticker,
                         is_main=(main_line is not None and home_line == main_line), scraped_at=scraped_at, run_id=run_id))
    return out


def parse_total_event(event: dict[str, Any], sport: str, scraped_at: datetime | None, run_id: str | None) -> list[GameLine]:
    ident = event_identity(event, sport)
    if ident is None:
        return []
    game_id = ident["game_id"]
    priced: list[tuple[float, float, float, str]] = []
    for mk in event.get("markets") or []:
        q = quote(mk)
        strike = _dollars(mk.get("floor_strike"))
        if q is None or strike is None:
            continue
        priced.append((strike, q[0], q[1], mk.get("ticker") or ""))
    main_line = _pick_main([(s, (b + a) / 2.0) for s, b, a, _ in priced])
    out: list[GameLine] = []
    for strike, bid, ask, ticker in priced:
        out.extend(_pair(sport, game_id, "total", "over", "under", strike, strike, bid, ask, ticker,
                         is_main=(strike == main_line), scraped_at=scraped_at, run_id=run_id))
    return out


_PARSERS = {"ml": parse_winner_event, "spread": parse_spread_event, "total": parse_total_event}


def parse(
    payload: dict[str, Any],
    sport: str,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    """``{series_ticker: [events]}`` (or ``{series_ticker: {"events": [...]}}``) -> rows."""
    if sport not in SERIES_BY_SPORT:
        raise ValueError(f"unknown sport {sport!r}")
    out: list[GameLine] = []
    for series, events in (payload or {}).items():
        market = MARKET_BY_SERIES.get(series)
        if market is None or SERIES_BY_SPORT[sport].get(market) != series:
            continue
        if isinstance(events, dict):
            events = events.get("events") or []
        fn = _PARSERS[market]
        for ev in events or []:
            out.extend(fn(ev, sport, scraped_at, run_id))
    return out


__all__ = [
    "BOOK", "SERIES_BY_SPORT", "MARKET_BY_SERIES", "MAX_WIDTH", "MAIN_TOL",
    "dollar_to_american", "effective_price", "taker_fee", "quote",
    "parse_event_ticker", "split_abbrs", "split_names", "provisional_game_id", "event_identity", "event_teams",
    "parse_winner_event", "parse_spread_event", "parse_total_event", "parse",
]
