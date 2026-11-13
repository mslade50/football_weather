"""Offline checks at the pipeline-to-scraper boundary."""
import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import main
from pipeline import build
from pipeline.odds.base import BaseScraper
from pipeline.outputs.raw_out import RawStore

FIXTURES = Path(__file__).parent / "fixtures" / "raw"


@pytest.mark.parametrize("book", build.BOOK_ORDER)
def test_books_inherit_one_keyword_only_constructor_and_scrape_signature(book):
    cls = build.load_scraper_class(book)
    assert cls.__init__ is BaseScraper.__init__
    assert inspect.signature(cls.scrape, eval_str=True) == inspect.signature(BaseScraper.scrape, eval_str=True)
    raw = object()
    scraper = cls(headless=False, raw_store=raw, run_id="run-1", season=2026, timeout=7.5)
    assert (scraper.headless, scraper.raw_store, scraper.run_id, scraper.season, scraper.timeout) == (
        False, raw, "run-1", 2026, 7.5)
    assert cls().timeout == (15.0 if book == "kalshi" else 30.0)
    with pytest.raises(TypeError):
        cls(False)


@pytest.mark.parametrize("book", ["kalshi", "novig", "polymarket_us"])
def test_pipeline_captures_api_books_and_stamps_run_id(book, tmp_path, monkeypatch):
    cls = build.load_scraper_class(book)
    module = __import__(cls.__module__, fromlist=["_"])
    monkeypatch.setenv(f"BOOK_{book.upper()}_ENABLED", "1")
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", FixtureClock)
    raw = RawStore("nfl", "run-1", tmp_path)
    if book in ("kalshi", "novig"):
        filename = "catalog_nfl.json" if book == "novig" else "nfl.json"
        payload = json.loads((FIXTURES / book / filename).read_text(encoding="utf-8"))
        if book == "kalshi":
            payload = {key: {"events": rows} for key, rows in payload.items()}

        async def fetch_raw(self, sport, market=None):
            return payload

        monkeypatch.setattr(cls, "fetch_raw", fetch_raw)
        source = f"{book}_nfl"
    else:
        payload = json.loads((FIXTURES / book / "nfl.json").read_text(encoding="utf-8"))
        real_client = httpx.AsyncClient
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kwargs))
        source = "polymarket_us_nfl_0"

    degradations = []
    rows, scrapers = asyncio.run(build.scrape_books(
        "nfl", [book], raw, "run-1", lambda *args, **kw: degradations.append(args)))
    assert not degradations and set(scrapers) == {book}
    assert rows[book] and all(row.run_id == "run-1" for row in rows[book])
    assert json.loads((raw.run_dir / raw.manifest[source]["path"]).read_text(encoding="utf-8")) == payload


def test_constructor_type_error_is_reported_once_by_pipeline_and_cli():
    class Broken:
        calls = 0

        def __init__(self, **kwargs):
            Broken.calls += 1
            raise TypeError("constructor bug")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(build, "load_scraper_class", lambda name: Broken)
        degradations = []
        assert asyncio.run(build.scrape_books(
            "nfl", ["broken"], None, "run-1", lambda *args, **kw: degradations.append(args))) == ({}, {})
    assert Broken.calls == 1
    assert degradations == [("odds", "nfl: broken init failed: TypeError: constructor bug", "warn")]
    with pytest.raises(TypeError, match="constructor bug"):
        asyncio.run(main.run_scraper("broken", Broken, "nfl", None, True))
    assert Broken.calls == 2


def test_scrape_type_error_does_not_restart_the_book():
    class Broken:
        calls = 0

        async def scrape_with_retry(self, sport, market=None):
            self.calls += 1
            raise TypeError("scrape bug")

    scraper = Broken()
    with pytest.raises(TypeError, match="scrape bug"):
        asyncio.run(build._run_book(scraper, "nfl"))
    assert scraper.calls == 1
