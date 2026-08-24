"""BetOnline football scraper via the internal offering JSON API.

Transport copied from golf_scraping/scrapers/betonline.py (NO login): Playwright
chromium + stealth loads betonline.ag once to pass Cloudflare, then
``page.evaluate(fetch)`` calls ``api-offering.betonline.ag`` from the browser
context (which already carries the CF cookies). Pure JSON, no HTML parsing.

Endpoint (recon 2026-08-23):
  POST /api/offering/Sports/offering-by-league
  Body: {"Sport":"football","League":"nfl"|"nfl-preseason"|"ncaa","filterTime":0}
  Headers: gsetting=bolsassite, utc-offset=240

Parsing lives in ``pipeline.odds.parsers.betonline``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import betonline as parser
from pipeline.odds.parsers.betonline import LEAGUES, BetOnlineGame

logger = logging.getLogger(__name__)

API_BASE = "https://api-offering.betonline.ag/api/offering/Sports"
SITE_URL = "https://www.betonline.ag"

# Headers the browser sends on API calls
API_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "gsetting": "bolsassite",
    "utc-offset": str(parser.UTC_OFFSET_MINUTES),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

_FETCH_JS = """
async (args) => {
    const [url, payload, headers] = args;
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 20000);  // bound a hung CF fetch
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: headers,
            body: payload,
            signal: ctrl.signal,
        });
        return await resp.json();
    } finally {
        clearTimeout(t);
    }
}
"""


def enabled() -> bool:
    return os.environ.get("BOOK_BETONLINE_ENABLED", "1").strip() not in ("0", "false", "no")


async def _launch(pw: Any, headless: bool) -> Any:
    """Bundled Chromium first; fall back to an installed Google Chrome when
    ``playwright install chromium`` has not been run (BETONLINE_CHANNEL overrides)."""
    channel = os.environ.get("BETONLINE_CHANNEL", "").strip() or None
    if channel:
        return await pw.chromium.launch(headless=headless, channel=channel)
    try:
        return await pw.chromium.launch(headless=headless)
    except Exception as e:  # playwright Error: Executable doesn't exist
        if "Executable doesn't exist" not in str(e):
            raise
        logger.warning(f"[betonline] bundled chromium missing, using channel=chrome ({str(e).splitlines()[0]})")
        return await pw.chromium.launch(headless=headless, channel="chrome")


class BetOnlineScraper(BaseScraper):
    BOOK_NAME = "betonline"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None) -> None:
        self.headless = headless
        self.raw_store = raw_store
        self.run_id = run_id
        self.last_games: list[BetOnlineGame] = []

    async def _fetch_api(self, page: Any, endpoint: str, payload: dict) -> dict | None:
        """Call a BetOnline API endpoint from the browser context (has CF cookies)."""
        url = f"{API_BASE}/{endpoint}"
        data = await page.evaluate(_FETCH_JS, [url, json.dumps(payload), API_HEADERS])
        if not data or data.get("IsError"):
            logger.warning(f"[{self.BOOK_NAME}] API error for {endpoint}: {data}")
            return None
        return data

    async def fetch_leagues(self, page: Any, sport: str) -> dict[str, dict]:
        """{league_slug: offering-by-league payload} for every league feeding ``sport``."""
        out: dict[str, dict] = {}
        for slug in LEAGUES[sport]:
            body = {"Sport": "football", "League": slug, "filterTime": 0}
            try:
                data = await self._fetch_api(page, "offering-by-league", body)
            except Exception as e:
                logger.warning(f"[{self.BOOK_NAME}] {slug}: fetch failed: {e}")
                continue
            if data is None:
                continue
            if self.raw_store is not None:
                self.raw_store.put(f"{self.BOOK_NAME}_{slug}", data,
                                   url=f"{API_BASE}/offering-by-league?League={slug}", ext="json")
            if not data.get("GameOffering"):
                logger.info(f"[{self.BOOK_NAME}] {slug}: no GameOffering (league dark), skipping")
                continue
            out[slug] = data
        return out

    async def scrape(self, sport: str, market: str | None = None, **kwargs: Any) -> list[GameLine]:
        if sport not in LEAGUES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info(f"[{self.BOOK_NAME}] disabled via BOOK_BETONLINE_ENABLED")
            return []

        async with async_playwright() as p:
            browser = await _launch(p, self.headless)
            context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080})
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
            try:
                logger.info(f"[{self.BOOK_NAME}] navigating to BetOnline (Cloudflare pass)...")
                await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
                payloads = await self.fetch_leagues(page, sport)
            finally:
                await browser.close()

        scraped_at = datetime.now(timezone.utc)
        games: list[BetOnlineGame] = []
        for slug, data in payloads.items():
            lg_games = parser.parse_games(data, sport, league=slug)
            priced = sum(1 for g in lg_games if g.total is not None or g.away_spread is not None)
            logger.info(f"[{self.BOOK_NAME}] {slug}: {len(lg_games)} games, {priced} priced")
            games.extend(lg_games)
        self.last_games = parser.dedupe_games(games)
        lines: list[GameLine] = []
        for g in self.last_games:
            lines.extend(parser.game_lines(g, market=market, scraped_at=scraped_at, run_id=self.run_id))
        return lines


__all__ = ["API_BASE", "API_HEADERS", "BetOnlineScraper", "enabled"]
