"""Acceptance boundaries retained while sharing parser helpers."""
import math
from datetime import timezone

import pytest

from pipeline.model.fair import prob_to_american
from pipeline.odds import base
from pipeline.odds.parsers import betcris, betonline, kalshi, novig, polymarket_us, prophetx
from pipeline.odds.parsers.common import finite_number, parse_utc, positive_zero, valid_lines


@pytest.mark.parametrize("value", [None, "", "oops", "NaN", "inf", float("-inf"), True, False])
def test_strict_finite_numbers(value):
    assert finite_number(value) is None
    assert betcris._public_number(value) is None
    assert polymarket_us.number(value) is None


def test_boolean_and_zero_policies_remain_explicit():
    assert kalshi._dollars(True) == 1.0
    assert betonline._point(False) == 0.0
    assert positive_zero(None) is None
    assert math.copysign(1, positive_zero(-0.0)) == 1
    assert novig._num("1.234") == 1.23  # provider rounds, unlike finite_number
    assert prophetx._int_or_none("1.5") is None  # not float coercion


def test_iso_naive_policy_and_utc_conversion():
    assert parse_utc("2026-09-05T19:30:00").tzinfo is timezone.utc
    assert betcris._public_timestamp("2026-09-05T19:30:00") is None
    assert parse_utc("2026-09-05T15:30:00-04:00").hour == 19
    assert parse_utc("2026-09-05T19:30:00Z").hour == 19
    assert parse_utc(" 2026-09-05T19:30:00Z ") is None
    assert parse_utc(1) is None


def test_probability_rounding_over_requested_range():
    for i in range(10, 991):
        p = i / 1000
        expected = int(round(-100 * p / (1 - p))) if p >= .5 else int(round(100 * (1 - p) / p))
        assert prob_to_american(p) == novig.prob_to_american(p) == kalshi.dollar_to_american(p) == expected


@pytest.mark.parametrize("value,expected", [("0", False), ("1", True), ("false", True), ("no", True), (" 0 ", True)])
def test_workflow_switches(monkeypatch, value, expected):
    monkeypatch.setenv("BOOK_NOVIG_ENABLED", value)
    assert base.book_enabled("novig") is expected


def test_historical_opt_outs(monkeypatch):
    monkeypatch.setenv("BOOK_BETCRIS_ENABLED", " false ")
    monkeypatch.setenv("BOOK_PINNACLE_ENABLED", " 0 ")
    assert not base.book_enabled("betcris")
    assert not base.book_enabled("pinnacle")


def test_filter_preserves_order_and_identity():
    a, b = object(), object()
    assert valid_lines(iter([None, a, None, b])) == [a, b]
