"""BetOnline: when every in-page league fetch fails (Cloudflare pass didn't take on the
runner), fetch_leagues must raise so the base retry re-launches the browser instead of
reporting a clean 0 lines. Parser tests live in test_betonline_parse.py."""
from __future__ import annotations

import asyncio

import pytest

from pipeline.odds import betonline as B


class _Page:
    def __init__(self, fail_slugs: set[str] | None = None, data: dict | None = None) -> None:
        self.fail = fail_slugs or set()
        self.data = data or {"GameOffering": [{"x": 1}]}

    async def evaluate(self, js: str, args: list) -> dict:
        import json
        slug = json.loads(args[1])["League"]
        if slug in self.fail or "*" in self.fail:
            raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
        return self.data


def test_all_leagues_failing_raises_for_retry():
    s = B.BetOnlineScraper()
    with pytest.raises(RuntimeError, match="all .* league fetches failed"):
        asyncio.run(s.fetch_leagues(_Page({"*"}), "cfb"))


def test_partial_failure_keeps_the_good_leagues():
    s = B.BetOnlineScraper()
    slugs = B.LEAGUES["nfl"]
    out = asyncio.run(s.fetch_leagues(_Page({slugs[0]}), "nfl"))
    assert set(out) == set(slugs[1:]) if len(slugs) > 1 else out == {}


def test_settle_wait_is_not_too_short():
    assert B.PAGE_SETTLE_MS >= 5000
