"""Book eligibility is not subsumed by the final cross-book collapse."""
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pipeline.odds.main_select import select_main_lines
from pipeline.odds.parsers import kalshi, novig, polymarket_us, prophetx

FIX = Path(__file__).parent / "fixtures" / "raw"
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def load(book, filename):
    return json.loads((FIX / book / filename).read_text(encoding="utf-8"))


def main_sources(rows):
    return {(r.source_id, r.market, r.side, r.line) for r in rows if r.is_main}


def test_kalshi_tolerance_is_a_refusal_not_a_ranking_rule():
    payload = load("kalshi", "nfl.json")
    event = copy.deepcopy(payload["KXNFLTOTAL"][0])
    for market in event["markets"]:
        market.update(yes_bid_dollars="0.10", yes_ask_dollars="0.12")
    rows = kalshi.parse({"KXNFLTOTAL": [event]}, "nfl")
    assert rows and not any(row.is_main for row in rows)
    assert not any(row.is_main for row in select_main_lines(rows))
    assert any(row.is_main for row in select_main_lines([replace(r, is_main=True) for r in rows]))


def test_polymarket_source_id_tie_break_differs_from_smaller_line():
    payload = load("polymarket_us", "nfl.json")
    markets = payload["events"][0]["markets"]
    original = next(m for m in markets if m["sportsMarketType"] == "football_team_full_game_total")
    original.update(slug="a-larger-line", line=46.5)
    smaller = copy.deepcopy(original)
    smaller.update(slug="z-smaller-line", line=44.5)
    markets.append(smaller)
    rows = polymarket_us.parse(payload, "nfl", scraped_at=NOW)
    chosen = select_main_lines(rows)
    candidates = select_main_lines([replace(r, is_main=True) for r in rows])
    assert {r.line for r in chosen if r.market == "total" and r.is_main} == {46.5}
    assert {r.line for r in candidates if r.market == "total" and r.is_main} == {44.5}


def test_prophetx_provider_favourite_changes_final_fixture_winner():
    rows = prophetx.parse_payload(load("prophetx", "cfb.json"), "cfb", scraped_at=NOW)
    chosen = select_main_lines(rows)
    candidates = select_main_lines([replace(r, is_main=True) for r in rows])
    assert sum(a != b for a, b in zip(chosen, candidates, strict=True)) == 44


def test_novig_standalone_parser_keeps_one_pair_for_cli_and_is_idempotent():
    payload = load("novig", "catalog_nfl.json")
    rows = novig.parse(payload, "nfl", scraped_at=NOW)
    # main.py.pivot_books consumes is_main directly, without odds.merge.
    home = [r for r in rows if r.market == "spread" and r.side == "home" and r.is_main]
    assert len(home) == 1 and home[0].line == -3.5
    assert select_main_lines(rows) == rows
    expanded = [replace(row, is_main=True) for row in rows]
    assert sum(a != b for a, b in zip(rows, expanded, strict=True)) == 4
    assert select_main_lines(expanded) == rows


def test_probability_helpers_keep_keyword_callers_working():
    assert novig.prob_to_american(price=.5) == kalshi.dollar_to_american(price=.5) == -100
