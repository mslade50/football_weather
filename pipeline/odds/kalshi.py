"""Kalshi football scraper via the public trade API (no auth).

Transport copied from ``golf_scraping/scrapers/kalshi.py`` (``_fetch_all_events``
pagination against ``api.elections.kalshi.com/trade-api/v2``, same headers).
Football changes: series ``KXNFLGAME/KXNFLSPREAD/KXNFLTOTAL`` and
``KXNCAAFGAME/KXNCAAFSPREAD/KXNCAAFTOTAL`` (verified against ``/series`` on
2026-08-23), ``with_nested_markets=true`` so one call per series returns the
ladders. Parsing lives in ``pipeline/odds/parsers/kalshi.py``.

Depth-walked fill pricing (``kalshi_fill.py``) is intentionally not ported yet:
top-of-book + taker fee is what the board publishes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import kalshi as kalshi_parser

logger = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}

PAGE_LIMIT = 200
MAX_PAGES = 50

Capture = Callable[[str, Any, str | None], Any]


class KalshiScraper(BaseScraper):
    BOOK_NAME = "kalshi"

    def __init__(self, headless: bool = True, timeout: float = 15.0) -> None:
        # No browser needed.
        self.timeout = timeout

    async def _fetch_all_events(self, client: httpx.AsyncClient, series_ticker: str) -> list[dict]:
        """Fetch all open events (with nested markets) for a series, handling pagination."""
        all_events: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "limit": PAGE_LIMIT,
                "status": "open",
                "series_ticker": series_ticker,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            resp = None
            for attempt in range(3):
                resp = await client.get(f"{API_BASE}/events", params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            assert resp is not None
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])
            all_events.extend(events)

            prev = cursor
            cursor = data.get("cursor")
            if not cursor or len(events) < PAGE_LIMIT or cursor == prev:
                break
        else:
            logger.warning(f"Kalshi event pagination hit {MAX_PAGES}-page cap (series={series_ticker})")
        return all_events

    async def fetch_raw(self, sport: str, market: str | None = None) -> dict[str, list[dict]]:
        series = kalshi_parser.SERIES_BY_SPORT[sport]
        wanted = {m: s for m, s in series.items() if market is None or m == market}
        payload: dict[str, list[dict]] = {}
        async with httpx.AsyncClient(headers=HEADERS, timeout=self.timeout) as client:
            for m, tk in wanted.items():
                payload[tk] = await self._fetch_all_events(client, tk)
                logger.info(f"[kalshi] {sport} {m}: {len(payload[tk])} events from {tk}")
        return payload

    async def scrape(
        self,
        sport: str,
        market: str | None = None,
        capture: Capture | None = None,
        run_id: str | None = None,
        **kwargs: Any,
    ) -> list[GameLine]:
        if os.environ.get("BOOK_KALSHI_ENABLED", "1") == "0":
            logger.info("[kalshi] disabled via BOOK_KALSHI_ENABLED=0")
            return []
        if sport not in kalshi_parser.SERIES_BY_SPORT:
            raise ValueError(f"unknown sport {sport!r}")
        payload = await self.fetch_raw(sport, market)
        if capture is not None:
            capture(f"kalshi_{sport}", payload, f"{API_BASE}/events")
        scraped_at = datetime.now(timezone.utc)
        lines = kalshi_parser.parse(payload, sport, scraped_at=scraped_at, run_id=run_id)
        if market:
            lines = [ln for ln in lines if ln.market == market]
        n_events = sum(len(v) for v in payload.values())
        n_games = len({ln.game_id for ln in lines})
        n_main = sum(1 for ln in lines if ln.is_main)
        logger.info(f"[kalshi] {sport}: {n_events} events, {n_games} games with prices, {len(lines)} lines ({n_main} main)")
        return lines


__all__ = ["KalshiScraper", "API_BASE", "HEADERS"]
