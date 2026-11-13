"""Betcris football scraper via its public sportsbook odds JSON.

Source: https://sportsbook.betcris.com/assets/odds/v1/league/{1,2}.json

The former lines.bookmaker.eu host became unreachable in September 2026.
The current public feed includes actual spread/total prices and feed health.
"""

from __future__ import annotations

import asyncio as asyncio  # retained transport-test patch point; sleep lives in base
import json
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper, Capture, book_enabled, browser_headers, get_with_retry, retryable_status
from pipeline.odds.parsers import betcris as parser
from pipeline.odds.parsers.betcris import BetcrisGame

logger = logging.getLogger(__name__)

PUBLIC_URL = "https://sportsbook.betcris.com/assets/odds/v1/league/{league}.json"
FETCH_TIMEOUT_S = 60.0
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = 2.0

HEADERS = {
    "User-Agent": browser_headers()["User-Agent"],
}


def enabled() -> bool:
    return book_enabled("betcris")


class BetcrisScraper(BaseScraper):
    BOOK_NAME = "betcris"

    def _initialize(self) -> None:
        self.last_games: list[BetcrisGame] = []
        self.fetch_errors: dict[str, str] = {}

    async def _get_with_retry(self, client: httpx.AsyncClient, slug: str, path: str) -> str | None:
        """Retry transient transport failures for the public league JSON."""
        try:
            resp = await get_with_retry(
                client, path, label=self.BOOK_NAME, attempts=FETCH_ATTEMPTS, backoff=FETCH_BACKOFF_S,
                send=partial(client.get, path),
                retry_error=lambda exc: not isinstance(exc, httpx.HTTPStatusError)
                or retryable_status(exc.response.status_code),
            )
        except Exception as exc:  # noqa: BLE001 - a failed league remains a book health error
            self.fetch_errors[slug] = f"{type(exc).__name__}: {exc}".rstrip(": ")
            logger.warning("[betcris] %s: %s", slug, self.fetch_errors[slug])
            return None
        self.fetch_errors.pop(slug, None)
        return resp.text

    async def _revalidate_public(self, client: httpx.AsyncClient, slug: str, url: str) -> str | None:
        """One same-URL revalidation; never substitute catalog/download clocks."""
        try:
            response = await client.get(url, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.fetch_errors[slug] = f"revalidation failed: {type(exc).__name__}: {exc}".rstrip(": ")
            logger.warning("[betcris] %s: %s", slug, self.fetch_errors[slug])
            return None
        return response.text

    @staticmethod
    def _feed_age(raw: str) -> tuple[datetime, float, float | None] | None:
        """(observed, age_seconds vs wall clock, provider ttl) from the raw feed; None if unreadable."""
        try:
            payload = json.loads(raw)
            observed = parser._public_timestamp(payload.get("feed_fetched_at"))
            if observed is None:
                return None
            ttl = parser._public_number(payload.get("stale_after_seconds"))
            return observed, (datetime.now(timezone.utc) - observed).total_seconds(), ttl
        except Exception:  # noqa: BLE001 - diagnostics must never break the scrape
            return None

    def _log_feed_age(self, sport: str, raw: str) -> None:
        info = self._feed_age(raw)
        if info is None:
            logger.info("[betcris] %s: feed observation time unreadable", sport)
            return
        observed, age, ttl = info
        logger.info("[betcris] %s: feed observed %s, age %.0fs vs wall clock (ttl %s)",
                    sport, observed.isoformat(), age, f"{ttl:g}s" if ttl else "n/a")

    def _upstream_stale(self, raw: str, exc: BaseException) -> str:
        """Health marker. A stale public file is a warning, not a scrape failure."""
        info = self._feed_age(raw)
        if info is None:
            return f"upstream stale: {exc}"
        return f"upstream stale, age {info[1]:.0f}s: {exc}"

    async def scrape(
        self, sport: str, market: str | None = None, *,
        capture: Capture | None = None, run_id: str | None = None,
        include_alternates: bool = True, include_live: bool = False,
        **kwargs: Any,
    ) -> list[GameLine]:
        if sport not in parser.PUBLIC_LEAGUES:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info(f"[{self.BOOK_NAME}] disabled via BOOK_BETCRIS_ENABLED")
            return []
        self.fetch_errors.clear()
        self.last_games = []
        slug = "nfl" if sport == "nfl" else "college-football"
        url = PUBLIC_URL.format(league=parser.PUBLIC_LEAGUES[sport])
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                     timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=15.0)) as client:
            raw = await self._get_with_retry(client, slug, url)
            if raw is None:
                return []
            if self.raw_store is not None:
                self.raw_store.put(f"betcris_public_{sport}", raw, url=url, ext="json")
            self._log_feed_age(sport, raw)
            try:
                lines = parser.parse_public(json.loads(raw), sport, now=datetime.now(timezone.utc),
                                            market=market, run_id=self.run_id)
            except parser.StaleObservationError as original:
                if sport != "cfb":
                    self.fetch_errors[slug] = self._upstream_stale(raw, original)
                    return []
                logger.warning("[betcris] cfb: %s; revalidating once", original)
                refreshed = await self._revalidate_public(client, slug, url)
                if refreshed is None:
                    self.fetch_errors[slug] = (
                        f"{self._upstream_stale(raw, original)}; {self.fetch_errors[slug]}"
                    )
                    return []
                if self.raw_store is not None:
                    self.raw_store.put("betcris_public_cfb_revalidation", refreshed, url=url, ext="json")
                try:
                    lines = parser.parse_public(json.loads(refreshed), sport, now=datetime.now(timezone.utc),
                                                market=market, run_id=self.run_id)
                except ValueError as exc:
                    self.fetch_errors[slug] = self._upstream_stale(
                        refreshed, ValueError(f"revalidation did not recover fresh CFB observations: {exc}"),
                    )
                    return []
                self.fetch_errors.pop(slug, None)
                logger.info("[betcris] cfb: same-URL revalidation recovered a fresh league observation")
            except ValueError as exc:
                self.fetch_errors[slug] = str(exc)
                return []
        logger.info(f"[betcris] {sport}: {len(lines)} open pregame lines from public JSON")
        return lines
