"""One pricing-time freshness predicate shared by consensus and total comparison."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.contracts import GameLine
from pipeline.model import config as C
from pipeline.model import fair, total_prices

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)


def _gl(scraped_at, expires_at=None, book="novig"):
    return GameLine(sport="nfl", game_id="nfl:2026:9:a@b", book=book, market="total", side="over",
                    odds=-110, line=44.5, scraped_at=scraped_at, expires_at=expires_at)


@pytest.mark.parametrize("scraped,expires,ok", [
    (NOW - timedelta(minutes=5), None, True),
    (NOW - C.PRICING_MAX_QUOTE_AGE, None, True),
    (NOW - C.PRICING_MAX_QUOTE_AGE - timedelta(seconds=1), None, False),
    (None, None, False),
    (NOW + timedelta(seconds=30), None, True),                      # within skew tolerance
    (NOW + C.PRICING_CLOCK_SKEW + timedelta(seconds=1), None, False),  # future clock
    (datetime(2026, 11, 1, 17, 55), None, False),                   # naive
    (NOW - timedelta(minutes=5), NOW, False),                       # expires exactly now
    (NOW - timedelta(minutes=5), NOW - timedelta(seconds=1), False),
    (NOW - timedelta(minutes=5), NOW + timedelta(seconds=1), True),
])
def test_predicate_edges_and_both_callers_agree(scraped, expires, ok):
    gl = _gl(scraped, expires)
    assert fair.quote_usable_for_pricing(gl, NOW) is ok
    assert (gl in fair._pricing_lines([gl], NOW)) is ok
    assert fair._pricing_lines([gl], None) == [gl]  # no pricing clock: no filtering


def test_total_prices_imports_the_shared_predicate():
    assert total_prices.quote_usable_for_pricing is fair.quote_usable_for_pricing
