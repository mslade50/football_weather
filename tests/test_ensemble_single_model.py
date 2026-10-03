"""Regression: actual public API single-model response has unsuffixed fields."""
import json
from pathlib import Path

import httpx
import pytest

from pipeline.weather import openmeteo as OM
from pipeline.weather.member_cache import MemberCache, combine
from pipeline.weather.parsers.ensemble import parse_ensemble

FIXTURES = Path(__file__).parent / "fixtures/weather"


@pytest.mark.parametrize("requested,source,identity,count", [
    ("gfs_seamless", "gefs", "ncep_gefs_seamless", 31),
    ("ecmwf_ifs025", "ifs", "ecmwf_ifs025_ensemble", 51),
])
def test_single_model_response_keeps_control_and_all_perturbations(requested, source, identity, count):
    payload = json.loads((FIXTURES / f"ensemble_single_{source}.json").read_text(encoding="utf-8"))
    captured = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        location = OM.fetch_ensemble([(32.7795, -96.7598)], models=requested, client=client,
                                     capture=lambda name, raw, url: captured.append(raw))[0]
    assert location.models == [identity] and location.n_members() == count
    assert location.members[f"{identity}:control"].wind == payload["hourly"]["wind_speed_10m"]
    assert location.members[f"{identity}:member01"].precip == payload["hourly"]["precipitation_member01"]
    assert captured == [payload]  # raw response remains verbatim, unsuffixed
    MemberCache.validate_source(source, location)


def test_untagged_response_without_request_identity_is_rejected():
    payload = json.loads((FIXTURES / "ensemble_single_ifs.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="explicit requested model"):
        parse_ensemble(payload)


def test_two_explicit_sources_pool_all_eighty_two_real_members():
    locations = []
    for source, identity in (("ifs", "ecmwf_ifs025_ensemble"), ("gefs", "ncep_gefs_seamless")):
        payload = json.loads((FIXTURES / f"ensemble_single_{source}.json").read_text(encoding="utf-8"))
        locations += parse_ensemble(payload, model=identity)
    assert combine(locations).n_members() == 82


def test_existing_named_model_identity_is_not_overridden_by_request():
    payload = {"latitude": 32, "longitude": -96, "hourly": {
        "time": ["2026-10-03T13:00"], "wind_speed_10m_ncep_gefs_seamless": [1],
        "wind_gusts_10m_ncep_gefs_seamless": [2], "precipitation_ncep_gefs_seamless": [0],
    }}
    location = parse_ensemble(payload, model="ecmwf_ifs025_ensemble")[0]
    with pytest.raises(ValueError, match="model identity"):
        MemberCache.validate_source("ifs", location)
