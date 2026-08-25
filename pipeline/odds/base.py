"""Abstract base class for sportsbook scrapers (copied from golf_scraping/scrapers/base.py).

Golf's Matchup/ScoreLine/OutrightLine/PropLine dataclasses are replaced by the
single ``GameLine`` contract (ARCH §4.2), imported from ``pipeline.contracts``.
"""

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pandas as pd

from pipeline.contracts import GameLine

logger = logging.getLogger(__name__)
_MODULE_LOGGER = logger  # ``fetch_json_with_fallback`` takes a ``logger`` kwarg that shadows the name

# --- transport fallback (httpx -> curl_cffi Chrome impersonation) ---------------------
#
# Datacenter IPs (GitHub Actions) get HTTP 403 from Akamai/Cloudflare-fronted book APIs
# that answer fine from residential IPs. curl_cffi replays Chrome's TLS/HTTP2 fingerprint,
# which usually clears those rules. ``BOOK_<NAME>_TRANSPORT`` selects the policy:
#   auto  (default) httpx first, retry via curl_cffi on a bot-block status
#   httpx           httpx only (never fall back)
#   curl            curl_cffi only

TRANSPORT_AUTO = "auto"
TRANSPORT_HTTPX = "httpx"
TRANSPORT_CURL = "curl"
TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_HTTPX, TRANSPORT_CURL)
CURL_IMPERSONATE = "chrome"  # curl_cffi alias -> newest Chrome build the installed version ships
BLOCKED_STATUSES = frozenset({401, 403, 405, 406, 409, 418, 429})  # bot-block signatures
_FALLBACK_CHROME_MAJOR = 135


def transport_mode(book: str) -> str:
    """Resolve ``BOOK_<BOOK>_TRANSPORT`` (``auto`` | ``httpx`` | ``curl``; default ``auto``)."""
    var = f"BOOK_{book.upper()}_TRANSPORT"
    raw = (os.environ.get(var) or TRANSPORT_AUTO).strip().lower()
    if raw not in TRANSPORTS:
        logger.warning(f"[{book}] {var}={raw!r} not in {TRANSPORTS}; using {TRANSPORT_AUTO}")
        return TRANSPORT_AUTO
    return raw


def chrome_major() -> int:
    """Chrome major version curl_cffi impersonates for the ``chrome`` alias (UA/sec-ch-ua must match)."""
    try:
        from curl_cffi.requests.impersonate import DEFAULT_CHROME
        m = re.match(r"chrome(\d+)", str(DEFAULT_CHROME))
        if m:
            return int(m.group(1))
    except Exception:  # noqa: BLE001 - curl_cffi missing or API drift: fall back to a plausible build
        pass
    return _FALLBACK_CHROME_MAJOR


