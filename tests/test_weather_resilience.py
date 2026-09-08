"""Provider-outage behavior: partial success survives and rate limits fail fast."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline import build
from pipeline.contracts import Game, Stadium, WeatherForecast
from pipeline.outputs.raw_out import NullRawStore
from pipeline.run_context import RunContext
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


def test_stage_weather_recovers_only_rate_limited_batch_with_mean_spread(monkeypatch):
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(days=8)
    games = []
    stadiums = {}
    for index in range(51):
        game_id = f"cfb:2026:3:a{index}@h{index}"
        games.append(
            Game(
                game_id=game_id,
                sport="cfb",
                season=2026,
                week=3,
                kickoff_utc=kickoff,
                kickoff_local=kickoff,
                tz="UTC",
                home_id=f"h{index}",
                away_id=f"a{index}",
                stadium_id=f"s{index}",
            )
        )
        stadiums[game_id] = Stadium(
            stadium_id=f"s{index}",
            name=f"Stadium {index}",
            lat=30.0 + index * 0.1,
            lon=-100.0,
            country="US",
            roof_type="open",
        )

    monkeypatch.setattr(OM, "fetch_forecast", lambda points, **kwargs: [object() for _ in points])
    ensemble_calls = []

    def full_ensemble(points, **kwargs):
        ensemble_calls.append(list(points))
        if len(ensemble_calls) == 2:
            raise RuntimeError("open-meteo rate limited (HTTP 429)")
        return [f"members:{point}" for point in points]

    fallback_calls = []

    def mean_fallback(points, **kwargs):
        fallback_calls.append(list(points))
        return [f"mean:{point}" for point in points]

    monkeypatch.setattr(OM, "fetch_ensemble", full_ensemble)
    monkeypatch.setattr(OM, "fetch_ensemble_mean", mean_fallback)

    from pipeline.weather import merge as merge_mod
    from pipeline.weather import nws as nws_mod

    monkeypatch.setattr(nws_mod, "fetch_hourly", lambda *args, **kwargs: [])
    monkeypatch.setattr(nws_mod.PointsCache, "save", lambda self: None)

    seen = {}

    def fake_merge(game_id, kickoff_utc, now_utc, om, nws_rows, **kwargs):
        source = "mean_spread" if kwargs.get("ens_mean") else "members"
        seen[game_id] = source
        stats = SimpleNamespace(method=source)
        return SimpleNamespace(
            forecast=WeatherForecast(
                game_id=game_id,
                source="hrrr",
                temp_fg=70.0,
                wind_fg=8.0,
                rain_fg_mm=0.0,
            ),
            degradations=[],
            precip_prob_ens=None,
            roof_heuristic=False,
            ensemble=stats,
        )

    monkeypatch.setattr(merge_mod, "build_forecast", fake_merge)
    ctx = RunContext("cfb", started_at=now, git_sha="test")
    extras = {}
    forecasts = build.stage_weather(
        ctx,
        "cfb",
        games,
        stadiums,
        NullRawStore("cfb", "test"),
        {},
        extras=extras,
    )

    assert len(forecasts) == 51 and [len(call) for call in ensemble_calls] == [50, 1]
    assert fallback_calls == [[(35.0, -100.0)]]
    assert seen[games[-1].game_id] == "mean_spread"
    assert extras[games[-1].game_id]["ensemble_source"] == "mean_spread"
    assert any("recovered 1 with ensemble mean + spread" in d.reason for d in ctx.degradations)
    assert not [d for d in ctx.degradations if d.severity == "warn" and "ensemble" in d.reason]
