"""Compact, scan-first Telegram formatters in :mod:`pipeline.alerts`."""

from __future__ import annotations

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


def test_edge_message_is_a_compact_scan_first_play():
    c = _sample_card()
    c["signal"]["flags"] = ["NFL Wind"]
    text = A.format_edge(c, _edge(), BOARD)
    lines = text.split("\n")
    assert lines == [
        "🎯 <b>PLAY · MID · NFL W3</b>",
        "<b>SEA @ NE</b> · Sun 1:00p ET",
        "<b>Under 38 (−110) · BetOnline</b>",
        "Why: +3.4 pts above fair 34.6 · 18 mph wind",
        f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">Details &amp; all prices</a>',
    ]
    # unicode minus for negative odds, no ASCII hyphen-minus in the price
    assert "-110" not in text and "−110" in text
    assert "Books:" not in text and "Gillette Stadium" not in text


def test_edge_message_keeps_value_honest_and_handles_missing_lines():
    c = card([_edge(edge_pts=-0.6, edge_prob=-0.012, fair_line=38.6)], signal="Low (Rain)")
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert text.splitlines()[0] == "🎯 <b>PLAY · LOW · NFL W3</b>"
    assert "Why: −0.6 pts above fair 38.6 · 18 mph wind" in text
    zero = A.format_edge(c, dict(_edge(), edge_pts=0.0, edge_prob=0.0), BOARD)
    assert "Why: 0.0 pts above fair 34.6 · 18 mph wind" in zero
    # consensus-synthesised entries (consensus.total_now vs fair.fair_total): line + fair, or nothing posted
    c["fair"]["fair_total"] = 38.6
    cons = A.consensus_entry(c)
    assert cons["edge_pts"] == -1.1 and cons["line"] == 37.5 and cons["fair_line"] == 38.6
    assert "<b>Under 37.5 (?) · Consensus</b>" in A.format_edge(c, cons, BOARD)
    assert "Why: −1.1 pts above fair 38.6" in A.format_edge(c, cons, BOARD)
    c["consensus"]["total_now"] = None
    assert "<b>Under · no line available</b>" in A.format_edge(c, A.consensus_entry(c), BOARD)
    c["consensus"]["total_now"] = 37.5
    c["fair"]["fair_total"] = None
    assert "Why: ? pts above fair ?" in A.format_edge(c, A.consensus_entry(c), BOARD)


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
    lines = A.format_edge(c, c["fair"]["edges"][0], BOARD).splitlines()
    assert lines[2] == "<b>NE −3 (−110) · BetOnline</b>"
    assert lines[3] == "Why: +1.5 pts above fair −4.5 · 18 mph wind"
    assert len(lines) == 5


def test_edge_message_escapes_html_and_handles_missing_fields():
    c = card()
    c["stadium"] = {"name": "Tom & Jerry <Field>", "roof_state": "outdoors"}
    c["away"]["short"] = "A&M <script>"
    c["weather"] = {"wind_fg": None}
    c["odds"] = {}
    text = A.format_edge(c, dict(_edge(), book="book <x>&"), BOARD)
    assert "<b>A&amp;M &lt;script&gt; @ NE</b>" in text
    assert "Book &lt;X&gt;&amp;" in text
    assert "Why: +3.4 pts above fair 34.6 · ? mph wind" in text
    assert "<script" not in text
    assert f"week=3&amp;game={GID}" in text


def test_edge_message_driver_uses_the_dominant_component():
    c2 = card()
    c2["impact"]["v1"]["components"] = {"rain": 3.0, "wind": 2.0}
    assert "Why: +3.4 pts above fair 34.6 · 0.8 mm rain" in A.format_edge(c2, _edge(), BOARD)
    c3 = card()
    c3["impact"]["v1"]["components"] = {"cold": 1.0}
    assert "Why: +3.4 pts above fair 34.6 · 41°F" in A.format_edge(c3, _edge(), BOARD)


