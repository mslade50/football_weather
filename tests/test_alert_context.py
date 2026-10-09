from datetime import timedelta

from pipeline import alerts as A
from pipeline.model.wind_history import stadium_wind_history
from tests.test_alerts_rules import CFG, NOW, _ctx, _edge, card


def test_signal_entry_matches_fresh_roi_best_price_even_with_negative_edge():
    c = card(signal="Low (Wind)", sport="cfb")
    c["total_prices"] = {"quotes": [
        dict(book="novig", side="under", line=46.5, odds=108, ev_roi=-.02, updated_at=NOW.isoformat()),
        dict(book="betcris", side="under", line=47.5, odds=-125, ev_roi=-.04, updated_at=NOW.isoformat()),
        dict(book="stale", side="under", line=55, odds=110, ev_roi=.5,
             updated_at=(NOW - timedelta(hours=2)).isoformat()),
    ]}
    c["fair"]["fair_total"] = 48
    signal = A.edge_candidates(c, {}, CFG, now=NOW)[0]
    assert (signal.record["last_book"], signal.record["last_line"], signal.record["last_edge"]) == ("novig", 46.5, -1.5)
    assert "Best price: NoVig · Under 46.5 (+108)" in signal.text
    assert "Best exchange: NoVig · Under 46.5 (+108)" in signal.text


def test_priced_signal_without_model_then_price_outage_stays_active():
    from tests.test_alerts_rules import _fresh, _live

    c = card(signal="Low (Wind)", sport="cfb")
    c["total_prices"] = {"quotes": []}
    c["fair"] = {}
    c["odds"] = {"novig": {"total": dict(line=49, under=-110, updated_at=NOW.isoformat())}}
    alerts, tg = _fresh()
    initial = A.edge_candidates(c, alerts, CFG, now=NOW)
    assert initial[0].record["last_line"] == 49
    assert initial[0].record["last_fair"] is None
    _live(initial, alerts, tg)
    outage = A.followup_candidates(c, alerts, CFG, NOW + timedelta(hours=2))
    assert len(outage) == 1 and outage[0].family == "wx"
    assert "weather signal remains active" in outage[0].text
    assert "NoVig now: unavailable (quote stale or timestamp missing)" in outage[0].text
    _live(outage, alerts, tg, now=NOW + timedelta(hours=2))
    assert A.followup_candidates(c, alerts, CFG, NOW + timedelta(hours=2, minutes=1)) == []
    c["odds"]["novig"]["total"]["updated_at"] = (NOW + timedelta(hours=3)).isoformat()
    returned = A.followup_candidates(c, alerts, CFG, NOW + timedelta(hours=3))
    assert len(returned) == 1 and "Price now available" in returned[0].text


def test_best_prices_use_roi_same_side_freshness_and_all_four_exchanges(monkeypatch):
    monkeypatch.setattr(A, "now_utc", lambda: NOW)
    c = card()
    def quote(book, ev, **kw):
        return dict(book=book, ev_roi=ev, side="under", line=46.5, odds=108,
                    updated_at=NOW.isoformat(), **kw)
    quotes = [quote("betcris", .08), quote("novig", .05), quote("kalshi", .04),
              quote("prophetx", .03), quote("polymarket_us", .02)]
    c["total_prices"] = {"quotes": quotes + [dict(quote("stale", .9), updated_at=(NOW-timedelta(hours=2)).isoformat()),
                                              dict(quote("wrong-side", .9), side="over")]}
    for exchange in ("novig", "kalshi", "prophetx", "polymarket_us"):
        text = A.format_edge(c, _edge())
        assert "Best price: Betcris" not in text
        assert f"Best price: {'NoVig' if exchange == 'novig' else A._book_label(exchange)}" in text
        assert f"Best exchange: {'NoVig' if exchange == 'novig' else A._book_label(exchange)}" in text
        c["total_prices"]["quotes"] = [q for q in c["total_prices"]["quotes"] if q["book"] != exchange]
    assert "Best exchange: unavailable" in A.format_edge(c, _edge())
    c["total_prices"]["quotes"] = [quote("novig", -.02)]
    assert "est. EV −2.0% · no +EV" in A.format_edge(c, _edge())
    c["kickoff_utc"] = (NOW-timedelta(seconds=1)).isoformat()
    assert "Best price: unavailable" in A.format_edge(c, _edge())


