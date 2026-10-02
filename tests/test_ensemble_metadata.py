"""Provider metadata URLs/version validation without live requests."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pipeline.weather import openmeteo as OM
from pipeline.weather.rate_limit import RequestBudget


def payload():
    now = datetime.now(timezone.utc)
    return {"last_run_initialisation_time": int((now - timedelta(hours=4)).timestamp()),
            "last_run_modification_time": int((now - timedelta(hours=1)).timestamp()),
            "last_run_availability_time": int((now - timedelta(hours=1)).timestamp()),
            "update_interval_seconds": 21600, "data_end_time": int((now + timedelta(days=15)).timestamp())}


def test_ifs_and_both_gefs_metadata_urls_are_verified_and_do_not_spend_forecast_quota():
    seen, captures = [], []
    def response(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=payload())
    budget = RequestBudget()
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        ifs = OM.fetch_model_versions("ifs", client=client, budget=budget, capture=lambda *args: captures.append(args))
        gefs = OM.fetch_model_versions("gefs", client=client, budget=budget)
    assert set(ifs) == {"ecmwf_ifs025_ensemble"}
    assert set(gefs) == {"ncep_gefs025", "ncep_gefs05"}
    assert seen == [f"https://ensemble-api.open-meteo.com/data/{name}/static/meta.json" for name in ("ecmwf_ifs025_ensemble", "ncep_gefs025", "ncep_gefs05")]
    assert len(captures) == 1 and not budget.requests


def test_late_component_metadata_is_unavailable_not_guessed_by_clock():
    data = payload()
    data["last_run_availability_time"] = int(datetime.now(timezone.utc).timestamp())
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as client:
        with pytest.raises(ValueError, match="settling"):
            OM.fetch_model_versions("gefs", client=client)


def test_metadata_quota_failure_cools_down_without_retries():
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(429, json={"error": True, "reason": "Daily limit exceeded"})
    budget = RequestBudget()
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(RuntimeError, match="metadata restricted"):
            OM.fetch_model_versions("gefs", client=client, budget=budget)
    assert len(calls) == 1 and budget.blocked_until > budget.clock()
