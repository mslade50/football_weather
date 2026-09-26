from datetime import timedelta

from pipeline import alerts as A
from pipeline.model.wind_history import stadium_wind_history
from tests.test_alerts_rules import CFG, NOW, _ctx, _edge, card


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
        assert "Best price: Betcris · Under 46.5 (+108) · est. EV +8.0%" in text
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
