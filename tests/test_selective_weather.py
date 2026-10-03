"""End-to-end weather selection/cache with actual merge math and no network."""
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline.contracts import Game, Stadium
from pipeline.outputs.json_out import _weather_block, dump_json
from pipeline.outputs.raw_out import NullRawStore
from pipeline.run_context import RunContext
from pipeline.weather import merge as M
from pipeline.weather import nws
from pipeline.weather import openmeteo as OM
from pipeline.weather.member_cache import SOURCES
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import EnsembleLocation, Member
from pipeline.weather.parsers.openmeteo import ParsedLocation

NOW = datetime(2026, 10, 2, 20, tzinfo=timezone.utc)
KO = NOW + timedelta(days=2)
HOURS = [KO + timedelta(hours=i) for i in range(-1, 5)]


@pytest.fixture
def weather(monkeypatch):
    state = {"clock": NOW, "wind": 12, "temp": 50, "points": [], "ensembles": [], "versions": {}, "fail": set(), "transition": None}
    for source, (_, datasets) in SOURCES.items():
        state["versions"][source] = {dataset: {"snapshot": 1} for dataset in datasets}
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: state["clock"]))
    monkeypatch.setattr(nws, "fetch_hourly", lambda *a, **k: [])
    monkeypatch.setattr(nws.PointsCache, "save", lambda self: None)
    monkeypatch.setattr(M, "_default_climo", lambda: None)
    monkeypatch.setattr(M, "_default_blend_cfg", lambda: M.CB.DEFAULT_CONFIG)

    def points(batch, **kwargs):
        state["points"].append(list(batch))
        rows = [HourlyRow(t=t, wind=state["wind"], temp=state["temp"], gust=15, dir=180, precip=0, pop=0) for t in HOURS]
        return [ParsedLocation(*point, models={model: rows for model in (M.GFS, M.ECMWF, M.NBM, M.HRRR)}) for point in batch]

    def metadata(source, **kwargs):
        if f"meta_{source}" in state["fail"]:
            raise RuntimeError("source overdue/stale")
        return deepcopy(state["versions"][source])

    def ensemble(batch, models, **kwargs):
        source = "ifs" if models == "ecmwf_ifs025" else "gefs"
        state["ensembles"].append((source, list(batch)))
        if source in state["fail"] or (source == "ifs" and batch[0][0] >= 32):
            raise RuntimeError("provider daily quota 429")
        if state["transition"] == source:
            for value in state["versions"][source].values():
                value["snapshot"] += 1
        model, count = ("ecmwf_ifs025_ensemble", 51) if source == "ifs" else ("ncep_gefs_seamless", 31)
        members = {}
        for i in range(count):
            member = Member(model, "control" if i == 0 else f"member{i:02d}",
                            [8 + i / 10] * 6, [15 + i / 10] * 6, [0.1 * (i % 3)] * 6)
            members[member.key] = member
        return [EnsembleLocation(*point, HOURS, {}, deepcopy(members)) for point in batch]

    monkeypatch.setattr(OM, "fetch_forecast", points)
    monkeypatch.setattr(OM, "fetch_model_versions", metadata)
    monkeypatch.setattr(OM, "fetch_ensemble", ensemble)
    monkeypatch.setattr(OM, "fetch_ensemble_mean", lambda *a, **k: pytest.fail("approximation forbidden"))
    return state


def games(count=1):
    group, stadiums = [], {}
    for i in range(count):
        gid = f"nfl:2026:5:a{i}@h{i}"
        group.append(Game(gid, "nfl", 2026, 5, KO, KO, "UTC", f"h{i}", f"a{i}", f"s{i}"))
        stadiums[gid] = Stadium(f"s{i}", f"Venue {i}", 30 + i / 10, -96, country="US", roof_type="open")
    return group, stadiums


def run(state, tmp_path, count=1, handoff=None, reuse=None):
    ctx = RunContext("nfl", started_at=state["clock"], git_sha="test-sha")
    ctx.weather_state.update(write_point_dir=handoff, reuse_point_dir=reuse)
    group, stadiums = games(count)
    result = build.stage_weather(ctx, "nfl", group, stadiums, NullRawStore("nfl", ctx.run_id), {}, state_dir=tmp_path)
    return ctx, result


