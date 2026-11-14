"""Lossless, read-only evidence from the catalogs the collectors already fetch.

No network calls, price reconstruction from American odds, or settlement defaults.
Native prices are decimal strings: YES-book limits for Kalshi/US, selected
outcome probabilities for Novig. Selected-side cost is published separately.
"""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pipeline.contracts import Game, GameLine

VERSION = 1


def decimal(value: Any, *, probability: bool = False) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        return None
    if not n.is_finite() or n < 0 or (probability and not 0 < n < 1):
        return None
    return format(n, "f")


def complement(value: Any) -> str | None:
    p = decimal(value, probability=True)
    return format(1 - Decimal(p), "f") if p is not None else None


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def clock(value: Any) -> str | None:
    from utils.timeutil import parse_iso_utc

    if isinstance(value, datetime):
        return value.isoformat() if value.tzinfo is not None else None
    # Naive provider clocks cannot certify a UTC observation.
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value):
        return None
    at = parse_iso_utc(value)
    return at.isoformat() if at is not None else None


def settlement(market: dict, *, rules: Any = None, url: Any = None,
               source: str) -> dict:
    rules, url = text(rules), text(url)
    terms = {}
    for name, pattern in {"push": r"\b(push|tie|tied|exactly)\b", "void": r"\b(void\w*|cancel\w*)\b",
                          "overtime": r"\b(overtime|extra time)\b",
                          "postponement": r"\b(postpon\w*|reschedul\w*|delay\w*)\b"}.items():
        # Verbatim clauses are evidence, not an interpretation or a completeness claim.
        clauses = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n", rules or "")
                   if re.search(pattern, part, re.I)]
        terms[name] = {"value": "\n".join(clauses) or None, "source": source if clauses else None,
                       "reason": None if clauses else "not_explicit_in_collected_rules"}
    if market.get("voids") in ("FMV", "PUSH"):
        terms["void"] = {"value": market["voids"], "source": source + ".voids", "reason": None}
    return {"rules": rules, "rules_url": url,
            "rules_sha256": hashlib.sha256(rules.encode()).hexdigest() if rules else None,
            "rules_url_sha256": hashlib.sha256(url.encode()).hexdigest() if url else None,
            "source": source, "treatments": terms,
            "settlement_verified": False,
            "reason": "catalog_evidence_only_requires_independent_policy_verification"}


def kalshi_evidence(event: dict, market: dict, envelope: dict, side: str) -> dict:
    from pipeline.odds.parsers.kalshi import series_fee_coefficient

    yes = side == "over"
    selected = market.get("yes_ask_dollars") if yes else complement(market.get("yes_bid_dollars"))
    # Nested market catalogs generally have no sizes. Never use volume/open interest.
    coefficient = series_fee_coefficient(envelope.get("fee_metadata"))
    parts = [text(market.get(k)) for k in ("rules_primary", "rules_secondary")]
    rules = "\n".join(p for p in parts if p) or None
    return {"identity": {"event_id": text(event.get("event_ticker")),
                         "market_id": text(market.get("ticker")), "outcome_id": "yes" if yes else "no",
                         "native_side": "yes" if yes else "no", "series_id": text(event.get("series_ticker")),
                         "source": "kalshi.events.markets"},
            "native_quote": {"native_price": decimal(market.get("yes_ask_dollars") if yes else market.get("yes_bid_dollars"), probability=True),
                             "selected_side_ask": decimal(selected, probability=True),
                             "native_quantity": None,
                             "price_convention": "yes_book_dollars", "quantity_unit": "one_dollar_contracts",
                             "observed_at": clock((envelope.get("event_observed_at") or {}).get(event.get("event_ticker"))),
                             "provider_updated_at": clock(market.get("updated_time")),
                             "source": "kalshi.events.markets", "clock_source": "collector_event_page_received_at"},
            "fee": {"coefficient": decimal(round(coefficient, 12)) if coefficient is not None else None,
                    "source": "kalshi.series.fee_type+fee_multiplier", "charged": "taker",
                    "rounding_rule": "fee_ceiling_1_microdollar_then_total_debit_ceiling_100_microdollars",
                    "rounding_micros": "100", "rounding_source": "pipeline.odds.kalshi_fees.effective_price",
                    "account_verified": False},
            "settlement": settlement(market, rules=rules, url=market.get("rules_url"), source="kalshi.events.markets")}


