"""The Odds API historical snapshots → true openers (PLAN Phase 6, optional).

Off unless ``ODDS_API_KEY`` is set (and ``ODDS_API_ENABLED`` is not ``0``). Used
only to *seed* ``openers`` for games whose first scrape happened after the market
opened — never to overwrite an opener already recorded (ARCH §13).

    GET https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds
        ?apiKey=&regions=us&markets=spreads,totals&oddsFormat=american&date=<ISO>

Response: ``{"timestamp", "previous_timestamp", "next_timestamp", "data": [event]}``
with ``event = {id, commence_time, home_team, away_team, bookmakers: [{key, title,
last_update, markets: [{key: spreads|totals|h2h, outcomes: [{name, price, point}]}]}]}``.
Historical calls cost 10 credits per region-market; keep the date list short.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Optional

from pipeline import state as pstate

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}
MARKET_MAP = {"spreads": "spread", "totals": "total", "h2h": "ml"}
BOOK_MAP = {
    "pinnacle": "pinnacle", "betonlineag": "betonline", "betcris": "betcris", "fanduel": "fanduel",
    "draftkings": "draftkings", "novig": "novig", "prophetx": "prophetx", "kalshi": "kalshi",
}
USER_AGENT = "football_weather (mckinleyslade@gmail.com)"
NET_SLEEP_S = 1.0


def api_key() -> Optional[str]:
    return (os.environ.get("ODDS_API_KEY") or "").strip() or None


def enabled() -> bool:
    return api_key() is not None and os.environ.get("ODDS_API_ENABLED", "1").strip() != "0"


def _get_json(url: str, params: Mapping[str, str]) -> Any:
    import httpx

    r = httpx.get(url, params=dict(params), headers={"User-Agent": USER_AGENT}, timeout=30.0)
    r.raise_for_status()
    return r.json()


def fetch_historical(
    sport: str,
    date_iso: str,
    *,
    key: Optional[str] = None,
    regions: str = "us",
    markets: str = "spreads,totals",
    get: Callable[[str, Mapping[str, str]], Any] = _get_json,
) -> dict[str, Any]:
    k = key or api_key()
    if not k:
        raise RuntimeError("ODDS_API_KEY not set")
    return get(f"{ODDS_API_BASE}/historical/sports/{SPORT_KEYS[sport]}/odds",
               {"apiKey": k, "regions": regions, "markets": markets, "oddsFormat": "american", "date": date_iso})


def _odds(v: Any) -> Optional[int]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f))


def _line(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_historical(payload: Mapping[str, Any], sport: str, resolve: Callable[[str], Optional[str]]) -> list[dict[str, Any]]:
    """Flat rows ``{home_id, away_id, commence_time, book, market, side, line, odds, ts}``
    (side-relative lines, like GameLine). Unknown books / unresolved teams are dropped."""
    ts = str(payload.get("timestamp") or "")
    out: list[dict[str, Any]] = []
    for ev in payload.get("data") or []:
        home_name, away_name = str(ev.get("home_team") or ""), str(ev.get("away_team") or "")
        home_id, away_id = resolve(home_name), resolve(away_name)
        if not home_id or not away_id:
            continue
        for bm in ev.get("bookmakers") or []:
            book = BOOK_MAP.get(str(bm.get("key") or "").lower())
            if not book:
                continue
            seen = str(bm.get("last_update") or ts)
            for mk in bm.get("markets") or []:
                market = MARKET_MAP.get(str(mk.get("key") or ""))
                if not market:
                    continue
                for oc in mk.get("outcomes") or []:
                    name = str(oc.get("name") or "")
                    if market == "total":
                        side = name.lower()
                        if side not in ("over", "under"):
                            continue
                    else:
                        side = "home" if name == home_name else "away" if name == away_name else None
                        if side is None:
                            continue
                    odds = _odds(oc.get("price"))
                    line = _line(oc.get("point")) if market != "ml" else None
                    if odds is None or (market != "ml" and line is None):
                        continue
                    out.append({"sport": sport, "home_id": home_id, "away_id": away_id,
                                "commence_time": ev.get("commence_time"), "book": book, "market": market,
                                "side": side, "line": line, "odds": odds, "ts": seen})
    return out


def seed_openers(openers: dict, rows: Iterable[Mapping[str, Any]], game_index: Mapping[tuple[str, str], str]) -> int:
    """Add rows as openers for games in ``game_index`` (``(home_id, away_id) -> game_id``)
    that have NO opener yet. Existing openers are never touched. Returns the count added."""
    store = openers.setdefault("openers", {})
    added = 0
    for r in rows:
        gid = game_index.get((str(r.get("home_id")), str(r.get("away_id"))))
        if not gid:
            continue
        key = pstate.odds_key(gid, str(r.get("market")), str(r.get("side")), str(r.get("book")))
        if key in store:
            continue
        store[key] = {"line": r.get("line"), "odds": r.get("odds"), "ts": r.get("ts"), "source": "oddsapi"}
        added += 1
    return added


def seed_from_dates(
    sport: str,
    dates_iso: Sequence[str],
    openers: dict,
    game_index: Mapping[tuple[str, str], str],
    resolve: Callable[[str], Optional[str]],
    *,
    key: Optional[str] = None,
    get: Callable[[str, Mapping[str, str]], Any] = _get_json,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Walk ``dates_iso`` oldest → newest so the earliest snapshot wins as the opener."""
    if not enabled() and key is None:
        return 0
    added = 0
    for d in sorted(dates_iso):
        payload = fetch_historical(sport, d, key=key, get=get)
        added += seed_openers(openers, parse_historical(payload, sport, resolve), game_index)
        sleep(NET_SLEEP_S)
    return added


__all__ = ["ODDS_API_BASE", "SPORT_KEYS", "MARKET_MAP", "BOOK_MAP", "api_key", "enabled", "fetch_historical",
           "parse_historical", "seed_openers", "seed_from_dates"]