def test_ordinary_refresh_always_fetches_fresh_points_and_reuses_only_verified_raw_members(weather, tmp_path):
    _, first = run(weather, tmp_path)
    gid = next(iter(first))
    assert first[gid].ensemble_status == "full_members" and first[gid].ensemble_members == 82
    assert [source for source, _ in weather["ensembles"]] == ["ifs", "gefs"]
    weather["clock"] += timedelta(minutes=20)
    weather["wind"] = 13
    _, second = run(weather, tmp_path)
    assert len(weather["points"]) == 4 and len(weather["ensembles"]) == 2
    assert second[gid].wind_fg == 13 and second[gid].lead_hours < first[gid].lead_hours
    assert second[gid].run_time == weather["clock"]
    assert second[gid].ensemble_fetched_at == first[gid].ensemble_fetched_at
    assert set(second[gid].ensemble_cached_sources) == {"ifs", "gefs"}
    assert second[gid].wind_p90 == first[gid].wind_p90
    block = _weather_block(second[gid], None)
    assert block["ensemble_members"] == 82 and block["ensemble_fetched_at"] == first[gid].ensemble_fetched_at
    dump_json(tmp_path / "weather.json", block)
    assert json.loads((tmp_path / "weather.json").read_text())["ensemble_members"] == 82


def nws_provider(state, monkeypatch, *, wind=1, temp=75, rain=0, age_hours=0):
    def fetch(*args, metadata=None, **kwargs):
        metadata.update(updateTime=(state["clock"] - timedelta(hours=age_hours)).isoformat())
        return [HourlyRow(t=t, wind=wind, temp=temp, gust=wind + 2, dir=180, precip=rain, pop=20) for t in HOURS]
    monkeypatch.setattr(nws, "fetch_hourly", fetch)


def test_complete_benign_nws_first_pass_makes_no_openmeteo_requests(weather, tmp_path, monkeypatch):
    nws_provider(weather, monkeypatch)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert weather["points"] == [] and weather["ensembles"] == []
    assert fc.source == "nws" and fc.point_stage == "nws_first_pass"
    assert not fc.point_aged and fc.point_source_updated_at == {"nws": NOW.isoformat()}
    assert fc.ensemble_status == "not_sampled_below_signal_buffer"


def test_nws_rain_candidate_refines_once_then_retains_risk_under_dry_point_models(weather, tmp_path, monkeypatch):
    weather.update(wind=1, temp=75)
    nws_provider(weather, monkeypatch, rain=.8)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert len(weather["points"]) == 1 and len(weather["ensembles"]) == 2
    assert fc.point_stage == "refined_multimodel" and fc.ensemble_status == "full_members"
    assert "game_window_rain" in fc.ensemble_screen_reasons


def test_old_nws_publication_is_not_used_in_fresh_global_merge(weather, tmp_path, monkeypatch):
    weather.update(wind=1, temp=75)
    nws_provider(weather, monkeypatch, wind=40, temp=20, rain=5, age_hours=13)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert len(weather["points"]) == 1 and not weather["ensembles"]
    assert fc.wind_fg == 1 and fc.point_stage == "global_first_pass"


