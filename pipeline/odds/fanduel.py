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

Transport: httpx first; on a 403 (datacenter-IP bot block, e.g. GitHub Actions)
the same request is retried through curl_cffi with Chrome TLS impersonation
(``pipeline.odds.base.fetch_json_with_fallback``). ``BOOK_FANDUEL_TRANSPORT``
= ``auto`` (default) | ``httpx`` | ``curl``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import (
    TRANSPORT_CURL,
    TRANSPORT_HTTPX,
    BaseScraper,
    browser_headers,
    fetch_json_with_fallback,
    transport_mode,
)
from pipeline.odds.parsers import fanduel as parser

logger = logging.getLogger(__name__)

API_BASE = "https://sbapi.az.sportsbook.fanduel.com/api/content-managed-page"
COMPETITION_API = "https://sbapi.az.sportsbook.fanduel.com/api/competition-page"
API_KEY = "FhMFpcPWXMeyZxOx"
FOOTBALL_EVENT_TYPE_ID = 1
TIMEOUT_S = 20.0

PAGE_IDS: dict[str, str] = {"nfl": "nfl", "cfb": "ncaaf"}
SITE_ORIGIN = "https://sportsbook.fanduel.com"
SITE_PAGES: dict[str, str] = {
    "nfl": f"{SITE_ORIGIN}/navigation/nfl",
    "cfb": f"{SITE_ORIGIN}/navigation/ncaaf",
}

# httpx path (works from residential IPs as-is).
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


def curl_headers(sport: str) -> dict[str, str]:
    """Full Chrome XHR header set for the curl_cffi path (Origin/Referer = the sportsbook page)."""
    return browser_headers(origin=SITE_ORIGIN, referer=SITE_PAGES[sport], accept="application/json")


def _has_game_markets(payload: dict) -> bool:
    markets = (payload.get("attachments") or {}).get("markets") or {}
    return any(m.get("marketType") in parser.MARKET_TYPES for m in markets.values())


async def fetch_board(
    sport: str, client: httpx.AsyncClient | None = None, mode: str | None = None,
) -> tuple[dict, str]:
    """Return ``(raw FanDuel envelope, transport)`` for ``sport`` (custom page, competition fallback).

    ``mode`` defaults to ``BOOK_FANDUEL_TRANSPORT``; once curl_cffi has been needed for one
    request the remaining requests of this board go straight to curl.
    """
    page_id = PAGE_IDS[sport]
    mode = mode or transport_mode("fanduel")
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT_S)
    curl_hdrs = curl_headers(sport)
    transport = TRANSPORT_HTTPX

    async def get(url: str, params: dict[str, Any]) -> dict:
        nonlocal mode, transport
        res = await fetch_json_with_fallback(
            url, params=params, headers=HEADERS, curl_headers=curl_hdrs, timeout=TIMEOUT_S,
            label="fanduel", logger=logger, mode=mode, client=client,
        )
        if res.transport == TRANSPORT_CURL:
            mode = transport = TRANSPORT_CURL
        return res.payload

    try:
        payload: dict = await get(API_BASE, {"page": "CUSTOM", "customPageId": page_id, "_ak": API_KEY})
        if _has_game_markets(payload):
            return payload, transport
        logger.warning(f"[fanduel] custom page {page_id} has no game markets; trying competition pages")
        att: dict[str, dict] = {"competitions": {}, "events": {}, "markets": {}}
        for cid in parser.COMPETITIONS[sport]:
            try:
                comp = await get(COMPETITION_API, {
                    "eventTypeId": FOOTBALL_EVENT_TYPE_ID, "competitionId": cid, "_ak": API_KEY,
                })
                comp_att = comp.get("attachments") or {}
                for key in att:
                    att[key].update(comp_att.get(key) or {})
            except Exception as e:  # noqa: BLE001 - one competition failing must not kill the board
                logger.warning(f"[fanduel] competition-page {cid} failed: {e}")
        return {"attachments": att}, transport
    finally:
        if own:
            await client.aclose()


class FanDuelScraper(BaseScraper):
    BOOK_NAME = "fanduel"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None) -> None:
        self.raw_store = raw_store
        self.run_id = run_id
        self.last_transport: str | None = None

    async def scrape(self, sport: str, market: str | None = None, **kwargs: Any) -> list[GameLine]:
        if os.environ.get("BOOK_FANDUEL_ENABLED", "1") == "0":
            logger.info("[fanduel] disabled via BOOK_FANDUEL_ENABLED=0")
            return []
        if sport not in PAGE_IDS:
            raise ValueError(f"unknown sport {sport!r}")
        payload, self.last_transport = await fetch_board(sport)
        logger.info(f"[fanduel] {sport}: board fetched via {self.last_transport}")
        scraped_at = datetime.now(timezone.utc)
        if self.raw_store is not None:
            self.raw_store.put(f"{sport}_fanduel", payload, f"{API_BASE}?customPageId={PAGE_IDS[sport]}")
        lines = parser.parse(payload, sport, scraped_at=scraped_at, run_id=self.run_id, market=market)
        n_games = len({ln.game_id for ln in lines})
        logger.info(f"[fanduel] {sport}: {len(lines)} lines across {n_games} games")
        return lines


__all__ = ["API_BASE", "COMPETITION_API", "PAGE_IDS", "FanDuelScraper", "fetch_board"]
