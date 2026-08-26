"""HTML formatters in pipeline/alerts.py match the ARCH §10 sample."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.model import config as C
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
    c["signal"]["flags"] = ["NFL Wind"]
    text = A.format_edge(c, _edge(), BOARD)
    lines = text.split("\n")
    assert lines[0] == "<b>🌬 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert lines[1] == "Gillette Stadium · wind 18 mph SE (gust 26 · vol 6 · cross 15) · 41°F · rain 20% / 0.8 mm"
    assert lines[2] == "Impact −6.5% (wind 6.5 · v1) · conf 0.72 · fair total 34.6 (ref pinnacle, 6 books)"
    assert lines[3] == "<b>Mid Impact</b> · wind 18 mph · 41°F · rain 0.8 mm · NFL Wind"
    assert lines[4] == "<b>UNDER 38 −110 @ BetOnline</b> · market edge +3.4 pts / +4.1% · open 38"
    assert lines[5] == "Books: <b>BetOnline u38.0 −110</b> · ref u37.5"
    assert lines[6] == f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">board</a>'
    assert len(lines) == 7
    # unicode minus for negative odds, no ASCII hyphen-minus in the price
    assert "-110" not in text and "−110" in text
    # no flags -> no dangling separator on the signal line
    assert A.format_edge(_sample_card(), _edge(), BOARD).split("\n")[3] == "<b>Mid Impact</b> · wind 18 mph · 41°F · rain 0.8 mm"


def test_edge_message_market_edge_is_a_note_never_a_gate():
    c = card([_edge(edge_pts=-0.6, edge_prob=-0.012, fair_line=38.6)], signal="Low (Rain)")
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert "<b>UNDER 38 −110 @ BetOnline</b> · market edge −0.6 pts / −1.2% (market already there) · open 38" in text
    zero = A.format_edge(c, dict(_edge(), edge_pts=0.0, edge_prob=0.0), BOARD)
    assert "market edge 0.0 pts / 0.0% (market already there)" in zero
    # consensus-synthesised entries (consensus.total_now vs fair.fair_total): line + fair, or nothing posted
    c["fair"]["fair_total"] = 38.6
    cons = A.consensus_entry(c)
    assert cons["edge_pts"] == -1.1 and cons["line"] == 37.5 and cons["fair_line"] == 38.6
    assert "<b>UNDER 37.5 (consensus)</b> · market edge −1.1 pts vs fair 38.6 (market already there)" in A.format_edge(c, cons, BOARD)
    c["consensus"]["total_now"] = None
    assert "<b>UNDER</b> · no line posted yet" in A.format_edge(c, A.consensus_entry(c), BOARD)
    c["consensus"]["total_now"] = 37.5
    c["fair"]["fair_total"] = None
    assert "<b>UNDER 37.5 (consensus)</b> · market edge ?" in A.format_edge(c, A.consensus_entry(c), BOARD)


def test_books_ladder_best_first_kalshi_cents_and_tie_by_odds():
    c = card()
    c["odds"] = {
        "betonline": {"total": {"line": 38.5, "over": -110, "under": -110, "open_line": 38.0}},
        "fanduel": {"total": {"line": 38.5, "over": -112, "under": -108}},
        "betcris": {"total": {"line": 38.0, "over": -108, "under": -112}},
        "kalshi": {"total": {"line": 38.0, "over": 105, "under": -113}},
        "pinnacle": {"total": {"line": 37.5, "over": -115, "under": -105}},
        "novig": {"spread": {"home_line": -3.0, "home_odds": -105, "away_odds": -105}},   # no total -> skipped
    }
    ladder = A.book_ladder(c, _edge())
    assert ladder == ["Books: <b>FD u38.5 −108</b> · BetOnline u38.5 −110 · Betcris u38.0 −112 · Kalshi u38.0 (53¢) · "
                      "Pinnacle u37.5 −105 · ref u37.5"]
    # OVER bettor: lower line first, then better odds (Kalshi +105 beats Betcris −108 at the same line)
    over = A.book_ladder(c, _edge(side="over"))
    assert over[0].startswith("Books: <b>Pinnacle o37.5 −115</b> · Kalshi o38.0 (49¢) · Betcris o38.0 −108 · BetOnline o38.5 −110")
    assert over[0].endswith("· ref o37.5")
    # spread side: the more favourable line for that side, then odds; away = −home_line
    c["odds"]["betonline"]["spread"] = {"home_line": -2.5, "home_odds": -115, "away_odds": -105}
    home = A.book_ladder(c, _edge(market="spread", side="home", line=-3.0))
    assert home == ["Books: <b>BetOnline −2.5 −115</b> · Novig −3 −105 · ref −3"]
    away = A.book_ladder(c, _edge(market="spread", side="away", line=3.0))
    assert away == ["Books: <b>Novig +3 −105</b> · BetOnline +2.5 −105 · ref +3"]
    # nothing priced
    c["odds"] = {}
    c["consensus"]["total_now"] = None
    assert A.book_ladder(c, _edge()) == ["Books: no lines posted"]
    assert A._cents(-108) == 52 and A._cents(120) == 45 and A._cents(None) is None and A._cents(0) is None


def test_books_ladder_wraps_past_limit():
    c = card()
    c["odds"] = {f"book{i:02d}": {"total": {"line": 38.0 + (i % 4) * 0.5, "under": -100 - i}} for i in range(14)}
    lines = A.book_ladder(c, _edge())
    assert len(lines) >= 2 and all(ln.startswith("Books: ") for ln in lines)
    assert all(len(ln) <= A.LADDER_WRAP_CHARS for ln in lines)
    assert lines[0].startswith("Books: <b>Book03 u39.5 −103</b>") and lines[-1].endswith("· ref u37.5")


def test_edge_message_spread_side_sign():
    c = card([_edge(market="spread", side="home", line=-3.0, fair_line=-4.5, edge_pts=1.5, tier="strong")])
    c["odds"] = {"betonline": {"spread": {"home_line": -3.0, "home_odds": -110, "open_line": -2.5}}}
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert "<b>NE −3 −110 @ BetOnline</b> · market edge +1.5 pts / +4.1% · open −2.5" in text
    assert "fair spread −4.5" in text and "STRONG" not in text
    assert "Books: <b>BetOnline −3 −110</b> · ref −3" in text


def test_edge_message_escapes_html_and_handles_missing_fields():
    c = card()
    c["stadium"] = {"name": "Tom & Jerry <Field>", "roof_state": "outdoors"}
    c["away"]["short"] = "A&M"
    c["weather"] = {"wind_fg": None}
    c["odds"] = {}
    text = A.format_edge(c, _edge(), BOARD)
    assert "Tom &amp; Jerry &lt;Field&gt;" in text and "A&amp;M @ NE" in text
    assert "wind ? mph" in text and "open" not in text.split("\n")[4]
    assert text.split("\n")[3] == "<b>Mid Impact</b> · wind ? mph · ?°F · rain ? mm"
    assert "Books: ref u37.5" in text
    assert "<script" not in text
    c["signal"] = {"label": "Low (Rain) <x>", "flags": ["A&B"]}
    assert "<b>Low (Rain) &lt;x&gt;</b> · wind ? mph · ?°F · rain ? mm · A&amp;B" in A.format_edge(c, _edge(), BOARD)


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


def test_edge_message_uses_active_alert_model_block(monkeypatch):
    """ALERT_MODEL=v2 -> impact numbers, components, emoji and the label come from impact.v2;
    a card without a v2 block falls back to v1 and says so."""
    c = _sample_card()
    c["impact"]["v2"] = {"gs_fg_pct": -8.2, "away_fg_pct": 0.0, "components": {"wind": 5.2, "rain": 3.0, "cold": 0.0}}
    c["fair"]["fair_total_v2"] = 33.9
    monkeypatch.setattr(C, "ALERT_MODEL", "v2")
    text = A.format_edge(c, _edge(), BOARD)
    assert text.split("\n")[2] == "Impact −8.2% (wind 5.2 rain 3.0 · v2) · conf 0.72 · fair total 34.6 (ref pinnacle, 6 books)"
    assert text.startswith("<b>🌬 ")
    c["impact"]["v2"]["components"] = {"rain": 4.0, "wind": 1.0}
    assert A.format_edge(c, _edge(), BOARD).startswith("<b>🌧 ")
    # no edge fair_line -> the v2 fair line from the card, not v1's
    e = dict(_edge(), fair_line=None)
    assert "fair total 33.9" in A.format_edge(c, e, BOARD)
    # openers digest reads the same block
    op = A.format_openers("nfl", 2026, 3, [(c, [f"{GID}|total|over|betcris"])], BOARD)
    assert "−8.2%" in op and "−6.5%" not in op
    # fallback: v2 requested, card only carries v1 -> v1 numbers, labelled v1
    c1 = _sample_card()
    assert "Impact −6.5% (wind 6.5 · v1)" in A.format_edge(c1, _edge(), BOARD)
    monkeypatch.setattr(C, "ALERT_MODEL", "v1")
    assert "Impact −6.5% (wind 6.5 · v1)" in A.format_edge(c, _edge(), BOARD)
    # no components at all -> bare version label, no dangling separator
    c1["impact"]["v1"]["components"] = {}
    assert "Impact −6.5% (v1) ·" in A.format_edge(c1, _edge(), BOARD)


def test_move_gone_wx_messages():
    c = card([_edge(line=39.0, edge_pts=4.4)])
    e = c["fair"]["edges"][0]
    rec = {"first_line": 38.0, "first_edge": 3.4, "last_line": 38.0, "last_edge": 3.4, "last_fair": 34.6,
           "last_wind": 18.0, "last_rain": 0.8, "last_signal": "High Impact"}
    move = A.format_move(c, rec, e, "away from fair", BOARD)
    assert move.split("\n")[0] == "<b>↕️ NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert "<b>UNDER @ BetOnline</b> moved 38 → 39 (away from fair)" in move
    assert "fair 34.6 · edge now 4.4 pts (was 3.4) · −110" in move
    assert move.endswith(f'&amp;game={GID}">board</a>')

    gone_card = card([_edge(line=35.0, edge_pts=0.4)], signal="No Impact", wind=6.0, rain=0.0)
    gone = A.format_gone(gone_card, rec, gone_card["fair"]["edges"][0], BOARD)
    assert gone.split("\n")[0] == "<b>🚫 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert gone.split("\n")[1] == ("SIGNAL GONE: was High Impact → No Impact · <b>UNDER 35 @ BetOnline</b> · "
                                   "market edge now +0.4 pts / +4.1% (alerted at 38)")
    assert gone.split("\n")[2] == "wind 6 mph · 41°F · rain 0 mm"
    neg = A.format_gone(gone_card, rec, dict(gone_card["fair"]["edges"][0], edge_pts=-0.2, edge_prob=None), BOARD)
    assert "market edge now −0.2 pts (market already there)" in neg

    c2 = card([_edge(fair_line=36.1, edge_pts=1.9)], wind=13.0, rain=0.0)
    wx = A.format_wx_move(c2, rec, c2["fair"]["edges"][0], BOARD)
    assert "FORECAST MOVE: wind 18 → 13 mph · rain 0.8 → 0 mm" in wx
    assert "fair total 34.6 → 36.1 · <b>UNDER 38 @ BetOnline</b> edge 1.9 pts" in wx

    c3 = card(signal="Mid Impact", wind=17.0)
    chg = A.format_signal_change(c3, dict(rec, last_signal="Low Impact"), c3["fair"]["edges"][0], BOARD)
    lines = chg.split("\n")
    assert lines[0] == "<b>🌦 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b>"
    assert lines[1] == "SIGNAL Low Impact → <b>Mid Impact</b> · wind 17 mph · 41°F · rain 0.8 mm"
    assert lines[2] == "<b>UNDER 38 −110 @ BetOnline</b> · market edge +3.4 pts / +4.1%"
    assert lines[3] == "Books: <b>BetOnline u38.0 −110</b> · ref u37.5"
    assert lines[4].endswith(f'&amp;game={GID}">board</a>') and len(lines) == 5


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


def test_context_spread_is_the_consensus_spread_not_a_book_line():
    """Openers digest + Books-ladder ``ref`` quote ``consensus.spread_now`` (3-book average),
    never a single book's spread; the bet line itself stays the book's number."""
    c = card()
    c["odds"]["betonline"]["spread"] = {"home_line": -3.5, "home_odds": -110, "away_odds": -110, "open_line": -3.0}
    c["consensus"]["spread_now"] = -2.67
    c["consensus"]["spread_src"] = "cris+bol+pin"
    text = A.format_openers("nfl", 2026, 3, [(c, [f"{GID}|spread|home|betonline"])], BOARD)
    assert text.split("\n")[1] == "SEA @ NE Sun 1:00p ET · wind 18 · −6.5% · tot 37.5 sp −2.7 · BetOnline"
    assert "−3.5" not in text
    e = dict(_edge(), market="spread", side="home", line=-3.5, fair_line=-2.9, edge_pts=0.6)
    ladder = A.book_ladder(c, e)
    assert ladder == ["Books: <b>BetOnline −3.5 −110</b> · ref −2.7"]
    assert A._bet_line(c, e).startswith("<b>NE −3.5 −110 @ BetOnline</b>")


def test_candidate_summary_is_one_line_without_link():
    alerts = pstate.migrate(None, "alerts")
    c = A.edge_candidates(_sample_card(), alerts, A.Config(board_url=BOARD))[0]
    assert "\n" not in c.summary and "<a " not in c.summary
    # the play is the largest under edge on the card (Betcris 3.9), the summary is header + bet line
    assert c.summary == "<b>🌬 NFL Wk 3 · SEA @ NE · Sun 1:00p ET</b> · <b>UNDER 38.5 −108 @ Betcris</b> · market edge +3.9 pts / +5.0%"


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
