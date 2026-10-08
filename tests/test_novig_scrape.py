"""Public-query transport contract, including HTTP-200 GraphQL failures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pipeline.odds import base, novig

FIX = Path(__file__).parent / "fixtures" / "raw" / "novig"


@pytest.mark.parametrize("sport,league", [("nfl", "NFL"), ("cfb", "NCAAF")])
def test_public_query_filters_and_raw_capture(monkeypatch, sport, league):
    monkeypatch.delenv("BOOK_NOVIG_ENABLED", raising=False)
    monkeypatch.setenv("BOOK_NOVIG_TRANSPORT", "httpx")
    payload = json.loads((FIX / f"{sport}_markets.json").read_text())
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["operationName"] == "HotMarkets_Query"
        assert body["query"] == novig.MARKETS_QUERY
        where = body["variables"]["where_market"]
        assert where["event"] == {
            "league": {"_eq": league}, "type": {"_eq": "Game"},
            "status": {"_in": ["OPEN_PREGAME", "OPEN_INGAME"]},
        }
        assert where["status"] == {"_eq": "OPEN"}
        assert where["type"] == {"_in": ["MONEY", "SPREAD", "TOTAL"]}
        group = "main" if where["is_consensus"]["_eq"] else "alternate"
        return httpx.Response(200, json=payload[group])

    monkeypatch.setattr(base, "_httpx_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    captured = []
    scraper = novig.NovigScraper()
    rows = asyncio.run(scraper.scrape(sport, capture=lambda *args: captured.append(args), market="spread"))
    assert len(rows) == 4
    assert sum(r.is_main for r in rows) == 2
    assert len(requests) == 2
    assert captured == [(f"novig_{sport}", payload, novig.GRAPHQL_URL)]


@pytest.mark.parametrize("response,match", [
    ({"errors": [{"message": "query is not allowed"}]}, "query is not allowed"),
    ({"data": {"market": []}, "errors": [{"message": "partial failure"}]}, "partial failure"),
    ({"data": None}, "missing data.market"),
    ({"data": {"event": []}}, "missing data.market"),
    ({"data": {"market": None}}, "missing data.market"),
])
def test_bad_response_is_not_reported_as_empty_success(monkeypatch, response, match):
    async def gql(self, client, query, variables):
        return response

    monkeypatch.setattr(novig.NovigScraper, "_gql", gql)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(novig.NovigScraper().fetch_raw("nfl"))


def test_market_limit_fails_closed(monkeypatch):
    monkeypatch.setattr(novig, "MARKET_LIMIT", 2)

    async def gql(self, client, query, variables):
        return {"data": {"market": [{"id": "a"}, {"id": "b"}]}}

    monkeypatch.setattr(novig.NovigScraper, "_gql", gql)
    with pytest.raises(RuntimeError, match="refusing truncated data"):
        asyncio.run(novig.NovigScraper().fetch_raw("cfb"))


def test_alternate_failure_does_not_publish_partial_main_data(monkeypatch):
    monkeypatch.delenv("BOOK_NOVIG_ENABLED", raising=False)
    payload = json.loads((FIX / "nfl_markets.json").read_text())

    async def gql(self, client, query, variables):
        if variables["where_market"]["is_consensus"]["_eq"]:
            return payload["main"]
        return {"errors": [{"message": "query is not allowed"}]}

    monkeypatch.setattr(novig.NovigScraper, "_gql", gql)
    captured = []
    with pytest.raises(RuntimeError, match="alternate"):
        asyncio.run(novig.NovigScraper().scrape("nfl", capture=lambda *args: captured.append(args)))
    assert captured == []


def test_empty_league_is_valid(monkeypatch):
    async def gql(self, client, query, variables):
        return {"data": {"market": []}}

    monkeypatch.setattr(novig.NovigScraper, "_gql", gql)
    monkeypatch.delenv("BOOK_NOVIG_ENABLED", raising=False)
    assert asyncio.run(novig.NovigScraper().scrape("cfb")) == []


def test_unsupported_bulk_query_is_reported_without_repeating_it(monkeypatch):
    calls = []

    async def gql(self, client, query, variables):
        calls.append(variables)
        return {"errors": [{"message": "query is not allowed"}]}

    monkeypatch.setattr(novig.NovigScraper, "_gql", gql)
    scraper = novig.NovigScraper()
    rows = asyncio.run(scraper.scrape_with_retry("nfl"))

    assert rows == []
    assert len(calls) == 1
    assert "supported bulk feed or authenticated access is required" in scraper.fetch_errors["nfl"]
