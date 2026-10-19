"""Novig football lines from the official REST catalog.

The public GraphQL host (``api.novig.us/v1/graphql``) answers GitHub Actions
with HTTP 403. Novig's odds-screen docs replace that feed with the
unauthenticated catalog at ``api.novig.com/v3/public/catalog``: list the
league's events and markets, then read each market's order book. The price
for an outcome is one minus the best bid on the other outcome. The catalog
throttles each IP, so book reads are paced and a spread or total ladder is
read from the middle outward until the two-sided price nearest 50 cents is
in hand. Far alternates are left unread.

When that catalog returns 401 or 403 and ``NOVIG_CLIENT_ID`` plus
``NOVIG_CLIENT_SECRET`` are set, the scraper uses the NBX client-credentials
API instead (``POST /nbx/v1/auth/emm-token``, then open markets and
per-event books). Those two names are the GitHub secrets the pipeline passes
through. They are not required while the public catalog answers. This module
does not impersonate a browser.

Parsing lives in ``pipeline/odds/parsers/novig.py``. The raw capture is the
verbatim catalog pages (or the verbatim NBX payloads).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper, get_with_retry, request_with_retry
from pipeline.odds.parsers import novig as novig_parser

logger = logging.getLogger(__name__)

CATALOG_ROOT = "https://api.novig.com/v3/public/catalog"
TOKEN_URL = "https://api.novig.com/nbx/v1/auth/emm-token"
OPEN_MARKETS_URL = "https://api.novig.com/nbx/v2/emm/markets/open"
EVENT_MARKETS_URL = "https://api.novig.com/nbx/v2/emm/events/getMarketsByEvent"
PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "football-weather/novig-catalog",
}
PAGE_LIMIT = 5000
MAX_PAGES = 8
# The public catalog answers 429 after a short burst (about two requests a
# second sustained). This gap is between book-read starts. 275s is inside
# the pipeline's 300s per-book timeout, so a slow league returns the lines
# already read instead of being cancelled with nothing.
BOOK_MIN_INTERVAL_S = 0.5
BOOK_DEADLINE_S = 275.0
BOOK_429_ATTEMPTS = 8
# Stop walking a ladder once this many strikes have been read. The listed
# strikes are centered on the game, so the two-sided 50 cent pair is near
# the middle; the cap is what keeps a noisy book from reading every wing.
MAX_LADDER_PROBES = 9
BALANCED_BOOK = 0.12
MARKET_TYPES = ("MONEY", "SPREAD", "TOTAL")

_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_LOADED = False

Capture = Callable[[str, Any, str | None], Any]


class NovigCatalogRejected(RuntimeError):
    """The official catalog refused the request and no NBX credentials could take over."""


def _load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    from utils.env import load_repo_dotenv

    load_repo_dotenv(_ROOT / ".env")


def nbx_credentials() -> tuple[str, str] | None:
    """``NOVIG_CLIENT_ID`` and ``NOVIG_CLIENT_SECRET``, or None when either is unset."""
    _load_dotenv()
    client_id = os.environ.get("NOVIG_CLIENT_ID", "").strip()
    secret = os.environ.get("NOVIG_CLIENT_SECRET", "").strip()
    if client_id and secret:
        return client_id, secret
    return None


def quote_balance(market: dict[str, Any], book: dict[str, Any] | None) -> float | None:
    """How far a two-sided book is from a 50 cent pair. Lower is closer.

    A one-sided book scores above 1 so it never beats a two-sided pair.
    ``None`` means the book has no taker price.
    """
    market_type = market.get("marketType")
    outcomes = [o for o in market.get("outcomes") or [] if isinstance(o, dict) and o.get("outcomeId")]
    if market_type not in ("SPREAD", "TOTAL") or len(outcomes) != 2:
        return None
    strike = novig_parser._num(market.get("strike"))
    prices = novig_parser.taker_prices([str(o["outcomeId"]) for o in outcomes], book)
    sided: dict[str, float] = {}
    for outcome in outcomes:
        side = novig_parser._public_side(market_type, outcome, strike, {}, "", "")
        price = prices.get(str(outcome["outcomeId"]))
        if side and price is not None:
            sided[side] = price
    needed = ("home", "away") if market_type == "SPREAD" else ("over", "under")
    have = [sided[side] for side in needed if side in sided]
    if len(have) == 2:
        return max(abs(price - 0.5) for price in have)
    if len(have) == 1:
        return 1.0 + abs(have[0] - 0.5)
    return None


class NovigScraper(BaseScraper):
    BOOK_NAME = "novig"

    def __init__(self, headless: bool = True, timeout: float = 30.0) -> None:
        del headless  # API-only; retained for the common scraper constructor.
        self.timeout = timeout
        self.min_interval = BOOK_MIN_INTERVAL_S
        self.last_transport: str | None = None
        self.fetch_errors: dict[str, str] = {}
        self._book_lock = asyncio.Lock()
        self._next_book_at = 0.0
        self._book_deadline: float | None = None

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await get_with_retry(
            client, url, label="novig", headers=PUBLIC_HEADERS, params=params, timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Novig {url} returned a non-object response")
        return payload

    async def _pages(
        self, client: httpx.AsyncClient, url: str, params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            query = dict(params)
            if after:
                query["after"] = after
            page = await self._get_json(client, url, query)
            if not isinstance(page.get("items"), list):
                raise RuntimeError(f"Novig catalog page is missing items: {url}")
            pages.append(page)
            nxt = page.get("next")
            if not nxt:
                return pages
            after = str(nxt)
        raise RuntimeError(
            f"Novig catalog still had another page after {MAX_PAGES}; refusing truncated data"
        )

    async def _pace_book(self) -> None:
        """One book read at a time, with a gap, so the public throttle can refill."""
        if self.min_interval <= 0:
            return
        async with self._book_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = self._next_book_at - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = loop.time()
            self._next_book_at = now + self.min_interval

    async def _book_json(self, client: httpx.AsyncClient, market_id: str) -> dict[str, Any]:
        """One order book. 429 waits for ``Retry-After``; a missing book is empty."""
        url = f"{CATALOG_ROOT}/markets/{market_id}/book"
        last_status = 0
        for attempt in range(1, BOOK_429_ATTEMPTS + 1):
            await self._pace_book()
            response = await client.get(
                url, headers=PUBLIC_HEADERS, params={"depth": "1"}, timeout=self.timeout,
            )
            last_status = response.status_code
            if response.status_code == 429 and attempt < BOOK_429_ATTEMPTS:
                try:
                    delay = float(response.headers.get("Retry-After") or "1")
                except ValueError:
                    delay = 1.0
                delay = min(max(delay, 0.0), 8.0)
                logger.warning("[novig] book HTTP 429; waiting %ss (attempt %s)", delay, attempt)
                await asyncio.sleep(delay)
                continue
            if response.status_code == 404:
                return {"marketId": market_id, "orders": {}}
            if response.status_code == 429:
                logger.warning("[novig] book %s still HTTP 429; leaving it empty", market_id)
                return {"marketId": market_id, "orders": {}}
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Novig {url} returned a non-object response")
            return payload
        return {"marketId": market_id, "orders": {}, "status": last_status}

    def _past_book_deadline(self) -> bool:
        if self._book_deadline is None:
            return False
        return asyncio.get_running_loop().time() >= self._book_deadline

    async def _ladder_books(
        self, client: httpx.AsyncClient, markets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Walk from the middle strike to the local two-sided pair nearest 50 cents.

        Listed strikes sit around the game line, so the balanced book is near
        the middle. Each step reads that strike's neighbors and moves to the
        closer one. The far wings are not read.
        """
        ordered = [
            market for market in markets
            if market.get("marketId") and novig_parser._num(market.get("strike")) is not None
        ]
        ordered.sort(key=lambda market: novig_parser._num(market.get("strike")) or 0.0)
        books: dict[str, Any] = {}
        if not ordered:
            return books
        scores: dict[int, float | None] = {}

        async def score(index: int) -> None:
            if index in scores or self._past_book_deadline():
                return
            market = ordered[index]
            market_id = str(market["marketId"])
            book = await self._book_json(client, market_id)
            books[market_id] = book
            scores[index] = quote_balance(market, book)

        def best_of(indexes: list[int]) -> int | None:
            ranked = [index for index in indexes if scores.get(index) is not None]
            if not ranked:
                return None
            return min(ranked, key=lambda index: (scores[index], abs(index - start)))

        start = (len(ordered) - 1) // 2
        current = start
        await score(current)
        for _ in range(MAX_LADDER_PROBES):
            if self._past_book_deadline():
                break
            window = [index for index in (current - 1, current, current + 1) if 0 <= index < len(ordered)]
            for index in window:
                await score(index)
            best = best_of(window)
            if best is None:
                found: int | None = None
                for radius in range(2, 5):
                    for index in (start - radius, start + radius):
                        if 0 <= index < len(ordered):
                            await score(index)
                            if scores.get(index) is not None:
                                found = index
                                break
                    if found is not None or self._past_book_deadline():
                        break
                if found is None:
                    break
                current = found
                continue
            if best == current:
                break
            neighbors = [index for index in (best - 1, best + 1) if 0 <= index < len(ordered)]
            current = best
            if scores[best] is not None and scores[best] <= BALANCED_BOOK and all(
                index in scores for index in neighbors
            ):
                break
        return books

    async def _fetch_books(
        self, client: httpx.AsyncClient, markets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        books: dict[str, Any] = {}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for market in markets:
            market_type = str(market.get("marketType") or "")
            event_id = str(market.get("eventId") or "")
            if market_type == "MONEY" and market.get("marketId"):
                market_id = str(market["marketId"])
                books[market_id] = await self._book_json(client, market_id)
                continue
            if market_type in ("SPREAD", "TOTAL"):
                groups.setdefault((event_id, market_type), []).append(market)
        for group in groups.values():
            books.update(await self._ladder_books(client, group))
        return books

    async def _fetch_public(self, client: httpx.AsyncClient, sport: str) -> dict[str, Any]:
        league = novig_parser.LEAGUE_BY_SPORT[sport]
        event_pages = await self._pages(
            client, f"{CATALOG_ROOT}/events", {"league": league, "limit": PAGE_LIMIT},
        )
        market_pages = await self._pages(
            client, f"{CATALOG_ROOT}/markets",
            {"league": league, "marketType": ",".join(MARKET_TYPES), "limit": PAGE_LIMIT},
        )
        game_ids = novig_parser.public_game_event_ids(event_pages)
        markets: list[dict[str, Any]] = []
        for page in market_pages:
            for market in page.get("items") or []:
                if not isinstance(market, dict):
                    continue
                if str(market.get("eventId") or "") not in game_ids:
                    continue
                if market.get("marketType") not in MARKET_TYPES or market.get("status") not in (None, "OPEN"):
                    continue
                if market.get("marketId"):
                    markets.append(market)
        self._book_deadline = asyncio.get_running_loop().time() + BOOK_DEADLINE_S
        books = await self._fetch_books(client, markets)
        if self._past_book_deadline():
            logger.warning(
                "[novig] %s stopped at the book-read deadline with %s books", sport, len(books),
            )
        logger.info(
            "[novig] %s public catalog: %s event page(s), %s market page(s), %s books",
            sport, len(event_pages), len(market_pages), len(books),
        )
        return {"source": "public", "events": event_pages, "markets": market_pages, "books": books}

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        creds = nbx_credentials()
        if creds is None:
            raise NovigCatalogRejected(
                "Novig NBX credentials are not set. Set NOVIG_CLIENT_ID and NOVIG_CLIENT_SECRET."
            )
        client_id, secret = creds
        response = await request_with_retry(
            client, "POST", TOKEN_URL, label="novig", raise_status=False, timeout=self.timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        )
        if response.status_code != 200:
            raise NovigCatalogRejected(
                f"Novig rejected NOVIG_CLIENT_ID/NOVIG_CLIENT_SECRET (HTTP {response.status_code})."
            )
        body = response.json()
        token = ""
        if isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            token = str(body.get("access_token") or body.get("token") or data.get("access_token") or "").strip()
        if not token:
            raise NovigCatalogRejected("Novig token response did not contain an access token.")
        return token

    async def _nbx_json(self, client: httpx.AsyncClient, url: str, headers: dict[str, str], params: dict) -> Any:
        response = await request_with_retry(
            client, "GET", url, label="novig", headers=headers, params=params,
            raise_status=False, timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise NovigCatalogRejected(f"Novig NBX returned HTTP {response.status_code} for {url}.")
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("markets") or payload.get("items") or []
        if not isinstance(payload, list):
            raise RuntimeError(f"Novig NBX {url} returned a non-list response")
        return payload

    async def _fetch_nbx(self, client: httpx.AsyncClient, sport: str) -> dict[str, Any]:
        token = await self._access_token(client)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        league = novig_parser.LEAGUE_BY_SPORT[sport]
        open_markets: dict[str, list] = {}
        for market_type in MARKET_TYPES:
            open_markets[market_type] = await self._nbx_json(
                client, OPEN_MARKETS_URL, headers, {"league": league, "marketType": market_type},
            )
        event_ids: list[str] = []
        seen: set[str] = set()
        for rows in open_markets.values():
            for market in rows:
                if not isinstance(market, dict):
                    continue
                event = market.get("event") if isinstance(market.get("event"), dict) else {}
                event_id = str(market.get("eventId") or event.get("id") or "")
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    event_ids.append(event_id)
        event_books: dict[str, list] = {}
        sem = asyncio.Semaphore(8)

        async def one(event_id: str) -> None:
            async with sem:
                event_books[event_id] = await self._nbx_json(
                    client, f"{EVENT_MARKETS_URL}/{event_id}", headers, {"currency": "CASH"},
                )

        if event_ids:
            await asyncio.gather(*(one(event_id) for event_id in event_ids))
        logger.info("[novig] %s NBX: %s events", sport, len(event_ids))
        return {"source": "nbx", "open_markets": open_markets, "event_books": event_books}

    async def fetch_raw(self, sport: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                payload = await self._fetch_public(client, sport)
                self.last_transport = "httpx"
                return payload
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in (401, 403):
                    raise
                if nbx_credentials() is None:
                    raise NovigCatalogRejected(
                        f"Novig public catalog returned HTTP {status}. "
                        "Browser impersonation is not used. "
                        "Set NOVIG_CLIENT_ID and NOVIG_CLIENT_SECRET to use the NBX API."
                    ) from exc
                logger.warning("[novig] public catalog HTTP %s; trying NBX", status)
                payload = await self._fetch_nbx(client, sport)
                self.last_transport = "nbx"
                return payload

    async def scrape_with_retry(
        self, sport: str, market: str | None = None, **kwargs: Any
    ) -> list[GameLine]:
        """Retry transient failures. A rejected catalog is reported once."""
        last_exc: BaseException | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.info(f"[{self.BOOK_NAME}] {sport} attempt {attempt}/{self.MAX_RETRIES}")
                lines = await self.scrape(sport, market=market, **kwargs)
                self.fetch_errors.pop(sport, None)
                logger.info(f"[{self.BOOK_NAME}] {sport}: got {len(lines)} lines")
                return lines
            except NovigCatalogRejected as exc:
                self.fetch_errors[sport] = str(exc)
                logger.error(f"[{self.BOOK_NAME}] {sport}: {exc}")
                return []
            except Exception as exc:
                last_exc = exc
                logger.error(f"[{self.BOOK_NAME}] {sport} attempt {attempt} failed: {exc}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * attempt)
        if last_exc is not None:
            self.fetch_errors[sport] = f"{type(last_exc).__name__}: {last_exc}"[:200]
        return []

    async def scrape(
        self,
        sport: str,
        market: str | None = None,
        capture: Capture | None = None,
        run_id: str | None = None,
        **kwargs: Any,
    ) -> list[GameLine]:
        del kwargs
        if os.environ.get("BOOK_NOVIG_ENABLED", "1") == "0":
            logger.info("[novig] disabled via BOOK_NOVIG_ENABLED=0")
            return []
        data = await self.fetch_raw(sport)
        if capture is not None:
            capture(f"novig_{sport}", data, CATALOG_ROOT if data.get("source") == "public" else OPEN_MARKETS_URL)
        scraped_at = datetime.now(timezone.utc)
        lines = novig_parser.parse(data, sport, scraped_at=scraped_at, run_id=run_id)
        if market:
            lines = [ln for ln in lines if ln.market == market]
        n_games = len({ln.game_id for ln in lines})
        logger.info(
            f"[novig] {sport}: {n_games} games with prices, {len(lines)} lines (via {self.last_transport})"
        )
        return lines


__all__ = [
    "NovigScraper", "NovigCatalogRejected", "CATALOG_ROOT", "TOKEN_URL", "OPEN_MARKETS_URL",
    "PUBLIC_HEADERS", "nbx_credentials", "quote_balance",
]
