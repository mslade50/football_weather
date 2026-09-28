import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from pipeline.model.fair import prob_to_american
from pipeline.odds import polymarket_us as scraper
from pipeline.odds.merge import parse_provisional
from pipeline.odds.parsers.polymarket_us import parse
from pipeline.odds.teams import normalize_team

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "raw" / "polymarket_us"


def fixture(sport="nfl"):
    return json.loads((FIXTURES / f"{sport}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_real_fixture_full_game_prices_and_fees(sport):
    raw = fixture(sport)
    rows = parse(raw, sport, scraped_at=NOW)
    assert rows
    assert all(r.game_id.startswith(f"{sport}:raw:2026-09-") for r in rows)
    totals = [r for r in rows if r.market == "total"]
    assert {r.side for r in totals} == {"under", "over"}
    market = next(m for m in raw["events"][0]["markets"] if m["sportsMarketType"] == "football_team_full_game_total")
    bid, ask = float(market["bestBidQuote"]["value"]), float(market["bestAskQuote"]["value"])
    fee = market["feeCoefficient"]
    assert next(r for r in totals if r.side == "over").odds == prob_to_american(ask + fee * ask * (1-ask))
    price = 1-bid
    assert next(r for r in totals if r.side == "under").odds == prob_to_american(price + fee * price * (1-price))
    assert all(r.is_main for r in totals)
    assert len([r for r in rows if r.market == "spread"]) == 2


@pytest.mark.parametrize("name,expected", [("NM State", "new-mexico-state"),
    ("East Texas A&M", "texas-am-commerce"), ("Prairie View A&M", "prairie-view")])
def test_live_college_names_survive_provisional_ids(name, expected):
    raw = fixture("cfb")
    for market in raw["events"][0]["markets"]:
        for side in market["marketSides"]:
            if (side.get("team") or {}).get("ordering") == "away":
                side["team"]["name"] = name
    row = parse(raw, "cfb", scraped_at=NOW)[0]
    assert normalize_team("cfb", parse_provisional(row.game_id).away, "polymarket_us", fuzzy=False) == expected


@pytest.mark.parametrize("change", ["closed", "live", "started", "wrong_sport", "no_identity"])
def test_reject_non_pregame_or_unidentified_events(change):
    raw = fixture()
    e = raw["events"][0]
    if change == "closed":
        e["closed"] = True
    if change == "live":
        e["live"] = True
    if change == "started":
        e["startTime"] = "2026-09-25T12:00:00Z"
    if change == "wrong_sport":
        e["slug"] = "cfb-other"
    if change == "no_identity":
        e["markets"] = [m for m in e["markets"] if m["sportsMarketType"] != "football_team_full_game_winner"]
    assert parse(raw, "nfl", scraped_at=NOW) == []


@pytest.mark.parametrize("change", ["derivative", "crossed", "missing_fee", "integer", "halted", "missing_ask"])
def test_fail_closed_on_noncomparable_quotes(change):
    raw = fixture()
    m = raw["events"][0]["markets"][0]
    assert m["sportsMarketType"] == "football_team_full_game_total"
    if change == "derivative":
        m["sportsMarketType"] = "football_team_points_full_game_total"
    if change == "crossed":
        m["bestBidQuote"]["value"] = "0.99"
    if change == "missing_fee":
        m["feeCoefficient"] = None
    if change == "integer":
        m["line"] = 46
    if change == "halted":
        m["ep3Status"] = "HALTED"
    if change == "missing_ask":
        m["bestAskQuote"] = None
    assert not any(r.market == "total" for r in parse(raw, "nfl", scraped_at=NOW))


def test_pagination_raw_capture_and_no_silent_truncation(monkeypatch):
    raw = fixture()
    calls, captures = [], []
    second = copy.deepcopy(raw["events"][0])
    second["id"] = "second"
    pages = [raw, {"events": [second]}, {"events": []}]
    def handler(request):
        calls.append(int(request.url.params["offset"]))
        return httpx.Response(200, json=pages[len(calls)-1])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(scraper, "PAGE_SIZE", 1)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(scraper.PolymarketUSScraper().scrape("nfl", capture=lambda *args: captures.append(args)))
    assert calls == [0, 1, 2] and len(captures) == 3
    assert json.loads(captures[0][1]) == raw
    calls.clear()
    captures.clear()
    pages[1] = raw
    with pytest.raises(ValueError, match="repeated"):
        asyncio.run(scraper.PolymarketUSScraper().scrape("nfl", capture=lambda *args: captures.append(args)))
    assert len(captures) == 2


def test_overlapping_pages_keep_new_events_and_latest_quotes(monkeypatch):
    first = fixture()["events"][0]
    second = copy.deepcopy(first)
    second["id"] = "second"
    updated = copy.deepcopy(second)
    updated["title"] = "latest snapshot"
    third = copy.deepcopy(first)
    third["id"] = "third"
    pages = [[first, second], [updated, third], [third]]
    offsets = []
    def handler(request):
        offsets.append(int(request.url.params["offset"]))
        return httpx.Response(200, json={"events": pages[len(offsets)-1]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(scraper, "PAGE_SIZE", 2)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    received = []
    monkeypatch.setattr(scraper.parser, "parse", lambda payload, *args, **kw: received.extend(payload["events"]) or [])
    asyncio.run(scraper.PolymarketUSScraper().scrape("nfl"))
    assert offsets == [0, 2, 4]
    assert [e["id"] for e in received] == [first["id"], "second", "third"]
    assert received[1]["title"] == "latest snapshot"


@pytest.mark.parametrize("missing_id", [None, ""])
def test_pagination_rejects_unidentifiable_events(monkeypatch, missing_id):
    raw = fixture()
    raw["events"][0]["id"] = missing_id
    real_client = httpx.AsyncClient
    monkeypatch.setattr(scraper.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)), **kw))
    with pytest.raises(ValueError, match="omitted event IDs"):
        asyncio.run(scraper.PolymarketUSScraper().scrape("nfl"))


def test_pagination_limit_does_not_publish_partial_slate(monkeypatch):
    def handler(request):
        raw = fixture()
        raw["events"][0]["id"] = request.url.params["offset"]
        return httpx.Response(200, json=raw)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(scraper, "PAGE_SIZE", 1)
    monkeypatch.setattr(scraper, "MAX_PAGES", 2)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match="safety limit"):
        asyncio.run(scraper.PolymarketUSScraper().scrape("nfl"))