def test_edge_message_uses_active_alert_model_block(monkeypatch):
    """The one-line reason follows the selected model and falls back to v1."""
    c = _sample_card()
    c["impact"]["v2"] = {"gs_fg_pct": -8.2, "away_fg_pct": 0.0, "components": {"wind": 5.2, "rain": 3.0, "cold": 0.0}}
    c["fair"]["fair_total_v2"] = 33.9
    monkeypatch.setattr(C, "ALERT_MODEL", "v2")
    text = A.format_edge(c, _edge(), BOARD)
    assert text.splitlines()[3] == "Why: +3.4 pts above fair 34.6 · 18 mph wind"
    c["impact"]["v2"]["components"] = {"rain": 4.0, "wind": 1.0}
    assert A.format_edge(c, _edge(), BOARD).splitlines()[3].endswith("· 0.8 mm rain")
    # openers digest reads the same block
    op = A.format_openers("nfl", 2026, 3, [(c, [f"{GID}|total|over|betcris"])], BOARD)
    assert "−8.2%" in op and "−6.5%" not in op
    # A card without v2 falls back to the v1 driver.
    c1 = _sample_card()
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[3].endswith("· 18 mph wind")
    monkeypatch.setattr(C, "ALERT_MODEL", "v1")
    assert A.format_edge(c, _edge(), BOARD).splitlines()[3].endswith("· 18 mph wind")
    # With no components, a signal flag is the concise fallback reason.
    c1["impact"]["v1"]["components"] = {}
    c1["signal"]["flags"] = ["NFL Wind"]
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[3].endswith("· NFL Wind")


def test_update_closed_and_forecast_messages_are_concise():
    c = card([_edge(line=39.0, edge_pts=4.4)])
    e = c["fair"]["edges"][0]
    rec = {"first_line": 38.0, "first_edge": 3.4, "last_line": 38.0, "last_edge": 3.4, "last_fair": 34.6,
           "last_wind": 18.0, "last_rain": 0.8, "last_signal": "High Impact"}
    move = A.format_move(c, rec, e, "away from fair", BOARD)
    assert move.splitlines() == [
        "🔄 <b>UPDATE · MID · NFL W3</b>",
        "<b>SEA @ NE</b> · Sun 1:00p ET",
        "Line: Under 38 → 39 · BetOnline −110",
        "Value: +3.4 → +4.4 pts",
        f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">Details &amp; all prices</a>',
    ]

    gone_card = card([_edge(line=35.0, edge_pts=0.4)], signal="No Impact", wind=6.0, rain=0.0)
    gone = A.format_gone(gone_card, rec, gone_card["fair"]["edges"][0], BOARD)
    assert gone.splitlines() == [
        "⛔ <b>CLOSED · NFL W3</b>",
        "<b>SEA @ NE</b> · Sun 1:00p ET",
        "Reason: Signal High Impact → No Impact",
        "Was: Under 38 · Now: 35 (+0.4 pts vs fair)",
        f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">Details &amp; all prices</a>',
    ]
    neg = A.format_gone(gone_card, rec, dict(gone_card["fair"]["edges"][0], edge_pts=-0.2, edge_prob=None), BOARD)
    assert "Was: Under 38 · Now: 35 (−0.2 pts vs fair)" in neg

    c2 = card([_edge(fair_line=36.1, edge_pts=1.9)], wind=13.0, rain=0.0)
    wx = A.format_wx_move(c2, rec, c2["fair"]["edges"][0], BOARD)
    wx_lines = wx.splitlines()
    assert wx_lines[0] == "🔄 <b>UPDATE · MID · NFL W3</b>"
    assert wx_lines[2] == "Forecast: fair total 34.6 → 36.1"
    assert wx_lines[3] == "Weather: wind 18 → 13 mph · rain 0.8 → 0 mm"
    assert wx_lines[4] == "<b>Play: Under 38 (−110) · BetOnline</b>"
    assert len(wx_lines) == 6

    c3 = card(signal="Mid Impact", wind=17.0)
    chg = A.format_signal_change(c3, dict(rec, last_signal="Low Impact"), c3["fair"]["edges"][0], BOARD)
    lines = chg.splitlines()
    assert lines[0] == "🔄 <b>UPDATE · MID · NFL W3</b>"
    assert lines[2] == "Signal: <b>Low Impact → Mid Impact</b>"
    assert lines[3] == "<b>Play: Under 38 (−110) · BetOnline</b>"
    assert lines[4] == "Why: +3.4 pts above fair 34.6 · 17 mph wind"
    assert len(lines) == 6


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
    system = A.format_digest("SYSTEM", [ops])
    assert system == ["<b>SYSTEM (1)</b>\n\n1. ⚠️ <b>Degradation [warn] weather</b>\nopen-meteo &lt;503&gt; &amp; retry"]
    assert system[0].count("SYSTEM") == 1

    msgs = A.format_digest("SUMMARY", ["a", "b"])
    assert msgs == ["<b>SUMMARY (2)</b>\n\n1. a\n\n2. b"]
    big = A.format_digest("SUMMARY", ["x" * 1500] * 5)
    assert len(big) == 1 and len(big[0]) <= A.TELEGRAM_MAX_CHARS
    assert big[0].count("…") == 5 and "(cont.)" not in big[0]


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
    # The summary keeps only the tier, matchup, action, price source, and kickoff.
    assert c.summary == "🎯 MID · SEA @ NE · Under 38.5 (−108) · Betcris · Sun 1:00p ET"


