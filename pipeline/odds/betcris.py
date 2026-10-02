"""Betcris football scraper via its public sportsbook odds JSON.

Source: https://sportsbook.betcris.com/assets/odds/v1/league/{1,2}.json

The former lines.bookmaker.eu host became unreachable in September 2026.
The current public feed includes actual spread/total prices and feed health.
Legacy HTML fetching remains available for diagnostics; live scraping uses JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import betcris as parser
from pipeline.odds.parsers.betcris import PAGES, BetcrisGame

logger = logging.getLogger(__name__)

BASE_URL = "https://lines.bookmaker.eu"
PUBLIC_URL = "https://sportsbook.betcris.com/assets/odds/v1/league/{league}.json"
FOOTBALL_PATH = "/en/sports/football/{slug}/"
FETCH_TIMEOUT_S = 60.0
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


def enabled() -> bool:
    return os.environ.get("BOOK_BETCRIS_ENABLED", "1").strip() not in ("0", "false", "no")


class BetcrisScraper(BaseScraper):
    BOOK_NAME = "betcris"

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None,
                 season: int | None = None) -> None:
        # No browser needed — `headless` accepted for interface parity.
        self.raw_store = raw_store
        self.run_id = run_id
        self.season = season
        self.last_games: list[BetcrisGame] = []
        self.fetch_errors: dict[str, str] = {}

    async def fetch_pages(self, sport: str) -> dict[str, str]:
        """{page_slug: html} for every viewer page that feeds ``sport``."""
        pages: dict[str, str] = {}
        self.fetch_errors.clear()
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers=HEADERS,
            follow_redirects=True,
            # The college-football page is large; GitHub runners saw read timeouts at 20 s.
            timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=15.0),
        ) as client:
            for slug in PAGES[sport]:
                path = FOOTBALL_PATH.format(slug=slug)
                html = await self._get_with_retry(client, slug, path)
                if html is None:
                    continue
                if self.raw_store is not None:
                    self.raw_store.put(f"{self.BOOK_NAME}_{slug}", html, url=f"{BASE_URL}{path}", ext="html")
                if "oddsTable" not in html:
                    logger.info(f"[{self.BOOK_NAME}] {slug}: no oddsTable on page, skipping")
                    continue
                pages[slug] = html
        return pages

    async def _get_with_retry(self, client: httpx.AsyncClient, slug: str, path: str) -> str | None:
        """Per-page retry: a single timed-out page must not blank the whole sport."""
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                resp = await client.get(path)
                resp.raise_for_status()
                self.fetch_errors.pop(slug, None)
                return resp.text
            except Exception as e:  # noqa: BLE001
                # str(httpx.ReadTimeout) is empty — always name the exception type.
                logger.warning(f"[{self.BOOK_NAME}] {slug}: fetch attempt {attempt}/{FETCH_ATTEMPTS} failed: "
                               f"{type(e).__name__}: {e}")
                self.fetch_errors[slug] = f"{type(e).__name__}: {e}".rstrip(": ")
                if attempt < FETCH_ATTEMPTS:
                    await asyncio.sleep(FETCH_BACKOFF_S * attempt)
        return None

    async def scrape(self, sport: str, market: str | None = None, **kwargs: Any) -> list[GameLine]:
        if sport not in PAGES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info(f"[{self.BOOK_NAME}] disabled via BOOK_BETCRIS_ENABLED")
            return []
        self.fetch_errors.clear()
        self.last_games = []
        slug = PAGES[sport][0]
        url = PUBLIC_URL.format(league=parser.PUBLIC_LEAGUES[sport])
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                     timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=15.0)) as client:
            raw = await self._get_with_retry(client, slug, url)
        if raw is None:
            return []
        if self.raw_store is not None:
            self.raw_store.put(f"betcris_public_{sport}", raw, url=url, ext="json")
        try:
            lines = parser.parse_public(json.loads(raw), sport, now=datetime.now(timezone.utc),
                                        market=market, run_id=self.run_id)
        except ValueError as exc:
            self.fetch_errors[slug] = str(exc)
            return []
        logger.info(f"[betcris] {sport}: {len(lines)} open pregame lines from public JSON")
        return lines
