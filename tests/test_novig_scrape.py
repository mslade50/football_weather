"""Official Novig catalog transport. No network and no browser impersonation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pipeline.odds import novig

FIX = Path(__file__).parent / "fixtures" / "raw" / "novig" / "catalog_nfl.json"
CATALOG = json.loads(FIX.read_text(encoding="utf-8"))


def _client(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


def _serve_catalog(monkeypatch, pages=None):
    pages = pages or CATALOG
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/events"):
            if request.url.params.get("after") == "page-2":
                return httpx.Response(200, json={"items": [], "next": None})
            page = dict(pages["events"][0])
            if request.url.params.get("league") != "NFL":
                return httpx.Response(400, json={"message": "league"})
            return httpx.Response(200, json=page)
        if path.endswith("/markets") and "/book" not in path:
            assert request.url.params.get("marketType") == "MONEY,SPREAD,TOTAL"
            return httpx.Response(200, json=pages["markets"][0])
        if path.endswith("/book"):
            market_id = path.split("/")[-2]
            assert request.url.params.get("depth") == "1"
            book = pages["books"].get(market_id)
            if book is None:
                return httpx.Response(404, json={"message": "missing"})
            return httpx.Response(200, json=book)
        return httpx.Response(500, json={"message": path})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    return calls


def test_public_catalog_prices_books_and_captures_verbatim(monkeypatch):
    monkeypatch.delenv("BOOK_NOVIG_ENABLED", raising=False)
    monkeypatch.delenv("NOVIG_CLIENT_ID", raising=False)
    monkeypatch.delenv("NOVIG_CLIENT_SECRET", raising=False)
    calls = _serve_catalog(monkeypatch)
    captured = []
    scraper = novig.NovigScraper()
    rows = asyncio.run(scraper.scrape(
        "nfl", capture=lambda *args: captured.append(args), market="spread", run_id="r1",
    ))
    assert scraper.last_transport == "httpx"
    assert rows and all(r.market == "spread" for r in rows)
    assert captured[0][0] == "novig_nfl"
    assert captured[0][2] == novig.CATALOG_ROOT
    payload = captured[0][1]
    assert payload["source"] == "public"
    assert payload["events"] == [CATALOG["events"][0]]
    assert payload["markets"] == [CATALOG["markets"][0]]
    assert "final-spread" not in payload["books"]
    assert payload["books"]["spread-main"]["orders"]["h1"][0]["price"] == "0.5"
    assert all(call.method == "GET" for call in calls)
    assert not any(call.headers.get("authorization") for call in calls)
    assert not any(call.headers.get("origin") for call in calls)
    assert calls[0].headers["user-agent"] == novig.PUBLIC_HEADERS["User-Agent"]


def test_catalog_follows_next_and_stops(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.url.params.get("after")))
        path = request.url.path
        after = request.url.params.get("after")
        if path.endswith("/events"):
            if after == "page-2":
                return httpx.Response(200, json={"items": [], "next": None})
            return httpx.Response(200, json={"items": CATALOG["events"][0]["items"], "next": "page-2"})
        if path.endswith("/markets") and not path.endswith("/book"):
            return httpx.Response(200, json={"items": [], "next": None})
        return httpx.Response(404, json={})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    payload = asyncio.run(novig.NovigScraper().fetch_raw("nfl"))
    assert [item for item in calls if item[0].endswith("/events")] == [
        ("/v3/public/catalog/events", None),
        ("/v3/public/catalog/events", "page-2"),
    ]
    assert payload["events"][1]["items"] == []


def test_catalog_refuses_a_truncated_page(monkeypatch):
    monkeypatch.setattr(novig, "MAX_PAGES", 1)
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "next": "again"})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    with pytest.raises(RuntimeError, match="refusing truncated data"):
        asyncio.run(novig.NovigScraper().fetch_raw("cfb"))


def test_empty_catalog_is_valid(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    monkeypatch.delenv("BOOK_NOVIG_ENABLED", raising=False)
    assert asyncio.run(novig.NovigScraper().scrape("cfb")) == []


def test_forbidden_without_credentials_is_reported_once(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(403, text="<html>forbidden</html>")

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    monkeypatch.delenv("NOVIG_CLIENT_ID", raising=False)
    monkeypatch.delenv("NOVIG_CLIENT_SECRET", raising=False)
    scraper = novig.NovigScraper()
    rows = asyncio.run(scraper.scrape_with_retry("nfl"))
    assert rows == []
    assert calls == ["/v3/public/catalog/events"]
    message = scraper.fetch_errors["nfl"]
    assert "HTTP 403" in message
    assert "NOVIG_CLIENT_ID" in message and "NOVIG_CLIENT_SECRET" in message
    assert "Browser impersonation is not used" in message


def test_forbidden_with_credentials_uses_nbx_books(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    monkeypatch.setenv("NOVIG_CLIENT_ID", "test-client")
    monkeypatch.setenv("NOVIG_CLIENT_SECRET", "test-secret")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/events"):
            return httpx.Response(403, text="no")
        if path.endswith("/emm-token"):
            body = json.loads(request.content)
            assert body == {
                "grant_type": "client_credentials",
                "client_id": "test-client",
                "client_secret": "test-secret",
            }
            return httpx.Response(200, json={"access_token": "issued-token"})
        if path.endswith("/markets/open"):
            assert request.headers["authorization"] == "Bearer issued-token"
            market_type = request.url.params.get("marketType")
            if market_type != "SPREAD":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{
                "id": "m1", "type": "SPREAD", "strike": -3.5, "league": "NFL", "status": "OPEN",
                "eventId": "e1",
                "outcomes": [
                    {"id": "h", "type": "Home", "index": 0, "last": 0.1, "description": "DET -3.5"},
                    {"id": "a", "type": "Away", "index": 1, "last": 0.9, "description": "GB +3.5"},
                ],
                "event": {
                    "id": "e1", "status": "OPEN_PREGAME",
                    "game": {
                        "scheduledStart": "2026-11-26T17:30:00Z", "league": "NFL",
                        "homeTeam": {"id": "det", "name": "Detroit Lions"},
                        "awayTeam": {"id": "gb", "name": "Green Bay Packers"},
                    },
                },
            }])
        if "/getMarketsByEvent/" in path:
            assert request.url.params.get("currency") == "CASH"
            return httpx.Response(200, json=[{
                "id": "m1",
                "book": {"outcomeLadders": [
                    {"outcomeId": "h", "bids": [{"price": 0.5}]},
                    {"outcomeId": "a", "bids": [{"price": 0.5}]},
                ]},
            }])
        return httpx.Response(500, json={"message": path})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    scraper = novig.NovigScraper()
    rows = asyncio.run(scraper.scrape("nfl", market="spread"))
    assert scraper.last_transport == "nbx"
    home = next(r for r in rows if r.side == "home" and r.is_main)
    assert home.prob_raw == 0.5
    assert home.line == -3.5
    assert any(path.endswith("/emm-token") for _method, path in calls)


def test_rejected_token_names_the_secret_pair(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    monkeypatch.setenv("NOVIG_CLIENT_ID", "test-client")
    monkeypatch.setenv("NOVIG_CLIENT_SECRET", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(403, text="no")
        return httpx.Response(401, json={"error": "access_denied"})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    scraper = novig.NovigScraper()
    assert asyncio.run(scraper.scrape_with_retry("nfl")) == []
    message = scraper.fetch_errors["nfl"]
    assert "NOVIG_CLIENT_ID/NOVIG_CLIENT_SECRET" in message
    assert "test-secret" not in message


def _ladder_event() -> dict:
    markets = []
    for index in range(11):
        strike = (index - 5) - 0.5  # index 5 is the median strike, -0.5
        markets.append({
            "marketId": f"s{index}",
            "eventId": "e-lions",
            "marketType": "SPREAD",
            "strike": str(strike),
            "status": "OPEN",
            "outcomes": [
                {"outcomeId": f"h{index}", "name": f"DET {strike:+g}"},
                {"outcomeId": f"a{index}", "name": f"GB {-strike:+g}"},
            ],
        })
    return {
        "events": [{"items": [{
            "eventId": "e-lions",
            "description": "Green Bay Packers @ Detroit Lions",
            "league": "NFL",
            "status": "OPEN_PREGAME",
            "startsTs": 1792959900000,
        }]}],
        "markets": [{"items": markets}],
    }


def test_catalog_reads_the_balanced_strike_not_the_whole_ladder(monkeypatch):
    """A centered ladder is not fetched wing to wing. The public catalog 429s when it is."""
    book_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            return httpx.Response(200, json=_ladder_event()["events"][0])
        if path.endswith("/markets") and not path.endswith("/book"):
            return httpx.Response(200, json=_ladder_event()["markets"][0])
        if path.endswith("/book"):
            market_id = path.split("/")[-2]
            book_calls.append(market_id)
            index = int(market_id[1:])
            # Index 5 (strike 0) is a 50/50 book. Every other strike is a longshot.
            bid = "0.5" if index == 5 else "0.9"
            other = "0.5" if index == 5 else "0.05"
            return httpx.Response(200, json={"marketId": market_id, "orders": {
                f"h{index}": [{"price": bid}],
                f"a{index}": [{"price": other}],
            }})
        return httpx.Response(404, json={})

    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    rows = asyncio.run(novig.NovigScraper().scrape("nfl", market="spread"))
    assert len(book_calls) <= 4
    assert "s5" in book_calls
    home = next(row for row in rows if row.side == "home" and row.is_main)
    assert home.line == -0.5
    assert home.prob_raw == 0.5


def test_book_429_waits_for_retry_after(monkeypatch):
    monkeypatch.setattr(novig, "BOOK_MIN_INTERVAL_S", 0)
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            return httpx.Response(200, json=CATALOG["events"][0])
        if path.endswith("/markets") and "/book" not in path:
            return httpx.Response(200, json=CATALOG["markets"][0])
        if path.endswith("/book"):
            market_id = path.split("/")[-2]
            if market_id == "spread-main":
                seen["n"] += 1
                if seen["n"] == 1:
                    return httpx.Response(
                        429, headers={"Retry-After": "0"}, json={"code": "RATE_LIMIT_EXCEEDED"},
                    )
            book = CATALOG["books"].get(market_id)
            if book is None:
                return httpx.Response(404, json={"message": "missing"})
            return httpx.Response(200, json=book)
        return httpx.Response(500, json={"message": path})

    monkeypatch.setattr(novig.httpx, "AsyncClient", _client(handler))
    rows = asyncio.run(novig.NovigScraper().scrape("nfl", market="spread"))
    assert seen["n"] >= 2
    assert any(row.line == -3.5 and row.is_main for row in rows)