def history_row(gid, **kw):
    return dict(game_id=gid, sport="nfl", stadium_id="gillette-stadium", kickoff_utc="2025-11-01T17:00:00Z",
                wind_fc=18, temp_fc=40, total_close=44, under_result="W", **kw)


def test_stadium_history_is_wind_only_sport_specific_graded_and_deduplicated():
    c = card()
    c["stadium"]["stadium_id"] = "gillette-stadium"
    base = history_row("win")
    rows = [base, base, dict(base, game_id="loss", under_result="L"), dict(base, game_id="push", under_result="P")]
    for i, change in enumerate([{"wind_fc": 15}, {"temp_fc": 60}, {"stadium_id": "other"}, {"sport": "cfb"},
                               {"roof_state": "closed"}, {"under_result": None}, {"total_close": None},
                               {"kickoff_utc": "2027-11-01T17:00:00Z"}, {"wind_fc": None}, {"wind_fc": 4, "wind_act": 30}]):
        rows.append(dict(base, game_id=f"excluded-{i}", **change))
    rows.append(dict(base, game_id=c["game_id"]))
    result = stadium_wind_history(c, {"games": rows}, now=NOW)
    assert (result["wins"], result["losses"], result["pushes"], result["n"]) == (1, 1, 1, 3)
    assert result["seasons"] == [2025]
    assert stadium_wind_history(c, None, now=NOW)["status"] == "unavailable"
    assert stadium_wind_history(c, {"games": []}, now=NOW)["status"] == "ok"
    c["sport"] = "cfb"
    rows = [dict(base, sport="cfb", temp_fc=69, wind_fc=14.1, spread_open=-10)]
    assert stadium_wind_history(c, {"games": rows}, now=NOW)["n"] == 1
    rows[0]["spread_open"] = 40
    assert stadium_wind_history(c, {"games": rows}, now=NOW)["n"] == 0


def test_alert_run_loads_wind_history_and_formats_record(tmp_path):
    import json

    c = card()
    c["stadium"]["stadium_id"] = "gillette-stadium"
    (tmp_path / "backtest.json").write_text(json.dumps({"meta": {"generated_at": NOW.isoformat()},
                                                        "games": [history_row("past")]}), encoding="utf-8")
    run = A.run_alerts(_ctx(), {"nfl": [c]}, tmp_path, dry_run=True, now=NOW, cfg=CFG)
    text = next(candidate.text for candidate in run.candidates if candidate.family == "edge")
    assert "Stadium wind unders: 1-0-0 W-L-P · n=1 · 2025" in text
    assert "Basis: NFL Wind · closing forecasts/totals · as of 09/18/2026" in text
    assert "Historical archive unavailable; forecast record covers current feed only" in text


