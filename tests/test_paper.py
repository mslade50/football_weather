"""No network: prospective selection, immutable persistence and grading boundary tests."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import paper
from pipeline.outputs.json_out import sanitize
from scripts import paper_run

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
KICK = NOW + timedelta(hours=96)


def card(name="away@home", wind=14, now=NOW, kickoff=KICK, sport="cfb", books=("fanduel",)):
    gid = f"{sport}:2026:9:{name}"
    return {"game_id": gid, "sport": sport, "season": 2026, "week": 9,
            "kickoff_utc": paper.iso(kickoff), "status": "scheduled",
            "home": {"team_id": name.split("@")[1]}, "away": {"team_id": name.split("@")[0]},
            "stadium": {"roof_type": "open", "roof_state": "outdoors"},
            "weather": {"fetched_at": paper.iso(now), "point_aged": False, "ecmwf_source": "open-meteo",
                        "paper_ifs": [{"t": paper.iso(kickoff.replace(minute=0) + timedelta(hours=i)), "wind_mph": wind} for i in range(4)]},
            "paper_quotes": [{"game_id": gid, "sport": sport, "book": book, "market": "total", "is_main": True,
                              "side": side, "line": 50, "odds": -110, "scraped_at": paper.iso(now)}
                             for book in books for side in ("under", "over")]}


def entry(state):
    return next(iter(state["flags"].values()))


def capture(cards, now=NOW, state=None, run="r1"):
    return paper.capture(state or paper.empty(), cards, run, now, requested_books=paper.POLICY["books"])[0]


def test_first_crossing_frozen_fades_repeats_and_thresholds_independent():
    c = card()
    first = capture([c])
    frozen = copy.deepcopy(entry(first))
    later = card(wind=8, now=NOW + timedelta(hours=1))
    state = capture([later], NOW + timedelta(hours=1), first, "r2")
    assert entry(state) == frozen
    later = card(wind=16, now=NOW + timedelta(hours=2))
    state = capture([later], NOW + timedelta(hours=2), state, "r3")
    assert state["flags"][c["game_id"] + "|12"] == frozen
    assert state["flags"][c["game_id"] + "|15"]["run_id"] == "r3"
    assert len(state["flags"]) == 2


def test_retry_identical_is_idempotent_conflicting_run_and_out_of_order_fail():
    c = card()
    original = capture([c])
    assert capture([c], state=original) == original
    with pytest.raises(ValueError, match="reused"):
        capture([card(wind=16)], state=original)
    with pytest.raises(ValueError, match="Out-of-order"):
        capture([card(now=NOW - timedelta(hours=1))], NOW - timedelta(hours=1), original, "r0")
    assert len(original["runs"]) == 1


def test_missing_first_quote_is_missed_never_entered_later():
    original = capture([card(books=())])
    updated = capture([card(now=NOW + timedelta(hours=1))], NOW + timedelta(hours=1), original, "r2")
    assert entry(updated)["variants"]["fixed"]["status"] == "missed_no_quote"
    assert paper.report(updated, NOW)["groups"]["cfb|12|fixed"]["entries"] == 0


def test_two_job_refresh_defers_nfl_until_its_requested_book_run():
    cfb, nfl = card(wind=16), card(sport="nfl", books=("pinnacle", "fanduel"), wind=16)
    state, event = paper.capture(paper.empty(), [cfb, nfl], "light", NOW, requested_books=["pinnacle", "fanduel"])
    assert all(flag["observation"]["sport"] == "cfb" for flag in state["flags"].values())
    assert "reference_book_not_requested_this_run" in event["observations"][1]["reasons"]
    later = NOW + timedelta(minutes=10)
    fresh = card(sport="nfl", books=("betonline", "pinnacle"), wind=16, now=later)
    state, _ = paper.capture(state, [fresh], "pw", later, requested_books=["betonline", "betcris"])
    assert state["flags"][fresh["game_id"] + "|12"]["variants"]["fixed"]["status"] == "entered"
    assert state["flags"][fresh["game_id"] + "|15"]["run_id"] == "pw"
    assert entry(state)["run_id"] == "light"


def test_weather_only_archives_without_consuming_first_opportunity():
    state, event = paper.capture(paper.empty(), [card()], "weather", NOW, requested_books=[])
    assert not state["flags"] and event["observations"][0]["raw_quotes"]
    state = capture([card(now=NOW + timedelta(minutes=10))], NOW + timedelta(minutes=10), state, "quotes")
    assert entry(state)["variants"]["fixed"]["status"] == "entered"


@pytest.mark.parametrize("change,reason", [
    (lambda c: c["weather"].update(point_aged=True), "forecast_missing_stale_or_future"),
    (lambda c: c["weather"].update(fetched_at=paper.iso(NOW + timedelta(seconds=1))), "forecast_missing_stale_or_future"),
    (lambda c: c["weather"].update(fetched_at=paper.iso(NOW - timedelta(hours=7))), "forecast_missing_stale_or_future"),
    (lambda c: c["weather"].update(paper_ifs=c["weather"]["paper_ifs"][:3]), "ifs_four_hour_window_incomplete"),
    (lambda c: c["stadium"].update(roof_state=None), "roof_not_confirmed_exposed"),
    (lambda c: c["stadium"].update(roof_type="dome"), "roof_not_confirmed_exposed"),
    (lambda c: c.update(kickoff_utc=paper.iso(NOW)), "outside_24_to_144h_window"),
    (lambda c: c.update(status="final"), "not_scheduled"),
    (lambda c: c["weather"].update(point_source_updated_at={"ecmwf_opendata": paper.iso(NOW - timedelta(hours=25))}), "model_init_stale_or_future"),
])
def test_weather_fail_closed_capture_retains_rejection(change, reason):
    c = card()
    change(c)
    state, event = paper.capture(paper.empty(), [c], "r1", NOW, requested_books=paper.POLICY["books"])
    assert reason in event["observations"][0]["reasons"]
    assert not state["flags"]
    assert len(event["observations"]) == 1
    assert event["observations"][0]["raw_quotes"] == c["paper_quotes"]


@pytest.mark.parametrize("change", [
    lambda c: c["paper_quotes"][0].update(scraped_at=paper.iso(NOW - timedelta(minutes=61))),
    lambda c: c["paper_quotes"][0].update(source_updated_at=paper.iso(NOW + timedelta(seconds=1))),
    lambda c: c["paper_quotes"][0].update(expires_at=paper.iso(NOW)),
    lambda c: c["paper_quotes"][0].update(line=51),
    lambda c: c["paper_quotes"][0].update(odds=None),
    lambda c: c["paper_quotes"].append(copy.deepcopy(c["paper_quotes"][0])),
])
def test_only_actual_fresh_unambiguous_pairs_can_enter(change):
    c = card()
    change(c)
    assert not paper.observation(c, NOW)["quotes"]
    assert entry(capture([c]))["variants"]["fixed"]["quote"] is None


def test_best_line_selection_uses_frozen_pairs_excludes_reference_and_exchanges():
    c = card(books=("fanduel", "draftkings", "betcris", "kalshi"))
    for q in c["paper_quotes"]:
        if q["book"] == "draftkings":
            q.update(line=51, odds=-115)
        elif q["book"] not in paper.POLICY["books"]:
            q.update(line=70)
    v = entry(capture([c]))["variants"]
    assert v["fixed"]["quote"]["line"] == 50
    assert v["best"]["quote"]["book"] == "draftkings"
    assert v["best"]["quote"]["odds"] == -115


def test_controls_same_capture_week_lead_bin_book_and_leave_itself_out():
    target = card(wind=16)
    good = card("c@d", wind=8)
    next_week = card("e@f", wind=8)
    next_week["week"] = 10
    different_lead = card("g@h", wind=8, kickoff=KICK - timedelta(hours=30))
    wrong_book = card("i@j", wind=8, books=("betonline",))
    windy = card("k@l", wind=17)
    state = capture([target, good, next_week, different_lead, wrong_book, windy])
    v = state["flags"][target["game_id"] + "|12"]["variants"]["fixed"]
    assert [c["game_id"] for c in v["controls"]] == [good["game_id"]]


def grade_state():
    target, control = card(), card("c@d", wind=8)
    state = capture([target, control])
    close_at = KICK - timedelta(minutes=30)
    latest = [card(wind=2, now=close_at), card("c@d", wind=2, now=close_at)]
    for c, line in zip(latest, (48, 49), strict=True):
        for q in c["paper_quotes"]:
            q["line"] = line
    return capture(latest, close_at, state, "closing"), target, control


def test_clv_raw_excess_actual_odds_roi_push_and_no_clv_roi_conversion():
    state, target, control = grade_state()
    state["scores"][target["game_id"]] = {"actual_total": 49, "src_result": "espn"}
    report = paper.report(state, KICK + timedelta(hours=4))
    s = report["groups"]["cfb|12|fixed"]
    assert s["raw_clv"] == 2
    assert s["excess_clv"] == 1
    assert s["roi"] == pytest.approx(100 / 110)
    state["scores"][target["game_id"]]["actual_total"] = 50
    assert paper.report(state, KICK)["groups"]["cfb|12|fixed"]["record"] == {"P": 1}
    assert paper.report(state, KICK)["groups"]["cfb|12|fixed"]["roi"] == 0
    state["scores"][target["game_id"]]["actual_total"] = 51
    assert paper.report(state, KICK)["groups"]["cfb|12|fixed"]["roi"] == -1


def test_stale_close_not_valid_even_if_last_observed_and_postkick_cannot_replace():
    state, target, _ = grade_state()
    fixed = entry(state)["variants"]["fixed"]["quote"]
    state["games"][target["game_id"]]["closes"]["fanduel"]["observed_at"] = paper.iso(KICK - timedelta(hours=2))
    now = KICK + timedelta(minutes=1)
    post = card(now=now)
    state = capture([post], now, state, "post")
    g = paper.grade(state, entry(state)["observation"], fixed, now)
    assert g["clv"] is None and g["close_status"] == "missing_fresh_close"


def test_carried_older_quote_cannot_replace_a_newer_close_candidate():
    state, target, _ = grade_state()
    before = copy.deepcopy(state["games"][target["game_id"]]["closes"])
    now = KICK - timedelta(minutes=10)
    carried = card(now=KICK - timedelta(minutes=45))
    state = capture([carried], now, state, "carried")
    assert state["games"][target["game_id"]]["closes"] == before


def test_reschedule_excludes_frozen_bet_and_no_late_reentry():
    state, target, _ = grade_state()
    now = KICK - timedelta(minutes=10)
    changed = card(now=now, kickoff=KICK + timedelta(days=1), wind=20)
    state = capture([changed], now, state, "newkick")
    row = paper.report(state, KICK + timedelta(days=2))["rows"][0]
    assert row["status"] == "excluded_reschedule"
    assert row["profit"] is None and row["clv"] is None
    assert len(state["flags"]) == 2  # new 15 mph crossing, old 12 never rewritten
    reverted = capture([card(now=now + timedelta(minutes=1))], now + timedelta(minutes=1), state, "restoredkick")
    assert paper.report(reverted, KICK)["rows"][0]["status"] == "excluded_reschedule"


def test_missing_control_close_does_not_change_selected_baseline():
    state, target, control = grade_state()
    state["games"][control["game_id"]]["closes"] = {}
    row = paper.report(state, KICK)["rows"][0]
    assert row["clv"] == 2
    assert row["control_n"] == 1 and row["control_clv_n"] == 0
    assert row["baseline_clv"] is None and row["excess_clv"] is None


def test_nfl_separate_fixed_book_policy_and_bootstrap_requires_weeks():
    state = capture([card(sport="nfl", books=("betonline",), wind=16)])
    result = paper.report(state, NOW)
    assert result["groups"]["nfl|12|fixed"]["entries"] == 1
    assert result["groups"]["cfb|12|fixed"]["entries"] == 0
    assert result["groups"]["nfl|12|fixed"]["roi_interval"] is None
    rows = [{"week_key": str(i), "clv": i} for i in range(8)]
    a = paper.cluster_interval(rows, "clv")
    assert a == paper.cluster_interval(rows, "clv") and a[0] < 3.5 < a[1]


def test_unknown_schema_and_missing_checkpoint_fail_closed():
    bad = paper.empty()
    bad["schema_version"] = 2
    with pytest.raises(ValueError, match="mismatch"):
        capture([card()], state=bad)
    with pytest.raises(ValueError, match="restore"):
        paper_run.load_checkpoint(lambda key: {} if key == paper_run.REPORT_KEY else None)


def test_transaction_archive_before_checkpoint_idempotent_restart_and_failure(tmp_path):
    store, writes = {}, []
    snap = tmp_path / "snapshots/cfb/2026/9/run.json"
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({"meta": {"run_id": "r1", "last_updated": paper.iso(NOW), "books_requested": ["fanduel"]}, "games": [card()]}))

    def put(key, path):
        writes.append(key)
        store[key] = json.loads(path.read_text(encoding="utf-8"))

    args = dict(read=lambda key: copy.deepcopy(store.get(key)), put=put, snapshot_dir=tmp_path / "snapshots",
                run_id="r1", backtest=Path("unused"), export_dir=Path("unused"), output_dir=tmp_path / "output", now=NOW)
    paper_run.run("capture", **args)
    checkpoint = copy.deepcopy(store[paper_run.STATE_KEY])
    assert writes[0].startswith("paper/ifs-first-observed-v1/captures/")
    assert writes[1:] == [paper_run.STATE_KEY, paper_run.REPORT_KEY, paper_run.HEALTH_KEY]
    paper_run.run("capture", **args)
    assert store[paper_run.STATE_KEY] == checkpoint
    snap.write_text(json.dumps({"meta": {"run_id": "r2", "last_updated": paper.iso(NOW), "books_requested": ["fanduel"]}, "games": [card()]}))

    def fail_archive(key, path):
        raise RuntimeError("network unavailable")

    args.update(run_id="r2", put=fail_archive)
    with pytest.raises(RuntimeError):
        paper_run.run("capture", **args)
    assert store[paper_run.STATE_KEY] == checkpoint


def test_weekly_scores_require_completed_sources_and_authoritative_reschedules():
    state = capture([card()])
    gid = card()["game_id"]
    data = {"meta": {"generated_at": paper.iso(KICK)}, "games": [
        {"game_id": gid, "home_score": 20, "away_score": 17, "src_result": None}]}
    paper_run.update_scores(state, data, [], KICK)
    assert not state["scores"]
    data["games"][0]["src_result"] = "espn"
    paper_run.update_scores(state, data, [{"game_id": gid, "kickoff_utc": paper.iso(KICK + timedelta(days=1))}], KICK)
    assert state["scores"][gid]["actual_total"] == 37
    assert paper.report(state, KICK + timedelta(days=2))["rows"][0]["status"] == "excluded_reschedule"


def test_raw_four_hour_ifs_is_independent_of_three_hour_v1_window():
    from pipeline.weather import merge
    from pipeline.weather.parsers import HourlyRow
    from pipeline.weather.parsers.openmeteo import ParsedLocation

    hours = [KICK + timedelta(hours=i) for i in range(4)]
    raw = [HourlyRow(t=t, wind=w, temp=60) for t, w in zip(hours, (8, 8, 8, 40), strict=True)]
    om = ParsedLocation(latitude=40, longitude=-75, models={merge.ECMWF: raw})
    forecast = merge.build_forecast("cfb:2026:9:a@b", KICK, NOW, om=om, auto_climo=False).forecast
    assert forecast.model_windows[merge.ECMWF]["wind_mph"] == 8
    assert [s["wind_mph"] for s in forecast.paper_ifs] == [8, 8, 8, 40]
    c = card(wind=1)
    c["weather"]["paper_ifs"] = sanitize(forecast.paper_ifs)
    assert paper.observation(c, NOW)["wind_mph"] == 16


def test_workflow_capture_is_separate_and_paper_objects_protected():
    from pipeline.publication import BUILD_PROTECTED_NAMES

    pipeline = Path(".github/workflows/pipeline.yml").read_text(encoding="utf-8")
    weekly = Path(".github/workflows/backtest.yml").read_text(encoding="utf-8")
    assert pipeline.count("run: python scripts/paper_run.py capture") == 2
    for text in (pipeline, weekly):
        assert "continue-on-error: true\n        timeout-minutes: 6\n        run: python scripts/paper_run.py" in text
        assert "vars.FOOTBALL_PAPER_ENABLED != '0'" in text
    assert {"paper", "paper_health"} <= BUILD_PROTECTED_NAMES
