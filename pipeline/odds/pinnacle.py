"""Pinnacle football lines via the credential-free guest feed.

Transport copied from ``golf_scraping/scrapers/pinnacle.py``: two bulk,
unauthenticated GETs to ``guest.api.arcadia.pinnacle.com`` (no login, no
Cloudflare challenge) fetched concurrently, joined by the pure parser in
``pipeline/odds/parsers/pinnacle.py``.  Sport id 15 = Football; leagues
``NFL`` / ``NFL Pre Season`` -> ``nfl``, ``NCAA`` -> ``cfb``.

Pinnacle is the consensus reference book (BOOK_WEIGHTS pinnacle=3, ARCH §7.3).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import pinnacle as parser

logger = logging.getLogger(__name__)

BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
SPORT_ID = 15  # Football

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.pinnacle.com",
    "Referer": "https://www.pinnacle.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


def enabled() -> bool:
    return os.environ.get("BOOK_PINNACLE_ENABLED", "1").strip() != "0"


async def fetch_feed(client: httpx.AsyncClient | None = None) -> dict[str, list[dict]]:
    """Return ``{"matchups": [...], "markets": [...]}`` for sport 15 (both leagues)."""
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=15.0)
    try:
        matchups_resp, markets_resp = await asyncio.gather(
            client.get(f"{BASE_URL}/sports/{SPORT_ID}/matchups", params={"withSpecials": "false"}),
            client.get(
                f"{BASE_URL}/sports/{SPORT_ID}/markets/straight",
                params={"primaryOnly": "false", "withSpecials": "false"},
            ),
        )
    finally:
        if own:
            await client.aclose()
    matchups_resp.raise_for_status()
    markets_resp.raise_for_status()
    matchups = matchups_resp.json()
    markets = markets_resp.json()
    if not isinstance(matchups, list) or not isinstance(markets, list):
        raise ValueError("Pinnacle guest feed returned an unexpected payload")
    return {"matchups": matchups, "markets": markets}


class PinnacleScraper(BaseScraper):
    BOOK_NAME = "pinnacle"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None):
        # No browser needed - pure API calls. ``headless`` accepted for main.py parity.
        self.raw_store = raw_store
        self.run_id = run_id

    async def scrape(
        self,
        sport: str,
        market: str | None = None,
        include_alternates: bool = True,
        **kwargs: Any,
    ) -> list[GameLine]:
        if sport not in parser.LEAGUES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info("[pinnacle] disabled via BOOK_PINNACLE_ENABLED=0")
            return []
        payload = await fetch_feed()
        scraped_at = datetime.now(timezone.utc)
        if self.raw_store is not None:
            try:
                self.raw_store.put(f"pinnacle_{sport}", payload, url=f"{BASE_URL}/sports/{SPORT_ID}")
            except Exception as e:  # raw capture is best-effort
                logger.warning(f"[pinnacle] raw capture failed: {e}")
        lines = parser.parse(
            payload["matchups"],
            payload["markets"],
            sport,
            market=market,
            include_alternates=include_alternates,
            scraped_at=scraped_at,
        )
        if self.run_id:
            from dataclasses import replace

            lines = [replace(ln, run_id=self.run_id) for ln in lines]
        n_games = len({ln.game_id for ln in lines})
        logger.info(
            "Pinnacle bulk feed: %d matchups, %d markets -> %s: %d games, %d lines",
            len(payload["matchups"]), len(payload["markets"]), sport, n_games, len(lines),
        )
        return lines
