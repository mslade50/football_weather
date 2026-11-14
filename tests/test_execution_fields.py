"""Synthetic catalog evidence only; no network, credentials, publishing or orders."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from pipeline.contracts import Game, Stadium, WeatherForecast
from pipeline.model.total_prices import compare_totals
from pipeline.odds.execution_evidence import publish_evidence, settlement
from pipeline.odds.merge import _flip_line, _rekey
from pipeline.odds.parsers import kalshi, novig, polymarket_us
from pipeline.outputs import json_out

NOW = datetime(2030, 9, 1, 12, tzinfo=timezone.utc)
KICK = NOW + timedelta(hours=5)
GID = "nfl:2030:1:synthetic-away@synthetic-home"
GAME = Game(GID, "nfl", 2030, 1, KICK, KICK, "UTC", "synthetic-home", "synthetic-away", None)
PROCESS = {"eligible": True, "discovery_state": "signal", "discovery_reasons": ["wind_signal"],
           "evaluated_at": NOW.isoformat()}
FAIR = SimpleNamespace(fair_total=46.5, total=SimpleNamespace(thin=False, prob=.5), edges=[])


def kalshi_payload():
    return {"KXNFLTOTAL": {"received_at": NOW.isoformat(),
        "event_observed_at": {"KXNFLTOTAL-30SEP01AAABBB": NOW.isoformat()},
        "fee_metadata": {"series": {"fee_type": "quadratic", "fee_multiplier": "1.2"}},
        "events": [{"event_ticker": "KXNFLTOTAL-30SEP01AAABBB", "series_ticker": "KXNFLTOTAL",
                    "sub_title": "AAA vs BBB", "title": "Synthetic Away vs Synthetic Home: Total",
                    "markets": [{"ticker": "KXNFLTOTAL-30SEP01AAABBB-47", "floor_strike": 46.5,
                        "yes_bid_dollars": "0.4321", "yes_ask_dollars": "0.4621",
                        "rules_primary": "Overtime is included. Exactly the line is a push.",
                        "rules_secondary": "Postponement beyond two days voids this contract.",
                        "volume": 99999, "open_interest": 99999}]}]}}


def novig_payload(source="public"):
    event = {"eventId": "synthetic-event", "description": "Synthetic Away @ Synthetic Home",
             "status": "OPEN_PREGAME", "league": "NFL", "startsTs": int(KICK.timestamp()*1000)}
    market = {"marketId": "synthetic-market", "eventId": event["eventId"], "marketType": "TOTAL",
              "strike": "46.5", "status": "OPEN", "voids": "FMV",
              "fee": {"coefficient": ".03", "charged": "WHEN_LIVE"},
              "outcomes": [{"outcomeId": "synthetic-under", "name": "Under 46.5"},
                           {"outcomeId": "synthetic-over", "name": "Over 46.5"}]}
    book = {"marketId": market["marketId"], "seq": 7,
            "orders": {"synthetic-over": [{"price": ".45123", "qty": 100}, {"price": ".45123", "qty": 50}],
                       "synthetic-under": [{"price": ".51", "qty": 75}]}}
    if source == "public":
        return {"source": source, "events": [{"items": [event]}], "markets": [{"items": [market]}],
                "books": {market["marketId"]: book}, "book_observed_at": {market["marketId"]: NOW.isoformat()}}
    nbx_event = {"id": event["eventId"], "status": event["status"],
                 "game": {"scheduledStart": KICK.isoformat(), "homeTeam": {"name": "Synthetic Home"},
                          "awayTeam": {"name": "Synthetic Away"}}}
    nbx_market = {**market, "id": market["marketId"], "type": "TOTAL", "event": nbx_event,
                  "outcomes": [{"id": "synthetic-under", "type": "Under"},
                               {"id": "synthetic-over", "type": "Over"}]}
    return {"source": "nbx", "open_markets": {"TOTAL": [nbx_market]},
            "event_books": {event["eventId"]: [{"id": market["marketId"], "book": book}]},
            "event_observed_at": {event["eventId"]: NOW.isoformat()}}


def poly_payload():
    return {"observed_at": {"synthetic-event": NOW.isoformat()}, "events": [{
        "id": "synthetic-event", "slug": "nfl-synthetic-event", "active": True, "startTime": KICK.isoformat(),
        "markets": [{"id": "synthetic-total", "slug": "synthetic-total-slug", "active": True,
            "ep3Status": "OPEN", "sportsMarketType": "football_team_full_game_total", "line": 46.5,
            "feeCoefficient": ".025", "description": "Overtime is included. A cancelled game is void.",
            "bestBidQuote": {"value": ".4321", "currency": "USD"},
            "bestAskQuote": {"value": ".4621", "currency": "USD"},
            "marketSides": [{"description": "Over", "long": True, "tradable": True, "tokenId": "synthetic-over"},
                            {"description": "Under", "long": False, "tradable": True, "tokenId": "synthetic-under"}]},
            {"sportsMarketType": "football_team_full_game_winner", "marketSides": [
                {"team": {"ordering": "home", "name": "Synthetic Home"}},
                {"team": {"ordering": "away", "name": "Synthetic Away"}}]}]}]}


def quotes(parser, payload):
    before = deepcopy(payload)
    rows = parser.parse(payload, "nfl", scraped_at=NOW)
    assert payload == before
    assert len(rows) == 2
    return {r.side: r for r in rows}


def test_kalshi_yes_book_price_is_bid_for_under_never_rounded_odds_or_volume():
    rows = quotes(kalshi, kalshi_payload())
    under = publish_evidence(_rekey(rows["under"], GID), GAME, PROCESS)
    assert under["native_quote"]["native_price"] == "0.4321"
    assert under["native_quote"]["selected_side_ask"] == "0.5679"
    assert under["native_quote"]["native_quantity"] is None
    assert under["identity"]["native_side"] == "no"
    assert under["identity"]["outcome_id"] == "no"
    assert under["fee"]["coefficient"] == "0.084"
    assert under["fee"]["rounding_micros"] == "100"
    assert under["native_quote"]["observed_at"] == NOW.isoformat()
    assert under["binding"]["game_id"] == GID
    assert under["binding"]["home_team_id"] == GAME.home_id
    assert under["binding"]["line"] == "46.5"
    assert under["settlement"]["rules_sha256"] == sha256(under["settlement"]["rules"].encode()).hexdigest()
    assert all(t["value"] for t in under["settlement"]["treatments"].values())
    assert under["settlement"]["settlement_verified"] is False
    assert under["eligibility"]["current_signal"] is True
    assert publish_evidence(rows["over"], GAME, PROCESS)["eligibility"]["current_signal"] is False
    assert publish_evidence(rows["over"], GAME, PROCESS)["native_quote"]["native_price"] == "0.4621"


@pytest.mark.parametrize("source", ["public", "nbx"])
def test_novig_opposing_bid_quantity_and_fee_phase_survive_parsing(source):
    rows = quotes(novig, novig_payload(source))
    ev = rows["under"].execution_evidence
    assert ev["native_quote"]["native_price"] == "0.54877"
    assert ev["native_quote"]["native_quantity"] == "150"
    assert ev["native_quote"]["quantity_unit"] == "one_cent_contracts"
    assert ev["native_quote"]["observed_at"] == NOW.isoformat()
    assert ev["identity"]["outcome_id"] == "synthetic-under"
    assert ev["fee"]["coefficient"] == "0"
    assert ev["fee"]["rounding_micros"] == "10"
    assert ev["settlement"]["treatments"]["void"]["value"] == "FMV"
    assert ev["settlement"]["treatments"]["overtime"]["value"] is None


@pytest.mark.parametrize("status,charged,expected", [("OPEN_INGAME", "WHEN_LIVE", None),
    ("OPEN_PREGAME", "ALWAYS", "0.03"), ("OPEN_PREGAME", "UNKNOWN", None)])
def test_novig_never_infers_free_fee_for_unknown_phase_or_schedule(status, charged, expected):
    payload = novig_payload()
    payload["events"][0]["items"][0]["status"] = status
    payload["markets"][0]["items"][0]["fee"]["charged"] = charged
    assert quotes(novig, payload)["under"].execution_evidence["fee"]["coefficient"] == expected


def test_polymarket_us_exact_slug_token_no_price_and_missing_size():
    rows = quotes(polymarket_us, poly_payload())
    ev = publish_evidence(rows["under"], GAME, PROCESS)
    assert ev["identity"]["market_slug"] == "synthetic-total-slug"
    assert ev["identity"]["outcome_id"] == "synthetic-under"
    assert ev["identity"]["native_side"] == "no"
    assert ev["native_quote"]["native_price"] == "0.4321"
    assert ev["native_quote"]["selected_side_ask"] == "0.5679"
    assert ev["native_quote"]["native_quantity"] is None
    assert ev["fee"]["coefficient"] == "0.025"
    assert ev["fee"]["rounding_rule"] == "usd_cent_half_even"
    assert ev["settlement"]["treatments"]["overtime"]["value"] == "Overtime is included."
    assert "settlement.treatments.postponement" in ev["missing"]


def test_missing_token_bad_size_and_mismatched_book_are_not_certified():
    payload = poly_payload()
    del payload["events"][0]["markets"][0]["marketSides"][1]["tokenId"]
    ev = publish_evidence(quotes(polymarket_us, payload)["under"], GAME, PROCESS)
    assert ev["identity"]["outcome_id"] is None
    assert "identity.outcome_id" in ev["missing"]
    payload = novig_payload()
    payload["books"]["synthetic-market"]["orders"]["synthetic-over"][0]["qty"] = "1.5"
    ev = quotes(novig, payload)["under"].execution_evidence
    assert ev["native_quote"]["native_quantity"] is None
    payload["books"]["synthetic-market"]["marketId"] = "different-market"
    ev = quotes(novig, payload)["under"].execution_evidence
    assert ev["native_quote"]["native_price"] is None
    assert ev["native_quote"]["observed_at"] is None


def test_collection_clock_is_per_kalshi_page_not_series_completion(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from pipeline.odds import kalshi as collector

    stamps = iter([NOW, NOW + timedelta(minutes=1)])
    monkeypatch.setattr(collector, "datetime", SimpleNamespace(now=lambda tz: next(stamps)))
    monkeypatch.setattr(collector, "PAGE_LIMIT", 1)
    pages = [{"events": [{"event_ticker": "synthetic-first"}], "cursor": "next"},
             {"events": [{"event_ticker": "synthetic-second"}]}]
    monkeypatch.setattr(collector, "get_with_retry", AsyncMock(side_effect=[
        SimpleNamespace(json=lambda page=page: page) for page in pages]))
    scraper = collector.KalshiScraper()
    asyncio.run(scraper._fetch_all_events(None, "synthetic-series"))
    assert scraper.event_observed_at == {"synthetic-first": NOW.isoformat(),
                                        "synthetic-second": (NOW+timedelta(minutes=1)).isoformat()}


@pytest.mark.parametrize("state", ["moved", "near", "unknown", "ineligible", "quiet"])
def test_priced_candidate_and_alert_are_not_a_current_signal(state):
    row = quotes(novig, novig_payload())["under"]
    ev = publish_evidence(row, GAME, {**PROCESS, "candidate": True, "alert": True, "discovery_state": state})
    assert ev["eligibility"]["current_signal"] is False


def test_old_missing_receipts_metadata_and_rules_fail_closed():
    payload = kalshi_payload()
    del payload["KXNFLTOTAL"]["received_at"]
    # Historical captures have no fee attestation: keep prices, omit effective fee evidence.
    payload["KXNFLTOTAL"] = payload["KXNFLTOTAL"]["events"]
    rows = quotes(kalshi, payload)
    ev = publish_evidence(rows["under"], GAME, PROCESS)
    assert ev["fee"]["coefficient"] is None
    assert ev["native_quote"]["observed_at"] is None
    assert "fee.coefficient" in ev["missing"]
    legacy = replace(rows["under"], execution_evidence={})
    assert publish_evidence(legacy, GAME, PROCESS)["missing"]["evidence"].startswith("legacy_or_retained")
    assert settlement({}, rules=None, source="synthetic")["rules_sha256"] is None
    url = "https://synthetic.invalid/rules"
    policy = settlement({}, url=url, source="synthetic")
    assert policy["rules_url_sha256"] == sha256(url.encode()).hexdigest()
    assert policy["rules_sha256"] is None  # A URL hash does not hash a fetched policy.


def test_rekey_flip_and_carry_keep_native_receipts_and_probabilities():
    from pipeline.build import carry_forward_lines

    row = quotes(novig, novig_payload())["under"]
    canonical = _rekey(row, GID)
    assert _flip_line(row, GID).execution_evidence == canonical.execution_evidence
    archive = {"last": {canonical.key: {"line": row.line, "odds": row.odds, "ts": NOW.isoformat(),
        "available": True, "source_id": row.source_id, "outcome_ids": row.outcome_ids,
        "execution_evidence": row.execution_evidence}}}
    carried = carry_forward_lines(archive, "nfl", {GID}, [], now=NOW+timedelta(minutes=10))
    assert carried[0].execution_evidence == row.execution_evidence
    enriched = compare_totals("nfl", [canonical], FAIR, now=NOW, game=GAME, process=PROCESS)
    original = compare_totals("nfl", [canonical], FAIR, now=NOW)
    assert enriched["probability_status"] == "empirically_unvalidated"
    assert {k: v for k, v in enriched["quotes"][0].items() if k != "execution"} == original["quotes"][0]


def test_network_free_local_build_and_sealed_manifest(tmp_path, capsys):
    """Actual output stage dry build in pytest's disposable directory; never publish."""
    import json

    from pipeline.build import OddsResult, SportResult, write_outputs
    from pipeline.publication import prepare_generation, verify_generation
    from pipeline.run_context import RunContext

    rows = [_rekey(r, GID) for r in quotes(novig, novig_payload()).values()]
    stadium = Stadium("synthetic", "Synthetic stadium", 1, 1, roof_type="open")
    fc = WeatherForecast(GID, "synthetic", temp_fg=40, wind_fg=20, rain_fg_mm=0)
    card = json_out.build_card("nfl", GAME, stadium, None, None, fc, None,
        SimpleNamespace(label="Low Impact", color="blue", drivers=["wind"]), [],
        lines=rows, fair=FAIR, run_id="synthetic-run", priced_at=NOW)
    assert card["total_prices"]["quotes"][0]["execution"]["schema_version"] == 1
    assert all(q["execution"]["eligibility"]["current_signal"] == (
        card["process"]["discovery_state"] == "signal" and q["side"] == "under")
        for q in card["total_prices"]["quotes"])
    ctx = RunContext("all", started_at=NOW, run_id="synthetic-run", git_sha="a"*40)
    result = SportResult("nfl", rows=[], games=[GAME], cards=[card], stadiums={}, teams={}, forecasts={}, impacts={},
                         odds=OddsResult(rows, {}, {}, {GID: rows}, [], {}))
    paths = write_outputs(ctx, [result], ["novig"], board_dir=tmp_path/"board",
        snapshot_dir=tmp_path/"snapshots", state_dir=tmp_path/"state", d1_sql=tmp_path/"archive.sql",
        raw_files={}, finished_at=NOW)
    sealed, pointer = prepare_generation(paths, tmp_path/"publication")
    manifest = verify_generation(json.loads(pointer.read_bytes()),
                                 lambda key: sealed[key].read_bytes() if key in sealed else None)
    assert manifest["run_id"] == "synthetic-run" and manifest["git_sha"] == "a"*40
    assert manifest["execution_fields"]["coverage"] == {
        "nfl": {"quotes": 2, "with_execution_v1": 2}, "cfb": {"quotes": 0, "with_execution_v1": 0}}
    assert list(paths)[-1] == "board/meta.json"
    games = next(path for key, path in sealed.items() if key.endswith("games_nfl.json"))
    assert json.loads(games.read_bytes())["games"][0]["execution_markets"] == card["execution_markets"]
    # No stdout from the build (game identity or paths) is needed in test output.
    capsys.readouterr()
