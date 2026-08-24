"""HTML formatters in pipeline/alerts.py match the ARCH §10 sample."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pipeline import alerts as A
from pipeline import state as pstate
from tests.test_alerts_rules import GID, KICK, NOW, _edge, card

BOARD = "https://football-board.test.workers.dev"


def _sample_card() -> dict:
    c = card([
        _edge(),
        _edge(book="betcris", line=38.5, odds=-108, edge_pts=3.9, edge_prob=0.05),
        _edge(book="fanduel", line=38.0, odds=-112, edge_pts=3.4, edge_prob=0.03, tier="watch"),
        _edge(book="kalshi", line=37.5, odds=-108, edge_pts=2.9, edge_prob=0.02, tier="watch", vigfree=0.52),
        _edge(book="novig", line=38.0, odds=-105, edge_pts=3.4, edge_prob=0.02, tier="watch"),
        _edge(book="betonline", side="over", line=38.0, odds=-110, edge_pts=-3.4, edge_prob=-0.05, tier="none"),
    ])
    c["weather"]["wind_fg"] = 18.0
    c["weather"]["rain_fg"] = 0.8
    return c


def test_edge_message_matches_spec_sample():
    c = _sample_card()
    text = A.format_edge(c, _edge(), BOARD)
    lines = text.split("\n")
    assert lines[0] == "<b>🌬 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert lines[1] == "Gillette Stadium · wind 18 mph SE (gust 26 · vol 6 · cross 15) · 41°F · rain 20% / 0.8 mm"
    assert lines[2] == "Impact −6.5% (wind 6.5) · conf 0.72 · fair total 34.6 (ref pinnacle, 6 books)"
    assert lines[3] == "<b>UNDER 38 −110 @ BetOnline</b> · edge 3.4 pts / +4.1% · open 38"
    assert lines[4] == "Best: Under 38.5 −108 Betcris · FD 38 · Novig 38 · Kalshi 37.5 (52¢)"
    assert lines[5] == f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">board</a>'
    assert len(lines) == 6
    # unicode minus for negative odds, no ASCII hyphen-minus in the price
    assert "-110" not in text and "−110" in text


def test_edge_message_strong_tier_and_spread_sign():
    c = card([_edge(market="spread", side="home", line=-3.0, fair_line=-4.5, edge_pts=1.5, tier="strong")])
    c["odds"] = {"betonline": {"spread": {"home_line": -3.0, "open_line": -2.5}}}
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert "<b>NE −3 −110 @ BetOnline</b>" in text
    assert "fair spread −4.5" in text and "open −2.5" in text and "<b>STRONG</b>" in text


def test_edge_message_escapes_html_and_handles_missing_fields():
    c = card()
    c["stadium"] = {"name": "Tom & Jerry <Field>", "roof_state": "outdoors"}
    c["away"]["short"] = "A&M"
    c["weather"] = {"wind_fg": None}
    c["odds"] = {}
    text = A.format_edge(c, _edge(), BOARD)
    assert "Tom &amp; Jerry &lt;Field&gt;" in text and "A&amp;M @ NE" in text
    assert "wind ? mph" in text and "open" not in text.split("\n")[3]
    assert "<script" not in text


def test_edge_message_roof_closed_and_emoji_by_dominant_component():
    c = card()
    c["stadium"]["roof_state"] = "closed"
    assert "Gillette Stadium · roof closed" in A.format_edge(c, _edge(), BOARD)
    c2 = card()
    c2["impact"]["v1"]["components"] = {"rain": 3.0, "wind": 2.0}
    assert A.format_edge(c2, _edge(), BOARD).startswith("<b>🌧 ")
    c3 = card()
    c3["impact"]["v1"]["components"] = {"cold": 1.0}
    assert A.format_edge(c3, _edge(), BOARD).startswith("<b>🥶 ")


def test_move_gone_wx_messages():
    c = card([_edge(line=39.0, edge_pts=4.4)])
    e = c["fair"]["edges"][0]
    rec = {"first_line": 38.0, "first_edge": 3.4, "last_line": 38.0, "last_edge": 3.4, "last_fair": 34.6,
           "last_wind": 18.0, "last_rain": 0.8}
    move = A.format_move(c, rec, e, "away from fair", BOARD)
    assert move.split("\n")[0] == "<b>↕️ NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert "<b>UNDER @ BetOnline</b> moved 38 → 39 (away from fair)" in move
    assert "fair 34.6 · edge now 4.4 pts (was 3.4) · −110" in move
    assert move.endswith(f'&amp;game={GID}">board</a>')

    gone = A.format_gone(c, rec, dict(e, line=35.0, edge_pts=0.4), BOARD)
    assert "EDGE GONE: <b>UNDER 35 @ BetOnline</b> · edge 0.4 pts (alerted at 38, 3.4 pts)" in gone

    c2 = card([_edge(fair_line=36.1, edge_pts=1.9)], wind=13.0, rain=0.0)
    wx = A.format_wx_move(c2, rec, c2["fair"]["edges"][0], BOARD)
    assert "FORECAST MOVE: wind 18 → 13 mph · rain 0.8 → 0 mm" in wx
    assert "fair total 34.6 → 36.1 · <b>UNDER 38 @ BetOnline</b> edge 1.9 pts" in wx


def test_openers_and_ops_and_digest_format():
    c = card()
    text = A.format_openers("cfb", 2026, 3, [(c, [f"{GID}|total|over|betcris", f"{GID}|spread|home|fanduel"])], BOARD)
    lines = text.split("\n")
    assert lines[0] == "<b>📋 CFB Wk 3 openers · 1 weather game(s)</b>"
    assert lines[1] == "SEA @ NE Sun 1:00p ET · wind 18 · −6.5% · tot 37.5 sp −3 · Betcris, FD"
    assert lines[2] == f'<a href="{BOARD}/#sport=cfb&amp;week=3">board</a>'

    ops = A.format_ops("Degradation [warn] weather", "open-meteo <503> & retry")
    assert ops == "⚠️ <b>Degradation [warn] weather</b>\nopen-meteo &lt;503&gt; &amp; retry"
    assert A.format_ops("x") == "⚠️ <b>x</b>"

    msgs = A.format_digest("Alert digest", ["a", "b"])
    assert msgs == ["<b>Alert digest (2)</b>\n\n1. a\n\n2. b"]
    big = A.format_digest("Alert digest", ["x" * 1500] * 5)
    assert len(big) == 3 and all(len(m) <= A.TELEGRAM_MAX_CHARS for m in big)
    assert big[1].startswith("<b>Alert digest (cont.)</b>")


def test_candidate_summary_is_one_line_without_link():
    alerts = pstate.migrate(None, "alerts")
    c = A.edge_candidates(_sample_card(), alerts, A.Config(board_url=BOARD))[0]
    assert "\n" not in c.summary and "<a " not in c.summary
    assert c.summary == "<b>🌬 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b> · <b>UNDER 38 −110 @ BetOnline</b> · edge 3.4 pts / +4.1% · open 38"


def test_kickoff_label_and_helpers():
    c = card(kickoff=datetime(2026, 11, 30, 1, 15, tzinfo=timezone.utc))   # Sun 8:15p ET (EST)
    assert A._kick_label(c) == "Sun 8:15p ET"
    assert A._fmt_odds(105) == "+105" and A._fmt_odds(-105) == "−105" and A._fmt_odds(None) == "?"
    assert A._fmt_line(38.5) == "38.5" and A._fmt_line(-3.0, signed=True) == "−3" and A._fmt_line(2.5, signed=True) == "+2.5"
    assert A._fmt_pct(4.06) == "+4.1%" and A._fmt_pct(-6.5) == "−6.5%" and A._fmt_pct(0) == "0.0%"
    assert A.parse_edge_key(f"edge|2026|3|{GID}|total|under|betonline|v1")["book"] == "betonline"
    assert A.parse_edge_key("move|x|1") is None
    assert A.et_day(NOW) == "2026-09-18" and A.et_day(KICK + timedelta(hours=8)) == "2026-09-20"


def test_clv_digest_groups_and_top_bottom():
    alerts = pstate.migrate(None, "alerts")
    assert "no settled EDGE alerts" in A.clv_digest(alerts)
    for i, (book, clv) in enumerate([("betonline", 1.5), ("betcris", -0.5), ("fanduel", 2.0), ("betonline", 0.0)]):
        pstate.upsert_alert_record(alerts, f"edge|2026|3|nfl:2026:3:t{i}@ne|total|under|{book}|v1",
                                   {"family": "edge", "sport": "nfl", "book": book, "tier": "edge" if i else "strong",
                                    "side": "under", "first_line": 40.0 + i, "closing_line": 40.0 + i + clv, "clv_pts": clv,
                                    "game_id": f"nfl:2026:3:t{i}@ne"}, "t")
    text = A.clv_digest(alerts, top_n=2)
    assert text.startswith("<b>📊 Weekly CLV digest · 4 alerts</b>")
    assert "<b>by tier</b>" in text and "<b>by league</b>" in text and "<b>by book</b>" in text
    assert re.search(r"FD: n=1 avg \+2\.00 · \+CLV 1/1", text)
    assert re.search(r"BetOnline: n=2 avg \+0\.75 · \+CLV 1/2", text)
    top = text.split("<b>top 2</b>")[1].split("<b>bottom 2</b>")[0]
    assert "t2@ne" in top.split("\n")[1] and "t0@ne" in top.split("\n")[2]
    bottom = text.split("<b>bottom 2</b>")[1]
    assert "t1@ne" in bottom.split("\n")[1]
    assert A.clv_digest(alerts, sport="cfb").startswith("<b>📊 Weekly CLV digest</b>\nno settled")


def test_cli_digest_and_flush_dry_run(tmp_path, capsys):
    assert A.main(["--digest", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Weekly CLV digest" in out and "clv digest: sent" in out
    tg = pstate.migrate(None, "telegram_state")
    pstate.queue_alert(tg, {"key": "edge|k", "family": "edge", "sport": "nfl", "text": "<b>hi</b>", "record": {"family": "edge"}})
    pstate.save_telegram_state(tmp_path, tg)
    assert A.main(["--flush", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Overnight alerts (1)" in out and "flush: 1 alert(s) in 1 message(s)" in out
    assert pstate.load_telegram_state(tmp_path)["queue"] != []   # dry-run keeps the queue
    assert A.main(["--state-dir", str(tmp_path)]) == 2
