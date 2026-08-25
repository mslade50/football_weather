"""httpx -> curl_cffi transport fallback (``pipeline.odds.base.fetch_json_with_fallback``).

httpx is driven through ``httpx.MockTransport``; curl_cffi is stubbed in conftest, so the
session factory ``base._curl_session`` is replaced with an in-memory fake. No network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import pipeline.odds.base as base
from pipeline.odds import fanduel as fd
from pipeline.odds import novig as nv

FIX = Path(__file__).parent / "fixtures" / "raw"


def _load(book: str, sport: str) -> dict:
    return json.loads((FIX / book / f"{sport}.json").read_text(encoding="utf-8"))


# --- fakes ------------------------------------------------------------------------------


class FakeCurlResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeCurlSession:
    """Stands in for ``curl_cffi.requests.AsyncSession``; records every call."""

    calls: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    status: int = 200
    payload: Any = {"ok": True}

    def __init__(self, **kwargs: Any) -> None:
        type(self).sessions.append(kwargs)

    async def __aenter__(self) -> FakeCurlSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeCurlResponse:
        type(self).calls.append({"method": method, "url": url, **kwargs})
        return FakeCurlResponse(type(self).status, type(self).payload)


@pytest.fixture
def curl(monkeypatch: pytest.MonkeyPatch) -> type[FakeCurlSession]:
    FakeCurlSession.calls = []
    FakeCurlSession.sessions = []
    FakeCurlSession.status = 200
    FakeCurlSession.payload = {"ok": True}
    monkeypatch.setattr(
        base, "_curl_session", lambda impersonate, timeout: FakeCurlSession(impersonate=impersonate, timeout=timeout)
    )
    return FakeCurlSession


class HttpxLog:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


@pytest.fixture
def httpx_status(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``httpx_status(403)`` -> every httpx.AsyncClient answers 403 (or 200 + ``payload``)."""
    real_client = httpx.AsyncClient
    log = HttpxLog()

    def configure(status: int, payload: Any = None) -> HttpxLog:
        def handler(request: httpx.Request) -> httpx.Response:
            log.requests.append(request)
            if status == 200:
                return httpx.Response(200, json=payload if payload is not None else {"ok": "httpx"})
            return httpx.Response(status, text="Access Denied")

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return log

    return configure


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BOOK_FANDUEL_TRANSPORT", "BOOK_NOVIG_TRANSPORT", "BOOK_X_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- config helpers ----------------------------------------------------------------------


def test_transport_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert base.transport_mode("x") == "auto"
    monkeypatch.setenv("BOOK_X_TRANSPORT", "curl")
    assert base.transport_mode("x") == "curl"
    monkeypatch.setenv("BOOK_X_TRANSPORT", " HTTPX ")
    assert base.transport_mode("x") == "httpx"
    monkeypatch.setenv("BOOK_X_TRANSPORT", "bogus")
    assert base.transport_mode("x") == "auto"


