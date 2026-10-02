"""Betcris page fetch: per-page retry so one timed-out page (college-football on a
GitHub runner) does not blank the whole sport. Parser tests live in test_betcris_parse.py."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
    assert s.fetch_errors == {"college-football": "ReadTimeout"}


def test_recovers_without_leaving_a_stale_failure():
    s = B.BetcrisScraper()
    bad = _Client([httpx.ConnectTimeout("")] * B.FETCH_ATTEMPTS)
    assert asyncio.run(s._get_with_retry(bad, "nfl", "/x/")) is None
    assert s.fetch_errors == {"nfl": "ConnectTimeout"}
    good = _Client(["<div class='oddsTable'>ok</div>"])
    assert asyncio.run(s._get_with_retry(good, "nfl", "/x/")) is not None
    assert s.fetch_errors == {}


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
    catalog = {"version": 1, "feed_ok": True, "generated_at": payload["feed_fetched_at"],
               "leagues": [{"id": 2, "path": "league/2.json", "hash": "0" * 16,
                            "name": "COLLEGE FOOTBALL", "sport": "FOOTBALL",
                            "stale_after_seconds": payload["stale_after_seconds"]}]}
    seen_urls = []

    async def fetch(self, client, slug, path):
        seen_urls.append(path)
        return json.dumps(catalog) if path == B.PUBLIC_INDEX_URL else raw_text

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 3, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(B, "datetime", Clock)
    monkeypatch.setattr(B.BetcrisScraper, "_get_with_retry", fetch)
    raw = RawStore("cfb", "test", base_dir=tmp_path)
    scraper = B.BetcrisScraper(raw_store=raw)
    lines = asyncio.run(scraper.scrape("cfb"))
    assert seen_urls == [B.PUBLIC_INDEX_URL, B.PUBLIC_URL.format(league=2) + "?v=" + "0" * 16]
    assert json.loads((raw.run_dir / "betcris_public_index_cfb.json").read_text()) == catalog
    assert (raw.run_dir / "betcris_public_cfb.json").read_text(encoding="utf-8") == raw_text
    if bad_feed:
        assert lines == []
        assert "unavailable" in scraper.fetch_errors["college-football"]
    else:
        assert len(lines) == 12 and not scraper.fetch_errors
        assert next(ln for ln in lines if "old-dominion" in ln.game_id and ln.side == "under").odds == -107
