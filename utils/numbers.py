"""Low-level finite scalar coercion, shared by models, odds and weather."""

import math


def finite_number(value: object, *, allow_bool: bool = False) -> float | None:
    """Coerce with float; reject non-finite values and (by default) native bools.

    Deliberately retain float's acceptance rules: signs/whitespace work, commas
    do not, numpy/pandas numeric scalars work, and overflow is not swallowed.
    """
    if not allow_bool and isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
