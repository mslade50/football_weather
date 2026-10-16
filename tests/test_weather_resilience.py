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


def test_point_forecast_client_uses_short_timeout_and_is_reused(monkeypatch):
    built: list[dict[str, object]] = []

    class FakeClient:
        is_closed = False

        def __init__(self, **kwargs):
            built.append(kwargs)

        def close(self):
            self.is_closed = True

    monkeypatch.setattr(OM.httpx, "Client", FakeClient)
    monkeypatch.setattr(OM, "_CLIENTS", {})
    used: list[object] = []
    monkeypatch.setattr(OM, "_get_json", lambda client, url, params, **kwargs: (used.append(client), ({}, url))[1])
    monkeypatch.setattr(OM, "parse_forecast", lambda payload: [object()])
    OM.fetch_forecast([(40.0, -75.0)], forecast_days=2)
    OM.fetch_forecast([(41.0, -76.0)], forecast_days=2)
    OM.fetch_forecast_raw([(42.0, -77.0)], forecast_days=2)
    # One keep-alive client serves every point-forecast call: no new TLS handshake per call.
    assert len(built) == 1 and built[0]["timeout"].read == OM.FORECAST_TIMEOUT_S <= 30.0 and built[0]["timeout"].connect == OM.CONNECT_TIMEOUT_S
    assert built[0]["limits"].keepalive_expiry == OM.KEEPALIVE_EXPIRY_S > 5.0
    assert len(used) == 3 and used[0] is used[1] is used[2] and not used[0].is_closed
    # A different timeout (ensemble) gets its own client; closing all clears the cache.
    assert OM._client(90.0) is not used[0] and len(built) == 2
    OM._close_clients()
    assert used[0].is_closed and OM._CLIENTS == {}


def test_openmeteo_timeout_retry_is_logged(monkeypatch, caplog):
    class StallingClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, params):
            self.calls += 1
            if self.calls < 3:
                raise OM.httpx.ReadTimeout("timed out")
            return type("Response", (), {"status_code": 200, "url": url,
                                         "raise_for_status": lambda self: None,
                                         "json": lambda self: {"ok": True}})()

    monkeypatch.setattr(OM.time, "sleep", lambda seconds: None)
    client = StallingClient()
    with caplog.at_level("WARNING", logger=OM.__name__):
        payload, _ = OM._get_json(client, OM.FORECAST_URL, {})
    assert payload == {"ok": True} and client.calls == 3
    retries = [r.getMessage() for r in caplog.records if "open-meteo attempt" in r.getMessage()]
    assert len(retries) == 2 and "ReadTimeout" in retries[0]