def test_missing_first_and_detailed_points_do_not_select_members_without_active_alert(weather, tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("point provider unavailable")
    monkeypatch.setattr(OM, "fetch_forecast", failed)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert not weather["ensembles"] and fc.ensemble_status == "not_sampled_point_unavailable"
    assert fc.point_aged and fc.wind_fg is None
    (tmp_path / "alerts.json").write_text(json.dumps({"schema_version": 1, "records": {"k": {
        "family": "edge", "game_id": fc.game_id, "notification_active": True}}}))
    _, result = run(weather, tmp_path)
    assert next(iter(result.values())).ensemble_eligible
    assert len(weather["ensembles"]) == 2


def test_nws_is_rechecked_and_new_risk_is_selected_on_next_ordinary_refresh(weather, tmp_path, monkeypatch):
    nws_provider(weather, monkeypatch)
    run(weather, tmp_path)
    assert not weather["points"] and not weather["ensembles"]
    weather["clock"] += timedelta(minutes=20)
    nws_provider(weather, monkeypatch, rain=.8)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.ensemble_status == "full_members" and fc.run_time == weather["clock"]


def test_lean_fallback_requests_only_signal_fields_at_lower_weight(weather, tmp_path, monkeypatch):
    weather.update(wind=1, temp=75)
    original, requests = OM.fetch_forecast, []
    def fetch(points, **kwargs):
        requests.append(kwargs)
        return original(points, **kwargs)
    monkeypatch.setattr(OM, "fetch_forecast", fetch)
    run(weather, tmp_path)
    assert len(requests) == 1
    params = OM.build_params([(30, -96)], models=requests[0]["models"], hourly=requests[0]["hourly"])
    assert set(params["hourly"].split(",")) == {"temperature_2m", "wind_speed_10m", "precipitation"}
    from pipeline.weather.rate_limit import query_weight
    assert query_weight(OM.FORECAST_URL, params) == 1.5


def test_gefs_component_change_refetches_gefs_only_then_ifs_change_refetches_ifs_only(weather, tmp_path):
    run(weather, tmp_path)
    weather["versions"]["gefs"]["ncep_gefs05"]["snapshot"] = 2
    run(weather, tmp_path)
    assert [source for source, _ in weather["ensembles"]] == ["ifs", "gefs", "gefs"]
    weather["versions"]["ifs"]["ecmwf_ifs025_ensemble"]["snapshot"] = 2
    run(weather, tmp_path)
    assert [source for source, _ in weather["ensembles"]] == ["ifs", "gefs", "gefs", "ifs"]


def test_point_retrieval_advances_clock_without_false_stale_selection(weather, tmp_path, monkeypatch):
    weather.update(wind=1, temp=70)
    original = OM.fetch_forecast
    def fetch(*args, **kwargs):
        response = original(*args, **kwargs)
        weather["clock"] += timedelta(seconds=10)
        return response
    monkeypatch.setattr(OM, "fetch_forecast", fetch)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.run_time == weather["clock"]
    assert fc.ensemble_status == "not_sampled_below_signal_buffer"
    assert not weather["ensembles"]


def test_benign_game_not_sampled_active_signal_still_retained(weather, tmp_path):
    weather.update(wind=1, temp=70)
    _, result = run(weather, tmp_path)
    assert not weather["ensembles"]
    fc = next(iter(result.values()))
    assert fc.ensemble_status == "not_sampled_below_signal_buffer" and fc.wind_p90 is None
    (tmp_path / "alerts.json").write_text(json.dumps({"schema_version": 1, "records": {"k": {"family": "edge", "game_id": fc.game_id, "notification_active": True}}}))
    _, result = run(weather, tmp_path)
    assert next(iter(result.values())).ensemble_screen_reasons == ["existing_active_signal"]
    assert len(weather["ensembles"]) == 2


def test_partial_source_failure_preserves_real_members_without_mean_fallback(weather, tmp_path):
    weather["fail"].add("gefs")
    ctx, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.wind_p90 is not None and fc.ensemble_members == 51
    assert fc.ensemble_status == "partial_members_degraded"
    assert set(fc.ensemble_fetched_at) == {"ifs"}
    assert any("429" in d.reason for d in ctx.degradations)


def test_closed_roof_is_explicit_and_not_claimed_below_weather_buffer(weather, tmp_path):
    ctx = RunContext("nfl", git_sha="test-sha")
    group, stadiums = games()
    result = build.stage_weather(ctx, "nfl", group, stadiums, NullRawStore("nfl", ctx.run_id),
                                 {group[0].game_id: "closed"}, state_dir=tmp_path)
    fc = next(iter(result.values()))
    assert fc.ensemble_status == "not_sampled_closed_roof" and not weather["ensembles"]


def test_unverified_metadata_never_reuses_old_source_as_fresh(weather, tmp_path):
    run(weather, tmp_path)
    weather["fail"].update({"meta_ifs", "meta_gefs"})
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.ensemble_status == "unavailable_degraded" and fc.wind_p90 is None
    assert fc.ensemble_fetched_at == {} and len(weather["ensembles"]) == 2


def test_source_transition_during_fetch_discards_unverified_payload(weather, tmp_path):
    weather["transition"] = "ifs"
    ctx, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.ensemble_members == 31 and fc.ensemble_status == "partial_members_degraded"
    assert "ifs" not in fc.ensemble_source_versions
    assert any("changed during retrieval" in d.reason for d in ctx.degradations)


def test_later_batch_failure_keeps_earlier_selected_locations_and_attempts_every_game(weather, tmp_path):
    ctx, result = run(weather, tmp_path, count=41)
    assert len(result) == 41
    assert [len(batch) for source, batch in weather["ensembles"] if source == "ifs"] == [32, 9]
    assert [len(batch) for source, batch in weather["ensembles"] if source == "gefs"] == [41]
    assert result[games(41)[0][0].game_id].ensemble_status == "full_members"
    assert result[games(41)[0][-1].game_id].ensemble_status == "partial_members_degraded"
    assert all(fc.ensemble_eligible for fc in result.values())
    assert any("successful batches retained" in d.reason for d in ctx.degradations)


def test_same_workflow_raw_point_handoff_preserves_time_reweights_and_ordinary_cycle_refetches(weather, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    handoff = tmp_path / "handoff"
    _, first = run(weather, tmp_path, handoff=handoff)
    weather["clock"] += timedelta(minutes=10)
    _, second = run(weather, tmp_path, reuse=handoff)
    a, b = next(iter(first.values())), next(iter(second.values()))
    assert len(weather["points"]) == 2 and len(weather["ensembles"]) == 2
    assert b.run_time == a.run_time and b.lead_hours < a.lead_hours
    monkeypatch.setenv("GITHUB_RUN_ID", "124")
    run(weather, tmp_path, reuse=handoff)
    assert len(weather["points"]) == 4


def test_cached_members_and_handoff_recompute_climatology_weights_at_current_clock(weather, tmp_path, monkeypatch):
    cell = SimpleNamespace(wind_mean=4, wind_p10=2, wind_p50=4, wind_p90=6, gust_mean=8, temp_mean=50, rain_freq=0)
    monkeypatch.setattr(M, "_default_climo", lambda: SimpleNamespace(lookup=lambda *a, **k: cell))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    weather["clock"] = NOW - timedelta(hours=24)
    handoff = tmp_path / "handoff"
    _, first = run(weather, tmp_path, handoff=handoff)
    weather["clock"] += timedelta(minutes=20)
    _, second = run(weather, tmp_path, reuse=handoff)
    a, b = next(iter(first.values())), next(iter(second.values()))
    assert b.blend_w > a.blend_w
    assert b.wind_fg != a.wind_fg and b.wind_p90 != a.wind_p90
    assert b.ensemble_fetched_at == a.ensemble_fetched_at and b.run_time == a.run_time
    assert len(weather["points"]) == 2 and len(weather["ensembles"]) == 2


@pytest.mark.parametrize("change", ["stale", "future", "attempt", "sha", "venue", "kickoff", "units", "invalid"])
def test_point_handoff_rejects_incompatible_inputs(weather, tmp_path, monkeypatch, change):
    from pipeline.weather import point_handoff
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    handoff = tmp_path / "handoff"
    run(weather, tmp_path, handoff=handoff)
    group, stadiums = games()
    ctx = RunContext("nfl", git_sha="test-sha")
    if change == "stale":
        weather["clock"] += timedelta(minutes=31)
    elif change == "future":
        weather["clock"] -= timedelta(minutes=1)
    elif change == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    elif change == "sha":
        ctx.git_sha = "different"
    elif change == "venue":
        stadiums[group[0].game_id] = replace(stadiums[group[0].game_id], lat=31)
    elif change == "kickoff":
        group[0] = replace(group[0], kickoff_utc=KO + timedelta(hours=1))
    elif change == "units":
        monkeypatch.setattr(OM, "UNIT_PARAMS", {"wind_speed_unit": "kmh"})
    else:
        (handoff / "nfl.json").write_text("NaN")
    assert not point_handoff.read(handoff, ctx, "nfl", OM, {(30, -96): group}, stadiums, {}, {(30, -96): set(HOURS)})


@pytest.mark.parametrize("spread,expected", [(20, False), (10, True), (None, True)])
def test_cfb_uses_current_consensus_opener_and_preserves_active_alert(weather, tmp_path, spread, expected):
    group, stadiums = games()
    group = [replace(group[0], sport="cfb")]
    gid = group[0].game_id
    openers = {"openers": {}}
    if spread is not None:
        openers["openers"][f"{gid}|spread|home|betcris"] = {"line": spread}
    ctx = RunContext("cfb", git_sha="test-sha")
    result = build.stage_weather(ctx, "cfb", group, stadiums, NullRawStore("cfb", ctx.run_id), {},
                                 state_dir=tmp_path, openers=openers)
    fc = result[gid]
    assert fc.ensemble_eligible is expected
    if not expected:
        assert len(weather["points"]) == 1 and not weather["ensembles"]
        assert fc.ensemble_status == "not_sampled_ineligible_signal"
        (tmp_path / "alerts.json").write_text(json.dumps({"schema_version": 1, "records": {"k": {
            "family": "edge", "game_id": gid, "notification_active": True}}}))
        ctx = RunContext("cfb", git_sha="test-sha")
        result = build.stage_weather(ctx, "cfb", group, stadiums, NullRawStore("cfb", ctx.run_id), {},
                                     state_dir=tmp_path, openers=openers)
        assert result[gid].ensemble_eligible and len(weather["ensembles"]) == 2


def test_cfb_captures_new_openers_before_weather_selection(weather, tmp_path, monkeypatch):
    current = {"openers": {"new": {"line": 20}}}
    calls = []
    monkeypatch.setattr(build, "stage_stadiums", lambda *a: None)
    monkeypatch.setattr(build, "stage_schedule", lambda *a: ([], []))
    def odds(*args, **kwargs):
        calls.append("odds")
        return SimpleNamespace(openers=current)
    class StopAfterWeather(Exception):
        pass
    def wx(*args, **kwargs):
        assert calls == ["odds"] and kwargs["openers"] is current
        raise StopAfterWeather
    monkeypatch.setattr(build, "stage_odds", odds)
    monkeypatch.setattr(build, "stage_weather", wx)
    with pytest.raises(StopAfterWeather):
        build.run_sport(RunContext("cfb"), "cfb", NullRawStore("cfb", "test"), 2026, state_dir=tmp_path)


def test_global_candidate_fetches_only_missing_fields_without_restamping_signals(weather, tmp_path, monkeypatch):
    from pipeline.weather import first_pass as FP
    requests = []
    def fetch(points, **kwargs):
        requests.append(kwargs)
        fields = kwargs["hourly"]
        if fields == FP.EXTRA_HOURLY:
            weather["clock"] += timedelta(seconds=5)
        rows = [HourlyRow(t=t, wind=99 if fields == FP.EXTRA_HOURLY else 12,
                          temp=99 if fields == FP.EXTRA_HOURLY else 50, precip=99 if fields == FP.EXTRA_HOURLY else 0,
                          gust=20 if fields == FP.EXTRA_HOURLY else None, dir=180 if fields == FP.EXTRA_HOURLY else None,
                          pop=0 if fields == FP.EXTRA_HOURLY else None) for t in HOURS]
        return [ParsedLocation(*point, models={model: rows for model in kwargs["models"].split(",")}) for point in points]
    monkeypatch.setattr(OM, "fetch_forecast", fetch)
    ctx, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert [x["hourly"] for x in requests] == [FP.HOURLY, FP.EXTRA_HOURLY]
    assert fc.wind_fg == 12 and fc.temp_fg == 50 and fc.rain_fg_mm == 0 and fc.gust_fg == 20
    assert fc.run_time == NOW and fc.point_stage == "refined_split_fields"
    assert ctx.weather_state["point_meta"][(30, -96)]["details_fetched_at"] == weather["clock"].isoformat()
    from pipeline.weather.rate_limit import query_weight
    assert query_weight(OM.FORECAST_URL, OM.build_params([(30, -96)], hourly=FP.EXTRA_HOURLY)) == 1.5


def test_supplemental_failure_keeps_first_pass_signal_data_and_timestamp(weather, tmp_path, monkeypatch):
    from pipeline.weather import first_pass as FP
    def fetch(points, **kwargs):
        if kwargs["hourly"] == FP.EXTRA_HOURLY:
            weather["clock"] += timedelta(seconds=5)
            raise RuntimeError("detail request failed")
        rows = [HourlyRow(t=t, wind=12, temp=50, precip=0) for t in HOURS]
        return [ParsedLocation(*point, models={model: rows for model in kwargs["models"].split(",")}) for point in points]
    monkeypatch.setattr(OM, "fetch_forecast", fetch)
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.wind_fg == 12 and fc.temp_fg == 50 and fc.run_time == NOW and fc.gust_fg is None
    assert fc.point_stage == "global_first_pass" and fc.ensemble_eligible


def test_distant_members_keep_old_versions_until_twelve_hour_expiry(weather, tmp_path):
    weather["clock"] = NOW - timedelta(days=2)
    for versions in weather["versions"].values():
        for value in versions.values():
            value["last_run_initialisation_time"] = int((weather["clock"] - timedelta(hours=4)).timestamp())
    _, first = run(weather, tmp_path)
    old = next(iter(first.values()))
    weather["clock"] += timedelta(hours=3)
    for values in weather["versions"].values():
        for value in values.values():
            value["snapshot"] = 2
    _, second = run(weather, tmp_path)
    aged = next(iter(second.values()))
    assert len(weather["ensembles"]) == 2 and aged.ensemble_status == "aged_members"
    assert aged.ensemble_aged_sources == ["ifs", "gefs"] and aged.ensemble_fetched_at == old.ensemble_fetched_at
    assert aged.ensemble_source_versions == old.ensemble_source_versions
    assert aged.run_time == weather["clock"]  # point receipt is fresh; members keep their own older receipt
    weather["clock"] += timedelta(hours=9)
    _, third = run(weather, tmp_path)
    fresh = next(iter(third.values()))
    assert len(weather["ensembles"]) == 4 and fresh.ensemble_status == "full_members"
    assert fresh.ensemble_aged_sources == [] and fresh.ensemble_source_versions != old.ensemble_source_versions


@pytest.mark.parametrize("mode", ["near", "active"])
def test_near_or_active_games_refetch_changed_cycles_immediately(weather, tmp_path, mode):
    weather["clock"] = NOW - timedelta(days=2 if mode == "active" else 1)
    _, first = run(weather, tmp_path)
    gid = next(iter(first))
    weather["clock"] += timedelta(hours=1)
    for values in weather["versions"].values():
        for value in values.values():
            value["snapshot"] = 2
    if mode == "active":
        (tmp_path / "alerts.json").write_text(json.dumps({"schema_version": 1, "records": {"k": {
            "family": "edge", "game_id": gid, "notification_active": True}}}))
    _, result = run(weather, tmp_path)
    assert len(weather["ensembles"]) == 4 and result[gid].ensemble_aged_sources == []


def test_distant_reuse_still_requires_stable_current_source_metadata(weather, tmp_path):
    weather["clock"] = NOW - timedelta(days=2)
    run(weather, tmp_path)
    weather["clock"] += timedelta(hours=1)
    weather["fail"].update({"meta_ifs", "meta_gefs"})
    _, result = run(weather, tmp_path)
    fc = next(iter(result.values()))
    assert fc.ensemble_status == "unavailable_degraded" and fc.wind_p90 is None
    assert fc.ensemble_fetched_at == {} and fc.ensemble_aged_sources == []


def test_same_workflow_new_eligible_opener_refines_previously_skipped_points(weather, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "opener-change")
    handoff = tmp_path / "handoff"
    group, stadiums = games()
    group = [replace(group[0], sport="cfb")]
    gid = group[0].game_id
    def ctx():
        return RunContext("cfb", git_sha="test-sha")
    first_ctx = ctx()
    first_ctx.weather_state["write_point_dir"] = handoff
    disqualified = {"openers": {f"{gid}|spread|home|consensus": {"line": 20}}}
    result = build.stage_weather(first_ctx, "cfb", group, stadiums, NullRawStore("cfb", first_ctx.run_id), {},
                                 state_dir=tmp_path, openers=disqualified)
    assert not result[gid].ensemble_eligible and len(weather["points"]) == 1
    second_ctx = ctx()
    second_ctx.weather_state["reuse_point_dir"] = handoff
    eligible = {"openers": {f"{gid}|spread|home|consensus": {"line": 8}}}
    result = build.stage_weather(second_ctx, "cfb", group, stadiums, NullRawStore("cfb", second_ctx.run_id), {},
                                 state_dir=tmp_path, openers=eligible)
    assert result[gid].ensemble_eligible and result[gid].point_stage == "refined_multimodel"
    assert len(weather["points"]) == 2 and len(weather["ensembles"]) == 2


def test_same_workflow_nws_issue_age_is_rechecked_before_reuse(weather, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "nws-age")
    handoff = tmp_path / "handoff"
    weather.update(wind=1, temp=75)
    nws_provider(weather, monkeypatch, wind=1, temp=75, age_hours=12)
    _, first = run(weather, tmp_path, handoff=handoff)
    assert next(iter(first.values())).point_stage == "nws_first_pass" and not weather["points"]
    weather["clock"] += timedelta(minutes=10)
    _, second = run(weather, tmp_path, reuse=handoff)
    fc = next(iter(second.values()))
    assert fc.point_stage == "refined_multimodel" and len(weather["points"]) == 1
    assert fc.source != "nws" and not fc.point_aged and not weather["ensembles"]
