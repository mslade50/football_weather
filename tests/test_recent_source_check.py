"""Regression for successful NFL checks followed by CFB metadata TLS failures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline.build import _fetch_point_batches
from pipeline.run_context import RunContext
from pipeline.weather import openmeteo as OM
from pipeline.weather import selective
from pipeline.weather.member_cache import SOURCES, MemberCache, parameter_signature
from pipeline.weather.parsers.ensemble import EnsembleLocation, Member

NOW = datetime(2026, 10, 3, 13, 20, 33, tzinfo=timezone.utc)
POINTS = [(32., -96.), (44., -93.)]
HOURS = {NOW + timedelta(hours=i) for i in range(1, 7)}


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    state = {"clock": NOW, "calls": [], "fail": False, "transition": False, "captures": []}
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: state["clock"]))
    ctx = RunContext("all", run_id="same-pw-run", git_sha="same-commit", started_at=NOW)
    sig = parameter_signature(OM.ENSEMBLE_HOURLY, OM.ENSEMBLE_UNIT_PARAMS)
    cache = MemberCache(tmp_path / "ensemble_cache.json", sig)
    versions = {}
    for source, (_, datasets) in SOURCES.items():
        versions[source] = {d: {
            "last_run_initialisation_time": int((NOW - timedelta(hours=4)).timestamp()),
            "last_run_modification_time": int((NOW - timedelta(hours=1)).timestamp()),
            "last_run_availability_time": int((NOW - timedelta(hours=1)).timestamp()),
            "update_interval_seconds": 21600, "data_end_time": int((NOW + timedelta(days=15)).timestamp()),
        } for d in datasets}
        model, count = ("ecmwf_ifs025_ensemble", 51) if source == "ifs" else ("ncep_gefs_seamless", 31)
        for point in POINTS:
            members = {}
            for i in range(count):
                m = Member(model, "control" if i == 0 else f"member{i:02d}", [i + 1.] * 6, [i + 3.] * 6, [i % 3.] * 6)
                members[m.key] = m
            cache.put(source, point, versions[source], EnsembleLocation(*point, sorted(HOURS), {}, members), HOURS, NOW - timedelta(minutes=7))
    cache.save("light-run", NOW)
    # The dependent job loads the R2-restored cache afresh; no handoff metadata.
    ctx.weather_state["member_cache"] = MemberCache(cache.path, sig)

    def metadata(source, **kwargs):
        state["calls"].append(source)
        if state["fail"]:
            raise TimeoutError("TLS handshake timed out")
        result = deepcopy(versions[source])
        if state["transition"] and len(state["calls"]) % 2 == 0:
            next(iter(result.values()))["last_run_modification_time"] += 1
        return result

    def no_new_members(*args, **kwargs):
        pytest.fail("fully covered cache must not retrieve new members")

    om = SimpleNamespace(ENSEMBLE_HOURLY=OM.ENSEMBLE_HOURLY, ENSEMBLE_UNIT_PARAMS=OM.ENSEMBLE_UNIT_PARAMS,
                         fetch_model_versions=metadata, fetch_ensemble=no_new_members, ENSEMBLE_BATCH_SIZE=20)

    def run(point, sport):
        return selective.members(ctx, om, [point], {point: (min(HOURS), max(HOURS))}, {point: HOURS},
                                 lambda *args: state["captures"].append(args), _fetch_point_batches, sport=sport)
    return state, ctx, om, run


def test_nfl_stable_checks_cover_cfb_cache_without_redundant_tls_handshakes(scenario):
    state, ctx, om, run = scenario
    original, trace = run(POINTS[0], "nfl")
    assert original[POINTS[0]].n_members() == 82 and state["calls"] == ["ifs", "ifs", "gefs", "gefs"]
    state.update(clock=NOW + timedelta(seconds=20), fail=True)
    cfb, other = run(POINTS[1], "cfb")
    assert cfb[POINTS[1]].n_members() == 82
    assert len(state["calls"]) == 4  # no failed handshakes, forecast or member requests
    assert other[POINTS[1]]["fetched_at"] == trace[POINTS[0]]["fetched_at"]
    assert other[POINTS[1]]["source_versions"] == trace[POINTS[0]]["source_versions"]
    assert other[POINTS[1]]["cached_sources"] == ["ifs", "gefs"]
    records = [args[1] for args in state["captures"] if args[0].endswith("_reused")]
    assert len(records) == 2 and all(r["verified_at"] == NOW.isoformat() and r["cache_only"] for r in records)
    assert all(r["run_id"] == ctx.run_id and r["git_sha"] == ctx.git_sha for r in records)


@pytest.mark.parametrize("change", ["expired", "future_clock", "run", "commit", "signature", "overdue_metadata"])
def test_short_check_is_not_trusted_outside_its_provenance_or_age(scenario, change):
    state, ctx, om, run = scenario
    run(POINTS[0], "nfl")
    state["fail"] = True
    if change == "expired":
        state["clock"] += timedelta(seconds=60)
    elif change == "future_clock":
        state["clock"] -= timedelta(seconds=1)
    elif change == "run":
        ctx.run_id = "other-run"
    elif change == "commit":
        ctx.git_sha = "other-commit"
    elif change == "signature":
        om.ENSEMBLE_UNIT_PARAMS = {**om.ENSEMBLE_UNIT_PARAMS, "wind_speed_unit": "kmh"}
    else:
        for check in ctx.weather_state["verified_member_sources"].values():
            for v in check["versions"].values():
                v["last_run_initialisation_time"] = int((NOW - timedelta(hours=19)).timestamp())
    locations, coverage = run(POINTS[1], "cfb")
    assert len(state["calls"]) == 6
    if change == "signature":
        assert not locations
    else:
        assert locations[POINTS[1]].n_members() == 82
        assert coverage[POINTS[1]]["unverified_sources"] == ["ifs", "gefs"]
    assert set(coverage[POINTS[1]]["errors"]) == {"ifs", "gefs"}


@pytest.mark.parametrize("gap", ["entry", "hour"])
def test_missing_source_window_requires_new_check_and_preserves_other_verified_source(scenario, gap):
    state, ctx, om, run = scenario
    run(POINTS[0], "nfl")
    cache = ctx.weather_state["member_cache"]
    key = cache.key("ifs", POINTS[1])
    if gap == "entry":
        cache.entries.pop(key)
    else:
        location = cache.entries[key]["location"]
        location["times"].pop(0)
        for member in location["members"].values():
            for field in ("wind", "gust", "precip"):
                member[field].pop(0)
    state.update(clock=NOW + timedelta(seconds=20), fail=True)
    locations, coverage = run(POINTS[1], "cfb")
    assert locations[POINTS[1]].n_members() == 31
    assert len(state["calls"]) == 5 and state["calls"][-1] == "ifs"
    assert set(coverage[POINTS[1]]["errors"]) == {"ifs"}


def test_changed_source_between_checks_does_not_create_a_trusted_snapshot(scenario):
    state, ctx, om, run = scenario
    state["transition"] = True
    locations, coverage = run(POINTS[0], "nfl")
    assert locations[POINTS[0]].n_members() == 82 and not ctx.weather_state.get("verified_member_sources")
    assert coverage[POINTS[0]]["unverified_sources"] == ["ifs", "gefs"]
    state.update(transition=False, fail=True)
    locations, coverage = run(POINTS[1], "cfb")
    assert locations[POINTS[1]].n_members() == 82 and len(state["calls"]) == 6
    assert coverage[POINTS[1]]["unverified_sources"] == ["ifs", "gefs"]


def test_new_member_retrieval_requires_both_fresh_checks_even_inside_sixty_seconds(scenario):
    state, ctx, om, run = scenario
    run(POINTS[0], "nfl")
    cache = ctx.weather_state["member_cache"]
    entry = cache.entries.pop(cache.key("ifs", POINTS[1]))
    from pipeline.weather.member_cache import decode_location
    fresh = decode_location(entry["location"])
    fetched = []

    def fetch(batch, **kwargs):
        fetched.append((batch, kwargs["models"]))
        return [fresh]

    om.fetch_ensemble = fetch
    state["clock"] += timedelta(seconds=20)
    locations, trace = run(POINTS[1], "cfb")
    assert locations[POINTS[1]].n_members() == 82
    assert fetched == [([POINTS[1]], "ecmwf_ifs025")]
    assert state["calls"] == ["ifs", "ifs", "gefs", "gefs", "ifs", "ifs"]
    assert trace[POINTS[1]]["cached_sources"] == ["gefs"]
    assert trace[POINTS[1]]["fetched_at"]["ifs"] == state["clock"].isoformat()
    assert trace[POINTS[1]]["fetched_at"]["gefs"] == (NOW - timedelta(minutes=7)).isoformat()