def novig_evidence(event: dict, market: dict, outcome: dict, book: dict, *, observed_at: Any,
                   source: str) -> dict:
    oid = str(outcome.get("outcomeId") or outcome.get("id") or "")
    outcomes = market.get("outcomes") or []
    ids = [str(o.get("outcomeId") or o.get("id") or "") for o in outcomes]
    other = next((i for i in ids if i and i != oid), None) if len(ids) == 2 else None
    market_id = str(market.get("marketId") or market.get("id") or "")
    if book.get("marketId") is not None and str(book["marketId"]) != market_id:
        book, observed_at = {}, None
    levels = (book.get("orders") or {}).get(other) if isinstance(book.get("orders"), dict) else None
    if levels is None:
        levels = next((row.get("bids") for row in book.get("outcomeLadders") or []
                       if str(row.get("outcomeId")) == other), None)
    valid = [row for row in levels or [] if isinstance(row, dict)
             and decimal(row.get("price"), probability=True) is not None]
    best = max((Decimal(str(row["price"])) for row in valid), default=None)
    at_best = [row for row in valid if Decimal(str(row["price"])) == best]
    sizes = [decimal(row.get("qty")) for row in at_best]
    sizes = [q if q is not None and Decimal(q) == Decimal(q).to_integral_value() else None for q in sizes]
    quantity = str(sum((Decimal(q) for q in sizes), Decimal(0))) if sizes and all(q is not None for q in sizes) else None
    # GraphQL available is a native taker price, but supplies no certified size.
    price = complement(best) if best is not None else decimal(outcome.get("available"), probability=True)
    fee = market.get("fee") if isinstance(market.get("fee"), dict) else {}
    rate = decimal(fee.get("coefficient"))
    if rate is not None and Decimal(rate) > 1:
        rate = None
    charged = fee.get("charged")
    pregame = event.get("status") == "OPEN_PREGAME"
    effective = rate if charged == "ALWAYS" else "0" if charged == "WHEN_LIVE" and pregame and rate is not None else None
    detail = market.get("market_detail") if isinstance(market.get("market_detail"), dict) else {}
    rules = market.get("settlementRules") or detail.get("settlementRules")
    return {"identity": {"event_id": text(event.get("eventId") or event.get("id")),
                         "market_id": text(market.get("marketId") or market.get("id")),
                         "outcome_id": oid or None, "native_side": "selected_outcome",
                         "source": source},
            "native_quote": {"native_price": price, "selected_side_ask": price,
                             "native_quantity": quantity, "price_convention": "selected_outcome_probability",
                             "quantity_unit": "one_cent_contracts", "observed_at": clock(observed_at),
                             "provider_updated_at": None,
                             "sequence": book.get("seq") if type(book.get("seq")) is int and book["seq"] >= 0 else None,
                             "source": source + ".book",
                             "clock_source": "collector_book_received_at"},
            "fee": {"coefficient": effective, "listed_coefficient": rate, "charged": charged,
                    "source": source + ".fee", "phase": "pregame" if pregame else None,
                    "rounding_rule": "usd_5_decimal_places_half_up", "rounding_micros": "10",
                    "rounding_source": "site/worker/execution-preview.js:novig_5dp_half_up",
                    "account_verified": False, "included_in_display_odds": False},
            "settlement": settlement(market, rules=rules, url=market.get("rulesUrl"), source=source)}


