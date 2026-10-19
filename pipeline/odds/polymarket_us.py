"""Read-only public US exchange API; no account or trading endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from pipeline.odds.base import BaseScraper, get_with_retry
from pipeline.odds.parsers import polymarket_us as parser

logger = logging.getLogger(__name__)

BASE_URL = "https://gateway.polymarket.us"
PAGE_SIZE = 20
MAX_PAGES = 50
# The league list embeds the winner only. Spreads and totals still arrive on
# the event document, so a list without them is not an empty book.
LINE_MARKET_TYPES = {"football_team_full_game_spread", "football_team_full_game_total"}
_EVENT_SLUG = re.compile(r"^[a-z0-9-]{1,200}$")
DETAIL_CONCURRENCY = 8


def _needs_event_document(event: dict) -> bool:
    """True when this list row can still hide full-game spreads and totals."""
    if not isinstance(event, dict):
        return False
    slug = event.get("slug")
    if not isinstance(slug, str) or _EVENT_SLUG.fullmatch(slug) is None:
        return False
    markets = event.get("markets")
    if isinstance(markets, list):
        types = {m.get("sportsMarketType") for m in markets if isinstance(m, dict)}
        if types & LINE_MARKET_TYPES:
            return False
    counts = event.get("marketCounts") if isinstance(event.get("marketCounts"), dict) else {}
    return counts.get("numSpreadsAndTotalsMarkets") != 0


class PolymarketUSScraper(BaseScraper):
    BOOK_NAME = "polymarket_us"

    def __init__(self, headless: bool = True, timeout: float = 30.0):
        self.timeout = timeout
        self.fetch_errors: dict[str, str] = {}

    async def _attach_line_markets(self, client: httpx.AsyncClient, sport: str, events: list[dict], capture) -> None:
        pending = [event for event in events if _needs_event_document(event)]
        if not pending:
            return
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        failures = 0

        async def load(event: dict) -> None:
            nonlocal failures
            slug = event["slug"]
            url = f"{BASE_URL}/v1/events/slug/{slug}"
            try:
                async with sem:
                    response = await get_with_retry(client, url, label="polymarket_us")
            except Exception as exc:  # noqa: BLE001 - one game must not drop the slate
                failures += 1
                logger.warning("[polymarket_us] %s event %s document failed: %s", sport, slug, type(exc).__name__)
                return
            if capture:
                capture(f"polymarket_us_{sport}_event_{slug}", response.text, str(response.url))
            try:
                body = response.json()
            except ValueError:
                failures += 1
                logger.warning("[polymarket_us] %s event %s document was not JSON", sport, slug)
                return
            detail = body.get("event") if isinstance(body, dict) else None
            markets = detail.get("markets") if isinstance(detail, dict) else None
            if not isinstance(detail, dict) or detail.get("slug") != slug or not isinstance(markets, list):
                failures += 1
                logger.warning("[polymarket_us] %s event %s document did not match the listed game", sport, slug)
                return
            event["markets"] = markets

        await asyncio.gather(*(load(event) for event in pending))
        logger.info("[polymarket_us] %s: loaded %d event documents (%d failed)", sport, len(pending), failures)
        if failures:
            self.fetch_errors["event_documents"] = (
                f"{failures} of {len(pending)} event documents failed; spreads and totals may be missing")

    async def scrape(self, sport, market=None, capture=None, run_id=None, **kwargs):
        if sport not in ("nfl", "cfb"):
            raise ValueError(f"unknown sport {sport}")
        if os.getenv("BOOK_POLYMARKET_US_ENABLED", "1") == "0":
            return []
        self.fetch_errors = {}
        events = {}
        url = f"{BASE_URL}/v2/leagues/{sport}/events"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for page in range(MAX_PAGES):
                response = await get_with_retry(
                    client, url, label="polymarket_us", params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE})
                if capture:
                    capture(f"polymarket_us_{sport}_{page}", response.text, str(response.url))
                payload = response.json()
                batch = payload.get("events")
                if not isinstance(batch, list):
                    raise ValueError("Polymarket US response missing events")
                ids = {e.get("id") for e in batch}
                if batch and (None in ids or "" in ids):
                    raise ValueError("Polymarket US pagination omitted event IDs")
                new_ids = ids - events.keys()
                # Live ordering can move games across page boundaries. Keep the
                # latest copy without rejecting the rest of a valid slate.
                events.update((e["id"], e) for e in batch)
                if len(batch) < PAGE_SIZE:
                    break
                if not new_ids:
                    raise ValueError("Polymarket US pagination repeated without progress")
            else:
                raise ValueError("Polymarket US pagination exceeded safety limit")
            await self._attach_line_markets(client, sport, list(events.values()), capture)
        lines = parser.parse({"events": list(events.values())}, sport, scraped_at=datetime.now(timezone.utc), run_id=run_id)
        return [ln for ln in lines if market is None or ln.market == market]
