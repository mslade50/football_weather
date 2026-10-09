from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline.contracts import GameLine
from pipeline.model.total_prices import compare_totals, expected_roi, outcome_probabilities

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def offer(book, line, odds, side="under", **kwargs):
    return GameLine("nfl", "game", book, "total", side, odds, line,
                    scraped_at=kwargs.pop("scraped_at", NOW), **kwargs)


def fair(number=46.5, thin=False):
    return SimpleNamespace(fair_total=number, total=SimpleNamespace(thin=thin, prob=.5), edges=[])


def test_user_example_changes_winner_with_probability_of_exactly_47():
    # U46.5 wins 50%: 48 cents costs $48 per $100 payout, expected gain $2.
    assert expected_roi(.5, 0, .48) == pytest.approx(2 / 48)
    # The extra point is worth 3% in the first case and 6% in the second.
    assert expected_roi(.53, 0, .525) < expected_roi(.5, 0, .48)
    assert expected_roi(.56, 0, .525) > expected_roi(.5, 0, .48)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_integer_push_mass_matches_adjacent_half_points(sport):
    low = outcome_probabilities(sport, 46.5, 46.5, "under")[0]
    high = outcome_probabilities(sport, 46.5, 47.5, "under")[0]
    win, push, loss = outcome_probabilities(sport, 46.5, 47, "under")
    assert win == pytest.approx(low)
    assert push == pytest.approx(high - low)
    assert win + push + loss == pytest.approx(1)
    assert outcome_probabilities(sport, 46.5, 47, "over") == pytest.approx((loss, push, win))
    assert outcome_probabilities(sport, 46.5, 47.5, "under")[1] == 0
    assert expected_roi(win, push, win / (1 - push)) == pytest.approx(0)


def test_push_refunds_stake_and_fair_coinflip_at_even_money():
    assert expected_roi(.45, .1, .5) == pytest.approx(0)
    assert outcome_probabilities("nfl", 46.5, 46.5, "under") == pytest.approx((.5, 0, .5))
    win, push, loss = outcome_probabilities("nfl", 46.5, 0, "under")
    assert win == 0
    assert push + loss == pytest.approx(1)


def test_rank_by_roi_not_points_and_keep_negative_best_honest():
    lines = [offer("novig", 46.5, 108), offer("kalshi", 47.5, -111), offer("draftkings", 49.5, 500)]
    prices = compare_totals("nfl", lines, fair(), now=NOW)
    assert prices["best_under"]["book"] == "novig"
    assert {q["book"] for q in prices["quotes"]} == {"novig", "kalshi"}
    assert [q["book"] for q in prices["reference_quotes"]] == ["draftkings"]
    assert prices["best_under"]["ev_roi"] > 0
    negative = compare_totals("nfl", lines, fair(60), now=NOW)
    assert negative["best_under"]["ev_roi"] < 0
    assert negative["best_over"] is None


def test_skip_stale_unknown_price_and_thin_model_but_keep_executable_alternate_quotes():
    lines = [offer("old", 46.5, 200, scraped_at=NOW-timedelta(hours=2)),
             offer("kalshi", 46.5, 0), offer("kalshi", 46.5, -110),
             offer("kalshi", 70.5, 200, is_main=False)]
    assert len(compare_totals("nfl", lines, fair(), now=NOW)["quotes"]) == 2
    assert compare_totals("nfl", lines, fair(thin=True), now=NOW)["best_under"] is None
    with pytest.raises(ValueError):
        outcome_probabilities("nfl", 46.5, 47.25, "under")


def test_expired_exchange_quote_is_excluded_without_turning_sportsbook_reference_into_best():
    rows = [offer("novig", 47.5, 110, expires_at=NOW), offer("draftkings", 50.5, 150)]
    result = compare_totals("nfl", rows, fair(), now=NOW)
    assert result["quotes"] == [] and result["best_under"] is None
    assert result["reference_quotes"][0]["execution_status"] == "reference_only"
    assert result["probability_status"] == "empirically_unvalidated"
