"""Betcris/Bookmaker football scraper via the public bookmaker.eu LINES viewer.

Source: https://lines.bookmaker.eu/en/sports/football/{nfl,nfl-preseason,college-football}/

Transport copied from golf_scraping/scrapers/betcris.py: the viewer is PUBLIC —
no login, no Cloudflare, no browser — server-rendered HTML with odds baked in,
pure httpx + BS4. Parsing lives in ``pipeline.odds.parsers.betcris``.
"""

from __future__ import annotations

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
FOOTBALL_PATH = "/en/sports/football/{slug}/"

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

    async def fetch_pages(self, sport: str) -> dict[str, str]:
        """{page_slug: html} for every viewer page that feeds ``sport``."""
        pages: dict[str, str] = {}
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers=HEADERS,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            for slug in PAGES[sport]:
                path = FOOTBALL_PATH.format(slug=slug)
                try:
                    resp = await client.get(path)
                    resp.raise_for_status()
                except Exception as e:
                    logger.warning(f"[{self.BOOK_NAME}] {slug}: fetch failed: {e}")
                    continue
                html = resp.text
                if self.raw_store is not None:
                    self.raw_store.put(f"{self.BOOK_NAME}_{slug}", html, url=f"{BASE_URL}{path}", ext="html")
                if "oddsTable" not in html:
                    logger.info(f"[{self.BOOK_NAME}] {slug}: no oddsTable on page, skipping")
                    continue
                pages[slug] = html
        return pages

    async def scrape(self, sport: str, market: str | None = None, **kwargs: Any) -> list[GameLine]:
        if sport not in PAGES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info(f"[{self.BOOK_NAME}] disabled via BOOK_BETCRIS_ENABLED")
            return []
        scraped_at = datetime.now(timezone.utc)
        games: list[BetcrisGame] = []
        for slug, html in (await self.fetch_pages(sport)).items():
            page_games = parser.parse_games(html, sport, page=slug, season=self.season)
            priced = sum(1 for g in page_games if g.total is not None or g.away_ml is not None)
            logger.info(f"[{self.BOOK_NAME}] {slug}: {len(page_games)} games, {priced} priced")
            games.extend(page_games)
        self.last_games = parser.dedupe_games(games)
        lines: list[GameLine] = []
        for g in self.last_games:
            lines.extend(parser.game_lines(g, market=market, scraped_at=scraped_at, run_id=self.run_id))
        return lines