def test_archived_history_is_used_without_current_feed_and_actuals_stay_separate(tmp_path):
    import json

    c = card()
    c["stadium"]["stadium_id"] = "gillette-stadium"
    past = history_row("archived")
    archive = {"schema_version": 1, "games": [past, past], "actual_games": [
        dict(past, wind_fc=0, wind_act=15, under_result="P"),
        dict(past, game_id="calm", wind_act=14.99),
        dict(past, game_id="other", stadium_id="other", wind_act=20),
        dict(past, game_id="closed", roof_state="closed", wind_act=20),
    ]}
    (tmp_path / "wind-history-v1.json").write_text(json.dumps(archive), encoding="utf-8")
    run = A.run_alerts(_ctx(), {"nfl": [c]}, tmp_path, dry_run=True, now=NOW, cfg=CFG)
    text = next(x.text for x in run.candidates if x.family == "edge")
    assert "Stadium wind unders: 1-0-0 W-L-P · n=1 · 2025" in text
    assert "Actual-wind unders: 0-0-1 W-L-P · n=1 · 2025" in text
    assert "ERA5 wind ≥15 mph · closing totals · descriptive, not forecast signals" in text
    assert "archive unavailable" not in text
    result = stadium_wind_history(c, {"games": [history_row("new", season=2026)]}, now=NOW, archive=archive)
    assert result["n"] == 2
    assert result["seasons"] == [2025, 2026]
    assert result["actual"]["n"] == 1
    # The current feed wins when it also contains an archived game, even if its
    # corrected forecast no longer qualifies. Never count it from the older copy.
    result = stadium_wind_history(c, {"games": [dict(past, wind_fc=1)]}, now=NOW, archive=archive)
    assert result["n"] == 0
    archive["schema_version"] = 99
    result = stadium_wind_history(c, None, now=NOW, archive=archive)
    assert result["status"] == "unavailable"


def test_history_export_uses_closing_grade_not_alert_grade():
    from scripts.export_wind_history import normalize_row

    raw = {"game_id": "past", "close_total": 44, "actual_total": 44,
           "close_result": "P", "under_result": "W", "wind_fc": 20, "wind_act": 2}
    normalized = normalize_row(raw, "venue", "outdoors", forecast=True)
    assert normalized["under_result"] == "P"
    assert normalized["wind_fc"] == 20
    assert "wind_act" not in normalized
    raw["close_result"] = "L"
    import pytest

    with pytest.raises(ValueError, match="Closing grade mismatch"):
        normalize_row(raw, "venue", "outdoors", forecast=True)


def test_history_export_does_not_guess_ambiguous_venues(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import pandas as pd

    from pipeline.stadiums.loader import StadiumBook
    from scripts import export_wind_history as E

    book = StadiumBook(stadiums={"venue-a": SimpleNamespace(stadium_id="venue-a", timezone="America/New_York",
                                                           roof_type="open")},
                       cfbd_venue_index={"101": "venue-a"}, name_index={"memorial-stadium": "venue-a"})
    monkeypatch.setattr(E, "load_stadium_book", lambda **kw: book)
    schedules, forecasts = [], []
    for home, venue in [("Home", 101), ("Other", 999)]:
        schedules.append(dict(season=2025, week=1, homeTeam=home, awayTeam="Away", homeClassification="fbs",
                              venueId=venue, venue="Memorial Stadium", startDate="2025-09-01T18:00:00Z"))
        forecasts.append(dict(game_id=f"cfb:2025:1:away@{home.lower()}", sport="cfb", season=2025,
                              kickoff_utc="2025-09-01T18:00:00Z", wind_fc=20, temp_fc=50, close_lead_h=2,
                              close_total=40, actual_total=35, close_result="W"))
    (tmp_path / "git").mkdir()
    (tmp_path / "git/cfbd_games_2025.json").write_text(json.dumps(schedules), encoding="utf-8")
    (tmp_path / "git/nflverse_games.csv").write_text("season,week\n", encoding="utf-8")
    pd.DataFrame(forecasts).to_parquet(tmp_path / "hist_games.parquet")
    pd.DataFrame([]).to_parquet(tmp_path / "stadium_wx_games.parquet")
    payload = E.export_history(tmp_path, tmp_path)
    assert [r["game_id"] for r in payload["games"]] == ["cfb:2025:1:away@home"]
    assert payload["games"][0]["stadium_id"] == "venue-a"
    assert payload["meta"]["forecast_skipped"]["unresolved_venue"] == 1
    json.dumps(payload, allow_nan=False)
