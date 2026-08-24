"""Novig football scraper via the public Hasura GraphQL API (no auth).

Transport copied from ``golf_scraping/scrapers/novig.py`` (``_gql`` POST to
``api.novig.us/v1/graphql`` with Origin/Referer headers). Football changes:
``league _in [NFL | NCAAF]``, ``type Game``, markets ``MONEY/SPREAD/TOTAL``.
Parsing lives in ``pipeline/odds/parsers/novig.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import novig as novig_parser

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.novig.us/v1/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://novig.com",
    "Referer": "https://novig.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}

GAMES_QUERY = """query($leagues: [String!]) {
  event(
    where: {league: {_in: $leagues}, type: {_eq: "Game"}, status: {_in: ["OPEN_PREGAME", "OPEN_INGAME"]}}
    order_by: {scheduled_start: asc}
  ) {
    id description league status scheduled_start
    game {
      homeTeam { id name symbol __typename }
      awayTeam { id name symbol __typename }
      __typename
    }
    markets(where: {status: {_eq: "OPEN"}, type: {_in: ["MONEY", "SPREAD", "TOTAL"]}}) {
      id type strike is_consensus
      outcomes {
        id index type description available last
        competitor { id name symbol __typename }
        __typename
      }
      __typename
    }
    __typename
  }
}"""

Capture = Callable[[str, Any, str | None], Any]


class NovigScraper(BaseScraper):
    BOOK_NAME = "novig"

    def __init__(self, headless: bool = True, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def _gql(self, client: httpx.AsyncClient, query: str, variables: dict | None = None) -> dict:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = await client.post(GRAPHQL_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def fetch_raw(self, sport: str) -> dict:
        league = novig_parser.LEAGUE_BY_SPORT[sport]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data = await self._gql(client, GAMES_QUERY, {"leagues": [league]})
        if data.get("errors"):
            raise RuntimeError(f"Novig GraphQL errors: {data['errors']}")
        return data

    async def scrape(
        self,
        sport: str,
        market: str | None = None,
        capture: Capture | None = None,
        run_id: str | None = None,
        **kwargs: Any,
    ) -> list[GameLine]:
        if os.environ.get("BOOK_NOVIG_ENABLED", "1") == "0":
            logger.info("[novig] disabled via BOOK_NOVIG_ENABLED=0")
            return []
        data = await self.fetch_raw(sport)
        if capture is not None:
            capture(f"novig_{sport}", data, GRAPHQL_URL)
        scraped_at = datetime.now(timezone.utc)
        lines = novig_parser.parse(data, sport, scraped_at=scraped_at, run_id=run_id)
        if market:
            lines = [ln for ln in lines if ln.market == market]
        n_events = len(((data.get("data") or {}).get("event")) or [])
        n_games = len({ln.game_id for ln in lines})
        logger.info(f"[novig] {sport}: {n_events} events, {n_games} games with prices, {len(lines)} lines")
        return lines


__all__ = ["NovigScraper", "GRAPHQL_URL", "GAMES_QUERY", "HEADERS"]