def browser_headers(
    *,
    origin: str | None = None,
    referer: str | None = None,
    accept: str = "application/json, text/plain, */*",
    fetch_site: str = "same-site",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Realistic Chrome XHR headers (consistent with the curl_cffi impersonation target)."""
    major = chrome_major()
    headers = {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": fetch_site,
    }
    if origin:
        headers["Origin"] = origin
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


@dataclass
class FetchResult:
    payload: Any
    transport: str
    status: int


class TransportError(RuntimeError):
    """Non-2xx from the curl_cffi path (httpx raises its own ``HTTPStatusError``)."""

    def __init__(self, transport: str, status: int, url: str) -> None:
        super().__init__(f"{transport} -> HTTP {status} for {url}")
        self.transport = transport
        self.status = status


def _httpx_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _curl_session(impersonate: str, timeout: float) -> Any:
    from curl_cffi.requests import AsyncSession  # lazy: optional dep, stubbed in tests
    return AsyncSession(impersonate=impersonate, timeout=timeout)


def _short(url: str) -> str:
    return url.split("?", 1)[0]


async def _via_httpx(
    url: str, *, method: str, params: Any, headers: Any, json_body: Any, timeout: float,
    client: httpx.AsyncClient | None,
) -> tuple[int, Any]:
    own = client is None
    client = client or _httpx_client(timeout)
    try:
        resp = await client.request(method, url, params=params, headers=headers, json=json_body)
        resp.raise_for_status()
        return resp.status_code, resp.json()
    finally:
        if own:
            await client.aclose()


async def _via_curl(
    url: str, *, method: str, params: Any, headers: Any, json_body: Any, timeout: float, impersonate: str,
) -> tuple[int, Any]:
    async with _curl_session(impersonate, timeout) as session:
        resp = await session.request(method, url, params=params, headers=headers, json=json_body)
        status = int(resp.status_code)
        if status >= 400:
            raise TransportError(TRANSPORT_CURL, status, _short(url))
        return status, resp.json()


async def fetch_json_with_fallback(
    url: str,
    *,
    method: str = "GET",
    params: Any = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 20.0,
    label: str = "",
    logger: logging.Logger | None = None,
    mode: str | None = None,
    client: httpx.AsyncClient | None = None,
    curl_headers: dict[str, str] | None = None,
    impersonate: str = CURL_IMPERSONATE,
) -> FetchResult:
    """GET/POST ``url`` and return parsed JSON plus the transport that succeeded.

    ``mode`` defaults to ``transport_mode(label)``. In ``auto`` a bot-block status from
    httpx (403 & co.) is retried once through curl_cffi with Chrome impersonation;
    ``curl_headers`` (default ``headers``) lets the caller send a fuller browser header
    set on that path. Raw capture stays with the caller (payload returned verbatim).
    """
    log = logger or _MODULE_LOGGER
    mode = mode or transport_mode(label)
    if mode != TRANSPORT_CURL:
        try:
            status, payload = await _via_httpx(
                url, method=method, params=params, headers=headers, json_body=json_body, timeout=timeout, client=client,
            )
            log.info(f"[{label}] {method} {_short(url)} -> {status} via httpx")
            return FetchResult(payload, TRANSPORT_HTTPX, status)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if mode != TRANSPORT_AUTO or status not in BLOCKED_STATUSES:
                raise
            log.warning(
                f"[{label}] httpx -> HTTP {status} (bot-block signature); "
                f"retrying via curl_cffi impersonate={impersonate}"
            )
    status, payload = await _via_curl(
        url, method=method, params=params, headers=curl_headers or headers, json_body=json_body,
        timeout=timeout, impersonate=impersonate,
    )
    log.info(f"[{label}] {method} {_short(url)} -> {status} via curl_cffi ({impersonate})")
    return FetchResult(payload, TRANSPORT_CURL, status)


def _line_to_dict(line: Any) -> dict:
    if hasattr(line, "as_dict"):
        return line.as_dict()
    if hasattr(line, "to_dict"):
        return line.to_dict()
    from dataclasses import asdict
    return asdict(line)


class BaseScraper(ABC):
    """Base class all sportsbook scrapers inherit from."""

    BOOK_NAME: str = ""
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 5.0  # seconds

    @abstractmethod
    async def scrape(self, sport: str, market: Optional[str] = None, **kwargs: Any) -> list[GameLine]:
        """Scrape game lines for ``sport`` (``nfl`` | ``cfb``).

        Args:
            sport: which league to scrape; threaded through every call.
            market: filter to ``ml`` / ``spread`` / ``total``, or None for all.
        """
        ...

    async def scrape_with_retry(self, sport: str, market: Optional[str] = None, **kwargs: Any) -> list[GameLine]:
        """Scrape with retry logic. Wraps scrape() with retries and error handling."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.info(f"[{self.BOOK_NAME}] {sport} attempt {attempt}/{self.MAX_RETRIES}")
                lines = await self.scrape(sport, market=market, **kwargs)
                logger.info(f"[{self.BOOK_NAME}] {sport}: got {len(lines)} lines")
                return lines
            except Exception as e:
                logger.error(f"[{self.BOOK_NAME}] {sport} attempt {attempt} failed: {e}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * attempt)
        logger.error(f"[{self.BOOK_NAME}] All {self.MAX_RETRIES} attempts failed")
        return []

    @staticmethod
    def to_dataframe(lines: list[GameLine]) -> pd.DataFrame:
        """Convert list of GameLine objects to a DataFrame."""
        if not lines:
            return pd.DataFrame()
        return pd.DataFrame([_line_to_dict(ln) for ln in lines])
