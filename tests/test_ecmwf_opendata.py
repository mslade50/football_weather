"""ECMWF Open Data fallback: units, staleness, mocked GRIB sampling, and source markers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    session = _budget_session(tmp_path, sleeps=sleeps, mirror_failover=False)
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
    session = _budget_session(tmp_path, sleeps=sleeps, slowdown_event_limit=2, mirror_failover=False)
    transport = _Transport([503, 200, 503])
    assert session.guarded_http(transport, "GET", "https://ecmwf.example/ok.grib2").status_code == 200
    assert sleeps == [0.5]
    with pytest.raises(od.OpenDataSlowDown):
        session.guarded_http(transport, "GET", "https://ecmwf.example/next.grib2")
    assert len(transport.calls) == 3
    assert session._abandoned == "slowdown"


def test_download_stops_on_slowdown_and_does_not_start_the_next_mirror(tmp_path, monkeypatch):
    seen: list[str] = []

    def transport(method, url, headers):
        if "amazonaws.com" in url:
            seen.append("aws")
        elif "googleapis.com" in url:
            seen.append("google")
        else:
            seen.append(url)
        return _Resp(503)

    monkeypatch.setattr(od, "_client", lambda *args, **kwargs: pytest.fail("library client"))
    session = _budget_session(
        tmp_path, sources=("aws", "google"), mirror_failover=False, transport=transport,
    )
    request = od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",))
    with pytest.raises(od.OpenDataSlowDown):
        session._download(request)
    assert seen == ["aws", "aws", "aws"]
    later = od.Request("ifs", RUN, "enfo", "pf", (51,), ("10u",))
    with pytest.raises(od.OpenDataSlowDown):
        session._download(later)
    assert seen == ["aws", "aws", "aws"]


def test_budget_expiry_does_not_open_a_download(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(od, "_client", lambda *a, **k: pytest.fail("client"))
    session = _budget_session(tmp_path, clock=clock, fetch_budget_s=10, total_budget_s=10)
    clock.t = 10
    with pytest.raises(od.OpenDataBudget):
        session._download(od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",)))


def test_other_mirror_errors_still_fall_through(tmp_path, monkeypatch):
    seen: list[str] = []
    index = b'{"param":"2t","levtype":"sfc","step":"48","_offset":0,"_length":8}\n'
    body = b"GRIB" + b"xxxx"

    def host(url: str) -> str:
        if "amazonaws.com" in url:
            return "aws"
        if "googleapis.com" in url:
            return "google"
        if "data.ecmwf.int" in url:
            return "ecmwf"
        return url

    def transport(method, url, headers):
        name = host(url)
        if url.endswith(".index"):
            seen.append(name)
        if name != "ecmwf":
            return _Resp(404)
        return type("R", (), {"status_code": 200, "content": index if url.endswith(".index") else body})()

    monkeypatch.setattr(od, "_client", lambda *args, **kwargs: pytest.fail("library client"))
    session = _budget_session(tmp_path, sources=("aws", "google", "ecmwf"), transport=transport)
    path = session._download(od.Request("ifs", RUN, "oper", "fc", (48,), ("2t",)))
    assert seen == ["aws", "google", "ecmwf"]
    assert path.read_bytes() == b"GRIB" + b"xxxx"


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


def test_index_selects_only_the_surface_messages_we_sample():
    from pipeline.weather.ecmwf_ranges import coalesce_ranges, parse_index_entries, select_indexed

    payload = "\n".join([
        '{"param":"tp","levtype":"sfc","type":"pf","number":"24","step":"12","_offset":759702,"_length":313460}',
        '{"param":"10u","levtype":"sfc","type":"pf","number":"29","step":"12","_offset":24404583,"_length":622046}',
        '{"param":"t","levtype":"pl","number":"1","step":"12","_offset":10,"_length":999}',
        '{"param":"2t","levtype":"sfc","type":"fc","step":"12","_offset":24613644,"_length":316036}',
        '{"param":"10u","levtype":"sfc","type":"pf","number":"24","step":"12","_offset":759702,"_length":10}',
    ])
    rows = parse_index_entries(payload)
    perturbed = select_indexed(rows, ("10u", "10v", "tp"), (24, 29), 12)
    assert perturbed.messages == 3
    assert perturbed.nbytes == 313460 + 622046 + 10
    assert perturbed.members == (24, 29)
    control = select_indexed(rows, ("2t",), (), 12)
    assert control.messages == 1 and control.spans == ((24613644, 316036),)
    assert coalesce_ranges(((0, 10), (10, 5), (30, 4))) == [(0, 15), (30, 4)]


def test_slowdown_on_the_first_mirror_fails_over_and_keeps_the_byte_range(tmp_path):
    index = b'{"param":"10u","levtype":"sfc","step":"48","number":"1","_offset":0,"_length":8}\n'
    body = b"GRIBxxxx"
    hosts: list[str] = []

    def transport(method, url, headers):
        host = "aws" if "amazonaws.com" in url else "google"
        hosts.append(host)
        if host == "aws":
            return _Resp(503)
        payload = index if url.endswith(".index") else body
        return type("R", (), {"status_code": 200, "content": payload})()

    session = _budget_session(tmp_path, sources=("aws", "google"), transport=transport, mirror_failover=True)
    path = session._download(od.Request("ifs", RUN, "enfo", "pf", (48,), ("10u",), (1,), "perturbed"))
    assert path.read_bytes() == body
    assert hosts[:3] == ["aws", "aws", "aws"]
    assert "google" in hosts
    assert session.mirror == "google"
    assert session._abandoned is None


def test_implausible_three_hour_rain_is_a_data_error_not_a_signal():
    samples = {0: {"tp": 0.0}, 3: {"tp": 6.516}}
    assert od.precip_mm(samples, 3, (0, 3)) is None
    assert od.precip_mm({0: {"tp": 0.0}, 3: {"tp": 0.003}}, 1, (0, 3)) == pytest.approx(1.0)
    from pipeline.weather.parsers.ensemble import parse_ensemble

    payload = {
        "latitude": POINT[0], "longitude": POINT[1],
        "hourly": {
            "time": ["2026-10-19T00:00"],
            "wind_speed_10m_member01_ecmwf_ifs025_ensemble": [12.0],
            "precipitation_member01_ecmwf_ifs025_ensemble": [2172.0],
        },
    }
    member = parse_ensemble(payload)[0].members["ecmwf_ifs025_ensemble:member01"]
    assert member.wind == [12.0]
    assert member.precip == [None]


def test_sampled_fields_are_reused_for_the_same_model_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ECMWF_OD_SAMPLE_KEEP_H", "100000")
    calls: list[od.Request] = []
    session = od.OpenDataSession(
        retriever=_retriever(calls), fixed_run=RUN, cache_dir=tmp_path,
    )
    hour = RUN + timedelta(hours=48)
    first = session.ensemble_forecasts([POINT], {POINT: {hour}})
    assert first[POINT].n_members() == 51
    assert calls
    again = od.OpenDataSession(
        retriever=lambda _request: pytest.fail("cached run must not download"),
        fixed_run=RUN, cache_dir=tmp_path,
    )
    second = again.ensemble_forecasts([POINT], {POINT: {hour}})
    assert second[POINT].n_members() == 51
    assert second[POINT].members[f"{od.ENS_MODEL}:member01"].wind[0] == pytest.approx(5.0 * od.MPS_TO_MPH)


def test_slowdown_on_every_mirror_does_not_start_the_next_step(tmp_path):
    hosts: list[str] = []

    def transport(method, url, headers):
        hosts.append("aws" if "amazonaws.com" in url else "google")
        return _Resp(503)

    session = _budget_session(
        tmp_path, sources=("aws", "google"), transport=transport, mirror_failover=True,
    )
    request = od.Request("ifs", RUN, "enfo", "pf", (48, 51), ("10u",), (1,), "perturbed")
    assert session._fields(request) == []
    assert hosts == ["aws", "aws", "aws", "google", "google", "google"]
    assert session._abandoned == "slowdown"
    with pytest.raises(od.OpenDataSlowDown):
        session._fields(od.Request("ifs", RUN, "enfo", "pf", (54,), ("10u",), (1,), "perturbed"))
    assert hosts == ["aws", "aws", "aws", "google", "google", "google"]


def test_steps_past_the_lead_cap_are_not_fetched():
    calls: list[od.Request] = []
    session = _session(calls)
    near = RUN + timedelta(hours=48)
    far = RUN + timedelta(hours=222)
    session.ensemble_forecasts([POINT], {POINT: {near, far}})
    fetched = [step for call in calls for step in call.steps]
    assert fetched
    assert 222 not in fetched
    assert all(step <= 168 for step in fetched)
    before = len(calls)
    assert session.point_forecasts([POINT], {POINT: {far}}) == {}
    assert len(calls) == before
    edge = RUN + timedelta(hours=168)
    session.point_forecasts([POINT], {POINT: {edge}})
    assert any(168 in call.steps for call in calls[before:])


def test_negative_lead_cap_keeps_the_far_step():
    kept = od.hours_within_lead(RUN, [RUN + timedelta(hours=48), RUN + timedelta(hours=222)], 168)
    assert kept == {RUN + timedelta(hours=48)}
    assert od.hours_within_lead(RUN, [RUN + timedelta(hours=222)], -1) == {RUN + timedelta(hours=222)}


def test_time_between_fetches_is_not_charged_to_the_budget(tmp_path):
    clock = _Clock()
    session = _budget_session(tmp_path, clock=clock, fetch_budget_s=30, total_budget_s=100)
    clock.t = 10
    session.end_fetch()
    clock.t = 500  # odds scrape and the rest of the first sport
    session.begin_fetch()
    clock.t = 515
    session.check_budget()
    clock.t = 535
    with pytest.raises(od.OpenDataBudget, match="fetch budget of 30s"):
        session.check_budget()


def test_cache_only_miss_does_not_download_or_blame_the_index(monkeypatch, tmp_path):
    def boom(_request: od.Request) -> list[od.GridField]:
        raise AssertionError("cache-only must not fetch")

    ctx = RunContext(sport="cfb", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=boom, fixed_run=RUN, cache_dir=tmp_path, cache_only=True,
    )
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "force")
    hour = RUN + timedelta(hours=48)
    ecmwf_fallback.supplement_ensemble(ctx, "cfb", [POINT], {POINT: {hour}}, {}, {}, None)
    reasons = [item.reason for item in ctx.degradations]
    assert any("found no fields" in reason and "cache miss" in reason for reason in reasons)
    assert not any("index entries" in reason for reason in reasons)


def test_cache_only_gust_without_wind_keeps_the_existing_ensemble(tmp_path, monkeypatch):
    from pipeline.weather.parsers.ensemble import EnsembleLocation, Member

    monkeypatch.setenv("ECMWF_OD_SAMPLE_KEEP_H", "100000")
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "force")
    session = od.OpenDataSession(retriever=_retriever([]), fixed_run=RUN, cache_dir=tmp_path)
    hour = RUN + timedelta(hours=48)
    assert session.ensemble_forecasts([POINT], {POINT: {hour}})[POINT].n_members() == 51
    wind_files = list((tmp_path / "samples").rglob("*10u-10v-tp.json"))
    assert wind_files
    for path in wind_files:
        path.unlink()

    def boom(_request: od.Request) -> list[od.GridField]:
        raise AssertionError("cache-only must not fetch")

    ctx = RunContext(sport="cfb", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=boom, fixed_run=RUN, cache_dir=tmp_path, cache_only=True,
    )
    labels = ["control"] + [f"member{i:02d}" for i in range(1, 51)]
    members = {
        f"{od.ENS_MODEL}:{label}": Member(od.ENS_MODEL, label, [9.0], [10.0], [0.1]) for label in labels
    }
    locations = {POINT: [EnsembleLocation(POINT[0], POINT[1], [hour], {}, members)]}
    coverage = {POINT: {"errors": {"ifs": "settling"}, "fetched_at": {}, "source_versions": {}}}
    ecmwf_fallback.supplement_ensemble(ctx, "cfb", [POINT], {POINT: {hour}}, locations, coverage, None)
    assert locations[POINT][0].members[f"{od.ENS_MODEL}:member01"].wind == [9.0]
    assert any("cache miss" in item.reason for item in ctx.degradations)


def test_budget_stop_is_not_reported_as_a_missing_index(monkeypatch, tmp_path):
    ctx = RunContext(sport="cfb", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=lambda _request: [], fixed_run=RUN, cache_dir=tmp_path,
    )

    def empty(self, points, hours):
        self.stopped_early = "ECMWF open-data fetch budget of 240s exceeded"
        self.cache_note = None
        return {}

    monkeypatch.setattr(od.OpenDataSession, "ensemble_forecasts", empty)
    monkeypatch.setenv("ECMWF_OPENDATA_FALLBACK", "force")
    hour = RUN + timedelta(hours=48)
    ecmwf_fallback.supplement_ensemble(ctx, "cfb", [POINT], {POINT: {hour}}, {}, {}, None)
    reasons = [item.reason for item in ctx.degradations]
    assert any("found no fields" in reason and "fetch budget of 240s" in reason for reason in reasons)
    assert not any("index entries" in reason for reason in reasons)


def test_gust_interval_samples_are_reused_under_the_10fg_name(tmp_path, monkeypatch):
    monkeypatch.setenv("ECMWF_OD_SAMPLE_KEEP_H", "100000")
    calls: list[od.Request] = []
    session = od.OpenDataSession(retriever=_retriever(calls), fixed_run=RUN, cache_dir=tmp_path)
    hour = RUN + timedelta(hours=100)
    first = session.ensemble_forecasts([POINT], {POINT: {hour}})
    gust = first[POINT].members[f"{od.ENS_MODEL}:member01"].gust[0]
    assert gust == pytest.approx(8.0 * od.MPS_TO_MPH)
    assert any(call.params == ("10fg3",) for call in calls)

    def boom(_request: od.Request) -> list[od.GridField]:
        raise AssertionError("cached 10fg3 gust must not download")

    again = od.OpenDataSession(retriever=boom, fixed_run=RUN, cache_dir=tmp_path)
    second = again.ensemble_forecasts([POINT], {POINT: {hour}})
    assert second[POINT].members[f"{od.ENS_MODEL}:member01"].gust[0] == pytest.approx(gust)


def test_a_cached_step_is_not_downloaded_again(tmp_path, monkeypatch):
    monkeypatch.setenv("ECMWF_OD_SAMPLE_KEEP_H", "100000")
    calls: list[od.Request] = []
    session = od.OpenDataSession(retriever=_retriever(calls), fixed_run=RUN, cache_dir=tmp_path)
    hour = RUN + timedelta(hours=48)
    session.point_forecasts([POINT], {POINT: {hour}})
    base = [path for path in (tmp_path / "samples").rglob("*-2t-10u-10v-tp.json")]
    assert len(base) >= 2
    base[0].unlink()
    downloaded: list[int] = []

    def download(self, request: od.Request):
        downloaded.extend(int(step) for step in request.steps)
        path = tmp_path / "refetch.grib2"
        path.write_bytes(b"GRIB")
        return path

    def decoder(_path):
        step = downloaded[-1]
        for param, value in (("2t", 280.0), ("10u", 5.0), ("10v", 0.0), ("tp", 0.001)):
            yield _grid(param, step, value)

    again = od.OpenDataSession(decoder=decoder, fixed_run=RUN, cache_dir=tmp_path)
    again._download = download.__get__(again, od.OpenDataSession)
    again.point_forecasts([POINT], {POINT: {hour}})
    assert len(downloaded) == 1


def test_ensemble_steps_are_decoded_one_message_at_a_time_and_grib_is_removed(tmp_path):
    import tracemalloc

    class Payload:
        live = 0
        peak = 0
        nbytes = 1 << 20
        created = 0

        def __init__(self) -> None:
            Payload.created += 1
            Payload.live += 1
            Payload.peak = max(Payload.peak, Payload.live)
            self._buf = bytearray(self.nbytes)

        def __len__(self) -> int:
            return 4

        def __getitem__(self, index: int) -> float:
            return 280.0

        def __del__(self) -> None:
            Payload.live -= 1

    def decoder(_path):
        for param in ("10u", "10v"):
            yield od.GridField(
                param=param, step=0, lat0=50.0, lon0=260.0, dlat=-20.0, dlon=40.0,
                ni=2, nj=2, values=Payload(), member=1,
            )

    def download(self, request: od.Request):
        path = self.cache_dir / request.cache_name()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"GRIB")
        step_path = self._step_grib_path(request, int(request.steps[0]))
        step_path.parent.mkdir(parents=True, exist_ok=True)
        step_path.write_bytes(b"GRIB-step")
        # The decoder stamps every message as step 0; tag the yielded step via closure.
        decoder.step = int(request.steps[0])
        return path

    def stepping_decoder(path):
        step = decoder.step
        for grid in decoder(path):
            yield od.GridField(
                param=grid.param, step=step, lat0=grid.lat0, lon0=grid.lon0,
                dlat=grid.dlat, dlon=grid.dlon, ni=grid.ni, nj=grid.nj,
                values=grid.values, member=grid.member,
            )

    session = od.OpenDataSession(decoder=stepping_decoder, fixed_run=RUN, cache_dir=tmp_path)
    session._download = download.__get__(session, od.OpenDataSession)
    request = od.Request("ifs", RUN, "enfo", "pf", (48, 51, 54), ("10u", "10v"), (1,), "perturbed")
    tracemalloc.start()
    session._ensure(request, [POINT])
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert Payload.created == 6
    assert Payload.peak <= 2
    # Six retained 1 MiB grids would be 6 MiB. Two in flight stay under 4 MiB.
    assert peak < Payload.nbytes * 4
    assert list(tmp_path.rglob("*.grib2")) == []


def test_partial_open_data_does_not_replace_a_fuller_ensemble(monkeypatch, tmp_path):
    from pipeline.weather.parsers.ensemble import EnsembleLocation, Member

    ctx = RunContext(sport="nfl", run_id="t", git_sha="abc")
    ctx.weather_state["ecmwf_session"] = od.OpenDataSession(
        retriever=lambda _request: [], fixed_run=RUN, cache_dir=tmp_path,
    )
    hour = RUN + timedelta(hours=48)

    def bundle(labels: list[str], wind: float) -> EnsembleLocation:
        members = {
            f"{od.ENS_MODEL}:{label}": Member(od.ENS_MODEL, label, [wind], [wind + 1], [0.1])
            for label in labels
        }
        return EnsembleLocation(POINT[0], POINT[1], [hour], {}, members)

    full = ["control"] + [f"member{i:02d}" for i in range(1, 51)]
    locations = {POINT: [bundle(full, 9.0)]}
    coverage = {POINT: {"errors": {"ifs": "settling"}, "fetched_at": {}, "source_versions": {}}}
    monkeypatch.setattr(
        od.OpenDataSession, "ensemble_forecasts",
        lambda self, points, hours: {POINT: bundle(["control"], 3.0)},
    )
    ecmwf_fallback.supplement_ensemble(ctx, "nfl", [POINT], {POINT: {hour}}, locations, coverage, None)
    assert locations[POINT][0].n_members() == 51
    assert locations[POINT][0].members[f"{od.ENS_MODEL}:member01"].wind == [9.0]
    assert coverage[POINT]["errors"]["ifs"] == "settling"
