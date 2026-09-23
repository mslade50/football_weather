"""Novig football scraper via the public Hasura GraphQL API (no auth).

Novig began rejecting custom queries on 2026-09-22. Use the public web app's
allowlisted HotMarkets_Query, preserving its complete fragment order. Fetch
main and alternate markets separately because the response omits is_consensus.
Parsing lives in ``pipeline/odds/parsers/novig.py``; both raw responses are kept.

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
from pathlib import Path
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

MARKETS_QUERY = (Path(__file__).parent / "queries" / "novig_markets.graphql").read_text(encoding="utf-8")
# Current CFB alternate volume is ~5,500 markets. Fail closed if this ceiling
# is reached rather than silently publishing a truncated board.
MARKET_LIMIT = 10_000

Capture = Callable[[str, Any, str | None], Any]


class NovigScraper(BaseScraper):
    BOOK_NAME = "novig"

    def __init__(self, headless: bool = True, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.last_transport: str | None = None

    async def _gql(self, client: httpx.AsyncClient | None, query: str, variables: dict | None = None) -> dict:
        payload: dict[str, Any] = {"operationName": "HotMarkets_Query", "query": query}
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
        responses = {}
        for group, is_main in (("main", True), ("alternate", False)):
            variables = {
                "where_market": {
                    "event": {
                        "league": {"_eq": league},
                        "type": {"_eq": "Game"},
                        "status": {"_in": ["OPEN_PREGAME", "OPEN_INGAME"]},
                    },
                    "status": {"_eq": "OPEN"},
                    "type": {"_in": ["MONEY", "SPREAD", "TOTAL"]},
                    "is_consensus": {"_eq": is_main},
                },
                "limit": MARKET_LIMIT,
            }
            data = await self._gql(None, MARKETS_QUERY, variables)
            if data.get("errors"):
                raise RuntimeError(f"Novig GraphQL errors ({group}): {data['errors']}")
            markets = (data.get("data") or {}).get("market")
            if not isinstance(markets, list):
                raise RuntimeError(f"Novig {group} response is missing data.market")
            if len(markets) >= MARKET_LIMIT:
                raise RuntimeError(f"Novig {group} reached {MARKET_LIMIT} markets; refusing truncated data")
            responses[group] = data
        return responses

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
        n_markets = sum(len(response["data"]["market"]) for response in data.values())
        n_games = len({ln.game_id for ln in lines})
        logger.info(
            f"[novig] {sport}: {n_markets} markets, {n_games} games with prices, {len(lines)} lines "
            f"(via {self.last_transport})"
        )
        return lines


__all__ = ["NovigScraper", "GRAPHQL_URL", "MARKETS_QUERY", "HEADERS", "CURL_HEADERS"]
