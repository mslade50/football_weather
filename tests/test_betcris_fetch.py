"""Betcris page fetch: per-page retry so one timed-out page (college-football on a
GitHub runner) does not blank the whole sport. Parser tests live in test_betcris_parse.py."""
from __future__ import annotations

import asyncio
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

    async def get(self, path: str) -> _Resp:
        self.calls += 1
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


def test_timeout_is_generous_for_large_pages():
    assert B.FETCH_TIMEOUT_S >= 45.0 and B.FETCH_ATTEMPTS >= 2
