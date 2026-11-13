"""Decision boundaries and consumer contracts; the frozen v1 model is independent."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from pipeline import alerts
from pipeline.model import process as P

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def card(sport="cfb", **weather):
    return {"sport": sport, "status": "scheduled", "kickoff_utc": "2026-10-18T20:00:00Z",
            "stadium": {"roof_type": "open", "roof_state": "outdoors"},
            "consensus": {"spread_open": 0}, "weather": {"wind_fg": 3, "temp_fg": 60, "rain_fg": 0, **weather},
            "home_temp": 50, "away_temp": 50, "travel_alt": 900}


@pytest.fixture(autouse=True)
def default_gates(monkeypatch):
    for key in ("PROCESS_TEMP_GATE", "PROCESS_TEMP_GATE_CFB", "PROCESS_TEMP_GATE_NFL", "PROCESS_UNDER_PROB_MOVE_MAX"):
        monkeypatch.delenv(key, raising=False)


def evaluate(value):
    record = P.evaluate_card(value, now=NOW)
    assert record["decision_version"] == P.DECISION_VERSION
    assert record["discovery_state"] in set(P.DiscoveryState)
    assert set(record["discovery_reasons"]) <= set(P.Reason)
    assert set(record["ineligible_reasons"]) <= set(P.Reason)
    return record


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("wind,state,band", [(11.99, "quiet", "none"), (12, "near", "watch"),
                                           (12.01, "near", "watch"), (14.99, "near", "watch"),
                                           (15, "signal", "flag"), (15.01, "signal", "flag")])
def test_wind_boundaries(sport, wind, state, band):
    record = evaluate(card(sport, wind_fg=wind))
    assert (record["discovery_state"], record["wind_band"]) == (state, band)
    assert record["flags"] == ([f"{sport.upper()} Wind"] if band == "flag" else [])


@pytest.mark.parametrize("rain,state,tier", [(4.99, "quiet", "ignore"), (5, "near", "watch"),
                                           (5.01, "near", "watch"), (14.99, "near", "watch"),
                                           (15, "near", "elevated"), (15.01, "near", "elevated"),
                                           (22.99, "near", "elevated"), (23, "signal", "signal_amount"),
                                           (23.01, "signal", "signal_amount")])
def test_rain_boundaries(rain, state, tier):
    value = card(rain_fg=rain, models=[{"id": P.ECMWF_IFS, "rain_mm": rain}, {"id": "nws", "rain_mm": rain}])
    record = evaluate(value)
    assert (record["discovery_state"], record["rain_tier"]) == (state, tier)


@pytest.mark.parametrize("models,status", [([], "missing_ecmwf"),
    ([{"id": P.ECMWF_IFS, "rain_mm": 25}], "unverified"),
    ([{"id": P.ECMWF_IFS, "rain_mm": 25}, {"id": "nws", "rain_mm": 35}], "agree"),
    ([{"id": P.ECMWF_IFS, "rain_mm": 25}, {"id": "nws", "rain_mm": 35.01}], "disagree"),
    ([{"id": P.ECMWF_IFS, "rain_mm": 25}, {"id": "nws", "rain_mm": 22}, {"id": "gfs", "rain_mm": 30}], "agree")])
def test_rain_evidence_and_final_pill(models, status):
    value = card(rain_fg=25, models=models)
    value["signal"] = {"label": "Low (Rain)", "drivers": ["rain"]}
    record = evaluate(value)
    assert record["rain_agreement"] == status
    if status == "agree":
        assert record["display_label"] == "Low (Rain)"
        assert record["discovery_reasons"][0] == "rain_signal"
    else:
        assert record["display_muted"]
        assert record["display_label"] == ("Rain models disagree" if status == "disagree" else "Rain not confirmed")
        assert record["discovery_reasons"][0] == "rain_unconfirmed"
        value["weather"]["wind_fg"] = 15
        assert evaluate(value)["display_label"] == "Low (Wind)"


@pytest.mark.parametrize("roof_type,roof,eligible,reason", [
    ("dome", "open", False, "indoor"), (None, "closed", False, "closed_roof"),
    ("retractable", None, False, "roof_unconfirmed"), ("retractable", "outdoors", False, "roof_unconfirmed"),
    ("retractable", "indoor", False, "indoor"), ("retractable", "dome", False, "indoor"),
    ("retractable", "closed", False, "closed_roof"), ("retractable", "open", True, None),
    ("open", "closed", True, None), ("open", "indoor", True, None), (None, "indoor", True, None)])
def test_roof_policy_is_python_construction_policy(roof_type, roof, eligible, reason):
    value = card(wind_fg=15)
    value["stadium"] = {"roof_type": roof_type, "roof_state": roof}
    record = evaluate(value)
    assert record["eligible"] is eligible
    assert record["ineligible_reasons"] == ([] if reason is None else [reason])
    assert record["flags"] == (["CFB Wind"] if eligible else [])


@pytest.mark.parametrize("spread,state", [(None, "unknown"), (9.5, "signal"), (10, "signal"), (10.5, "ineligible")])
def test_spread_boundaries(spread, state):
    value = card(wind_fg=15)
    value["consensus"]["spread_open"] = spread
    record = evaluate(value)
    assert record["discovery_state"] == state
    if state in ("unknown", "ineligible"):
        assert record["ineligible_reasons"] == (["spread_unknown"] if spread is None else ["spread_outside"])


@pytest.mark.parametrize("temp", [64.99, 65, 65.01, None])
def test_temperature_gate_is_applied_and_published(monkeypatch, temp):
    value = card(wind_fg=15, temp_fg=temp)
    assert evaluate(value)["discovery_state"] == "signal"
    monkeypatch.setenv("PROCESS_TEMP_GATE", "1")
    record = evaluate(value)
    passes = temp is not None and temp < 65
    assert record["discovery_state"] == ("signal" if passes else "quiet")
    assert record["discovery_reasons"][0] == ("wind_signal" if passes else "temp_gate")
    assert record["thresholds"]["temp_gate"] is True


@pytest.mark.parametrize("line,blocked", [(45.0005, False), (45, False), (44.9995, True)])
def test_price_gate_boundary(line, blocked):
    value = card(wind_fg=15)
    value["odds"] = {"pinnacle": {"total": {"open_line": 47, "line": line, "under": -110, "open_under": -110}}}
    record = evaluate(value)
    assert record["price_gate_passed"] is not blocked
    assert record["discovery_state"] == ("moved" if blocked else "signal")
    if blocked:
        assert record["discovery_reasons"] == ["price_moved"]


@pytest.mark.parametrize("temp,alt,home,away,flags", [
    (80, 800, 50, 50, []), (80.01, 800, 50, 50, ["Heat"]),
    (75, 801, 50, 50, []), (75.01, 801, 50, 50, ["Alt+Heat"]),
    (81, 801, 57, 50, ["Alt+Heat"]), (81, 801, 50, 57, ["Alt+Heat"]),
    (81, 801, 56.99, 56.99, ["Heat", "Alt+Heat"])])
def test_heat_flags_do_not_require_wind(temp, alt, home, away, flags):
    value = card(temp_fg=temp, wind_fg=None)
    value.update(travel_alt=alt, home_temp=home, away_temp=away)
    assert evaluate(value)["flags"] == flags


def test_ordered_reasons_and_probability():
    value = card(wind_fg=12, rain_fg=16, ecmwf_apart=True)
    assert evaluate(value)["discovery_reasons"] == ["wind_watch", "rain_elevated", "ecmwf_apart", "opener_unknown"]
    value["discovery"] = {"signal_probability": .0499}
    assert evaluate(value)["discovery_reasons"] == ["low_probability"]
    value["discovery"]["signal_probability"] = .05
    assert evaluate(value)["discovery_state"] == "near"
    value["weather"]["wind_fg"] = None
    assert evaluate(value)["discovery_reasons"] == ["forecast_incomplete"]
    value["status"] = "in_progress"
    assert evaluate(value)["discovery_reasons"] == ["not_upcoming"]


def test_published_alerts_are_read_without_evaluation(monkeypatch):
    value = card(wind_fg=15)
    value["process"] = evaluate(value)
    value["weather"]["wind_fg"] = 0
    monkeypatch.setattr(P, "evaluate_card", lambda *args, **kwargs: pytest.fail("published record was recomputed"))
    assert alerts._signal_label(value) == "Low (Wind)"
    assert alerts._opener_line_note(value) == ["Opener line unknown"]
    assert alerts._qualifying_signal(value, alerts.Config())


def test_shared_legacy_fallback_and_deliberate_recheck():
    value = card(wind_fg=15)
    value["process"] = evaluate(value)
    changed = deepcopy(value)
    changed["weather"]["wind_fg"] = 0
    assert P.decision_for(changed) is changed["process"]
    assert P.decision_for(changed, recheck=True, now=NOW)["clear_signal_label"] == "No Impact"
    del changed["process"]
    assert P.decision_for(changed)["alert_label"] == "No Impact"
