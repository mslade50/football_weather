"""FanDuel football scraper via the public content-managed-page API (no auth).

Transport copied from golf_scraping/scrapers/fanduel.py: ONE unauthenticated
httpx GET returns the whole league board:

    GET https://sbapi.az.sportsbook.fanduel.com/api/content-managed-page
        ?page=CUSTOM&customPageId={nfl|ncaaf}&_ak=FhMFpcPWXMeyZxOx

If the custom page returns no game markets, fall back to the per-competition
``competition-page?eventTypeId=1&competitionId=...`` endpoints (NFL 12282733,
NFL Preseason 11432305, NCAA Games 12529073, NCAA FCS 12623176).

Parsing lives in ``pipeline/odds/parsers/fanduel.py``; raw payloads are
captured through ``raw_store.put`` when a ``RawStore`` is supplied.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import fanduel as parser

logger = logging.getLogger(__name__)

API_BASE = "https://sbapi.az.sportsbook.fanduel.com/api/content-managed-page"
COMPETITION_API = "https://sbapi.az.sportsbook.fanduel.com/api/competition-page"
API_KEY = "FhMFpcPWXMeyZxOx"
FOOTBALL_EVENT_TYPE_ID = 1

PAGE_IDS: dict[str, str] = {"nfl": "nfl", "cfb": "ncaaf"}

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


def _has_game_markets(payload: dict) -> bool:
    markets = (payload.get("attachments") or {}).get("markets") or {}
    return any(m.get("marketType") in parser.MARKET_TYPES for m in markets.values())


async def fetch_board(sport: str, client: httpx.AsyncClient | None = None) -> dict:
    """Return the raw FanDuel envelope for ``sport`` (custom page, competition fallback)."""
    page_id = PAGE_IDS[sport]
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=20.0)
    try:
        resp = await client.get(API_BASE, params={"page": "CUSTOM", "customPageId": page_id, "_ak": API_KEY})
        resp.raise_for_status()
        payload: dict = resp.json()
        if _has_game_markets(payload):
            return payload
        logger.warning(f"[fanduel] custom page {page_id} has no game markets; trying competition pages")
        att: dict[str, dict] = {"competitions": {}, "events": {}, "markets": {}}
        for cid in parser.COMPETITIONS[sport]:
            try:
                r = await client.get(COMPETITION_API, params={
                    "eventTypeId": FOOTBALL_EVENT_TYPE_ID, "competitionId": cid, "_ak": API_KEY,
                })
                r.raise_for_status()
                comp_att = r.json().get("attachments") or {}
                for key in att:
                    att[key].update(comp_att.get(key) or {})
            except Exception as e:  # noqa: BLE001 - one competition failing must not kill the board
                logger.warning(f"[fanduel] competition-page {cid} failed: {e}")
        return {"attachments": att}
    finally:
        if own:
            await client.aclose()


class FanDuelScraper(BaseScraper):
    BOOK_NAME = "fanduel"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None) -> None:
        self.raw_store = raw_store
        self.run_id = run_id

    async def scrape(self, sport: str, market: str | None = None, **kwargs: Any) -> list[GameLine]:
        if os.environ.get("BOOK_FANDUEL_ENABLED", "1") == "0":
            logger.info("[fanduel] disabled via BOOK_FANDUEL_ENABLED=0")
            return []
        if sport not in PAGE_IDS:
            raise ValueError(f"unknown sport {sport!r}")
        payload = await fetch_board(sport)
        scraped_at = datetime.now(timezone.utc)
        if self.raw_store is not None:
            self.raw_store.put(f"{sport}_fanduel", payload, f"{API_BASE}?customPageId={PAGE_IDS[sport]}")
        lines = parser.parse(payload, sport, scraped_at=scraped_at, run_id=self.run_id, market=market)
        n_games = len({ln.game_id for ln in lines})
        logger.info(f"[fanduel] {sport}: {len(lines)} lines across {n_games} games")
        return lines


__all__ = ["API_BASE", "COMPETITION_API", "PAGE_IDS", "FanDuelScraper", "fetch_board"]