def test_browser_headers_shape() -> None:
    h = base.browser_headers(origin="https://o.example", referer="https://o.example/p", fetch_site="cross-site")
    for key in ("Accept", "Accept-Language", "User-Agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
        assert key in h
    assert h["Origin"] == "https://o.example" and h["Referer"] == "https://o.example/p"
    assert h["Sec-Fetch-Site"] == "cross-site"
    major = base.chrome_major()
    assert f"Chrome/{major}." in h["User-Agent"] and f'v="{major}"' in h["sec-ch-ua"]
    assert base.CURL_IMPERSONATE == "chrome"


# --- helper --------------------------------------------------------------------------------


def test_auto_httpx_ok_never_touches_curl(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(200, {"via": "httpx"})
    res = _run(base.fetch_json_with_fallback("https://x.test/api", params={"a": 1}, label="x"))
    assert res.transport == "httpx" and res.status == 200 and res.payload == {"via": "httpx"}
    assert curl.calls == []


def test_auto_403_falls_back_to_curl(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(403)
    curl.payload = {"via": "curl"}
    hdrs = {"Accept": "application/json"}
    curl_hdrs = {"Accept": "application/json", "sec-ch-ua": "x", "Origin": "https://o"}
    res = _run(base.fetch_json_with_fallback(
        "https://x.test/api", method="POST", params={"q": 1}, headers=hdrs, curl_headers=curl_hdrs,
        json_body={"query": "{}"}, timeout=7.0, label="x",
    ))
    assert res.transport == "curl" and res.status == 200 and res.payload == {"via": "curl"}
    assert len(log.requests) == 1 and log.requests[0].method == "POST"
    assert curl.sessions == [{"impersonate": "chrome", "timeout": 7.0}]
    assert len(curl.calls) == 1
    call = curl.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://x.test/api"
    assert call["params"] == {"q": 1} and call["json"] == {"query": "{}"} and call["headers"] == curl_hdrs


@pytest.mark.parametrize("status", [401, 429])
def test_auto_other_block_signatures_fall_back(curl: type[FakeCurlSession], httpx_status: Any, status: int) -> None:
    httpx_status(status)
    res = _run(base.fetch_json_with_fallback("https://x.test/api", label="x"))
    assert res.transport == "curl" and len(curl.calls) == 1


def test_auto_500_is_not_a_bot_block(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(500)
    with pytest.raises(httpx.HTTPStatusError):
        _run(base.fetch_json_with_fallback("https://x.test/api", label="x"))
    assert curl.calls == []


def test_mode_httpx_never_falls_back(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(403)
    with pytest.raises(httpx.HTTPStatusError):
        _run(base.fetch_json_with_fallback("https://x.test/api", label="x", mode="httpx"))
    assert curl.calls == []


def test_mode_curl_skips_httpx(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(200)
    res = _run(base.fetch_json_with_fallback("https://x.test/api", label="x", mode="curl"))
    assert res.transport == "curl" and log.requests == [] and len(curl.calls) == 1


def test_env_override_selects_curl(monkeypatch: pytest.MonkeyPatch, curl: type[FakeCurlSession], httpx_status: Any) -> None:
    monkeypatch.setenv("BOOK_X_TRANSPORT", "curl")
    log = httpx_status(200)
    res = _run(base.fetch_json_with_fallback("https://x.test/api", label="x"))
    assert res.transport == "curl" and log.requests == []


def test_curl_4xx_raises_transport_error(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(403)
    curl.status = 403
    with pytest.raises(base.TransportError) as ei:
        _run(base.fetch_json_with_fallback("https://x.test/api?k=v", label="x"))
    assert ei.value.status == 403 and ei.value.transport == "curl" and "k=v" not in str(ei.value)


# --- scrapers ------------------------------------------------------------------------------


class RecordingRawStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, Any, str | None]] = []

    def put(self, source: str, payload: Any, url: str | None = None) -> None:
        self.puts.append((source, payload, url))


def test_fanduel_auto_fallback_parses_and_captures(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(403)
    curl.payload = _load("fanduel", "cfb")
    raw = RecordingRawStore()
    s = fd.FanDuelScraper(raw_store=raw, run_id="r1")
    lines = _run(s.scrape("cfb"))
    assert len(lines) == 113 and s.last_transport == "curl"
    assert len(curl.calls) == 1  # custom page had game markets: no competition-page requests
    call = curl.calls[0]
    assert call["url"] == fd.API_BASE and call["params"]["customPageId"] == "ncaaf"
    assert call["headers"]["Origin"] == fd.SITE_ORIGIN and call["headers"]["Referer"] == fd.SITE_PAGES["cfb"]
    assert "sec-ch-ua" in call["headers"]
    assert [(p[0], p[2]) for p in raw.puts] == [("cfb_fanduel", f"{fd.API_BASE}?customPageId=ncaaf")]
    assert raw.puts[0][1] is curl.payload  # verbatim payload reaches the RawStore hook


def test_fanduel_httpx_path_unchanged(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(200, _load("fanduel", "nfl"))
    s = fd.FanDuelScraper()
    lines = _run(s.scrape("nfl"))
    assert len(lines) == 120 and s.last_transport == "httpx" and curl.calls == []
    assert log.requests[0].headers["user-agent"] == fd.HEADERS["User-Agent"]


def test_fanduel_env_curl_first(monkeypatch: pytest.MonkeyPatch, curl: type[FakeCurlSession], httpx_status: Any) -> None:
    monkeypatch.setenv("BOOK_FANDUEL_TRANSPORT", "curl")
    log = httpx_status(200, _load("fanduel", "nfl"))
    curl.payload = _load("fanduel", "nfl")
    s = fd.FanDuelScraper()
    lines = _run(s.scrape("nfl"))
    assert len(lines) == 120 and s.last_transport == "curl" and log.requests == []


def test_fanduel_competition_pages_stay_on_curl_after_fallback(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(403)
    curl.payload = {"attachments": {"markets": {}}}  # no game markets -> competition-page loop
    payload, transport = _run(fd.fetch_board("cfb"))
    assert transport == "curl"
    assert len(log.requests) == 1  # only the first request tried httpx
    assert [c["url"] for c in curl.calls] == [fd.API_BASE] + [fd.COMPETITION_API] * len(fd.parser.COMPETITIONS["cfb"])
    assert payload == {"attachments": {"competitions": {}, "events": {}, "markets": {}}}


def test_novig_auto_fallback_parses_and_captures(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(403)
    curl.payload = _load("novig", "cfb")
    captured: list[tuple[str, Any, str | None]] = []
    s = nv.NovigScraper()
    lines = _run(s.scrape("cfb", capture=lambda src, data, url: captured.append((src, data, url)), run_id="r1"))
    assert lines and s.last_transport == "curl"
    assert len(log.requests) == 1 and log.requests[0].method == "POST"
    assert len(curl.calls) == 1
    call = curl.calls[0]
    assert call["method"] == "POST" and call["url"] == nv.GRAPHQL_URL
    assert call["json"]["query"] == nv.GAMES_QUERY and call["json"]["variables"] == {"leagues": ["NCAAF"]}
    assert call["headers"]["Origin"] == "https://novig.com" and call["headers"]["Sec-Fetch-Site"] == "cross-site"
    assert call["headers"]["Content-Type"] == "application/json" and "sec-ch-ua" in call["headers"]
    assert captured == [("novig_cfb", curl.payload, nv.GRAPHQL_URL)]


def test_novig_httpx_path_unchanged(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    log = httpx_status(200, _load("novig", "nfl"))
    s = nv.NovigScraper()
    lines = _run(s.scrape("nfl"))
    assert lines and s.last_transport == "httpx" and curl.calls == []
    req = log.requests[0]
    assert req.headers["origin"] == "https://novig.com" and json.loads(req.content)["variables"] == {"leagues": ["NFL"]}


def test_novig_env_curl_first(monkeypatch: pytest.MonkeyPatch, curl: type[FakeCurlSession], httpx_status: Any) -> None:
    monkeypatch.setenv("BOOK_NOVIG_TRANSPORT", "curl")
    log = httpx_status(200, _load("novig", "nfl"))
    curl.payload = _load("novig", "nfl")
    s = nv.NovigScraper()
    lines = _run(s.scrape("nfl"))
    assert lines and s.last_transport == "curl" and log.requests == []


def test_novig_curl_block_propagates_for_retry(curl: type[FakeCurlSession], httpx_status: Any) -> None:
    httpx_status(403)
    curl.status = 403
    with pytest.raises(base.TransportError):
        _run(nv.NovigScraper().fetch_raw("nfl"))
