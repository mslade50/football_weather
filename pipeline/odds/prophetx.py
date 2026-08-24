"""ProphetX football lines via the partner Market Data API.

Transport adapted from ``golf_scraping/scrapers/prophetx.py`` (auth, route
prefixes, tournament -> events -> markets walk); parsing lives in
``pipeline/odds/parsers/prophetx.py``.

Credential options (issued by ProphetX), read from the environment (the repo
``.env`` is loaded if python-dotenv is installed):

* ``PROPHETX_ACCESS_KEY`` + ``PROPHETX_SECRET_KEY`` -- exchanged at
  ``POST /auth/login`` for a Bearer session; uses the Trading API's read-only
  ``/mm/get_*`` endpoints.
* ``PROPHETX_API_KEY`` -- affiliate/read-only key sent directly in the
  ``Authorization`` header; uses the ``/affiliate`` Market Data API.

``PROPHETX_API_BASE_URL`` overrides the production base (a sandbox URL must
never be used on the live board).  ``BOOK_PROPHETX_ENABLED=0`` disables the
book.  The ``market_types`` filter on ``get_multiple_markets`` is sent but the
API still returns derivative markets, so the parser filters by market id.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from pipeline.contracts import GameLine
from pipeline.odds.base import BaseScraper
from pipeline.odds.parsers import prophetx as parser

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cash.api.prophetx.co/partner"
API_VERSION = "v3"
MARKET_TYPES = "moneyline,spread,total"
CHUNK = 100

_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_LOADED = False


def _load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_ROOT / ".env", override=False)


class ProphetXConfigError(RuntimeError):
    """Raised when the supported API has not been configured."""


def enabled() -> bool:
    return os.environ.get("BOOK_PROPHETX_ENABLED", "1").strip() != "0"


def _text(value: Any) -> str:
    return str(value or "").strip()


class ProphetXScraper(BaseScraper):
    BOOK_NAME = "prophetx"
    MAX_RETRIES = 2
    RETRY_DELAY = 2.0

    def __init__(self, headless: bool = True, raw_store: Any = None, run_id: str | None = None):
        del headless  # API-only; retained for the common scraper constructor.
        _load_dotenv()
        self.base_url = (os.getenv("PROPHETX_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.getenv("PROPHETX_API_KEY", "").strip()
        self.access_key = os.getenv("PROPHETX_ACCESS_KEY", "").strip()
        self.secret_key = os.getenv("PROPHETX_SECRET_KEY", "").strip()
        # Prefer a complete access/secret pair so a stale affiliate key cannot
        # shadow newly approved Trading API credentials (same rule as golf).
        self.api_flavor = "mm" if self.access_key and self.secret_key else "affiliate"
        self.raw_store = raw_store
        self.run_id = run_id

    # -- transport (golf-identical) -------------------------------------------------

    async def _auth_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        if self.access_key and self.secret_key:
            response = await client.post(
                f"{self.base_url}/auth/login",
                json={"access_key": self.access_key, "secret_key": self.secret_key},
            )
            if response.status_code == 401:
                raise ProphetXConfigError("ProphetX API credentials were rejected")
            response.raise_for_status()
            token = _text(response.json().get("data", {}).get("access_token"))
            if not token:
                raise ProphetXConfigError("ProphetX login response did not contain an access token")
            return {"Authorization": f"Bearer {token}"}
        if self.api_key:
            return {"Authorization": self.api_key}
        raise ProphetXConfigError(
            "ProphetX Market Data API is not configured; set PROPHETX_API_KEY "
            "or PROPHETX_ACCESS_KEY and PROPHETX_SECRET_KEY"
        )

    def _market_path(self, operation: str) -> str:
        if self.api_flavor == "affiliate":
            version = f"/{API_VERSION}" if operation == "get_multiple_markets" else ""
            return f"{version}/affiliate/{operation}"
        return f"/mm/{operation}"

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        params: Any = None,
    ) -> dict:
        response = await client.get(f"{self.base_url}/{path.lstrip('/')}", headers=headers, params=params)
        if response.status_code == 401:
            raise ProphetXConfigError("ProphetX Market Data API authorization failed")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"ProphetX {path} returned a non-object response")
        return payload

    @staticmethod
    def _tournaments(payload: dict) -> list[dict]:
        data = payload.get("data", {})
        if isinstance(data, list):
            return data
        rows = data.get("tournaments", []) if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _events(payload: dict) -> list[dict]:
        data = payload.get("data", {})
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        rows = data.get("sport_events") or data.get("events") or []
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _markets_by_event(payload: dict) -> dict[str, list[dict]]:
        data = payload.get("data", {})
        if isinstance(data, dict):
            return {str(eid): mks for eid, mks in data.items() if isinstance(mks, list)}
        if not isinstance(data, list):
            return {}
        out: dict[str, list[dict]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("markets"), list):
                eid = _text(item.get("eventId") or item.get("event_id"))
                out.setdefault(eid, []).extend(item["markets"])
            else:
                eid = _text(item.get("event_id") or item.get("eventId"))
                out.setdefault(eid, []).append(item)
        return out

    async def fetch_feed(self, sport: str) -> dict[str, Any]:
        """Return the raw-capture payload ``{"tournaments", "events", "markets"}`` for ``sport``."""
        async with httpx.AsyncClient(timeout=25.0, headers={"Accept": "application/json"}) as client:
            headers = await self._auth_headers(client)
            tournament_payload = await self._get_json(
                client, self._market_path("get_tournaments"), headers, params={"has_active_events": "true"},
            )
            tournaments = [
                t for t in self._tournaments(tournament_payload) if parser.tournament_sport(t) == sport
            ]
            events: list[dict] = []
            for tournament in tournaments:
                tid = _text(tournament.get("id") or tournament.get("tournament_id"))
                if not tid:
                    continue
                event_payload = await self._get_json(
                    client, self._market_path("get_sport_events"), headers, params={"tournament_id": tid},
                )
                for ev in self._events(event_payload):
                    ev.setdefault("tournament_name", _text(tournament.get("name")))
                    ev.setdefault("tournament_id", tournament.get("id"))
                    events.append(ev)

            event_ids = [
                ev.event_id for ev in parser.parse_events(events, sport)
                if ev.status not in parser.SKIP_EVENT_STATUS
            ]
            markets: dict[str, list[dict]] = {}
            for start in range(0, len(event_ids), CHUNK):
                chunk = event_ids[start:start + CHUNK]
                params: Any
                if self.api_flavor == "affiliate":
                    params = [("event_ids", eid) for eid in chunk] + [("market_types", MARKET_TYPES)]
                else:
                    params = {"event_ids": ",".join(chunk), "market_types": MARKET_TYPES}
                market_payload = await self._get_json(
                    client, self._market_path("get_multiple_markets"), headers, params=params,
                )
                for eid, mks in self._markets_by_event(market_payload).items():
                    markets.setdefault(eid, []).extend(mks)
        return {"tournaments": tournaments, "events": events, "markets": markets}

    # -- BaseScraper ----------------------------------------------------------------

    async def scrape(
        self,
        sport: str,
        market: str | None = None,
        include_alternates: bool = True,
        include_live: bool = False,
        **kwargs: Any,
    ) -> list[GameLine]:
        if sport not in parser.TOURNAMENTS:
            raise ValueError(f"unknown sport {sport!r}")
        if not enabled():
            logger.info("[prophetx] disabled via BOOK_PROPHETX_ENABLED=0")
            return []
        payload = await self.fetch_feed(sport)
        scraped_at = datetime.now(timezone.utc)
        if self.raw_store is not None:
            try:
                self.raw_store.put(f"prophetx_{sport}", payload, url=f"{self.base_url}/mm/get_multiple_markets")
            except Exception as e:  # raw capture is best-effort
                logger.warning(f"[prophetx] raw capture failed: {e}")
        lines = parser.parse_payload(
            payload, sport,
            market=market, include_alternates=include_alternates,
            include_live=include_live, scraped_at=scraped_at,
        )
        if self.run_id:
            from dataclasses import replace

            lines = [replace(ln, run_id=self.run_id) for ln in lines]
        n_games = len({ln.game_id for ln in lines})
        logger.info(
            "ProphetX %s: %d tournaments, %d events -> %d games, %d lines",
            sport, len(payload["tournaments"]), len(payload["events"]), n_games, len(lines),
        )
        return lines
