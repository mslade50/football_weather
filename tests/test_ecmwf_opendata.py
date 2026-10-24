"""ECMWF Open Data fallback: units, staleness, mocked GRIB sampling, and source markers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.contracts import WeatherForecast
from pipeline.outputs import json_out
from pipeline.run_context import RunContext
from pipeline.weather import ecmwf_fallback, first_pass
from pipeline.weather import ecmwf_opendata as od
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.openmeteo import ParsedLocation

RUN = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
POINT = (39.28, -76.62)
NOW = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)


def _grid(param: str, step: int, value: float, *, member: int | None = None) -> od.GridField:
    return od.GridField(
        param=param, step=step, lat0=50.0, lon0=260.0, dlat=-20.0, dlon=40.0,
        ni=2, nj=2, values=(value, value, value, value), member=member,
    )


def _member_id(request: od.Request, number: int | None) -> int | None:
    """Post-50r1 control is oper/fc (member unset in the GRIB). Label it as member 0."""
    if request.role == "control" or request.kind == "cf":
        return 0
    return number


def _retriever(calls: list[od.Request]):
    def retrieve(request: od.Request) -> list[od.GridField]:
        calls.append(request)
        if set(request.params) <= {"10fg", "10fg3"}:
            if request.kind == "pf" and request.role != "control":
                return [
                    _grid("10fg", step, 8.0, member=number)
                    for step in request.steps for number in request.numbers
                ]
            return [_grid("10fg", step, 8.0, member=_member_id(request, None)) for step in request.steps]
        values = {"2t": 280.0, "10u": 5.0, "10v": 0.0, "tp": None, "10u_skip": 0.0}
        fields = []
        for step in request.steps:
            for param in request.params:
                value = 0.001 * step if param == "tp" else values[param]
                numbers = request.numbers or (None,)
                for number in numbers:
                    fields.append(_grid(param, step, value, member=_member_id(request, number)))
        return fields

    return retrieve


def _session(calls: list[od.Request]) -> od.OpenDataSession:
    return od.OpenDataSession(retriever=_retriever(calls), fixed_run=RUN, cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"))


def test_wind_temp_and_direction_use_meteorological_convention():
    speed, direction = od.wind_from_uv(10.0, 0.0)
    assert speed == pytest.approx(22.369362920544)
    assert direction == pytest.approx(270.0)
    assert od.wind_from_uv(0.0, 10.0)[1] == pytest.approx(180.0)
    assert od.wind_from_uv(-10.0, 0.0)[1] == pytest.approx(90.0)
    assert od.wind_from_uv(0.0, -10.0)[1] == pytest.approx(0.0)
    assert od.kelvin_to_f(273.15) == pytest.approx(32.0)
    assert od.kelvin_to_f(280.0) == pytest.approx(44.33)


def test_precip_is_deaccumulated_across_the_native_interval():
    samples = {0: {"tp": 0.0}, 3: {"tp": 0.003}}
    assert od.precip_mm(samples, 1, (0, 3)) == pytest.approx(1.0)
    assert od.precip_mm(samples, 3, (0, 3)) == pytest.approx(1.0)
    assert od.precip_mm(samples, 0, (0, 3)) == 0.0


def test_hourly_rows_interpolate_components_and_hold_interval_gust():
    samples = {
        0: {"2t": 280.0, "10u": 0.0, "10v": 0.0, "tp": 0.0, "10fg": 4.0},
        3: {"2t": 286.0, "10u": 6.0, "10v": 0.0, "tp": 0.003, "10fg": 10.0},
    }
    row = od.hour_state(samples, RUN, RUN + timedelta(hours=1), gust=True)
    assert row is not None
    assert row.temp == pytest.approx(od.kelvin_to_f(282.0))
    assert row.wind == pytest.approx(2.0 * od.MPS_TO_MPH)
    assert row.dir == pytest.approx(270.0)
    assert row.gust == pytest.approx(10.0 * od.MPS_TO_MPH)
    assert row.precip == pytest.approx(1.0)


def test_bilinear_sample_and_longitude_wrap():
    field = od.GridField(
        param="2t", step=0, lat0=40.0, lon0=270.0, dlat=-10.0, dlon=10.0, ni=2, nj=2,
        values=(280.0, 282.0, 290.0, 292.0),
    )
    assert od.sample_grid(field, 40.0, -90.0) == pytest.approx(280.0)
    assert od.sample_grid(field, 39.0, -89.0) == pytest.approx(280.0 * 0.81 + 282.0 * 0.09 + 290.0 * 0.09 + 292.0 * 0.01)


def test_steps_cover_only_the_kickoff_window():
    hours = [RUN + timedelta(hours=lead) for lead in range(47, 53)]
    assert od.steps_for_hours(RUN, hours, od.native_steps("ifs", 0)) == [45, 48, 51, 54]
    assert od.native_steps("aifs-single", 6)[1] - od.native_steps("aifs-single", 6)[0] == 6
    assert 150 not in od.native_steps("ifs", 6)
    assert 150 in od.native_steps("ifs", 0)


def test_gust_short_name_follows_the_open_data_step():
    assert od.gust_groups([0, 24, 90, 96, 144, 156]) == [
        ("10fg", (24, 90, 156)),
        ("10fg3", (96, 144)),
    ]


def test_ifs_06z_past_144h_uses_the_previous_00z_run():
    short = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
    extended = short - timedelta(hours=6)
    near = short + timedelta(hours=48)
    far = short + timedelta(hours=150)
    plan = od.runs_for_hours("ifs", short, {near, far})
    assert plan[0] == (short, {near})
    assert plan[1][0] == extended and far in plan[1][1]
    assert od.runs_for_hours("aifs-single", short, {far}) == [(short, {far})]
    calls: list[od.Request] = []
    session = od.OpenDataSession(
        retriever=_retriever(calls), fixed_run=short,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
    )
    parsed = session.point_forecasts([POINT], {POINT: {near, far}})
    ifs_runs = {call.run for call in calls if call.model == "ifs" and call.params == od.POINT_PARAMS}
    assert ifs_runs == {short, extended}
    extended_steps = [call.steps for call in calls if call.model == "ifs" and call.run == extended and call.params == od.POINT_PARAMS]
    assert extended_steps and 156 in extended_steps[0]
    times = {row.t for row in parsed[POINT].models[od.IFS_MODEL]}
    assert times == {near, far}
    assert session.runs["ifs-extended"] == extended


def test_stale_when_empty_or_a_cycle_behind_the_published_run():
    fresh = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
    behind = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
    assert od.expected_published_run(NOW) == fresh
    assert od.init_is_stale(fresh, NOW, missing=False) is False
    assert od.init_is_stale(behind, NOW, missing=False) is True
    assert od.init_is_stale(fresh, NOW, missing=True) is True
    assert od.init_is_stale(None, NOW, missing=False) is False
    assert od.init_is_stale(NOW - timedelta(hours=19), NOW, missing=False) is True


def test_fallback_fills_ecmwf_models_and_keeps_other_models():
    calls: list[od.Request] = []
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = _session(calls)
    hour = RUN + timedelta(hours=48)
    nbm = HourlyRow(t=hour, temp=70.0, wind=4.0, precip=0.0)
    forecasts = {POINT: ParsedLocation(POINT[0], POINT[1], models={"ncep_nbm_conus": [nbm]})}
    hours = {POINT: {hour}}
    ecmwf_fallback.supplement_forecasts(
        ctx, "nfl", [POINT], forecasts, [(0, 1, RuntimeError("open-meteo rate limited (HTTP 429)"))],
        hours, None, {POINT: NOW},
    )
    assert "ncep_nbm_conus" in forecasts[POINT].models
    ifs = forecasts[POINT].models[od.IFS_MODEL]
    assert ifs[0].wind == pytest.approx(5.0 * od.MPS_TO_MPH)
    assert ifs[0].temp == pytest.approx(od.kelvin_to_f(280.0))
    assert ifs[0].dir == pytest.approx(270.0)
    assert forecasts[POINT].models[od.AIFS_MODEL][0].gust is None
    assert ctx.weather_state["point_meta"][POINT]["ecmwf_source"] == "ecmwf-opendata"
    assert ctx.weather_state["ecmwf_opendata"]["used"] is True
    assert ctx.weather_state["ecmwf_opendata"]["license"] == "CC-BY-4.0"
    assert any("ECMWF window empty" in item.reason for item in ctx.degradations)
    assert calls and calls[0].params == od.POINT_PARAMS


def test_same_run_is_sampled_once_for_every_point():
    calls: list[od.Request] = []
    session = _session(calls)
    other = (41.0, -81.0)
    hours = {POINT: {RUN + timedelta(hours=48)}, other: {RUN + timedelta(hours=49)}}
    session.point_forecasts([POINT, other], hours)
    first = len(calls)
    session.point_forecasts([POINT, other], hours)
    assert first >= 1 and len(calls) == first
    assert sum(1 for call in calls if call.model == "ifs" and call.params == od.POINT_PARAMS) == 1


def test_force_replaces_a_fresh_open_meteo_ecmwf_window():
    calls: list[od.Request] = []
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = _session(calls)
    hour = RUN + timedelta(hours=48)
    stale = HourlyRow(t=hour, temp=1.0, wind=1.0, precip=0.0)
    forecasts = {POINT: ParsedLocation(POINT[0], POINT[1], models={od.IFS_MODEL: [stale], "ncep_gfs_seamless": [stale]})}
    monkey_env = pytest.MonkeyPatch()
    monkey_env.setenv("ECMWF_OPENDATA_FALLBACK", "force")
    try:
        ecmwf_fallback.supplement_forecasts(ctx, "nfl", [POINT], forecasts, [], {POINT: {hour}}, None, {})
    finally:
        monkey_env.undo()
    assert forecasts[POINT].models[od.IFS_MODEL][0].temp == pytest.approx(od.kelvin_to_f(280.0))
    assert forecasts[POINT].models["ncep_gfs_seamless"][0].temp == 1.0


def test_auto_keeps_a_current_open_meteo_window(monkeypatch):
    calls: list[od.Request] = []
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = _session(calls)
    hour = RUN + timedelta(hours=48)
    row = HourlyRow(t=hour, temp=55.0, wind=9.0, precip=0.2)
    forecasts = {POINT: ParsedLocation(POINT[0], POINT[1], models={od.IFS_MODEL: [row]})}
    monkeypatch.setattr(od, "init_is_stale", lambda *args, **kwargs: False)
    ecmwf_fallback.supplement_forecasts(ctx, "nfl", [POINT], forecasts, [], {POINT: {hour}}, None, {})
    assert forecasts[POINT].models[od.IFS_MODEL][0].temp == 55.0
    assert calls == []
    assert ctx.weather_state["point_meta"][POINT]["ecmwf_source"] == "open-meteo"


def test_off_mode_does_not_fetch(monkeypatch):
    calls: list[od.Request] = []
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = _session(calls)
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "off")
    ecmwf_fallback.supplement_forecasts(
        ctx, "nfl", [POINT], {}, [(0, 1, RuntimeError("429"))], {POINT: {RUN}}, None, {},
    )
    assert calls == []
    assert ctx.weather_state["ecmwf_opendata"]["used"] is False


def test_pytest_does_not_open_the_network_without_a_retriever():
    assert od.network_allowed() is False
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ecmwf_fallback.supplement_forecasts(
        ctx, "nfl", [POINT], {}, [(0, 1, RuntimeError("open-meteo rate limited (HTTP 429)"))],
        {POINT: {RUN}}, None, {},
    )
    assert "ecmwf_session" not in ctx.weather_state or ctx.weather_state["ecmwf_opendata"]["used"] is False


def test_ensemble_members_match_open_meteo_labels():
    calls: list[od.Request] = []
    session = _session(calls)
    hour = RUN + timedelta(hours=48)
    locations = session.ensemble_forecasts([POINT], {POINT: {hour}})
    members = locations[POINT].members
    assert f"{od.ENS_MODEL}:control" in members
    assert f"{od.ENS_MODEL}:member01" in members
    assert members[f"{od.ENS_MODEL}:member01"].model == od.ENS_MODEL
    assert members[f"{od.ENS_MODEL}:control"].wind[0] == pytest.approx(5.0 * od.MPS_TO_MPH)
    assert members[f"{od.ENS_MODEL}:control"].gust[0] == pytest.approx(8.0 * od.MPS_TO_MPH)
    assert members[f"{od.ENS_MODEL}:control"].precip[0] == pytest.approx(1.0)


def test_collect_uses_the_fallback_when_open_meteo_fails():
    calls: list[od.Request] = []
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = _session(calls)
    hour = RUN + timedelta(hours=48)

    class Om:
        BATCH_SIZE = 50

        def fetch_forecast(self, *args, **kwargs):
            raise RuntimeError("open-meteo rate limited (HTTP 429)")

    class Nws:
        class PointsCache:
            def __init__(self, path=None):
                pass

            def save(self):
                pass

    def fetch_batches(group, fetcher, **kwargs):
        try:
            fetcher(group)
        except Exception as exc:  # noqa: BLE001
            return {}, [(0, len(group), exc)]
        return {}, []

    monkey = pytest.MonkeyPatch()
    monkey.setenv("ECMWF_OPENDATA_FALLBACK", "auto")
    try:
        forecasts, _, stamps, meta = first_pass.collect(
            ctx, "nfl", Om(), Nws(), {POINT: []}, [], {POINT: {hour}}, {},
            hour - timedelta(hours=1), hour + timedelta(hours=4), None, fetch_batches, None, {},
        )
    finally:
        monkey.undo()
    assert od.IFS_MODEL in forecasts[POINT].models
    assert meta[POINT]["stage"] == "ecmwf_opendata"
    assert meta[POINT]["ecmwf_source"] == "ecmwf-opendata"
    assert abs((stamps[POINT] - ctx.now_utc).total_seconds()) < 2
    assert any("429" in item.reason for item in ctx.degradations)


def test_weather_block_and_markers_name_the_open_data_source():
    fc = WeatherForecast(game_id="g", source="medium:aifs+ifs", ecmwf_source="ecmwf-opendata")
    block = json_out._weather_block(fc, None)
    assert block["ecmwf_source"] == "ecmwf-opendata"
    source, updated = ecmwf_fallback.forecast_markers(
        {"updated_at": "2026-10-19T12:00:00+00:00", "ecmwf_run": "2026-10-19T06:00:00+00:00", "ecmwf_source": "open-meteo"},
        {"ecmwf_source": "ecmwf-opendata"},
    )
    assert source == "ecmwf-opendata"
    assert updated["nws"].startswith("2026-10-19T12:00:00")
    assert "ecmwf_opendata" in updated


def test_build_flag_sets_the_fallback_mode(monkeypatch):
    from pipeline.build import parse_args

    args = parse_args(["--sport", "nfl", "--scope", "weather", "--ecmwf-opendata", "force"])
    assert args.ecmwf_opendata == "force"
    monkeypatch.delenv("ECMWF_OPENDATA_FALLBACK", raising=False)
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", args.ecmwf_opendata)
    assert od.fallback_mode() == "force"


def test_decode_round_trip_of_a_small_grib(tmp_path):
    from eccodes import codes_grib_new_from_samples, codes_release, codes_set, codes_set_values, codes_write

    gid = codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        codes_set(gid, "Ni", 4)
        codes_set(gid, "Nj", 3)
        codes_set(gid, "latitudeOfFirstGridPointInDegrees", 40.0)
        codes_set(gid, "longitudeOfFirstGridPointInDegrees", 270.0)
        codes_set(gid, "iDirectionIncrementInDegrees", 1.0)
        codes_set(gid, "jDirectionIncrementInDegrees", 1.0)
        codes_set(gid, "iScansNegatively", 0)
        codes_set(gid, "jScansPositively", 0)
        codes_set(gid, "shortName", "2t")
        codes_set(gid, "step", 6)
        codes_set_values(gid, [280.0] * 12)
        path = tmp_path / "tiny.grib2"
        with path.open("wb") as handle:
            codes_write(gid, handle)
    finally:
        codes_release(gid)
    fields = list(od.decode_grib(path))
    assert len(fields) == 1
    assert fields[0].param == "2t"
    assert fields[0].step == 6
    assert fields[0].ni == 4 and fields[0].nj == 3
    assert od.sample_grid(fields[0], 39.2, -89.2) == pytest.approx(280.0)


def test_control_follows_the_50r1_stream_and_filename_type():
    cut = od.IFS_CONTROL_CUTOVER
    assert od.control_candidates(cut) == (("oper", "fc"), ("enfo", "cf"))
    assert od.control_candidates(cut - timedelta(hours=6)) == (("enfo", "cf"), ("oper", "fc"))
    assert od.perturbed_candidates() == (("enfo", "pf"), ("enfo", "ef"))
    assert od.filename_type("cf", "ifs") == "ef"
    assert od.filename_type("pf", "ifs") == "ef"
    assert od.filename_type("fc", "ifs") == "fc"
    assert od.filename_type("cf", "aifs-ens") == "cf"
    names = [
        "20261023/00z/ifs/0p25/enfo/20261023000000-0h-enfo-ef.index",
        "20261023/00z/ifs/0p25/enfo/20261023000000-48h-enfo-ef.index",
        "20261023/00z/ifs/0p25/oper/20261023000000-48h-oper-fc.index",
        "20261023/00z/ifs/0p25/enfo/20261023000000-6h-enfo-cf.index",
    ]
    assert od.parse_published_steps(names, "enfo", "ef") == [0, 48]
    assert od.parse_published_steps(names, "oper", "fc") == [48]
    assert od.parse_published_steps(names, "enfo", "cf") == [6]


def test_missing_steps_snap_to_the_nearest_published_hours():
    available = list(range(0, 361, 6))
    assert od.cover_with_available([45, 48, 51], available) == [42, 48, 54]
    assert od.cover_with_available([48], available) == [48]
    assert od.cover_with_available([400], available) == []


def test_ensemble_requests_oper_control_and_enfo_perturbed_members():
    calls: list[od.Request] = []
    session = _session(calls)
    hour = RUN + timedelta(hours=48)
    session.ensemble_forecasts([POINT], {POINT: {hour}})
    base = [call for call in calls if set(call.params) == set(od.ENS_PARAMS)]
    assert any(
        call.stream == "oper" and call.kind == "fc" and call.role == "control" and call.numbers == ()
        for call in base
    )
    assert any(
        call.stream == "enfo" and call.kind == "pf" and call.role == "perturbed"
        and call.numbers == tuple(range(1, 51))
        for call in base
    )
    assert not any(call.kind == "cf" for call in calls)


def test_pre_cutover_control_still_uses_enfo_cf():
    calls: list[od.Request] = []
    old = datetime(2026, 5, 11, 0, tzinfo=timezone.utc)
    session = od.OpenDataSession(
        retriever=_retriever(calls), fixed_run=old,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
    )
    locations = session.ensemble_forecasts([POINT], {POINT: {old + timedelta(hours=48)}})
    assert calls[0].stream == "enfo" and calls[0].kind == "cf" and calls[0].role == "control"
    assert f"{od.ENS_MODEL}:control" in locations[POINT].members


def test_ensemble_keeps_perturbed_members_when_control_index_entries_are_missing():
    calls: list[od.Request] = []
    base = _retriever(calls)

    def retrieve(request: od.Request) -> list[od.GridField]:
        if request.role == "control":
            raise od.OpenDataError("no control index entries")
        return base(request)

    session = od.OpenDataSession(
        retriever=retrieve, fixed_run=RUN,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
    )
    locations = session.ensemble_forecasts([POINT], {POINT: {RUN + timedelta(hours=48)}})
    members = locations[POINT].members
    assert f"{od.ENS_MODEL}:control" not in members
    assert f"{od.ENS_MODEL}:member01" in members
    assert members[f"{od.ENS_MODEL}:member01"].wind[0] == pytest.approx(5.0 * od.MPS_TO_MPH)


def test_missing_native_steps_retry_the_nearest_published_steps(caplog):
    calls: list[od.Request] = []
    base = _retriever(calls)

    def retrieve(request: od.Request) -> list[od.GridField]:
        if any(step % 6 for step in request.steps):
            raise od.OpenDataError(f"steps {list(request.steps)} are not in the index")
        return base(request)

    session = od.OpenDataSession(
        retriever=retrieve, fixed_run=RUN,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
        step_catalog=lambda _request: list(range(0, 145, 6)),
    )
    with caplog.at_level("WARNING"):
        locations = session.ensemble_forecasts([POINT], {POINT: {RUN + timedelta(hours=48)}})
    assert f"{od.ENS_MODEL}:control" in locations[POINT].members
    assert calls and all(step % 6 == 0 for call in calls for step in call.steps)
    assert any(48 in call.steps and 42 in call.steps for call in calls)
    assert "nearest published steps" in caplog.text


def test_ensemble_with_no_published_fields_returns_empty_and_logs(caplog):
    def retrieve(_request: od.Request) -> list[od.GridField]:
        raise od.OpenDataError("Cannot find index entries matching 10u/10v/tp")

    session = od.OpenDataSession(
        retriever=retrieve, fixed_run=RUN,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
        step_catalog=lambda _request: [],
    )
    with caplog.at_level("WARNING"):
        locations = session.ensemble_forecasts([POINT], {POINT: {RUN + timedelta(hours=48)}})
    assert locations == {}
    assert "found no control or perturbed fields" in caplog.text
    assert "10u/10v/tp" in caplog.text


def test_supplement_ensemble_degrades_when_no_fields_are_published(monkeypatch):
    def retrieve(_request: od.Request) -> list[od.GridField]:
        raise od.OpenDataError("Cannot find index entries matching 10u/10v/tp")

    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=retrieve, fixed_run=RUN,
        cache_dir=__import__("pathlib").Path("/tmp/ecmwf-test"),
        step_catalog=lambda _request: [],
    )
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "force")
    hour = RUN + timedelta(hours=48)
    ecmwf_fallback.supplement_ensemble(ctx, "nfl", [POINT], {POINT: {hour}}, {}, {}, None)
    assert not any(item.severity == "error" for item in ctx.degradations)
    assert any(
        item.severity == "warn" and "found no fields" in item.reason and "index entries" in item.reason
        for item in ctx.degradations
    )
    assert "error" not in ctx.weather_state["ecmwf_opendata"]


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.reason = "Slow Down" if status == 503 else "OK"
        self.text = "<Code>SlowDown</Code>" if status == 503 else ""


class _Transport:
    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls: list[tuple] = []

    def __call__(self, method: str, url: str, **kwargs):
        self.calls.append(kwargs.get("timeout"))
        return _Resp(self.codes.pop(0))


def _budget_session(tmp_path, **kwargs) -> od.OpenDataSession:
    sleeps = kwargs.pop("sleeps", [])
    values = dict(
        cache_dir=tmp_path, fetch_budget_s=90, total_budget_s=180,
        slowdown_attempts=3, slowdown_event_limit=4, give_up_on_slowdown=True,
        sleeper=sleeps.append, jitter=lambda: 0.0,
    )
    values.update(kwargs)
    session = od.OpenDataSession(**values)
    session.begin_fetch()
    return session


def test_fetch_and_total_budgets_stop_the_open_data_call(tmp_path):
    clock = _Clock()
    session = _budget_session(tmp_path, clock=clock, fetch_budget_s=90, total_budget_s=180)
    clock.t = 89
    session.check_budget()
    clock.t = 90
    with pytest.raises(od.OpenDataBudget, match="fetch budget of 90s"):
        session.check_budget()
    clock.t = 0
    session.begin_fetch()
    clock.t = 180
    with pytest.raises(od.OpenDataBudget, match="total budget of 180s"):
        session.check_budget()


def test_slowdown_backs_off_then_gives_up_the_rest_of_the_fetch(tmp_path):
    sleeps: list[float] = []
    session = _budget_session(tmp_path, sleeps=sleeps)
    transport = _Transport([503, 503, 503, 200])
    with pytest.raises(od.OpenDataSlowDown, match="SlowDown"):
        session.guarded_http(transport, "GET", "https://ecmwf.example/a.grib2")
    assert len(transport.calls) == 3
    assert sleeps == [od.slowdown_backoff(1, lambda: 0.0), od.slowdown_backoff(2, lambda: 0.0)]
    assert sleeps == [0.5, 1.0]
    assert session._abandoned == "slowdown"
    connect, read = transport.calls[0]
    assert connect <= od.HTTP_CONNECT_TIMEOUT_S
    assert read <= od.HTTP_READ_TIMEOUT_S
    with pytest.raises(od.OpenDataSlowDown):
        session.guarded_http(transport, "GET", "https://ecmwf.example/b.grib2")
    assert len(transport.calls) == 3


def test_recovered_slowdown_still_gives_up_once_the_event_limit_is_hit(tmp_path):
    sleeps: list[float] = []
    session = _budget_session(tmp_path, sleeps=sleeps, slowdown_event_limit=2)
    transport = _Transport([503, 200, 503])
    assert session.guarded_http(transport, "GET", "https://ecmwf.example/ok.grib2").status_code == 200
    assert sleeps == [0.5]
    with pytest.raises(od.OpenDataSlowDown):
        session.guarded_http(transport, "GET", "https://ecmwf.example/next.grib2")
    assert len(transport.calls) == 3
    assert session._abandoned == "slowdown"


def test_download_stops_on_slowdown_and_does_not_start_the_next_mirror(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake_client(source, model, owner=None):
        class Client:
            def _get_urls(self, payload, target=None, use_index=True):
                self.target = target
                return type("Indexed", (), {"urls": ()})()

            def _download(self, indexed):
                seen.append(source)
                raise od.OpenDataSlowDown("503 Server Error: Slow Down")

        return Client()

    monkeypatch.setattr(od, "_client", fake_client)
    session = _budget_session(tmp_path, sources=("aws", "azure", "ecmwf"))
    request = od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",))
    with pytest.raises(od.OpenDataSlowDown):
        session._download(request)
    assert seen == ["aws"]
    later = od.Request("ifs", RUN, "enfo", "pf", (51,), ("10u",))
    with pytest.raises(od.OpenDataSlowDown):
        session._download(later)
    assert seen == ["aws"]


def test_budget_expiry_does_not_open_a_download(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(od, "_client", lambda *a, **k: pytest.fail("client"))
    session = _budget_session(tmp_path, clock=clock, fetch_budget_s=10, total_budget_s=10)
    clock.t = 10
    with pytest.raises(od.OpenDataBudget):
        session._download(od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",)))


def test_other_mirror_errors_still_fall_through(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake_client(source, model, owner=None):
        class Client:
            def _get_urls(self, payload, target=None, use_index=True):
                self.target = target
                return type("Indexed", (), {"urls": ()})()

            def _download(self, indexed):
                seen.append(source)
                if source != "ecmwf":
                    raise RuntimeError("404")
                Path(self.target).write_bytes(b"GRIB")

        return Client()

    monkeypatch.setattr(od, "_client", fake_client)
    session = _budget_session(tmp_path, sources=("aws", "azure", "ecmwf"))
    path = session._download(od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",)))
    assert seen == ["aws", "azure", "ecmwf"]
    assert path.read_bytes() == b"GRIB"


def test_slowdown_keeps_open_meteo_models(monkeypatch, tmp_path):
    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=lambda _request: [], fixed_run=RUN, cache_dir=tmp_path,
    )
    hour = RUN + timedelta(hours=48)
    row = HourlyRow(t=hour, temp=55.0, wind=9.0, precip=0.2)
    forecasts = {POINT: ParsedLocation(POINT[0], POINT[1], models={od.IFS_MODEL: [row], "ncep_gfs_seamless": [row]})}
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "force")

    def boom(self, points, hours_by_point):
        raise od.OpenDataSlowDown("503 SlowDown")

    monkeypatch.setattr(od.OpenDataSession, "point_forecasts", boom)
    ecmwf_fallback.supplement_forecasts(ctx, "nfl", [POINT], forecasts, [], {POINT: {hour}}, None, {})
    assert forecasts[POINT].models[od.IFS_MODEL][0].temp == 55.0
    assert forecasts[POINT].models["ncep_gfs_seamless"][0].wind == 9.0
    assert any("fallback failed" in item.reason and "SlowDown" in item.reason for item in ctx.degradations)
    assert "SlowDown" in ctx.weather_state["ecmwf_opendata"]["error"]