def test_kickoff_label_and_helpers():
    c = card(kickoff=datetime(2026, 11, 30, 1, 15, tzinfo=timezone.utc))   # Sun 8:15p ET (EST)
    assert A._kick_label(c) == "Sun 8:15p ET"
    assert A._fmt_odds(105) == "+105" and A._fmt_odds(-105) == "−105" and A._fmt_odds(None) == "?"
    assert A._fmt_line(38.5) == "38.5" and A._fmt_line(-3.0, signed=True) == "−3" and A._fmt_line(2.5, signed=True) == "+2.5"
    assert A._fmt_pct(4.06) == "+4.1%" and A._fmt_pct(-6.5) == "−6.5%" and A._fmt_pct(0) == "0.0%"
    assert A.parse_edge_key(f"edge|2026|3|{GID}|total|under|betonline|v1")["book"] == "betonline"
    assert A.parse_edge_key("move|x|1") is None
    assert A.et_day(NOW) == "2026-09-18" and A.et_day(KICK + timedelta(hours=8)) == "2026-09-20"


def test_clv_scorecard_prioritizes_overall_signal_and_best_worst():
    alerts = pstate.migrate(None, "alerts")
    assert A.clv_digest(alerts) == "<b>📊 CLV SCORECARD</b>\nNo settled plays with a closing line yet."
    for i, (book, clv) in enumerate([("betonline", 1.5), ("betcris", -0.5), ("fanduel", 2.0), ("betonline", 0.0)]):
        pstate.upsert_alert_record(alerts, f"edge|2026|3|nfl:2026:3:t{i}@ne|total|under|{book}|v1",
                                   {"family": "edge", "sport": "nfl", "book": book, "tier": "edge" if i else "strong",
                                    "side": "under", "first_line": 40.0 + i, "closing_line": 40.0 + i + clv, "clv_pts": clv,
                                    "game_id": f"nfl:2026:3:t{i}@ne"}, "t")
    text = A.clv_digest(alerts, top_n=2)
    assert text.startswith("<b>📊 CLV SCORECARD · 4 settled plays</b>\nOverall: avg +0.75 pts · positive 2/4")
    assert "<b>By signal</b>\n  Strong: 1 plays · avg +1.50 · positive 1/1" in text
    assert "  Edge: 3 plays · avg +0.50 · positive 1/3" in text
    assert "By league" not in text and "By book" not in text
    best = text.split("<b>Best 2</b>")[1].split("<b>Worst 2</b>")[0]
    assert "T2 @ NE" in best.splitlines()[1] and "T0 @ NE" in best.splitlines()[2]
    worst = text.split("<b>Worst 2</b>")[1]
    assert "T1 @ NE" in worst.splitlines()[1]
    assert A.clv_digest(alerts, sport="cfb") == "<b>📊 CLV SCORECARD</b>\nNo settled plays with a closing line yet."


def test_cli_digest_and_flush_dry_run(tmp_path, capsys):
    assert A.main(["--digest", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "CLV SCORECARD" in out and "clv digest: sent" in out
    tg = pstate.migrate(None, "telegram_state")
    pstate.queue_alert(tg, {"key": "edge|k", "family": "edge", "sport": "nfl", "text": "<b>hi</b>", "record": {"family": "edge"}})
    pstate.save_telegram_state(tmp_path, tg)
    assert A.main(["--flush", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "MANUAL QUEUE · SNAPSHOT (1)" in out and "flush: 1 alert(s) in 1 message(s)" in out
    assert pstate.load_telegram_state(tmp_path)["queue"] != []   # dry-run keeps the queue
    assert A.main(["--state-dir", str(tmp_path)]) == 2
