"""Observable request budgets, pacing and fatal failures of shared retries."""
import asyncio

import httpx
import pytest

from pipeline.odds import base, betcris, novig


@pytest.mark.parametrize("retry_after,delay", [("30", 8.0), ("-2", 0.0), ("invalid", 1.0), ("0", 0.0), (None, 1.0)])
def test_novig_persistent_throttle_preserves_budget_and_pacing(monkeypatch, retry_after, delay):
    calls, sleeps, paced = [], [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def pace():
        paced.append(len(calls))

    def handler(request):
        calls.append(request)
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(429, headers=headers)

    scraper = novig.NovigScraper()
    monkeypatch.setattr(scraper, "_pace_book", pace)
    monkeypatch.setattr(base.asyncio, "sleep", sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await scraper._book_json(client, "missing")

    assert asyncio.run(run()) == {"marketId": "missing", "orders": {}}
    assert len(calls) == novig.BOOK_429_ATTEMPTS == 8
    assert sleeps == [delay] * 7
    assert paced == list(range(8))
    assert all(request.url.params["depth"] == "1" for request in calls)


@pytest.mark.parametrize("status", [404, 503])
def test_novig_other_statuses_are_never_book_retries(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    scraper = novig.NovigScraper()
    scraper.min_interval = 0

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await scraper._book_json(client, "missing")

    if status == 404:
        assert asyncio.run(run()) == {"marketId": "missing", "orders": {}}
    else:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())
    assert len(calls) == 1


def test_betcris_backoff_and_non_http_exception_retries(monkeypatch):
    calls, sleeps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    class Client:
        async def get(self, url):
            calls.append(url)
            if len(calls) < 3:
                raise ValueError("transient get failure")
            return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(base.asyncio, "sleep", sleep)
    monkeypatch.setattr(base.random, "random", lambda: .5)
    scraper = betcris.BetcrisScraper()
    assert asyncio.run(scraper._get_with_retry(Client(), "nfl", "https://example.test")) == "ok"
    assert len(calls) == 3 and sleeps == [2.0, 4.0]
    assert scraper.fetch_errors == {}


def test_fatal_marker_stops_shared_scrape_retry():
    class Scraper(base.BaseScraper):
        calls = 0

        async def scrape(self, sport, market=None, **kwargs):
            self.calls += 1
            raise base.FatalScrapeError("catalog rejected")

    scraper = Scraper()
    assert asyncio.run(scraper.scrape_with_retry("nfl")) == []
    assert scraper.calls == 1
    assert scraper.fetch_errors == {"nfl": "catalog rejected"}
