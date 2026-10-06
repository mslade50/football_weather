"""Betcris page fetch: per-page retry so one timed-out page (college-football on a
GitHub runner) does not blank the whole sport. Parser tests live in test_betcris_parse.py."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.odds import betcris as B


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path: str, *, headers=None) -> _Resp:
        self.calls += 1
        self.requests.append((path, headers))
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return _Resp(o)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_s: float) -> None:
        return None
    monkeypatch.setattr(B.asyncio, "sleep", _noop)


def test_retries_then_succeeds():
    s = B.BetcrisScraper()
    c = _Client([httpx.ReadTimeout(""), httpx.ReadTimeout(""), "<div class='oddsTable'>ok</div>"])
    html = asyncio.run(s._get_with_retry(c, "college-football", "/x/"))
    assert html is not None and "oddsTable" in html and c.calls == 3


def test_gives_up_after_attempts_and_returns_none():
    s = B.BetcrisScraper()
    c = _Client([httpx.ReadTimeout("")] * B.FETCH_ATTEMPTS)
    assert asyncio.run(s._get_with_retry(c, "college-football", "/x/")) is None
    assert c.calls == B.FETCH_ATTEMPTS
    assert s.fetch_errors == {"college-football": "ReadTimeout"}


def test_recovers_without_leaving_a_stale_failure():
    s = B.BetcrisScraper()
    bad = _Client([httpx.ConnectTimeout("")] * B.FETCH_ATTEMPTS)
    assert asyncio.run(s._get_with_retry(bad, "nfl", "/x/")) is None
    assert s.fetch_errors == {"nfl": "ConnectTimeout"}
    good = _Client(["<div class='oddsTable'>ok</div>"])
    assert asyncio.run(s._get_with_retry(good, "nfl", "/x/")) is not None
    assert s.fetch_errors == {}


@pytest.mark.parametrize("status", [401, 403])
def test_access_denial_stops_transport_retries(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.test/"))
    client = _Client([httpx.HTTPStatusError("denied", request=response.request, response=response)])
    scraper = B.BetcrisScraper()
    assert asyncio.run(scraper._get_with_retry(client, "college-football", "/x/")) is None
    assert client.calls == 1 and "denied" in scraper.fetch_errors["college-football"]


def _public_payload(sport, now, age, ttl=9000):
    data = json.loads((Path(__file__).parent / f"fixtures/raw/betcris/{sport}_public_utc.json").read_text())
    data.update(feed_fetched_at=(now - timedelta(seconds=age)).isoformat(), stale_after_seconds=ttl)
    return data


def _public_client(monkeypatch, outcomes, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    client = _Client(outcomes)
    monkeypatch.setattr(B, "datetime", Clock)
    monkeypatch.setattr(B.httpx, "AsyncClient", lambda **kwargs: client)
    return client


def test_cfb_revalidation_recovers_only_a_new_fresh_observation_and_captures_both(tmp_path, monkeypatch):
    from pipeline.outputs.raw_out import RawStore

    now = datetime(2026, 10, 2, 20, 29, tzinfo=timezone.utc)
    old = json.dumps(_public_payload("cfb", now, 3774))
    fresh = _public_payload("cfb", now, 60, ttl=1500)
    fresh["games"][0]["markets"]["total"]["under"]["price"] = -125
    new = json.dumps(fresh)
    client = _public_client(monkeypatch, [old, new], now)
    raw = RawStore("cfb", "revalidation", base_dir=tmp_path)
    scraper = B.BetcrisScraper(raw_store=raw)
    rows = asyncio.run(scraper.scrape("cfb"))
    url = B.PUBLIC_URL.format(league=2)
    assert client.requests == [(url, None), (url, {"Cache-Control": "no-cache"})]
    assert len(rows) == 12 and scraper.fetch_errors == {}
    assert all(r.source_updated_at == now - timedelta(seconds=60) for r in rows)
    assert all(r.scraped_at == r.source_updated_at and r.expires_at == r.source_updated_at + timedelta(seconds=1500)
               for r in rows)
    assert next(r for r in rows if r.market == "total" and r.side == "under").odds == -125
    assert (raw.run_dir / "betcris_public_cfb.json").read_text(encoding="utf-8") == old
    assert (raw.run_dir / "betcris_public_cfb_revalidation.json").read_text(encoding="utf-8") == new


@pytest.mark.parametrize("response_kind", ["same", "global_clock", "future", "bad_json", "unhealthy", "denied", "timeout"])
def test_cfb_revalidation_is_bounded_and_never_renews_stale_prices(monkeypatch, response_kind):
    now = datetime(2026, 10, 2, 20, 29, tzinfo=timezone.utc)
    stale = _public_payload("cfb", now, 3774)
    again = json.loads(json.dumps(stale))
    if response_kind == "global_clock":
        again.update(generated_at=now.isoformat(), last_good_at=now.isoformat())
    elif response_kind == "future":
        again["feed_fetched_at"] = (now + timedelta(seconds=1)).isoformat()
    elif response_kind == "unhealthy":
        again["feed_ok"] = False
    outcome = json.dumps(again)
    if response_kind == "bad_json":
        outcome = "not json"
    elif response_kind == "denied":
        r = httpx.Response(403, request=httpx.Request("GET", B.PUBLIC_URL.format(league=2)))
        outcome = httpx.HTTPStatusError("denied", request=r.request, response=r)
    elif response_kind == "timeout":
        outcome = httpx.ReadTimeout("")
    client = _public_client(monkeypatch, [json.dumps(stale), outcome], now)
    scraper = B.BetcrisScraper()
    assert asyncio.run(scraper.scrape("cfb")) == []
    assert client.calls == 2 and scraper.fetch_errors["college-football"]
    if response_kind in ("same", "global_clock"):
        assert "age_seconds=3774" in scraper.fetch_errors["college-football"]


@pytest.mark.parametrize("sport,age,invalid", [
    ("nfl", 3774, False), ("nfl", 60, False), ("cfb", 60, False), ("cfb", 60, True), ("cfb", 60, "future"),
])
def test_normal_nfl_and_nonstale_cfb_errors_do_not_add_requests(monkeypatch, sport, age, invalid):
    now = datetime(2026, 10, 2, 20, 29, tzinfo=timezone.utc)
    data = _public_payload(sport, now, age)
    if invalid:
        data["feed_fetched_at"] = (now + timedelta(seconds=1)).isoformat() if invalid == "future" else "invalid"
    client = _public_client(monkeypatch, [json.dumps(data)], now)
    rows = asyncio.run(B.BetcrisScraper().scrape(sport))
    assert len(rows) == (12 if age == 60 and not invalid else 0) and client.calls == 1


def test_book_failure_reaches_board_diagnostics(monkeypatch):
    from pipeline import build

    async def unavailable(self, client, slug, path):
        self.fetch_errors[slug] = "ConnectTimeout"
        return None

    monkeypatch.setattr(B.BetcrisScraper, "_get_with_retry", unavailable)
    monkeypatch.setattr(build, "load_scraper_class", lambda name: B.BetcrisScraper)
    issues = []
    result, _ = asyncio.run(build.scrape_books("cfb", ["betcris"], None, "r", lambda *args: issues.append(args)))
    assert result == {"betcris": []}
    assert any(component == "odds.betcris" and "college-football: ConnectTimeout" in reason and severity == "warn"
               for component, reason, severity in issues)


def test_timeout_is_generous_for_large_pages():
    assert B.FETCH_TIMEOUT_S >= 45.0 and B.FETCH_ATTEMPTS >= 2


@pytest.mark.parametrize("bad_feed", [False, True])
def test_public_scrape_captures_raw_and_rejects_unhealthy_feed(tmp_path, monkeypatch, bad_feed):
    from pipeline.outputs.raw_out import RawStore

    payload = json.loads((Path(__file__).parent / "fixtures/raw/betcris/cfb_public.json").read_text(encoding="utf-8"))
    if bad_feed:
        payload["feed_ok"] = False
    raw_text = json.dumps(payload)
    seen_urls = []

    async def fetch(self, client, slug, path):
        seen_urls.append(path)
        return raw_text

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 3, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(B, "datetime", Clock)
    monkeypatch.setattr(B.BetcrisScraper, "_get_with_retry", fetch)
    raw = RawStore("cfb", "test", base_dir=tmp_path)
    scraper = B.BetcrisScraper(raw_store=raw)
    lines = asyncio.run(scraper.scrape("cfb"))
    assert seen_urls == ["https://sportsbook.betcris.com/assets/odds/v1/league/2.json"]
    assert (raw.run_dir / "betcris_public_cfb.json").read_text(encoding="utf-8") == raw_text
    if bad_feed:
        assert lines == []
        assert "unavailable" in scraper.fetch_errors["college-football"]
    else:
        assert len(lines) == 12 and not scraper.fetch_errors
        assert next(ln for ln in lines if "old-dominion" in ln.game_id and ln.side == "under").odds == -107
