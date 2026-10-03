"""Raw cache invariants: source versions, original timestamps and exact members."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.weather.member_cache import MemberCache, combine, parameter_signature, version
from pipeline.weather.merge import ensemble_stats
from pipeline.weather.parsers.ensemble import EnsembleLocation, Member

NOW = datetime(2026, 10, 2, 20, tzinfo=timezone.utc)
POINT = (32.0, -96.0)
HOURS = [NOW + timedelta(days=2, hours=i) for i in range(6)]
SIG = parameter_signature("wind_speed_10m,wind_gusts_10m,precipitation", {"wind_speed_unit": "mph"})


def versions(at=NOW):
    return {"last_run_initialisation_time": int((at - timedelta(hours=4)).timestamp()),
            "last_run_modification_time": int((at - timedelta(hours=1)).timestamp()),
            "last_run_availability_time": int((at - timedelta(hours=1)).timestamp()),
            "update_interval_seconds": 21600, "data_end_time": int((at + timedelta(days=15)).timestamp())}


def location(source="ifs", hours=HOURS, null=False):
    model, count = ("ecmwf_ifs025_ensemble", 51) if source == "ifs" else ("ncep_gefs_seamless", 31)
    members = {}
    for i in range(count):
        member = Member(model, "control" if i == 0 else f"member{i:02d}",
                        [None if null and i == 0 else float(i) for _ in hours],
                        [float(i) + 2 for _ in hours], [float(i % 3) for _ in hours])
        members[member.key] = member
    return EnsembleLocation(*POINT, list(hours), {}, members)


@pytest.mark.parametrize("mode", ["future", "settling", "overdue", "badtype", "init_after_availability"])
def test_source_metadata_rejects_unverifiable_versions(mode):
    data = versions()
    if mode == "future":
        data["last_run_modification_time"] = int((NOW + timedelta(seconds=1)).timestamp())
    elif mode == "settling":
        data["last_run_availability_time"] = int((NOW - timedelta(minutes=5)).timestamp())
    elif mode == "overdue":
        data["last_run_availability_time"] = int((NOW - timedelta(hours=8)).timestamp())
    elif mode == "badtype":
        data["update_interval_seconds"] = True
    else:
        data["last_run_initialisation_time"] = data["last_run_availability_time"] + 1
    with pytest.raises(ValueError):
        version(data, NOW)


def test_metadata_and_gefs_components_are_independent():
    assert version(versions(), NOW) == versions()
    cache = MemberCache(None, SIG)
    vector = {"ncep_gefs025": versions(), "ncep_gefs05": versions()}
    cache.put("gefs", POINT, vector, location("gefs"), set(HOURS), NOW)
    changed = deepcopy(vector)
    changed["ncep_gefs05"]["last_run_modification_time"] += 1
    assert cache.get("gefs", POINT, changed, set(HOURS), now=NOW) is None
    assert cache.get("gefs", POINT, vector, set(HOURS), now=NOW)


def test_disk_roundtrip_preserves_original_raw_members_and_statistics(tmp_path):
    path = tmp_path / "ensemble_cache.json"
    cache = MemberCache(path, SIG)
    loc = location()
    cache.put("ifs", POINT, versions(), loc, set(HOURS), NOW)
    cache.save("run", NOW)
    later = NOW + timedelta(hours=1)
    recovered, stamp = MemberCache(path, SIG).get("ifs", POINT, versions(), set(HOURS), now=later)
    assert stamp == NOW.isoformat()
    assert recovered == loc
    assert ensemble_stats(combine([recovered, location("gefs")]), HOURS[1:4]) == ensemble_stats(combine([loc, location("gefs")]), HOURS[1:4])


def test_nulls_reused_but_missing_hours_rescheduling_or_changed_venue_miss():
    cache = MemberCache(None, SIG)
    cache.put("ifs", POINT, versions(), location(null=True), set(HOURS), NOW)
    hit, _ = cache.get("ifs", POINT, versions(), set(HOURS), now=NOW)
    assert next(iter(hit.members.values())).wind[0] is None
    assert cache.get("ifs", POINT, versions(), {HOURS[-1] + timedelta(hours=1)}, now=NOW) is None
    assert cache.get("ifs", (33, -96), versions(), set(HOURS), now=NOW) is None


def test_shared_location_windows_keep_separate_retrieval_times():
    cache = MemberCache(None, SIG)
    cache.put("ifs", POINT, versions(), location(hours=HOURS[:3]), set(HOURS[:3]), NOW)
    later = NOW + timedelta(hours=1)
    cache.put("ifs", POINT, versions(), location(hours=HOURS[3:]), set(HOURS[3:]), later)
    assert cache.get("ifs", POINT, versions(), set(HOURS[:3]), now=later)[1] == NOW.isoformat()
    assert cache.get("ifs", POINT, versions(), set(HOURS[3:]), now=later)[1] == later.isoformat()
    assert cache.get("ifs", POINT, versions(), set(HOURS), now=later)[1] == NOW.isoformat()


def test_future_fetched_time_and_incorrect_model_rejected():
    cache = MemberCache(None, SIG)
    cache.put("ifs", POINT, versions(), location(), set(HOURS), NOW + timedelta(hours=1))
    assert cache.get("ifs", POINT, versions(), set(HOURS), now=NOW) is None
    assert cache.invalid
    with pytest.raises(ValueError):
        cache.put("ifs", POINT, versions(), location("gefs"), set(HOURS), NOW)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e400", "{}", "invalid"])
def test_corrupt_cache_is_ignored(tmp_path, bad):
    path = tmp_path / "cache.json"
    path.write_text(bad)
    cache = MemberCache(path, SIG)
    assert cache.invalid and not cache.entries


def test_units_or_parser_signature_changes_invalidate_disk_cache(tmp_path):
    path = tmp_path / "cache.json"
    cache = MemberCache(path, SIG)
    cache.put("ifs", POINT, versions(), location(), set(HOURS), NOW)
    cache.save("run", NOW)
    changed = {**SIG, "units": {"wind_speed_unit": "kmh"}}
    assert not MemberCache(path, changed).entries
    payload = json.loads(path.read_text())
    payload["entries"][MemberCache.key("ifs", POINT)] = "invalid"
    path.write_text(json.dumps(payload))
    bad = MemberCache(path, SIG)
    assert bad.get("ifs", POINT, versions(), set(HOURS), now=NOW) is None
    assert bad.invalid


def test_previous_version_reuse_preserves_original_data_and_enforces_age_hours_source():
    cache = MemberCache(None, SIG)
    original = {"ecmwf_ifs025_ensemble": versions()}
    current = {"ecmwf_ifs025_ensemble": versions(NOW + timedelta(hours=6))}
    cache.put("ifs", POINT, original, location(), set(HOURS), NOW)
    hit = cache.previous("ifs", POINT, current, set(HOURS), now=NOW + timedelta(hours=11), max_age_h=12)
    assert hit == (location(), NOW.isoformat(), original)
    assert not cache.previous("ifs", POINT, current, set(HOURS), now=NOW + timedelta(hours=12), max_age_h=12)
    assert not cache.previous("ifs", POINT, current, {HOURS[-1] + timedelta(hours=1)}, now=NOW, max_age_h=12)
    assert not cache.previous("ifs", POINT, {"wrong_model": versions()}, set(HOURS), now=NOW, max_age_h=12)
    assert not cache.previous("ifs", POINT, current, set(HOURS), now=NOW, max_age_h=0)


def test_current_dataset_initialization_age_is_bounded_even_with_new_publication():
    data = versions()
    data["last_run_initialisation_time"] = int((NOW - timedelta(hours=19)).timestamp())
    with pytest.raises(ValueError, match="dataset initialization"):
        version(data, NOW)


@pytest.mark.parametrize("initialization", [None, "unknown", int((NOW - timedelta(hours=31)).timestamp()), int((NOW + timedelta(hours=1)).timestamp())])
def test_old_or_unknown_dataset_initialization_cannot_reuse_distant_members(initialization):
    cache = MemberCache(None, SIG)
    original = {"ecmwf_ifs025_ensemble": {**versions(), "last_run_initialisation_time": initialization}}
    current = {"ecmwf_ifs025_ensemble": versions(NOW + timedelta(hours=6))}
    cache.put("ifs", POINT, original, location(), set(HOURS), NOW)
    assert cache.previous("ifs", POINT, current, set(HOURS), now=NOW, max_age_h=12) is None
