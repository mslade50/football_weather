"""Abstract base class for sportsbook scrapers (copied from golf_scraping/scrapers/base.py).

Golf's Matchup/ScoreLine/OutrightLine/PropLine dataclasses are replaced by the
single ``GameLine`` contract (ARCH §4.2), imported from ``pipeline.contracts``.
"""

import asyncio
import logging
import os
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pandas as pd

from pipeline.contracts import GameLine

logger = logging.getLogger(__name__)
_MODULE_LOGGER = logger  # ``fetch_json_with_fallback`` takes a ``logger`` kwarg that shadows the name
Capture = Callable[[str, Any, str | None], Any]

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
_DOTENV_LOADED = False


def book_enabled(name: str) -> bool:
    """One switch resolver, retaining each book's historical opt-out values.

    The workflow defaults to 1 but accepts arbitrary repository variables, so
    extending false/no to every book could disable a currently enabled book.
    """
    book = name.lower()
    value = os.environ.get(f"BOOK_{book.upper()}_ENABLED", "1")
    if book in ("betcris", "betonline"):
        return value.strip() not in ("0", "false", "no")
    if book in ("pinnacle", "prophetx"):
        return value.strip() != "0"
    return value != "0"


def load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        _DOTENV_LOADED = True
        from utils.env import load_repo_dotenv

        load_repo_dotenv()


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

# --- shared retrying request + season gate -------------------------------------------

RETRYABLE_4XX = frozenset({408, 429})
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_S = 1.0


def retryable_status(status: int) -> bool:
    """5xx and 408/429 are transient; every other 4xx (403, 401, 404...) is final."""
    return status >= 500 or status in RETRYABLE_4XX


def is_retryable_error(exc: BaseException) -> bool:
    """Timeouts / connection errors / 5xx / 408 / 429 retry; other HTTP statuses do not."""
    if isinstance(exc, httpx.HTTPStatusError):
        return retryable_status(exc.response.status_code)
    if isinstance(exc, TransportError):
        return retryable_status(exc.status)
    return isinstance(exc, httpx.TransportError)  # timeouts, connect/read/protocol errors


class FatalScrapeError(RuntimeError):
    """A rejected/configuration request that retrying the whole scrape cannot fix."""


def backoff_delay(attempt: int, base: float = REQUEST_BACKOFF_S) -> float:
    """Linear backoff with +-50% jitter so concurrent books do not retry in lockstep."""
    return base * attempt * (0.5 + random.random())


async def request_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    label: str = "",
    attempts: int = REQUEST_ATTEMPTS,
    backoff: float = REQUEST_BACKOFF_S,
    raise_status: bool = True,
    send: Callable[[], Awaitable[httpx.Response]] | None = None,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    retry_status: Callable[[int], bool] = retryable_status,
    retry_error: Callable[[BaseException], bool] = is_retryable_error,
    retry_after_cap: float | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """``client.request`` with retry on 5xx/408/429/timeouts/connection errors.

    Headers, auth and timeouts stay whatever the caller configured on ``client`` /
    ``kwargs``; raw capture stays with the caller (the response is returned verbatim).
    With ``raise_status=False`` a final non-2xx response is returned instead of raised
    (for callers that map 401 to their own error).

    ``send`` adapts a client's get-only interface; ``before_attempt`` preserves
    per-book pacing. Retry-After uses a one-second default and a 0..cap clamp
    when enabled. Status and exception predicates keep narrower policies (such
    as retrying only 429 responses, with no transport retries) explicit.
    """
    for attempt in range(1, attempts + 1):
        try:
            if before_attempt is not None:
                await before_attempt()
            resp = await send() if send is not None else await client.request(method, url, **kwargs)
            # Some get-only transports expose only raise_for_status and text.
            status = getattr(resp, "status_code", 200)
            if attempt < attempts and retry_status(status):
                _MODULE_LOGGER.warning(f"[{label}] {method} {_short(url)} -> HTTP {status}; retry {attempt}/{attempts}")
                if retry_after_cap is None:
                    delay = backoff_delay(attempt, backoff)
                else:
                    try:
                        delay = float(resp.headers.get("Retry-After") or "1")
                    except ValueError:
                        delay = 1.0
                    delay = min(max(delay, 0.0), retry_after_cap)
                await asyncio.sleep(delay)
                continue
            if raise_status:
                resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            if attempt >= attempts or isinstance(e, FatalScrapeError) or not retry_error(e):
                raise
            _MODULE_LOGGER.warning(f"[{label}] {method} {_short(url)} {type(e).__name__}; retry {attempt}/{attempts}")
            await asyncio.sleep(backoff_delay(attempt, backoff))
    raise RuntimeError("unreachable")  # pragma: no cover


