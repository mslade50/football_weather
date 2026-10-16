"""Shared odds transport policy, zero-event detection and BetCRIS staleness diagnostics (no network)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.async_api", MagicMock())
sys.modules.setdefault("playwright_stealth", MagicMock())

from pipeline.odds import base, pinnacle  # noqa: E402
from pipeline.odds import betcris as BC  # noqa: E402
from tests.test_betcris_fetch import _public_client, _public_payload  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_s: float) -> None:
        return None
    monkeypatch.setattr(base.asyncio, "sleep", _noop)


def _client(handler, **kw):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


def _run(coro):
    return asyncio.run(coro)


def test_retries_5xx_then_succeeds_and_keeps_headers():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("x-test"))
        return httpx.Response(503 if len(seen) < 3 else 200, json={"ok": True})

    async def go():
        async with _client(handler, headers={"x-test": "kept"}) as c:
            return await base.get_with_retry(c, "https://e.test/a", label="t")

    assert _run(go()).json() == {"ok": True}
    assert seen == ["kept"] * 3


def test_retries_timeout_and_429():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("t", request=req)
        return httpx.Response(429 if len(calls) == 2 else 200, json={})

    async def go():
        async with _client(handler) as c:
            return await base.get_with_retry(c, "https://e.test/a", label="t")

    assert _run(go()).status_code == 200 and len(calls) == 3


@pytest.mark.parametrize("status", [401, 403, 404])
def test_does_not_retry_other_4xx(status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status)

    async def go():
        async with _client(handler) as c:
            return await base.get_with_retry(c, "https://e.test/a", label="t")

    with pytest.raises(httpx.HTTPStatusError):
        _run(go())
    assert len(calls) == 1


def test_gives_up_after_attempts_on_5xx():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    async def go():
        async with _client(handler) as c:
            return await base.get_with_retry(c, "https://e.test/a", label="t", attempts=3)

    with pytest.raises(httpx.HTTPStatusError):
        _run(go())
    assert len(calls) == 3


def test_raise_status_false_returns_final_response():
    async def go():
        async with _client(lambda r: httpx.Response(401)) as c:
            return await base.get_with_retry(c, "https://e.test/a", label="t", raise_status=False)

    assert _run(go()).status_code == 401


def test_pinnacle_zero_events_in_season_records_fetch_error(monkeypatch):
    payload = {"matchups": [{"id": 1, "type": "matchup", "league": {"name": "Tennis"}}] * 214, "markets": []}

    async def fake_feed(client=None):
        return payload

    monkeypatch.setattr(pinnacle, "fetch_feed", fake_feed)
    monkeypatch.setattr(pinnacle, "in_season", lambda sport, now=None: True)
    s = pinnacle.PinnacleScraper()
    assert _run(s.scrape("nfl")) == []
    assert s.fetch_errors["nfl"] == "schema drift? 0 NFL events in 214 total"
    monkeypatch.setattr(pinnacle, "in_season", lambda sport, now=None: False)
    _run(s.scrape("nfl"))
    assert "nfl" not in s.fetch_errors


def test_betonline_all_leagues_dark_in_season_records_fetch_error(monkeypatch):
    from pipeline.odds import betonline as BO
    from tests.test_betonline_fetch import _Page

    monkeypatch.setattr(BO, "in_season", lambda sport, now=None: True)
    s = BO.BetOnlineScraper()
    assert _run(s.fetch_leagues(_Page(data={"GameOffering": []}), "nfl")) == {}
    assert "schema drift?" in s.fetch_errors["nfl"]


def test_betcris_stale_feed_reports_age_and_logs_it(monkeypatch, caplog):
    now = datetime(2026, 10, 2, 20, 29, tzinfo=timezone.utc)
    stale = json.dumps(_public_payload("nfl", now, 339, ttl=300))
    _public_client(monkeypatch, [stale], now)
    s = BC.BetcrisScraper()
    with caplog.at_level(logging.INFO):
        assert _run(s.scrape("nfl")) == []
    err = s.fetch_errors[BC.PAGES["nfl"][0]]
    assert "age 339s" in err and "age_seconds=339" in err
    assert any("age 339s vs wall clock" in r.getMessage() for r in caplog.records)
