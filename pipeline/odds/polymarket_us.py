"""Read-only public US exchange API; no account or trading endpoints."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import polymarket_us as parser

BASE_URL = "https://gateway.polymarket.us"
PAGE_SIZE = 20
MAX_PAGES = 50


class PolymarketUSScraper(BaseScraper):
    BOOK_NAME = "polymarket_us"

    def __init__(self, headless: bool = True, timeout: float = 30.0):
        self.timeout = timeout

    async def scrape(self, sport, market=None, capture=None, run_id=None, **kwargs):
        if sport not in ("nfl", "cfb"):
            raise ValueError(f"unknown sport {sport}")
        if os.getenv("BOOK_POLYMARKET_US_ENABLED", "1") == "0":
            return []
        events, seen = [], set()
        url = f"{BASE_URL}/v2/leagues/{sport}/events"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for page in range(MAX_PAGES):
                response = await client.get(url, params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE})
                response.raise_for_status()
                if capture:
                    capture(f"polymarket_us_{sport}_{page}", response.text, str(response.url))
                payload = response.json()
                batch = payload.get("events")
                if not isinstance(batch, list):
                    raise ValueError("Polymarket US response missing events")
                ids = {e.get("id") for e in batch}
                if batch and (None in ids or len(ids) != len(batch) or ids & seen):
                    raise ValueError("Polymarket US pagination repeated or omitted event IDs")
                seen.update(ids)
                events.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
            else:
                raise ValueError("Polymarket US pagination exceeded safety limit")
        lines = parser.parse({"events": events}, sport, scraped_at=datetime.now(timezone.utc), run_id=run_id)
        return [ln for ln in lines if market is None or ln.market == market]