async def get_with_retry(client: Any, url: str, **kwargs: Any) -> httpx.Response:
    return await request_with_retry(client, "GET", url, **kwargs)


# (month, day) windows incl. preseason / bowls / playoffs; outside them zero events is normal.
_SEASON_WINDOWS = {"nfl": ((8, 10), (2, 20)), "cfb": ((8, 20), (1, 25))}


def in_season(sport: str, now: datetime | None = None) -> bool:
    start, end = _SEASON_WINDOWS[sport]
    d = now or datetime.now(timezone.utc)
    md = (d.month, d.day)
    return md >= start or md <= end


def _line_to_dict(line: Any) -> dict:
    if hasattr(line, "as_dict"):
        return line.as_dict()
    if hasattr(line, "to_dict"):
        return line.to_dict()
    from dataclasses import asdict
    return asdict(line)


def _describe_failure(exc: BaseException, limit: int = 200) -> str:
    """``Type: message`` (with the HTTP status for httpx status errors), truncated."""
    text = f"{type(exc).__name__}: {exc}".rstrip(": ")
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None and str(status) not in text:
        text = f"{text} (HTTP {status})"
    return text[:limit]


class BaseScraper(ABC):
    """Base class all sportsbook scrapers inherit from."""

    BOOK_NAME: str = ""
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 5.0  # seconds
    DEFAULT_TIMEOUT: float = 30.0
    CLEAR_ERROR_ON_SUCCESS: bool = False

    def __init__(
        self, *, headless: bool = True, raw_store: Any = None,
        run_id: str | None = None, season: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.headless = headless
        self.raw_store = raw_store
        self.run_id = run_id
        self.season = season
        self.timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        self._initialize()

    def _initialize(self) -> None:  # noqa: B027 - optional hook, default is a no-op
        """Initialize book-specific state after the shared constructor options."""

    @property
    def capture(self) -> Capture | None:
        """Callback for API books, using the same raw store as page-based books."""
        return self.raw_store.put if self.raw_store is not None else None

    @abstractmethod
    async def scrape(
        self, sport: str, market: str | None = None, *,
        capture: Capture | None = None, run_id: str | None = None,
        include_alternates: bool = True, include_live: bool = False,
        **kwargs: Any,
    ) -> list[GameLine]:
        """Scrape game lines for ``sport`` (``nfl`` | ``cfb``).

        Args:
            sport: which league to scrape; threaded through every call.
            market: filter to ``ml`` / ``spread`` / ``total``, or None for all.
        """
        ...

    async def scrape_with_retry(self, sport: str, market: Optional[str] = None, **kwargs: Any) -> list[GameLine]:
        """Scrape with retry logic. Wraps scrape() with retries and error handling."""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.info(f"[{self.BOOK_NAME}] {sport} attempt {attempt}/{self.MAX_RETRIES}")
                lines = await self.scrape(sport, market=market, **kwargs)
                if self.CLEAR_ERROR_ON_SUCCESS:
                    self.fetch_errors.pop(sport, None)
                logger.info(f"[{self.BOOK_NAME}] {sport}: got {len(lines)} lines")
                return lines
            except Exception as e:
                if isinstance(e, FatalScrapeError):
                    if not isinstance(getattr(self, "fetch_errors", None), dict):
                        self.fetch_errors = {}
                    self.fetch_errors[sport] = str(e)
                    logger.error(f"[{self.BOOK_NAME}] {sport}: {e}")
                    return []
                last_exc = e
                logger.error(f"[{self.BOOK_NAME}] {sport} attempt {attempt} failed: {e}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * attempt)
        logger.error(f"[{self.BOOK_NAME}] All {self.MAX_RETRIES} attempts failed")
        if last_exc is not None:
            if not isinstance(getattr(self, "fetch_errors", None), dict):
                self.fetch_errors = {}
            self.fetch_errors[sport] = _describe_failure(last_exc)
        return []

    @staticmethod
    def to_dataframe(lines: list[GameLine]) -> pd.DataFrame:
        """Convert list of GameLine objects to a DataFrame."""
        if not lines:
            return pd.DataFrame()
        return pd.DataFrame([_line_to_dict(ln) for ln in lines])
