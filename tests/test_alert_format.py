"""Compact, scan-first Telegram formatters in :mod:`pipeline.alerts`."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.model import config as C
from pipeline.model import signals
from tests.test_alerts_rules import CFG, GID, NOW, _edge, card

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
        "🌬️ <b>SIGNAL · MID · NFL W3</b>",
        "<b>SEA @ NE</b> · Sun 1:00p ET",
        "<b>Under 38 (−110) · BetOnline</b>",
        "Weather: Wind: 18 mph",
        "Forecast: wind 18 mph · temp 41.2 °F · rain 0.8 mm",
        "Best price: unavailable (no fresh comparison)",
        "Best exchange: unavailable (no fresh comparison)",
        "Stadium wind unders: history unavailable",
        "Week open: Under 38 (?) · BetOnline",
        "Kalshi now: unavailable",
        "NoVig now: unavailable",
        f'<a href="{BOARD}/#sport=nfl&amp;week=3&amp;game={GID}">Details &amp; all prices</a>',
    ]
    # unicode minus for negative odds, no ASCII hyphen-minus in the price
    assert "-110" not in text and "−110" in text
    assert "Books:" not in text and "Gillette Stadium" not in text


def test_edge_message_omits_fair_estimates_and_handles_missing_lines():
    c = card([_edge(edge_pts=-0.6, edge_prob=-0.012, fair_line=38.6)], signal="Low (Rain)")
    text = A.format_edge(c, c["fair"]["edges"][0], BOARD)
    assert text.splitlines()[0] == "🌧️ <b>SIGNAL · LOW · NFL W3</b>"
    assert "fair" not in text.lower() and "Weather: Wind: 18 mph" in text
    zero = A.format_edge(c, dict(_edge(), edge_pts=0.0, edge_prob=0.0), BOARD)
    assert "fair" not in zero.lower() and "Weather: Wind: 18 mph" in zero
    # Internal estimates remain available, but are not displayed in notifications.
    c["fair"]["fair_total"] = 38.6
    cons = A.consensus_entry(c)
    assert cons["edge_pts"] == -1.1 and cons["line"] == 37.5 and cons["fair_line"] == 38.6
    assert "<b>Under · no posted price available</b>" in A.format_edge(c, cons, BOARD)
    assert "fair" not in A.format_edge(c, cons, BOARD)
    c["consensus"]["total_now"] = None
    assert "<b>Under · no posted price available</b>" in A.format_edge(c, A.consensus_entry(c), BOARD)
    c["consensus"]["total_now"] = 37.5
    c["fair"]["fair_total"] = None
    assert "fair" not in A.format_edge(c, A.consensus_entry(c), BOARD)


def test_edge_message_spread_side_sign():
    c = card([_edge(market="spread", side="home", line=-3.0, fair_line=-4.5, edge_pts=1.5, tier="strong")])
    c["odds"]["betonline"]["spread"] = {"home_line": -3.0, "home_odds": -110, "open_line": -2.5}
    lines = A.format_edge(c, c["fair"]["edges"][0], BOARD).splitlines()
    assert lines[2] == "<b>NE −3 (−110) · BetOnline</b>"
    assert lines[3] == "Weather: Wind: 18 mph"
    assert len(lines) == 10


def test_edge_message_escapes_html_and_handles_missing_fields():
    c = card()
    c["stadium"] = {"name": "Tom & Jerry <Field>", "roof_state": "outdoors"}
    c["away"]["short"] = "A&M <script>"
    c["weather"] = {"wind_fg": None}
    c["odds"] = {}
    text = A.format_edge(c, dict(_edge(), book="book <x>&"), BOARD)
    assert "<b>A&amp;M &lt;script&gt; @ NE</b>" in text
    assert "Book &lt;X&gt;&amp;" in text
    assert "fair" not in text.lower() and "Weather: Wind: ? mph" in text
    assert "<script" not in text
    assert f"week=3&amp;game={GID}" in text


def test_edge_message_driver_uses_the_dominant_component():
    c2 = card()
    c2["impact"]["v1"]["components"] = {"rain": 3.0, "wind": 2.0}
    assert "Weather: Rain: 0.8 mm" in A.format_edge(c2, _edge(), BOARD)
    c3 = card()
    c3["impact"]["v1"]["components"] = {"cold": 1.0}
    assert "Weather: Temperature: 41°F" in A.format_edge(c3, _edge(), BOARD)


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
    altitude = "Weather: Altitude + warmth: +3,135 ft climb · 78°F"
    assert "fair" not in text
    assert altitude in text and "Weather: wind 6.7 mph" not in text

def test_edge_message_uses_active_alert_model_block(monkeypatch):
    """The one-line reason follows the selected model and falls back to v1."""
    c = _sample_card()
    c["impact"]["v2"] = {"gs_fg_pct": -8.2, "away_fg_pct": 0.0, "components": {"wind": 5.2, "rain": 3.0, "cold": 0.0}}
    c["fair"]["fair_total_v2"] = 33.9
    monkeypatch.setattr(C, "ALERT_MODEL", "v2")
    text = A.format_edge(c, _edge(), BOARD)
    assert text.splitlines()[3] == "Weather: Wind: 18 mph"
    c["impact"]["v2"]["components"] = {"rain": 4.0, "wind": 1.0}
    assert A.format_edge(c, _edge(), BOARD).splitlines()[3] == "Weather: Rain: 0.8 mm"
    # A card without v2 falls back to the v1 driver.
    c1 = _sample_card()
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[3] == "Weather: Wind: 18 mph"
    monkeypatch.setattr(C, "ALERT_MODEL", "v1")
    assert A.format_edge(c, _edge(), BOARD).splitlines()[3] == "Weather: Wind: 18 mph"
    # With no components, a signal flag is the concise fallback reason.
    c1["impact"]["v1"]["components"] = {}
    c1["signal"]["flags"] = ["NFL Wind"]
    assert A.format_edge(c1, _edge(), BOARD).splitlines()[3] == "Weather: Signal: NFL Wind"


def test_book_health_ops_format():
    ops = A.format_ops("Degradation [warn] weather", "open-meteo <503> & retry")
    assert ops == "⚠️ <b>Degradation [warn] weather</b>\nopen-meteo &lt;503&gt; &amp; retry"
    assert A.format_ops("x") == "⚠️ <b>x</b>"


def test_telegram_says_when_the_opener_line_is_unknown():
    c = card()
    edge = c["fair"]["edges"][0]
    assert "Opener line unknown" not in A.format_edge(c, edge, BOARD)
    assert "Opener line unknown" not in A._play_summary(c, edge)
    c["process"] = {"line_open_missing": True}
    for text in (A.format_edge(c, edge, BOARD), A._play_summary(c, edge)):
        assert "Opener line unknown" in text
    bare = card()
    del bare["odds"]["betonline"]["total"]["open_line"]
    assert "Opener line unknown" in A.format_edge(bare, bare["fair"]["edges"][0], BOARD)


def test_candidate_summary_includes_weather_without_link():
    alerts = pstate.migrate(None, "alerts")
    c = A.edge_candidates(_sample_card(), alerts, A.Config(board_url=BOARD))[0]
    assert "Forecast: wind 18 mph · temp 41.2 °F · rain 0.8 mm" in c.summary and "<a " not in c.summary
    # The summary includes the current bet, weekly opener, and complete weather.
    assert c.summary == ("🌬️ MID · SEA @ NE · Under 38 (−105) · Novig · size unverified · Sun 1:00p ET\n"
                         "Week open: Under 38 (?) · BetOnline\n"
                         "Forecast: wind 18 mph · temp 41.2 °F · rain 0.8 mm")


@pytest.mark.parametrize("drivers,expected", [
    (["rain"], "🌧️"), (["wind"], "🌬️"), (["temperature"], "🌡️"),
    (["heat"], "🔥"), (["cold"], "🥶"), (["altitude_warmth"], "⛰️ 🔥"),
    (["rain", "wind", "rain"], "🌧️ 🌬️"),
])
def test_signal_trigger_icons_are_consistent_in_alerts_and_summaries(drivers, expected):
    c = card()  # Wind model impact stays largest even for a rain-triggered signal.
    c["signal"]["drivers"] = drivers
    assert A.format_edge(c, _edge()).startswith(expected + " <b>SIGNAL")
    assert A._play_summary(c, _edge()).startswith(expected + " MID")


def test_unknown_signal_icon_has_a_neutral_fallback():
    c = card(signal=None)
    c["impact"] = {}
    assert A._emoji_for(c) == "📈"


def test_kickoff_label_and_helpers():
    c = card(kickoff=datetime(2026, 11, 30, 1, 15, tzinfo=timezone.utc))   # Sun 8:15p ET (EST)
    assert A._kick_label(c) == "Sun 8:15p ET"
    assert A._fmt_odds(105) == "+105" and A._fmt_odds(-105) == "−105" and A._fmt_odds(None) == "?"
    assert A._fmt_line(38.5) == "38.5" and A._fmt_line(-3.0, signed=True) == "−3" and A._fmt_line(2.5, signed=True) == "+2.5"
    assert A.parse_edge_key(f"edge|2026|3|{GID}|total|under|betonline|v1")["book"] == "betonline"
    assert A.parse_edge_key("move|x|1") is None


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


def test_cli_postmortem_dry_run(tmp_path, capsys):
    backtest = tmp_path / "backtest.json"
    backtest.write_text('{"postmortem":{"latest":{"bets":[]}}}', encoding="utf-8")
    assert A.main(["--digest", "postmortem", "--backtest", str(backtest), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "WEEKLY POST-MORTEM" in out and "postmortem digest: sent" in out
    assert A.main(["--state-dir", str(tmp_path)]) == 2


def test_alert_price_context_uses_weekly_opener_and_each_exchange_line():
    c = card()
    c["odds"]["betonline"]["total"].update(open_line=41.5, open_under=-105)
    c["weekly_total_open"].update(line=41.5, under=-105)
    c["odds"]["kalshi"] = {"total": {"line": 39.5, "under": -108, "over": 120}}
    c["odds"]["novig"] = {"total": {"line": 40.0, "under": -115, "over": 105}}
    expected = ["Week open: Under 41.5 (−105) · BetOnline",
                "Kalshi now: Under 39.5 (52¢)", "NoVig now: Under 40 (−115)"]
    for text in (A.format_edge(c, _edge()),):
        assert text.splitlines()[-4:-1] == expected
    over = A._price_context(c, _edge(side="over"))
    assert over[1:] == ["Kalshi now: Over 39.5 (45¢)", "NoVig now: Over 40 (+105)"]
    c["sport"] = "cfb"
    assert A._price_context(c, _edge())[0] == "Week open: Under 41.5 (−105) · BetOnline"


def test_alert_prices_do_not_invent_missing_side_or_opening_juice():
    c = card()
    c["odds"] = {"kalshi": {"total": {"line": 40.5, "over": -110}}}
    c["consensus"]["total_open"] = 43.5
    c["weekly_total_open"].update(line=43.5, under=None)
    assert A._price_context(c, _edge()) == ["Week open: Under 43.5 (?) · BetOnline",
                                            "Kalshi now: unavailable", "NoVig now: unavailable"]
    c["odds"] = {"novig": {"spread": {"home_line": -3.5, "away_odds": 105,
                                        "open_line": -2.5, "open_odds": -120}}}
    rows = A._price_context(c, _edge(book="novig", market="spread", side="away"))
    assert rows == ["Week open: Sea +2.5 (?) · Novig", "Kalshi now: unavailable",
                    "NoVig now: Sea +3.5 (+105)"]


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_weekly_opener_survives_signal_summary(sport):
    c = card(sport=sport)
    c["odds"]["betonline"]["total"].update(line=41.5, open_line=41.5, open_under=-105)
    c["weekly_total_open"].update(line=41.5, under=-105)
    expected = "Week open: Under 41.5 (−105) · BetOnline"
    signal = A.edge_candidates(c, {}, CFG, now=NOW)[0]
    for text in (signal.text, signal.summary):
        assert text.count(expected) == 1


def test_weekly_opener_always_uses_betonline_regardless_of_best_book_or_consensus():
    c = card()
    c["consensus"].update(ref_book="betonline", total_open=41.5)
    c["odds"]["betonline"]["total"].update(open_line=41.5, open_under=-105)
    c["weekly_total_open"].update(line=41.5, under=-105)
    edge = _edge(book="novig")
    assert A._price_context(c, edge)[0] == "Week open: Under 41.5 (−105) · BetOnline"
    c["sport"] = "cfb"
    c["consensus"]["ref_book"] = "fanduel"
    c["odds"]["fanduel"] = {"total": {"open_line": 44.5, "open_under": -115}}
    assert A._price_context(c, edge)[0] == "Week open: Under 41.5 (−105) · BetOnline"


def test_signal_does_not_invent_missing_opening_price():
    c = card()
    c["weekly_total_open"] = None
    c["odds"] = {}
    text = A.edge_candidates(c, {}, CFG, now=NOW)[0].text
    assert "Week open: unavailable" in text
