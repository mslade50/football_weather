"""Compact, scan-first Telegram formatters in :mod:`pipeline.alerts`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.model import config as C
from pipeline.model import signals
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
        "Why:",
        "• Value: +3.4 pts above fair 34.6",
        "• Wind: 18 mph",
        f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">Details &amp; all prices</a>',
    ]
    # unicode minus for negative odds, no ASCII hyphen-minus in the price
    assert "-110" not in text and "−110" in text
    assert "Books:" not in text and "Gillette Stadium" not in text


def test_edge_message_keeps_value_honest_and_handles_missing_lines():
    c = card([_edge(edge_pts=-0.6, edge_prob=-0.012, fair_line=38.6)], signal="Low (Rain)")
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert text.splitlines()[0] == "🎯 <b>PLAY · LOW · NFL W3</b>"
    assert "• Value: −0.6 pts above fair 38.6" in text and "• Wind: 18 mph" in text
    zero = A.format_edge(c, dict(_edge(), edge_pts=0.0, edge_prob=0.0), BOARD)
    assert "• Value: 0.0 pts above fair 34.6" in zero and "• Wind: 18 mph" in zero
    # consensus-synthesised entries (consensus.total_now vs fair.fair_total): line + fair, or nothing posted
    c["fair"]["fair_total"] = 38.6
    cons = A.consensus_entry(c)
    assert cons["edge_pts"] == -1.1 and cons["line"] == 37.5 and cons["fair_line"] == 38.6
    assert "<b>Under 37.5 (?) · Consensus</b>" in A.format_edge(c, cons, BOARD)
    assert "• Value: −1.1 pts above fair 38.6" in A.format_edge(c, cons, BOARD)
    c["consensus"]["total_now"] = None
    assert "<b>Under · no line available</b>" in A.format_edge(c, A.consensus_entry(c), BOARD)
    c["consensus"]["total_now"] = 37.5
    c["fair"]["fair_total"] = None
    assert "• Value: ? pts above fair ?" in A.format_edge(c, A.consensus_entry(c), BOARD)


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
    assert lines[3:6] == ["Why:", "• Value: +1.5 pts above fair −4.5", "• Wind: 18 mph"]
    assert len(lines) == 7


def test_edge_message_escapes_html_and_handles_missing_fields():
    c = card()
    c["stadium"] = {"name": "Tom & Jerry <Field>", "roof_state": "outdoors"}
    c["away"]["short"] = "A&M <script>"
    c["weather"] = {"wind_fg": None}
    c["odds"] = {}
    text = A.format_edge(c, dict(_edge(), book="book <x>&"), BOARD)
    assert "<b>A&amp;M &lt;script&gt; @ NE</b>" in text
    assert "Book &lt;X&gt;&amp;" in text
    assert "• Value: +3.4 pts above fair 34.6" in text and "• Wind: ? mph" in text
    assert "<script" not in text
    assert f"week=3&amp;game={GID}" in text


def test_edge_message_driver_uses_the_dominant_component():
    c2 = card()
    c2["impact"]["v1"]["components"] = {"rain": 3.0, "wind": 2.0}
    assert "• Rain: 0.8 mm" in A.format_edge(c2, _edge(), BOARD)
    c3 = card()
    c3["impact"]["v1"]["components"] = {"cold": 1.0}
    assert "• Temperature: 41°F" in A.format_edge(c3, _edge(), BOARD)


def test_edge_message_names_cfb_altitude_warmth_mid_trigger():
    edge = _edge(line=53.5, odds=-115, fair_line=52.5, edge_pts=1.0)
    c = card([edge], sport="cfb", game_id="cfb:2026:1:maine@appalachian-state", wind=6.7, rain=0.0, gs=0.0)
    c["away"] = {"team_id": "maine", "name": "Maine", "short": "MAINE"}
    c["home"] = {"team_id": "appalachian-state", "name": "Appalachian State", "short": "APP"}
    c["weather"]["temp_fg"] = 78.3
    c["travel_alt"] = 955.4
    sig = signals.cfb_signal(6.7, 78.3, 0.0, -9.5, 955.4, 52.4, 47.2, weekday=1)
    c["signal"].update({"label": sig.label, "level": sig.level, "drivers": list(sig.drivers)})
    c["impact"]["v1"]["components"] = {"wind": 0.0, "rain": 0.0, "heat": 0.0, "alt": 0.0}

    text = A.format_edge(c, edge, BOARD)
    altitude = "• Altitude + warmth: +3,135 ft climb · 78°F"
    assert "• Value: +1.0 pts above fair 52.5" in text
    assert altitude in text and "Weather: wind 6.7 mph" not in text

    rec = {"last_signal": "Low Impact", "last_line": 52.5, "last_edge": 0.0, "last_fair": 52.5,
           "last_wind": 6.7, "last_rain": 0.0}
    assert altitude in A.format_signal_change(c, rec, edge, BOARD)
    assert altitude in A.format_move(c, rec, edge, "away from fair", BOARD)
    assert altitude in A.format_wx_move(c, rec, edge, BOARD)


def test_edge_message_uses_active_alert_model_block(monkeypatch):
    """The one-line reason follows the selected model and falls back to v1."""
    c = _sample_card()
    c["impact"]["v2"] = {"gs_fg_pct": -8.2, "away_fg_pct": 0.0, "components": {"wind": 5.2, "rain": 3.0, "cold": 0.0}}
    c["fair"]["fair_total_v2"] = 33.9
    monkeypatch.setattr(C, "ALERT_MODEL", "v2")
    text = A.format_edge(c, _edge(), BOARD)
    assert text.splitlines()[3:6] == ["Why:", "• Value: +3.4 pts above fair 34.6", "• Wind: 18 mph"]
    c["impact"]["v2"]["components"] = {"rain": 4.0, "wind": 1.0}
    assert A.format_edge(c, _edge(), BOARD).splitlines()[5] == "• Rain: 0.8 mm"
    # openers digest reads the same block
    op = A.format_openers("nfl", 2026, 3, [(c, [f"{GID}|total|over|betcris"])], BOARD)
    assert "−8.2%" in op and "−6.5%" not in op
    # A card without v2 falls back to the v1 driver.
    c1 = _sample_card()
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[5] == "• Wind: 18 mph"
    monkeypatch.setattr(C, "ALERT_MODEL", "v1")
    assert A.format_edge(c, _edge(), BOARD).splitlines()[5] == "• Wind: 18 mph"
    # With no components, a signal flag is the concise fallback reason.
    c1["impact"]["v1"]["components"] = {}
    c1["signal"]["flags"] = ["NFL Wind"]
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[5] == "• Signal: NFL Wind"


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
    assert lines[4:7] == ["Why:", "• Value: +3.4 pts above fair 34.6", "• Wind: 17 mph"]
    assert len(lines) == 8


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


def test_postmortem_digest_is_clear_bounded_and_game_level():
    bet = {"game_id": GID, "sport": "nfl", "away": "Seattle", "home": "New England",
           "market": "total", "side": "under", "book": "betonline", "tier": "mid",
           "first_line": 38.5, "first_odds": -108, "first_edge": 2.2, "hours_before_kickoff": 40,
           "closing_line": 37.0, "clv_pts": 1.5, "result": "W", "unit_profit": 100 / 108,
           "actual_total": 34, "first_weather": {"wind_mph": 14}, "final_forecast": {"wind_mph": 13},
           "actual_weather": {"wind_mph": 12}, "wind_thesis": True, "wind_materialized": True}
    payload = {"postmortem": {"latest": {"window_start": "2026-09-15T13:17:00Z",
                                             "window_end": "2026-09-22T13:17:00Z", "bets": [bet]}}}
    text = A.postmortem_digest(payload, board_url=BOARD)
    assert text.startswith("<b>🧾 FOOTBALL WEEKLY POST-MORTEM · Sep 15–Sep 22</b>")
    assert "Results: 1-0-0 W-L-P · +0.93u · ROI +92.6%" in text
    assert "Closing line: beat 1/1 · avg +1.50 pts" in text and "Wind thesis: 1/1 materialized" in text
    assert "Wind MAE: 2.0 at alert → 1.0 final mph" in text
    assert "Bet: U38.5 −108 · BetOnline · edge +2.2 pts · 40h early" in text
    assert "Close: 37 · CLV +1.5 pts · game total 34" in text
    assert "Wind: 14 alert → 13 final fcst → 12 actual mph — materialized" in text
    assert "#view=backtest" in text and len(text) < A.TELEGRAM_MAX_CHARS


def test_postmortem_digest_marks_telegram_overflow_and_email_version_is_complete():
    bet = {"game_id": GID, "sport": "cfb", "away": "A" * 30, "home": "B" * 30,
           "market": "total", "side": "under", "first_line": 48.5, "first_odds": -110,
           "first_edge": 2.0, "result": "W", "unit_profit": 100 / 110,
           "first_weather": {"wind_mph": 15}, "final_forecast": {"wind_mph": 14},
           "actual_weather": {"wind_mph": 13}, "wind_thesis": True, "wind_materialized": True}
    payload = {"postmortem": {"latest": {"window_start": "2026-09-01T13:17:00Z",
                                            "window_end": "2026-09-08T13:17:00Z",
                                            "bets": [dict(bet, game_id=f"g{i}") for i in range(40)]}}}
    telegram = A.postmortem_digest(payload, sport="cfb", board_url=BOARD)
    complete = A.postmortem_digest(payload, sport="cfb", board_url=BOARD, max_chars=None)
    assert len(telegram) <= A.TELEGRAM_MAX_CHARS
    assert A.POSTMORTEM_MORE_MARKER in telegram
    assert A.POSTMORTEM_MORE_MARKER not in complete
    assert complete.count("• ✅") == 40


def test_postmortem_email_config_and_delivery_are_plain_text():
    env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "2525", "SMTP_USERNAME": "sender@test",
           "SMTP_PASSWORD": "secret", "POSTMORTEM_EMAIL_TO": "one@test, two@test",
           "SMTP_FROM": "reports@test", "SMTP_STARTTLS": "true"}
    cfg = A.EmailConfig.from_env(env)
    assert cfg.configured and cfg.to_addrs == ("one@test", "two@test") and cfg.port == 2525

    class FakeSMTP:
        instance = None

        def __init__(self, host, port, timeout):
            self.args = (host, port, timeout)
            self.tls = False
            self.auth = None
            self.message = None
            FakeSMTP.instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def starttls(self):
            self.tls = True

        def login(self, username, password):
            self.auth = (username, password)

        def send_message(self, message):
            self.message = message

    assert A.send_postmortem_email("<b>WEEKLY</b>\n• clear &amp; brief", sport="cfb",
                                   board_url=BOARD, cfg=cfg, smtp_factory=FakeSMTP)
    sent = FakeSMTP.instance
    assert sent.args == ("smtp.test", 2525, 20) and sent.tls
    assert sent.auth == ("sender@test", "secret")
    assert sent.message["Subject"] == "CFB weekly betting post-mortem"
    assert sent.message["To"] == "one@test, two@test"
    body = sent.message.get_content()
    assert "WEEKLY\n• clear & brief" in body and "<b>" not in body
    assert f"Dashboard: {BOARD}/#view=backtest" in body
    assert not A.send_postmortem_email("x", sport="nfl", board_url=BOARD,
                                       cfg=A.EmailConfig(), smtp_factory=FakeSMTP)


def test_postmortem_dry_run_previews_but_never_sends_email(tmp_path, capsys, monkeypatch):
    bet = {"game_id": "template", "sport": "cfb", "away": "A" * 30, "home": "B" * 30,
           "market": "total", "side": "under", "first_line": 48.5, "first_odds": -110,
           "result": "W", "first_weather": {}, "final_forecast": {}, "actual_weather": {}}
    payload = {"postmortem": {"latest": {"bets": [dict(bet, game_id=f"g{i}") for i in range(50)]}}}
    backtest = tmp_path / "backtest.json"
    import json
    backtest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(A, "send_postmortem_email", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    assert A.main(["--digest", "postmortem", "--backtest", str(backtest), "--sport", "cfb",
                   "--email-fallback", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "EMAIL FALLBACK PREVIEW" in out and "email fallback: dry-run (telegram was truncated)" in out


def test_cli_digest_and_flush_dry_run(tmp_path, capsys):
    assert A.main(["--digest", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "CLV SCORECARD" in out and "clv digest: sent" in out
    backtest = tmp_path / "backtest.json"
    backtest.write_text('{"postmortem":{"latest":{"bets":[]}}}', encoding="utf-8")
    assert A.main(["--digest", "postmortem", "--backtest", str(backtest), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "WEEKLY POST-MORTEM" in out and "postmortem digest: sent" in out
    tg = pstate.migrate(None, "telegram_state")
    pstate.queue_alert(tg, {"key": "edge|k", "family": "edge", "sport": "nfl", "text": "<b>hi</b>", "record": {"family": "edge"}})
    pstate.save_telegram_state(tmp_path, tg)
    assert A.main(["--flush", "--state-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "MANUAL QUEUE · SNAPSHOT (1)" in out and "flush: 1 alert(s) in 1 message(s)" in out
    assert pstate.load_telegram_state(tmp_path)["queue"] != []   # dry-run keeps the queue
    assert A.main(["--state-dir", str(tmp_path)]) == 2
