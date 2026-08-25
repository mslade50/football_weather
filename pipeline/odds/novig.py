"""Novig football scraper via the public Hasura GraphQL API (no auth).

Transport copied from ``golf_scraping/scrapers/novig.py`` (``_gql`` POST to
``api.novig.us/v1/graphql`` with Origin/Referer headers). Football changes:
``league _in [NFL | NCAAF]``, ``type Game``, markets ``MONEY/SPREAD/TOTAL``.
Parsing lives in ``pipeline/odds/parsers/novig.py``.

Transport: httpx first; on a 403 (datacenter-IP bot block, e.g. GitHub Actions)
the POST is retried through curl_cffi with Chrome TLS impersonation
(``pipeline.odds.base.fetch_json_with_fallback``). ``BOOK_NOVIG_TRANSPORT``
= ``auto`` (default) | ``httpx`` | ``curl``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper, browser_headers, fetch_json_with_fallback
from pipeline.odds.parsers import novig as novig_parser

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.novig.us/v1/graphql"
SITE_ORIGIN = "https://novig.com"

# httpx path (works from residential IPs as-is).
HEADERS = {
    "Content-Type": "application/json",
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}

# curl_cffi path: full Chrome XHR header set (api.novig.us <- novig.com is cross-site).
CURL_HEADERS = browser_headers(
    origin=SITE_ORIGIN, referer=f"{SITE_ORIGIN}/", accept="application/json", fetch_site="cross-site",
    extra={"Content-Type": "application/json"},
)

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
        self.last_transport: str | None = None

    async def _gql(self, client: httpx.AsyncClient | None, query: str, variables: dict | None = None) -> dict:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        res = await fetch_json_with_fallback(
            GRAPHQL_URL, method="POST", headers=HEADERS, curl_headers=CURL_HEADERS, json_body=payload,
            timeout=self.timeout, label="novig", logger=logger, client=client,
        )
        self.last_transport = res.transport
        return res.payload

    async def fetch_raw(self, sport: str) -> dict:
        league = novig_parser.LEAGUE_BY_SPORT[sport]
        data = await self._gql(None, GAMES_QUERY, {"leagues": [league]})  # helper owns the httpx client
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
        logger.info(
            f"[novig] {sport}: {n_events} events, {n_games} games with prices, {len(lines)} lines "
            f"(via {self.last_transport})"
        )
        return lines


__all__ = ["NovigScraper", "GRAPHQL_URL", "GAMES_QUERY", "HEADERS", "CURL_HEADERS"]
