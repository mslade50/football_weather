"""Provider-outage behavior: partial success survives and rate limits fail fast."""

from __future__ import annotations

from pipeline import build
from pipeline.weather import openmeteo as OM


def test_point_batches_retain_success_when_a_later_batch_is_rate_limited():
    points = [(float(i), float(-i)) for i in range(75)]
    calls: list[tuple[list[tuple[float, float]], str]] = []

    def fetcher(batch, *, source_prefix, **kwargs):
        calls.append((list(batch), source_prefix))
        if len(calls) == 2:
            raise RuntimeError("open-meteo rate limited (HTTP 429)")
        return [f"forecast-{point[0]:.0f}" for point in batch]

    fetched, failures = build._fetch_point_batches(
        points,
        fetcher,
        batch_size=50,
        source_prefix="openmeteo_conus",
    )

    assert len(calls) == 2
    assert len(fetched) == 50
    assert points[0] in fetched and points[49] in fetched and points[50] not in fetched
    assert failures and failures[0][:2] == (1, 25)
    assert "429" in str(failures[0][2])


def test_openmeteo_429_is_not_retried_immediately():
    class RateLimitedClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, params):
            self.calls += 1
            return type("Response", (), {"status_code": 429})()

    client = RateLimitedClient()
    try:
        OM._get_json(client, OM.FORECAST_URL, {})
    except RuntimeError as exc:
        assert str(exc) == "open-meteo rate limited (HTTP 429)"
    else:
        raise AssertionError("HTTP 429 should fail the batch")
    assert client.calls == 1