def polymarket_evidence(event: dict, market: dict, side: dict, observed_at: Any) -> dict:
    long = side["long"]
    quote = market.get("bestAskQuote" if long else "bestBidQuote") or {}
    p = decimal(quote.get("value"), probability=True) if quote.get("currency") == "USD" else None
    return {"identity": {"event_id": text(event.get("id")), "event_slug": text(event.get("slug")),
                         "market_id": text(market.get("id")), "market_slug": text(market.get("slug")),
                         "outcome_id": text(side.get("tokenId")), "native_side": "yes" if long else "no",
                         "source": "polymarket_us.events.markets.marketSides"},
            "native_quote": {"native_price": p, "selected_side_ask": p if long else complement(p),
                             "native_quantity": None, "price_convention": "yes_book_dollars",
                             "quantity_unit": "one_dollar_contracts", "observed_at": clock(observed_at),
                             "provider_updated_at": clock(market.get("updatedAt")),
                             "source": "polymarket_us.events.markets.bestAskQuote" if long else "polymarket_us.events.markets.bestBidQuote",
                             "clock_source": "collector_event_received_at"},
            "fee": {"coefficient": decimal(market.get("feeCoefficient")), "source": "polymarket_us.events.markets.feeCoefficient",
                    "charged": "taker", "rounding_rule": "usd_cent_half_even", "rounding_micros": "10000",
                    "rounding_source": "site/worker/execution-preview.js:polymarket_us_cent_half_even", "account_verified": False},
            "settlement": settlement(market, rules=market.get("description"), url=market.get("rulesUrl"),
                                     source="polymarket_us.events.markets.description")}


def publish_evidence(row: GameLine, game: Game, process: dict) -> dict:
    evidence = deepcopy(row.execution_evidence)
    evidence.update(schema_version=VERSION,
                    binding={"game_id": game.game_id, "sport": game.sport, "home_team_id": game.home_id,
                             "away_team_id": game.away_id, "kickoff_utc": game.kickoff_utc.isoformat(),
                             "market": row.market, "period": "full-game", "line": str(row.line),
                             "selected_side": row.side, "source_id": row.source_id,
                             "source": "pipeline.odds.merge.canonicalize"})
    signal = (process.get("eligible") is True and process.get("discovery_state") == "signal"
              and row.side == "under")
    evidence["eligibility"] = {"current_signal": signal,
                               "reason": "current_process_under_signal" if signal else
                               "model_signal_is_under_only" if row.side != "under" else "process_is_not_current_signal",
                               "discovery_state": process.get("discovery_state"),
                               "process_reasons": list(process.get("discovery_reasons") or []),
                               "evaluated_at": process.get("evaluated_at"), "source": "GameCard.process"}
    # Missing fields stay null and carry a machine-readable path/reason.
    defaults = {"identity": ("event_id", "market_id", "outcome_id", "native_side"),
                "native_quote": ("native_price", "selected_side_ask", "native_quantity", "observed_at"),
                "fee": ("coefficient", "rounding_rule", "rounding_micros"),
                "settlement": ("rules", "rules_url", "rules_sha256", "rules_url_sha256")}
    missing = {}
    if row.source_id is None:
        missing["binding.source_id"] = "legacy_line_without_native_identity"
    for section, fields in defaults.items():
        block = evidence.setdefault(section, {})
        for name in fields:
            if block.setdefault(name, None) is None:
                missing[f"{section}.{name}"] = "not_available_in_collected_catalog"
    for name in ("push", "void", "overtime", "postponement"):
        term = evidence["settlement"].setdefault("treatments", {}).setdefault(name, {
            "value": None, "source": None, "reason": "not_explicit_in_collected_rules"})
        if term["value"] is None:
            missing[f"settlement.treatments.{name}"] = term["reason"]
    evidence["settlement"].setdefault("settlement_verified", False)
    evidence["settlement"].setdefault("reason", "no_collected_settlement_evidence")
    if not row.execution_evidence:
        missing["evidence"] = "legacy_or_retained_line_without_native_evidence"
    if row.book in ("kalshi", "polymarket_us"):
        missing["native_quote.native_quantity"] = "collected_catalog_has_no_orderbook_size"
    if evidence["fee"].get("account_verified") is not True:
        missing["fee.account_verified"] = "collector_does_not_fetch_account_fee_overrides"
    for section in ("identity", "native_quote", "fee"):
        for name, value in evidence[section].items():
            if value is None:
                missing.setdefault(f"{section}.{name}", "not_available_in_collected_catalog")
    evidence["missing"] = missing
    return evidence
