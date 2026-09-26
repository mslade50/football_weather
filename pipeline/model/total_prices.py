"""Compare main total offers by estimated return per dollar at risk.

The existing total-point probability slope calibrates a logistic CDF locally
around the model fair. Discretizing at half points assigns mass to integer
scores, so integer sportsbook totals refund on pushes. This is an estimate,
not an empirically fitted football key-number distribution. It leaves the
weather fair, legacy edge tiers and alert rules unchanged.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.contracts import GameLine
from pipeline.model.config import PTS_PROB_TOTAL
from pipeline.model.fair import american_to_prob, main_lines


def outcome_probabilities(sport: str, fair: float, line: float, side: str,
                          under_at_fair: float = .5) -> tuple[float, float, float]:
    """Unconditional (win, push, loss); half points never push."""
    if side not in ("under", "over") or not all(math.isfinite(x) for x in (fair, line, under_at_fair)):
        raise ValueError("invalid total comparison")
    if not 0 < under_at_fair < 1 or not math.isclose(line * 2, round(line * 2)):
        raise ValueError("total must be an integer or half point")
    anchor = math.log(under_at_fair / (1 - under_at_fair))
    # Match the existing local probability-per-point slope at the anchor.
    scale = PTS_PROB_TOTAL[sport] / (under_at_fair * (1 - under_at_fair))

    def cdf(x: float) -> float:
        if x < .5:
            return 0.0  # total scores cannot be negative
        z = max(-700, min(700, anchor + scale * (x - fair)))
        return 1 / (1 + math.exp(-z))

    below = cdf(math.ceil(line) - .5)
    above = 1 - cdf(math.floor(line) + .5)
    push = max(0.0, 1 - below - above) if float(line).is_integer() else 0.0
    return (below, push, above) if side == "under" else (above, push, below)


def expected_roi(win: float, push: float, cost: float) -> float:
    """Cost is implied probability including vig/fees; push returns the stake."""
    if not 0 < cost < 1 or not 0 <= win <= 1 or not 0 <= push <= 1 - win + 1e-12:
        raise ValueError("invalid probability or price")
    return win / cost + push - 1


def compare_totals(sport: str, lines: Iterable[GameLine], fair: Any, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"method": "discrete_logistic_estimate", "quotes": [], "best_under": None,
                              "best_over": None, "model_version": None}
    consensus = getattr(fair, "total", None)
    if fair is None or fair.fair_total is None or consensus is None or consensus.thin:
        return result
    result["fair_total"] = fair.fair_total
    result["model_version"] = next((e.model_version for e in fair.edges), "v1")
    base = 1 - fair.total.prob if fair.total.prob is not None else .5
    for book, sides in main_lines(lines, "total").items():
        for side, row in sides.items():
            if (side not in ("over", "under") or row.line is None or abs(row.odds) < 100
                    or row.scraped_at is None or row.scraped_at.tzinfo is None
                    or not timedelta(0) <= now - row.scraped_at <= timedelta(hours=1)):
                continue
            try:
                win, push, loss = outcome_probabilities(sport, fair.fair_total, float(row.line), side, base)
            except ValueError:
                continue
            cost = american_to_prob(row.odds)  # exchange parsers already include taker fees
            roi = expected_roi(win, push, cost)
            result["quotes"].append({"book": book, "side": side, "line": row.line, "odds": row.odds,
                                     "cost_prob": cost, "win_prob": win, "push_prob": push, "loss_prob": loss,
                                     "fair_cost": win / (1 - push), "ev_roi": roi,
                                     "updated_at": row.scraped_at.isoformat()})
    result["quotes"].sort(key=lambda q: (-q["ev_roi"], q["book"], q["side"]))
    for side in ("under", "over"):
        result[f"best_{side}"] = next((q for q in result["quotes"] if q["side"] == side), None)
    return result
