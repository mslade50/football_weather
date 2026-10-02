"""Conservative, run-local Open-Meteo quota accounting (no persistent forecasts).

Provider query weight includes each ensemble member as a variable; a 50-point
IFS + GEFS request costs 1,230 calls even for a short game window. See the
provider's ForecastApiResult.calculateQueryWeight and ForecastapiController.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

MINUTE_BUDGET = 500.0  # headroom below the public 600-call/minute limit
HOUR_BUDGET = 4500.0  # fail locally rather than waiting an hour in a build
ENSEMBLE_BATCH_SIZE = 20  # 20 * 3 variables * (51 + 31) members / 10 = 492
ENSEMBLE_MEMBERS = {
    "ecmwf_ifs025": 51,
    "ecmwf_ifs025_ensemble": 51,
    "gfs_seamless": 31,
    "ncep_gefs_seamless": 31,
    "ncep_gefs_ensemble_mean_seamless": 1,
}


def query_weight(url: str, params: dict[str, str]) -> float:
    """Estimate the public provider formula conservatively for our model sets."""
    locations = len(params["latitude"].split(","))
    models = params["models"].split(",")
    if "ensemble-api." in url:
        try:
            members = sum(ENSEMBLE_MEMBERS[model] for model in models)
        except KeyError as exc:
            raise RuntimeError(f"unknown ensemble quota weight: {exc.args[0]}") from exc
    else:
        members = len(models)
    variables = len(params["hourly"].split(",")) * members
    if "start_hour" in params and "end_hour" in params:
        start = datetime.fromisoformat(params["start_hour"])
        end = datetime.fromisoformat(params["end_hour"])
        days = ((end - start).total_seconds() / 3600.0 + 1.0) / 24.0
    else:
        days = float(params.get("forecast_days", "7"))
    return locations * max(1.0, variables / 10.0 * max(1.0, days / 14.0))


class RequestBudget:
    """Shared across NFL/CFB in one build; every HTTP attempt consumes quota.

    Sequential requests wait only for a minute-sized quota window. Hourly,
    daily, or unclassified provider limits degrade the run instead of probing
    the same restricted service with fallback requests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self.requests: list[tuple[float, float]] = []
        self.blocked_until = 0.0
        self.block_reason = ""

    def acquire(self, url: str, params: dict[str, str]) -> None:
        cost = query_weight(url, params)
        if cost > MINUTE_BUDGET:
            raise RuntimeError(f"open-meteo batch exceeds local minute budget ({cost:.1f} calls)")
        while True:
            now = self.clock()
            self.requests = [(sent, weight) for sent, weight in self.requests if sent > now - 3600.0]
            delay = self.blocked_until - now
            if delay > 60.0:
                raise RuntimeError(f"open-meteo cooldown active: {self.block_reason}")
            if sum(weight for _, weight in self.requests) + cost > HOUR_BUDGET:
                raise RuntimeError("open-meteo local hourly budget exhausted; preserving available weather")
            recent = [(sent, weight) for sent, weight in self.requests if sent > now - 60.0]
            if sum(weight for _, weight in recent) + cost > MINUTE_BUDGET:
                delay = max(delay, recent[0][0] + 60.0 - now)
            if delay <= 0.0:
                self.requests.append((now, cost))
                return
            self.sleep(delay)

    def rate_limited(self, response: Any, capture: Callable[[Any, str], None] | None = None) -> str:
        """Retain the provider reason and honor its cooldown before another call."""
        reason = "HTTP 429 (provider cooldown unspecified)"
        try:
            payload = response.json()
            if capture is not None:
                capture(payload, str(response.url))
            if isinstance(payload, dict) and payload.get("reason"):
                reason = str(payload["reason"])
        except (AttributeError, ValueError):
            pass
        retry_after = getattr(response, "headers", {}).get("Retry-After")
        seconds = None
        if retry_after is not None:
            try:
                numeric = float(retry_after)
                if math.isfinite(numeric):
                    seconds = max(0.0, numeric)
            except ValueError:
                try:
                    until = parsedate_to_datetime(retry_after)
                    seconds = max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        lower = reason.lower()
        # Longer explicit quota scopes always win over a short Retry-After.
        if "daily" in lower:
            seconds = max(seconds or 0.0, 86400.0)
        elif "hourly" in lower:
            seconds = max(seconds or 0.0, 3600.0)
        elif "minutely" in lower or "one minute" in lower:
            seconds = max(seconds or 0.0, 60.0)
        self.blocked_until = max(self.blocked_until, self.clock() + (seconds if seconds is not None else float("inf")))
        self.block_reason = reason
        return reason
