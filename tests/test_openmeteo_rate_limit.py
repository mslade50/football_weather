"""Quota-weighted fake-clock tests: no network, no real waiting."""

from datetime import datetime, timezone

import httpx
import pytest

from pipeline.weather import openmeteo as OM
from pipeline.weather.rate_limit import HOUR_BUDGET, RequestBudget, query_weight


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def params(n=20):
    return OM.build_ensemble_params([(32.0, -96.0)] * n, forecast_days=10)


def test_member_count_is_included_even_for_short_time_windows():
    assert query_weight(OM.ENSEMBLE_URL, params()) == pytest.approx(492.0)
    p = params(50)
    assert query_weight(OM.ENSEMBLE_URL, p) == pytest.approx(1230.0)
    p.update(start_hour="2026-10-02T22:00", end_hour="2026-10-03T03:00")
    assert query_weight(OM.ENSEMBLE_URL, p) == pytest.approx(1230.0)
    assert query_weight(OM.ENSEMBLE_URL, OM.build_ensemble_mean_params([(32.0, -96.0)] * 50)) == 50.0
    p = OM.build_params([(32.0, -96.0)] * 50)
    assert query_weight(OM.FORECAST_URL, p) == 150.0


def test_long_ranges_increase_quota_weight_and_unknown_members_fail_locally():
    p = params()
    p["forecast_days"] = "28"
    assert query_weight(OM.ENSEMBLE_URL, p) == pytest.approx(984.0)
    p["models"] = "unverified_ensemble"
    with pytest.raises(RuntimeError, match="unknown ensemble quota weight"):
        query_weight(OM.ENSEMBLE_URL, p)


def test_batches_are_paced_below_minute_quota_and_oversized_call_is_never_sent():
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    with pytest.raises(RuntimeError, match="exceeds local minute budget"):
        budget.acquire(OM.ENSEMBLE_URL, params(50))
    assert budget.requests == []
    for _ in range(6):
        budget.acquire(OM.ENSEMBLE_URL, params())
    assert clock.waits == [60.0] * 5
    assert [t for t, _ in budget.requests] == [0, 60, 120, 180, 240, 300]


def test_forecast_and_ensemble_share_quota_including_http_retries(monkeypatch):
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    seen = []
    monkeypatch.setattr(OM.time, "sleep", lambda seconds: None)

    def respond(request):
        seen.append(clock.now)
        return httpx.Response(503 if len(seen) == 1 else 200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result, _ = OM._get_json(client, OM.ENSEMBLE_URL, params(), budget=budget)
    assert result == {"ok": True}
    assert seen == [0, 60]
    assert sum(weight for _, weight in budget.requests) == 984
    budget.acquire(OM.FORECAST_URL, OM.build_params([(32.0, -96.0)] * 10))
    assert clock.now == 120  # deterministic calls cannot ignore member quota


@pytest.mark.parametrize("reason,header,expected", [
    ("Minutely API request limit exceeded. Please try again in one minute.", None, 60),
    ("Minutely API request limit exceeded.", "2", 60),
    ("Too many concurrent requests", "12", 12),
])
def test_429_fails_original_batch_and_honors_cooldown_before_fallback(reason, header, expected):
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    requests = []

    def respond(request):
        requests.append(clock.now)
        headers = {} if header is None else {"Retry-After": header}
        return httpx.Response(429, json={"error": True, "reason": reason}, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError, match="HTTP 429") as exc:
            OM._get_json(client, OM.ENSEMBLE_URL, params(1), budget=budget)
    assert reason in str(exc.value)
    assert requests == [0]  # no immediate full-member retry
    budget.acquire(OM.ENSEMBLE_URL, OM.build_ensemble_mean_params([(32.0, -96.0)]))
    assert clock.waits == [expected]


@pytest.mark.parametrize("reason,header", [
    ("Hourly API request limit exceeded.", None),
    ("Daily API request limit exceeded.", "1"),
    ("Unknown provider restriction", None),
    ("Too many concurrent requests", "120"),
    ("Unknown provider restriction", "NaN"),
    ("Unknown provider restriction", "Infinity"),
])
def test_long_or_unknown_provider_cooldowns_stop_more_calls(reason, header):
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    headers = {} if header is None else {"Retry-After": header}
    budget.rate_limited(httpx.Response(429, json={"reason": reason}, headers=headers))
    with pytest.raises(RuntimeError, match="cooldown active"):
        budget.acquire(OM.ENSEMBLE_URL, OM.build_ensemble_mean_params([(32.0, -96.0)]))
    assert budget.requests == [] and clock.waits == []


def test_hourly_budget_degrades_without_waiting_for_an_hour():
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    for _ in range(9):
        budget.acquire(OM.ENSEMBLE_URL, params())
    assert sum(weight for _, weight in budget.requests) <= HOUR_BUDGET
    with pytest.raises(RuntimeError, match="hourly budget exhausted"):
        budget.acquire(OM.ENSEMBLE_URL, params())
    clock.now += 3600
    budget.acquire(OM.ENSEMBLE_URL, params())
    assert len(budget.requests) == 1


def test_retry_after_http_date_is_respected():
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    # Far-future dates stop this build, even without a recognized error reason.
    response = httpx.Response(429, json={}, headers={"Retry-After": "Mon, 01 Jan 2100 00:00:00 GMT"})
    budget.rate_limited(response)
    with pytest.raises(RuntimeError, match="cooldown active"):
        budget.acquire(OM.ENSEMBLE_URL, params(1))


def test_error_body_is_captured_before_reading_provider_reason():
    clock = Clock()
    budget = RequestBudget(clock, clock.sleep)
    captured = []
    payload = {"error": True, "reason": "Minutely API request limit exceeded."}

    def capture(body, url):
        assert budget.block_reason == ""
        captured.append((body, url))

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, json=payload))) as client:
        with pytest.raises(RuntimeError, match="Minutely"):
            OM._get_json(client, OM.ENSEMBLE_URL, params(1), budget=budget, capture_error=capture)
    assert captured[0][0] == payload and captured[0][1].startswith(OM.ENSEMBLE_URL)


def test_run_context_owns_budget_not_a_global_forecast_cache():
    from pipeline.run_context import RunContext

    one = RunContext("all", git_sha="test", started_at=datetime.now(timezone.utc))
    two = RunContext("all", git_sha="test")
    one.request_budgets["openmeteo"] = RequestBudget()
    assert two.request_budgets == {}
