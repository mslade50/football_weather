"""Golden acceptance boundaries for shared and deliberately special coercions."""

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from pipeline import alerts, backtest, calibrate, state
from pipeline.model import clv, fair, process, signals, wind_history
from pipeline.odds.parsers import common
from pipeline.weather import climatology_blend
from utils.numbers import finite_number
from utils.timeutil import parse_iso, parse_iso_utc

UTC = timezone.utc
EXPECTED = datetime(2026, 11, 13, 12, tzinfo=UTC)


@pytest.mark.parametrize('value,expected,with_bool', [
    (None, None, None), (True, None, 1.0), (False, None, 0.0),
    ('', None, None), (' ', None, None), ('nan', None, None), ('inf', None, None),
    ('-inf', None, None), ('1,000', None, None), (' +1.25 ', 1.25, 1.25), ('-2', -2.0, -2.0),
    (0, 0.0, 0.0), (2, 2.0, 2.0), (3.5, 3.5, 3.5),
    (float('nan'), None, None), (float('inf'), None, None),
    (np.int64(2), 2.0, 2.0), (np.float64(2.5), 2.5, 2.5), (np.float32(2.5), 2.5, 2.5),
    (np.bool_(True), 1.0, 1.0), (pd.NA, None, None), (Decimal('2.5'), 2.5, 2.5),
])
def test_finite_number_and_identical_consumers(value, expected, with_bool):
    for coerce in (finite_number, common.finite_number, backtest._num, calibrate._num, process._f):
        assert coerce(value) == expected
    assert finite_number(value, allow_bool=True) == with_bool
    assert wind_history._number(value) == with_bool


def test_float_overflow_and_signed_zero_are_unchanged():
    for coerce in (finite_number, backtest._num, calibrate._num, process._f, wind_history._number):
        with pytest.raises(OverflowError):
            coerce(10 ** 400)
        assert math.copysign(1, coerce('-0.0')) == -1


@pytest.mark.parametrize('value,expected,trimmed', [
    (None, None, None), (True, None, None), (False, None, None), ('', None, None), (' ', None, None),
    ('nan', None, None), ('inf', None, None), ('1,000', None, None), ('+123', None, None),
    (0, None, None), (1700000000, None, None), ('1700000000', None, None),
    ('2026-11-13T12:00:00', EXPECTED, EXPECTED),
    ('2026-11-13T12:00:00Z', EXPECTED, EXPECTED),
    ('2026-11-13T07:00:00-05:00', EXPECTED, EXPECTED),
    (' 2026-11-13T12:00:00Z ', None, EXPECTED),
    (EXPECTED.replace(tzinfo=None), EXPECTED, EXPECTED), (EXPECTED, EXPECTED, EXPECTED),
    (np.int64(1700000000), None, None), (np.datetime64('2026-11-13'), None, None),
    (pd.Timestamp('2026-11-13T07:00:00-05:00'), EXPECTED, EXPECTED), (pd.NA, None, None),
])
def test_iso_utc_and_identical_consumers(value, expected, trimmed):
    assert parse_iso_utc(value) == expected
    assert state.parse_utc(value) == expected
    for parse in (alerts._dt, backtest._dt, clv._dt):
        assert parse(value) == trimmed
    provider_expected = expected if isinstance(value, str) else None
    assert common.parse_utc(value) == provider_expected


def test_explicit_naive_and_string_only_policies():
    assert parse_iso_utc('2026-11-13T12:00:00', naive_utc=False) is None
    assert parse_iso_utc(EXPECTED.replace(tzinfo=None), naive_utc=False) is None
    assert parse_iso_utc(EXPECTED, strings_only=True) is None
    assert common.parse_utc('2026-11-13T12:00:00', naive_utc=False) is None
    assert common.parse_utc('2026-11-13T07:00:00-05:00', naive_utc=False) == EXPECTED
    # Legacy parse_iso only translates terminal Z; fromisoformat also permits Z
    # as the date/time separator. State/provider parsing replaces every Z.
    unusual = '2026-11-13Z12:00:00'
    assert parse_iso_utc(unusual) is None
    assert alerts._dt(unusual) == backtest._dt(unusual) == clv._dt(unusual) == EXPECTED


def test_deliberately_retained_number_policies():
    assert alerts._strict_number('1.25') is None
    assert alerts._strict_number(True) is None
    assert alerts._strict_number(np.int64(2)) is None
    assert alerts._strict_number(2) == 2.0
    assert fair._numeric_nan_to_none('1.25') is None
    assert fair._numeric_nan_to_none(True) == 1.0
    for coerce in (clv._number_allow_infinity, climatology_blend._number_allow_infinity):
        assert coerce(True) is None
        assert coerce('inf') == float('inf')
        assert coerce('nan') is None
    assert signals._number_allow_bool_and_infinity(True) == 1.0
    assert signals._number_allow_bool_and_infinity('inf') == float('inf')


def test_offset_preserving_and_evidence_parsers_are_deliberately_distinct():
    value = '2026-11-13T07:00:00-05:00'
    assert calibrate._offset_preserving_dt(value).utcoffset() == timedelta(hours=-5)
    assert parse_iso(value).utcoffset() == timedelta(hours=-5)
    assert parse_iso_utc(value).utcoffset() == timedelta(0)
    from pipeline.alert_policy import evidence_time
    assert evidence_time('2026-11-13T07:00:00') is None
    assert evidence_time(value).utcoffset() == timedelta(hours=-5)
    # ensure_utc historically preserves pandas' missing-datetime sentinel.
    for parse in (state.parse_utc, alerts._dt, backtest._dt, clv._dt):
        assert parse(pd.NaT) is pd.NaT
