"""Decision boundaries and consumer contracts; the frozen v1 model is independent."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import alerts
from pipeline.model import process as P

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def card(sport="cfb", **weather):
    return {"sport": sport, "status": "scheduled", "kickoff_utc": "2026-10-18T20:00:00Z",
            "signal": {"label": "Low Impact"},
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
    ("open", "closed", True, None), ("open", "indoor", True, None), (None, "indoor", False, "indoor")])
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


def test_ordered_reasons_and_ecmwf_apart_from_the_record():
    models = [{"id": P.ECMWF_IFS, "wind_mph": 12}, {"id": "nws", "wind_mph": 4}]
    value = card(wind_fg=12, rain_fg=16, models=models)
    record = evaluate(value)
    assert record["ecmwf_apart"] is True
    assert record["discovery_reasons"] == ["wind_watch", "rain_elevated", "ecmwf_apart", "opener_unknown"]
    # One source: the record's own comparison, not a separate weather.ecmwf_apart flag.
    flagged_only = card(wind_fg=12, rain_fg=16, ecmwf_apart=True)
    assert evaluate(flagged_only)["discovery_reasons"] == ["wind_watch", "rain_elevated", "opener_unknown"]
    value["weather"]["wind_fg"] = None
    value["weather"]["models"] = []
    assert evaluate(value)["discovery_reasons"] == ["forecast_incomplete"]
    value["status"] = "in_progress"
    assert evaluate(value)["discovery_reasons"] == ["not_upcoming"]


def test_signal_probability_is_gone_from_the_record():
    value = card(wind_fg=12)
    value["discovery"] = {"signal_probability": .0001}
    record = evaluate(value)
    assert "signal_probability" not in record and "signal_probability_min" not in record["thresholds"]
    assert record["discovery_state"] == "near"
    assert not hasattr(P.Reason, "LOW_PROBABILITY")


RAIN_AGREED = [{"id": P.ECMWF_IFS, "rain_mm": 30}, {"id": "nws", "rain_mm": 30}]


def test_agreed_heavy_rain_without_wind_is_one_decision():
    value = card("nfl", wind_fg=None, rain_fg=30, models=RAIN_AGREED)
    value["signal"] = {"label": "Low Impact", "drivers": ["rain"]}
    record = evaluate(value)
    # 1f1c58d alerted on this input; the published state now agrees with it.
    assert (record["discovery_state"], record["discovery_reasons"]) == ("signal", ["rain_signal", "opener_unknown"])
    assert record["alert"] is True and record["alert_label"] == "Low Impact"
    assert alerts._qualifying_signal(dict(value, process=record), alerts.Config())
    # Without agreement there is neither a signal nor an alert, and wind-less means unknown.
    value["weather"]["models"] = [{"id": P.ECMWF_IFS, "rain_mm": 30}]
    record = evaluate(value)
    assert (record["discovery_state"], record["discovery_reasons"]) == ("unknown", ["forecast_incomplete"])
    assert record["alert"] is False and record["alert_label"] == "No Impact"


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("weather", [
    {"wind_fg": 3}, {"wind_fg": 12}, {"wind_fg": 15}, {"wind_fg": 25, "temp_fg": 20}, {"wind_fg": None},
    {"wind_fg": 3, "rain_fg": 30, "models": RAIN_AGREED}, {"wind_fg": None, "rain_fg": 30, "models": RAIN_AGREED},
    {"wind_fg": 3, "rain_fg": 30, "models": RAIN_AGREED[:1]}, {"wind_fg": 16, "retained": True}, {"wind_fg": 16, "point_aged": True},
])
@pytest.mark.parametrize("label", ["No Impact", "Low Impact", "High Impact", None])
@pytest.mark.parametrize("roof,spread,odds", [
    ("outdoors", 0, None), ("dome", 0, None), ("outdoors", 30, None), ("outdoors", None, None),
    ("outdoors", 0, {"pinnacle": {"total": {"open_line": 47, "line": 40, "under": -110, "open_under": -110}}})])
def test_state_label_and_alert_never_disagree(sport, weather, label, roof, spread, odds):
    value = card(sport, **weather)
    value["stadium"] = {"roof_type": "dome" if roof == "dome" else "open", "roof_state": roof}
    value["consensus"] = {"spread_open": spread}
    value["signal"] = {"label": label} if label else {}
    if odds:
        value["odds"] = odds
    record = evaluate(value)
    assert record["alert"] is (record["discovery_state"] == "signal")
    assert (record["alert_label"] != "No Impact") is record["alert"]
    assert record["severity_rank"] == ["No", "Low", "Mid", "High", "Very High"].index(record["severity_tier"])
    if record["discovery_state"] in ("ineligible", "unknown") or record["display_muted"]:
        assert record["severity_tier"] == "No"


def test_stale_published_label_is_handled_the_same_way_in_state_label_and_alert():
    # CFB recomputes the label from the record's inputs.
    cfb = card("cfb", wind_fg=18)
    cfb["signal"] = {"label": "No Impact"}
    record = evaluate(cfb)
    assert (record["discovery_state"], record["alert"], record["alert_label"]) == ("signal", True, "Low (Wind)")
    assert (record["display_label"], record["severity_tier"]) == ("Low (Wind)", "Low")
    # NFL keeps an explicitly published No Impact (established alert policy), and the record agrees.
    nfl = card("nfl", wind_fg=18)
    nfl["signal"] = {"label": "No Impact"}
    record = evaluate(nfl)
    assert (record["discovery_state"], record["alert"], record["alert_label"]) == ("quiet", False, "No Impact")
    assert (record["display_label"], record["severity_tier"]) == ("No signal", "No")
    assert not alerts._qualifying_signal(dict(nfl, process=record), alerts.Config())
    nfl["signal"] = {"label": "Mid Impact"}
    record = evaluate(nfl)
    assert (record["discovery_state"], record["alert"], record["alert_label"]) == ("signal", True, "Mid Impact")
    assert (record["display_label"], record["severity_tier"], record["severity_size"]) == ("Mid", "Mid", 25)


def test_severity_follows_the_pill_not_the_raw_label():
    high = card("nfl", wind_fg=25, temp_fg=40)
    high["signal"] = {"label": "High Impact", "drivers": ["wind"]}
    record = evaluate(high)
    assert (record["severity_tier"], record["severity_rank"], record["severity_size"]) == ("High", 3, 40)
    retained = deepcopy(high)
    retained["weather"].update(retained=True, point_aged=True)
    record = evaluate(retained)
    assert (record["display_label"], record["severity_tier"], record["severity_rank"], record["severity_size"]) == ("Forecast retained", "No", 0, 7)
    dome = deepcopy(high)
    dome["stadium"] = {"roof_type": "dome", "roof_state": "dome"}
    assert (evaluate(dome)["display_label"], evaluate(dome)["severity_tier"]) == ("Ineligible", "No")
    rain = card("nfl", wind_fg=3, rain_fg=30, models=RAIN_AGREED[:1])
    rain["signal"] = {"label": "High Impact", "drivers": ["rain"]}
    record = evaluate(rain)
    assert record["display_label"] == "Rain not confirmed" and record["severity_tier"] == "No"


@pytest.mark.parametrize("patch", [{"retained": True, "point_aged": True}, {"point_aged": True}])
def test_weather_independent_ineligibility_wins_over_retained_weather(patch):
    dome = card("nfl", wind_fg=18)
    dome["stadium"] = {"roof_type": "dome", "roof_state": "dome"}
    dome["weather"].update(patch)
    record = evaluate(dome)
    assert (record["discovery_state"], record["discovery_reasons"]) == ("ineligible", ["indoor"])
    over = card("cfb", wind_fg=18)
    over["consensus"] = {"spread_open": 14}
    over["weather"].update(patch)
    record = evaluate(over)
    assert (record["discovery_state"], record["discovery_reasons"]) == ("ineligible", ["spread_outside"])
    missing = card("cfb", wind_fg=18)
    missing["consensus"] = {"spread_open": None}
    missing["weather"].update(patch)
    assert evaluate(missing)["discovery_reasons"] == ["spread_unknown"]
    outdoors = card("nfl", wind_fg=18)
    outdoors["weather"].update(patch)
    record = evaluate(outdoors)
    assert record["discovery_state"] == "unknown"
    assert record["discovery_reasons"] == (["forecast_retained"] if patch.get("retained") else ["forecast_aged"])
    assert record["alert"] is False and record["alert_label"] == "No Impact" and record["flags"] == []


@pytest.mark.parametrize("kickoff", ["2026-10-18T20:00:00", "2026-10-18 20:00:00", "2026-10-18T20:00:00+00:00", "2026-10-18T20:00:00Z"])
def test_kickoff_without_a_timezone_is_read_as_utc_once(kickoff):
    value = card(wind_fg=15)
    value["kickoff_utc"] = kickoff
    assert evaluate(value)["discovery_state"] == "signal"
    value["kickoff_utc"] = "2026-09-30T20:00:00"
    assert evaluate(value)["discovery_reasons"] == ["not_upcoming"]
    value["kickoff_utc"] = "not a time"
    assert evaluate(value)["discovery_reasons"] == ["not_upcoming"]


def test_decision_for_trusts_a_published_retained_record_unless_it_still_alerts():
    value = card(wind_fg=15)
    value["weather"].update(retained=True, point_aged=True)
    value["process"] = evaluate(value)
    assert value["process"]["alert"] is False
    assert P.decision_for(value, now=NOW + timedelta(days=30)) is value["process"]
    stale = deepcopy(value)
    stale["process"] = dict(stale["process"], alert=True)
    assert P.decision_for(stale, now=NOW)["alert"] is False


def test_alert_engine_evaluates_a_stored_card_once_at_the_alert_clock(monkeypatch):
    value = card("nfl", wind_fg=18)
    value["signal"] = {"label": "Mid Impact", "drivers": ["wind"]}
    value["game_id"] = "nfl:2026:6:a@b"
    calls = []
    real = P.decision_for
    monkeypatch.setattr(P, "decision_for", lambda *args, **kwargs: calls.append(kwargs.get("now")) or real(*args, **kwargs))
    clock = datetime(2026, 10, 16, tzinfo=timezone.utc)
    out = alerts.edge_candidates(value, {}, alerts.Config(), "run", now=clock)
    assert len(out) == 1 and len(calls) == 1 and calls[0] == clock
    assert out[0].tier == "mid"


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
    assert P.decision_for(changed, recheck=True, now=NOW)["alert_label"] == "No Impact"
    assert P.clear_signal_label(changed, 0, 50, 0) == "No Impact"
    del changed["process"]
    assert P.decision_for(changed)["alert_label"] == "No Impact"


def test_alert_engine_evaluates_once_even_without_signal_drivers(monkeypatch):
    value = card("nfl", wind_fg=18)
    value["signal"] = {"label": "Mid Impact"}  # no drivers: the emoji falls back to the label
    value["game_id"] = "nfl:2026:6:a@b"
    calls = []
    real = P.decision_for
    monkeypatch.setattr(P, "decision_for", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    assert len(alerts.edge_candidates(value, {}, alerts.Config(), "run", now=datetime(2026, 10, 16, tzinfo=timezone.utc))) == 1
    assert len(calls) == 1
