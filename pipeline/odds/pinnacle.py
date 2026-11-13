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
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper, Capture, book_enabled, browser_headers, get_with_retry, in_season
from pipeline.odds.parsers import pinnacle as parser

logger = logging.getLogger(__name__)

BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
SPORT_ID = 15  # Football

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.pinnacle.com",
    "Referer": "https://www.pinnacle.com/",
    "User-Agent": browser_headers()["User-Agent"],
}


def enabled() -> bool:
    return book_enabled("pinnacle")


async def fetch_feed(client: httpx.AsyncClient | None = None) -> dict[str, list[dict]]:
    """Return ``{"matchups": [...], "markets": [...]}`` for sport 15 (both leagues)."""
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=15.0)
    try:
        matchups_resp, markets_resp = await asyncio.gather(
            get_with_retry(client, f"{BASE_URL}/sports/{SPORT_ID}/matchups",
                           label="pinnacle", params={"withSpecials": "false"}),
            get_with_retry(
                client, f"{BASE_URL}/sports/{SPORT_ID}/markets/straight", label="pinnacle",
                params={"primaryOnly": "false", "withSpecials": "false"},
            ),
        )
    finally:
        if own:
            await client.aclose()
    matchups = matchups_resp.json()
    markets = markets_resp.json()
    if not isinstance(matchups, list) or not isinstance(markets, list):
        raise ValueError("Pinnacle guest feed returned an unexpected payload")
    return {"matchups": matchups, "markets": markets}


class PinnacleScraper(BaseScraper):
    BOOK_NAME = "pinnacle"

    def _initialize(self) -> None:
        self.fetch_errors: dict[str, str] = {}

    async def scrape(
        self, sport: str, market: str | None = None, *,
        capture: Capture | None = None, run_id: str | None = None,
        include_alternates: bool = True, include_live: bool = False,
        **kwargs: Any,
    ) -> list[GameLine]:
        if sport not in parser.LEAGUES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info("[pinnacle] disabled via BOOK_PINNACLE_ENABLED=0")
            return []
        payload = await fetch_feed()
        scraped_at = datetime.now(timezone.utc)
        self.fetch_errors.pop(sport, None)
        n_events = len(parser.parse_events(payload["matchups"], sport))
        if n_events == 0 and in_season(sport, scraped_at):
            # Valid list payload, zero events for this league in season: schema drift, not a quiet day.
            self.fetch_errors[sport] = f"schema drift? 0 {sport.upper()} events in {len(payload['matchups'])} total"
            logger.warning(f"[pinnacle] {self.fetch_errors[sport]}")
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
