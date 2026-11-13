"""Offline two-run outage retention through real merge, state and history paths."""
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from pipeline import build, state
from pipeline.contracts import WeatherForecast
from pipeline.obslog import shadow
from pipeline.outputs import d1_out, json_out
from pipeline.weather import retention
from tests.test_selective_weather import NOW, games, run
from tests.test_selective_weather import weather as weather_fixture

weather = weather_fixture


@pytest.fixture(autouse=True)
def no_fallback_network(monkeypatch):
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "off")


def save_run(ctx, forecasts, directory, count=1):
    group, stadiums = games(count)
    odds = build.OddsResult([], {}, state.load_openers(directory), {}, [], {})
    result = build.SportResult("nfl", [], [], group, stadiums, {}, forecasts, {}, odds)
    build.update_histories(ctx, "nfl", result, directory, False)
    return result


def fail_points(monkeypatch, partial=False):
    from pipeline.weather import openmeteo as om
    original = om.fetch_forecast

    def fetch(*args, **kwargs):
        if not partial:
            raise RuntimeError("upstream unavailable")
        return [replace(location, models={model: [replace(row, temp=99, wind=None) for row in rows]
                        for model, rows in location.models.items()}) for location in original(*args, **kwargs)]

    monkeypatch.setattr(om, "fetch_forecast", fetch)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("lead_hours", [48, 220])
def test_two_runs_preserve_whole_point_clocks_and_history(weather, tmp_path, monkeypatch, partial, lead_hours):
    import tests.test_selective_weather as fixtures
    kickoff = NOW + timedelta(hours=lead_hours)
    monkeypatch.setattr(fixtures, "KO", kickoff)
    monkeypatch.setattr(fixtures, "HOURS", [kickoff + timedelta(hours=i) for i in range(-1, 5)])
    path = tmp_path / "obs.sqlite"
    monkeypatch.setenv("OBSLOG_SHADOW_PATH", str(path))
    ctx, first = run(weather, tmp_path, count=2)
    save_run(ctx, first, tmp_path, count=2)
    before = {name: (tmp_path / name).read_bytes() for name in ("wx_history.json", "wx_last.json")}
    with sqlite3.connect(path) as conn:
        observations = conn.execute("select count(*) from forecast_observations").fetchone()[0]
    weather["clock"] += timedelta(hours=4)
    fail_points(monkeypatch, partial)
    ctx, second = run(weather, tmp_path, count=2)
    result = save_run(ctx, second, tmp_path, count=2)
    for gid, fc in second.items():
        assert fc.retained and fc.point_aged
        assert fc.run_time == first[gid].run_time == NOW
        assert fc.retained_from == json_out.sanitize(NOW)
        assert fc.retained_reason == ("incomplete_point" if partial else "source_unavailable")
        old_block, new_block = (json_out.sanitize(json_out._weather_block(f, None)) for f in (first[gid], fc))
        for key in ("retained", "retained_from", "retained_reason", "point_aged"):
            old_block.pop(key)
            new_block.pop(key)
        assert old_block == new_block  # Hourly, raw, blended, ensemble, lead and source clocks too.
    summaries = [d for d in ctx.degradations if "retained 2 previous forecasts" in d.reason]
    assert len(summaries) == 1 and summaries[0].severity == "warn"
    assert not any("final point forecast incomplete" in d.reason for d in ctx.degradations)
    assert not result.wx_changed
    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from forecast_observations").fetchone()[0] == observations
    # A repeat outage never renews the saved timestamp or the retention ceiling.
    weather["clock"] = NOW + timedelta(hours=12, seconds=1)
    _, expired = run(weather, tmp_path, count=2)
    assert all(not fc.retained and fc.wind_fg is None for fc in expired.values())


@pytest.mark.parametrize("hours,retained", [(12, True), (12.01, False), (-.01, False)])
def test_retention_ceiling_and_future_clock(weather, tmp_path, monkeypatch, hours, retained):
    ctx, first = run(weather, tmp_path)
    save_run(ctx, first, tmp_path)
    weather["clock"] += timedelta(hours=hours)
    fail_points(monkeypatch)
    ctx, result = run(weather, tmp_path)
    assert next(iter(result.values())).retained is retained
    assert any("final point forecast incomplete" in d.reason for d in ctx.degradations) is not retained


