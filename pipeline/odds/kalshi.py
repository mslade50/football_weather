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

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper, Capture, book_enabled, browser_headers, get_with_retry
from pipeline.odds.parsers import kalshi as kalshi_parser

logger = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": browser_headers()["User-Agent"],
}

PAGE_LIMIT = 200
MAX_PAGES = 50


class KalshiScraper(BaseScraper):
    BOOK_NAME = "kalshi"

    DEFAULT_TIMEOUT = 15.0

    def _initialize(self) -> None:
        self.event_observed_at: dict[str, str] = {}

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
            resp = await get_with_retry(client, f"{API_BASE}/events", label="kalshi", params=params, backoff=1.5)
            data = resp.json()
            events = data.get("events", [])
            received = datetime.now(timezone.utc).isoformat()
            self.event_observed_at.update((event["event_ticker"], received)
                                          for event in events if event.get("event_ticker"))
            all_events.extend(events)

            prev = cursor
            cursor = data.get("cursor")
            if not cursor or len(events) < PAGE_LIMIT or cursor == prev:
                break
        else:
            logger.warning(f"Kalshi event pagination hit {MAX_PAGES}-page cap (series={series_ticker})")
        return all_events

    async def fetch_raw(self, sport: str, market: str | None = None) -> dict[str, Any]:
        series = kalshi_parser.SERIES_BY_SPORT[sport]
        wanted = {m: s for m, s in series.items() if market is None or m == market}
        payload: dict[str, Any] = {}
        self.event_observed_at = {}
        async with httpx.AsyncClient(headers=HEADERS, timeout=self.timeout) as client:
            for m, tk in wanted.items():
                try:
                    response = await get_with_retry(client, f"{API_BASE}/series/{tk}", label="kalshi")
                    fee_metadata = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("[kalshi] %s %s current fee metadata unavailable: %s", sport, m, type(exc).__name__)
                    fee_metadata = {"error": type(exc).__name__}
                events = await self._fetch_all_events(client, tk)
                payload[tk] = {"events": events, "fee_metadata": fee_metadata,
                               "event_observed_at": {event["event_ticker"]: self.event_observed_at.get(event["event_ticker"])
                                                     for event in events if event.get("event_ticker")},
                               "received_at": datetime.now(timezone.utc).isoformat()}
                logger.info(f"[kalshi] {sport} {m}: {len(events)} events from {tk}")
        return payload

    async def scrape(
        self, sport: str, market: str | None = None, *,
        capture: Capture | None = None, run_id: str | None = None,
        include_alternates: bool = True, include_live: bool = False,
        **kwargs: Any,
    ) -> list[GameLine]:
        capture = self.capture if capture is None else capture
        run_id = self.run_id if run_id is None else run_id
        if not book_enabled("kalshi"):
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
        n_events = sum(len(v["events"]) if isinstance(v, dict) else len(v) for v in payload.values())
        n_games = len({ln.game_id for ln in lines})
        n_main = sum(1 for ln in lines if ln.is_main)
        logger.info(f"[kalshi] {sport}: {n_events} events, {n_games} games with prices, {len(lines)} lines ({n_main} main)")
        return lines


__all__ = ["KalshiScraper", "API_BASE", "HEADERS"]
