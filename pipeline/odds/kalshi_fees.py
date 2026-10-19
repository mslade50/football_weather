"""Kalshi direct-member taker debit, matching canonical golf fee precision.

https://docs.kalshi.com/getting_started/fee_rounding
No speculative accumulator rebates. Current series coefficient is supplied by
the caller; this is an entry estimate, not a statement of actual fill fees.
"""
from decimal import ROUND_CEILING, Decimal


def effective_price(price: float, coefficient: float = .07, count: int = 1) -> float:
    p, rate, n = Decimal(str(price)), Decimal(str(round(coefficient, 12))), Decimal(count)
    if not p.is_finite() or not rate.is_finite() or not 0 < p < 1 or not 0 <= rate <= Decimal(".7") or count <= 0:
        raise ValueError("invalid taker fee input")
    principal = p * n
    fee = (rate * n * p * (1 - p)).quantize(Decimal(".000001"), rounding=ROUND_CEILING)
    debit = (principal + fee).quantize(Decimal(".0001"), rounding=ROUND_CEILING)
    return float(debit / n)