@pytest.mark.parametrize("legacy", [False, True])
def test_no_previous_forecast_or_legacy_arrays_keep_incomplete_behavior(weather, tmp_path, monkeypatch, legacy):
    gid = games()[0][0].game_id
    if legacy:
        (tmp_path / "wx_last.json").write_text(json.dumps({"schema_version": 1, "last": {f"{gid}|nws": [12, 15, 50, 0]}}))
    loaded = d1_out.load_wx_last(tmp_path)
    assert loaded["schema_version"] == 2 and loaded["forecasts"] == {}
    if legacy:
        assert loaded["last"][f"{gid}|nws"] == [12, 15, 50, 0]
    fail_points(monkeypatch)
    ctx, result = run(weather, tmp_path)
    assert result[gid].wind_fg is None and not result[gid].retained
    assert any("final point forecast incomplete" in d.reason for d in ctx.degradations)
    assert any("1 games have no usable forecast" in d.reason for d in ctx.degradations)
    assert not any("previous forecasts" in d.reason for d in ctx.degradations)


def test_state_identity_and_bad_payload_fail_closed(weather, tmp_path):
    ctx, forecasts = run(weather, tmp_path)
    save_run(ctx, forecasts, tmp_path)
    stored = d1_out.load_wx_last(tmp_path)
    group, stadiums = games()
    game = group[0]
    stadium = stadiums[game.game_id]
    assert retention.restore(stored, game, stadium, "outdoors", NOW, "source_unavailable")
    assert not retention.restore(stored, replace(game, kickoff_utc=game.kickoff_utc + timedelta(hours=1)), stadium,
                                 "outdoors", NOW, "source_unavailable")
    assert not retention.restore(stored, game, replace(stadium, lat=31), "outdoors", NOW, "source_unavailable")
    assert not retention.restore(stored, game, stadium, "closed", NOW, "source_unavailable")
    stored["forecasts"][game.game_id]["forecast"].pop("hourly")
    assert not retention.restore(stored, game, stadium, "outdoors", NOW, "source_unavailable")
    with pytest.raises(state.StateSchemaError):
        state.migrate({"schema_version": 3}, "wx_last")


def test_never_covered_game_outside_source_horizons_is_not_retained(weather, tmp_path, monkeypatch):
    import tests.test_selective_weather as fixtures
    from pipeline.weather import openmeteo as om
    monkeypatch.setattr(fixtures, "KO", NOW + timedelta(hours=400))
    monkeypatch.setattr(om, "fetch_forecast", lambda *args, **kwargs: [])
    ctx, forecasts = run(weather, tmp_path)
    fc = next(iter(forecasts.values()))
    assert not fc.retained and fc.wind_fg is None
    assert any("nearest kickoff 400h away" in d.reason for d in ctx.degradations)
    assert any("final point forecast incomplete" in d.reason for d in ctx.degradations)


def test_obslog_direct_writer_also_skips_retained(tmp_path):
    from pipeline.obslog import append_forecasts, connect
    fc = WeatherForecast("nfl:test", "nws", run_time=NOW, retained=True, point_aged=True)
    with connect(tmp_path / "direct.sqlite") as conn:
        append_forecasts(conn, [fc], run_id="outage", fetched_at=NOW + timedelta(hours=4))
        assert conn.execute("select count(*) from forecast_observations").fetchone()[0] == 0
    assert json_out.wx_point(fc, None) is None
    # The normal shadow entry point also cannot append a retained observation.
    shadow.weather(tmp_path / "shadow.sqlite", build.RunContext("nfl"), [], {fc.game_id: fc})
    with sqlite3.connect(tmp_path / "shadow.sqlite") as conn:
        assert conn.execute("select count(*) from forecast_observations").fetchone()[0] == 0
