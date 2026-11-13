"""Live signal eligibility, stable identity, delivery receipts and output wiring."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.model import fair, signals
from pipeline.outputs import d1_out, json_out
from pipeline.run_context import RunContext

KICK = datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)      # Sun 1:00p ET
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)        # Fri 08:00 ET routine slot
QUIET = datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc)      # Fri 23:30 ET
MORNING = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)   # Sat 08:00 ET routine slot
GID = "nfl:2026:3:sea@ne"
EKEY = f"edge|2026|3|{GID}|total|under|best|v1"
LEGACY_EKEY = f"edge|2026|3|{GID}|total|under|betonline|v1"
CFG = A.Config(board_url="https://board.test", chat_default="C0", chat_by_sport={"nfl": "CNFL"})


@pytest.fixture(autouse=True)
def _enable_mock_delivery(monkeypatch):
    # Exercise rollback with fake senders; independent HTTP guards stay active.
    monkeypatch.setenv("FOOTBALL_TELEGRAM_ENABLED", "1")


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
@pytest.mark.parametrize("roof_type,state", [("dome", "outdoors"), ("dome", None),
                                            ("retractable", None), ("retractable", "closed"),
                                            ("retractable", "outdoors"), (None, "closed")])
def test_stale_covered_signals_cannot_alert(sport, roof_type, state):
    c = card(sport=sport)
    c["stadium"].update(roof_type=roof_type, roof_state=state)
    assert not A.edge_candidates(c, {}, CFG, now=NOW)


def _edge(book: str = "betonline", market: str = "total", side: str = "under", line: float = 38.0, odds: int = -110,
          fair_line: float = 34.6, edge_pts: float = 3.4, edge_prob: float = 0.041, tier: str = "edge",
          conf: float = 0.72, vigfree: float = 0.5) -> dict[str, Any]:
    return {"game_id": GID, "book": book, "market": market, "side": side, "line": line, "odds": odds,
            "fair_line": fair_line, "fair_prob": vigfree + edge_prob, "vigfree_prob": vigfree, "edge_pts": edge_pts,
            "edge_prob": edge_prob, "confidence": conf, "tier": tier, "model_version": "v1", "ref_book": "pinnacle",
            "n_books": 6}


def card(edges: list[dict[str, Any]] | None = None, *, weather_driven: bool = True, thin: bool = False,
         wind: float = 18.0, rain: float = 0.8, gs: float = -6.5, game_id: str = GID, sport: str = "nfl",
         kickoff: datetime = KICK, stadium: bool = True, signal: str | None = "Mid Impact",
         flags: list[str] | None = None) -> dict[str, Any]:
    edges = [_edge()] if edges is None else edges
    return {
        "game_id": game_id, "sport": sport, "season": 2026, "week": 3, "kickoff_utc": kickoff.isoformat(),
        "kickoff_local": kickoff.isoformat(), "tz": "America/New_York", "date_label": "SUN 09/20", "time_label": "01:00 PM",
        "neutral": False, "status": "scheduled",
        "signal": {"label": signal, "level": signal, "color": "orange", "size": 12, "flags": list(flags or [])},
        "home": {"team_id": "ne", "name": "New England Patriots", "short": "NE"},
        "away": {"team_id": "sea", "name": "Seattle Seahawks", "short": "SEA"},
        "stadium": {"name": "Gillette Stadium", "roof_state": "outdoors"} if stadium else None,
        "weather": {"temp_fg": 41.2, "wind_fg": wind, "gust_fg": 26.0, "wind_dir_fg": "SE", "rain_fg": rain,
                    "precip_prob": 0.2, "wind_vol_fc": 6.0, "cross_mph": 15.0},
        "impact": {"v1": {"gs_fg_pct": gs, "away_fg_pct": 0.0, "components": {"wind": 6.5, "rain": 0.0, "cold": 0.0}}},
        "odds": {"betonline": {"total": {"line": 38.0, "over": -110, "under": -110, "open_line": 38.0}}},
        "weekly_total_open": {"book": "betonline", "line": 38.0, "under": None, "over": None},
        "consensus": {"total_now": 37.5, "spread_now": -3.0,
                      "spread_open": -3.0 if sport == "cfb" else None,
                      "ref_book": "pinnacle", "n_books": 6, "thin": thin},
        "fair": {"fair_total": 34.6, "fair_spread": -2.9, "confidence": 0.72, "weather_driven": weather_driven,
                 "edges": edges},
        "alerts": [], "run_id": "r1",
    }


def _ctx(**kw: Any) -> RunContext:
    return RunContext(sport="all", scope="light", run_id="r1", git_sha="abc", started_at=NOW, **kw)


def _fresh() -> tuple[dict, dict]:
    return pstate.migrate(None, "alerts"), pstate.migrate(None, "telegram_state")


class Recorder:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str | None]] = []

    def __call__(self, text: str, chat: str | None) -> bool:
        self.sent.append((text, chat))
        return self.ok


def _live(cands: list[A.Candidate], alerts: dict, tg: dict, now: datetime = NOW, ok: bool = True,
          cfg: A.Config = CFG) -> tuple[A.Outcome, Recorder, A.Plan]:
    rec = Recorder(ok)
    p = A.plan(cands, alerts, tg, now, cfg)
    return A.dispatch(p, alerts, rec, now, cfg), rec, p


# ---- thresholds per sport / market (model.fair.tier: board tiers only, never an alert gate) ----

def test_edge_thresholds_per_sport_market():
    for sport, market, edge_t, strong_t in (("nfl", "total", 1.5, 2.5), ("nfl", "spread", 1.0, 1.5),
                                            ("cfb", "total", 2.5, 4.0), ("cfb", "spread", 1.5, 2.5)):
        assert fair.tier(sport, market, edge_t - 0.1, 0.05, 0.8, 24) in ("watch", "none")
        assert fair.tier(sport, market, edge_t, 0.05, 0.8, 24) == "edge"
        assert fair.tier(sport, market, strong_t, 0.05, 0.8, 24) == "strong"


def test_confidence_and_lead_bypass():
    assert fair.tier("nfl", "total", 2.0, 0.05, 0.3, 100) == "watch"     # low conf, far out
    assert fair.tier("nfl", "total", 2.0, 0.05, 0.3, 30) == "edge"       # lead ≤ 36 h bypasses conf
    assert fair.tier("nfl", "total", 3.0, 0.05, 0.3, 30) == "edge"       # strong still needs conf ≥ 0.5
    assert fair.tier("nfl", "total", 2.0, 0.01, 0.9, 30) == "watch"      # edge_prob < 0.03
    assert fair.tier("nfl", "ml", 5.0, 0.2, 0.9, 30) == "none"


# ---- Weather signals: Low+ regardless of posted price or model value ----

def test_notification_policy_defaults_and_env_overrides():
    assert CFG.min_tier == "low"
    assert CFG.max_lead_days == 7
    cfg = A.Config.from_env({"TELEGRAM_MIN_TIER": "high", "TELEGRAM_MIN_EDGE_PTS": "2.5",
                             "TELEGRAM_MAX_LEAD_DAYS": "9",
                             "BOOK_HEALTH_ALERTS": "1"})
    assert cfg.min_tier == "high" and cfg.book_health and cfg.max_lead_days == 9


def test_default_notification_gate_is_weather_only():
    # Board-only metadata and fair.tier do not veto an otherwise actionable play.
    assert fair.tier("nfl", "total", 5.0, 0.1, 0.9, 10, is_weather_driven=False) == "watch"
    alerts, _ = _fresh()
    for c in (card(weather_driven=False), card(thin=True), card([_edge(tier="watch")]),
              card([_edge(tier="none")])):
        got = A.edge_candidates(c, alerts, CFG)
        assert [x.key for x in got] == [EKEY]

    # Price/model fields cannot veto a qualifying weather signal.
    for c in (card(signal="Low Impact"), card([_edge(edge_pts=0.99)]),
              card([_edge(side="over")]), card([_edge(line=None)]), card([_edge(odds=None)])):
        assert len(A.edge_candidates(c, alerts, CFG)) == 1

    c = A.edge_candidates(card(), alerts, CFG)[0]
    assert c.tier == "mid" and c.sport == "nfl" and c.kickoff_utc == KICK
    assert c.record["tier"] == "mid" and c.record["last_signal"] == "Mid Impact"
    assert c.record["book"] == "betonline" and c.record["last_book"] == "betonline"


def test_betting_notifications_stop_at_kickoff():
    alerts, _ = _fresh()
    assert A.edge_candidates(card(), alerts, CFG, None, KICK - timedelta(seconds=1))
    assert A.edge_candidates(card(), alerts, CFG, None, KICK) == []
    assert A.edge_candidates(card(), alerts, CFG, None, KICK + timedelta(hours=2)) == []



def test_collect_candidates_applies_the_kickoff_gate():
    alerts, _ = _fresh()
    cards = {"nfl": [card()]}
    before = A.collect_candidates(_ctx(), cards, alerts, CFG, KICK - timedelta(seconds=1))
    assert "edge" in {c.family for c in before}
    started = A.collect_candidates(_ctx(), cards, alerts, CFG, KICK)
    assert [c.family for c in started] == []


def test_betting_alert_horizon_rejects_far_future_invalid_and_missing_kickoffs():
    alerts, _ = _fresh()
    boundary = card(kickoff=NOW + timedelta(days=7))
    far = card(kickoff=NOW + timedelta(days=8))
    missing = card()
    missing["kickoff_utc"] = "bad timestamp"
    assert len(A.edge_candidates(boundary, alerts, CFG, now=NOW)) == 1
    assert A.edge_candidates(far, alerts, CFG, now=NOW) == []
    assert A.edge_candidates(missing, alerts, CFG, now=NOW) == []
    assert A.collect_candidates(_ctx(), {"nfl": [far, missing]}, alerts, CFG, NOW) == []


def test_no_impact_never_alerts():
    alerts, _ = _fresh()
    assert A.edge_candidates(card(signal="No Impact"), alerts, CFG) == []
    assert A.edge_candidates(card(signal=None), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(edge_pts=9.0, tier="strong")], signal="No Impact"), alerts, CFG) == []
    assert A._alertable_edges(card(signal="")) == []


def test_cfb_wind_filter_overrides_stale_no_impact_for_alerts():
    alerts, _ = _fresh()
    duke = card(sport="cfb", signal="No Impact", wind=23.66)
    duke["weather"]["temp_fg"] = 64.0
    duke["consensus"]["spread_open"] = 7.17
    duke["signal"]["drivers"] = ["rain"]  # stale driver metadata cannot veto the process screen.
    # Process screen: sustained wind >= 15 mph. Temperature does not block unless the
    # optional gate is on. CFB opener within ±10.
    assert len(A.edge_candidates(duke, alerts, CFG, now=NOW)) == 1
    assert A.edge_candidates({**duke, "weather": {**duke["weather"], "wind_fg": 14.9}}, alerts, CFG, now=NOW) == []
    assert len(A.edge_candidates({**duke, "weather": {**duke["weather"], "temp_fg": 75.0}}, alerts, CFG, now=NOW)) == 1
    assert A.edge_candidates({**duke, "consensus": {**duke["consensus"], "spread_open": 10.01}}, alerts, CFG, now=NOW) == []


@pytest.mark.parametrize("sport,label", [("nfl", "Low Impact"), ("cfb", "Low (Wind)")])
@pytest.mark.parametrize("temp", [64.9, 65.0, 75.0, None, float("nan")])
def test_wind_flag_does_not_use_temperature_unless_the_gate_is_on(sport, label, temp):
    alerts, _ = _fresh()
    c = card(sport=sport, signal=label, wind=15.0, rain=0)
    c["signal"]["drivers"] = ["wind"]
    c["weather"]["temp_fg"] = temp
    if sport == "cfb":
        c["consensus"]["spread_open"] = -3.0
    assert len(A.edge_candidates(c, alerts, CFG, now=NOW)) == 1


def test_warm_game_below_the_flag_does_not_alert():
    c = card(signal="No Impact", wind=14.0, rain=0.0)
    c["weather"]["temp_fg"] = 72
    assert A.edge_candidates(c, {}, CFG, now=NOW) == []


@pytest.mark.parametrize("label", ["Mid Impact", "High Impact", "Very High Impact", "Low (Wind)", "Low (Temp)"])
def test_warm_game_at_the_wind_flag_still_alerts(label):
    c = card(signal=label, wind=18, rain=0)
    c["weather"]["temp_fg"] = 75
    c["signal"]["drivers"] = ["wind"]
    alerts, _ = _fresh()
    assert len(A.edge_candidates(c, alerts, CFG, now=NOW)) == 1


def test_heat_label_without_a_wind_flag_does_not_alert():
    c = card(signal="Low (Temp)", wind=3, rain=0)
    c["weather"]["temp_fg"] = 81
    alerts, _ = _fresh()
    assert A.edge_candidates(c, alerts, CFG, now=NOW) == []


def test_optional_temp_gate_blocks_a_warm_alert(monkeypatch):
    monkeypatch.setenv("PROCESS_TEMP_GATE", "1")
    c = card(signal="Low (Wind)", wind=18, rain=0)
    c["weather"]["temp_fg"] = 75
    alerts, _ = _fresh()
    assert A.edge_candidates(c, alerts, CFG, now=NOW) == []
    c["weather"]["temp_fg"] = 50
    assert len(A.edge_candidates(c, alerts, CFG, now=NOW)) == 1


def test_weather_signal_alerts_without_a_price_or_positive_model_edge():
    alerts, _ = _fresh()
    for advantage in (0.5, 0.0, -2.0, None):
        c = card([_edge(edge_pts=advantage)], sport="cfb", signal="Low (Wind)")
        assert len(A.edge_candidates(c, alerts, CFG, now=NOW)) == 1
    c["odds"] = {}
    c["fair"] = {}
    c["consensus"] = {"spread_open": -3.0}
    got = A.edge_candidates(c, alerts, CFG, now=NOW)
    assert len(got) == 1
    assert "no posted price available" in got[0].text


def test_first_signal_quote_survives_waiting_for_routine_delivery():
    alerts, tg = _fresh()
    first = A.edge_candidates(card([_edge(line=38)], signal="Low (Wind)"), alerts, CFG, now=QUIET)
    _live(first, alerts, tg, now=QUIET)
    current = A.edge_candidates(card([_edge(line=36)], signal="Low (Wind)"), alerts, CFG, now=MORNING)
    _live(current, alerts, tg, now=MORNING)
    rec = pstate.get_alert_record(alerts, EKEY)
    assert rec["first_signal_at"] == A.utc_iso(QUIET)
    assert rec["first_signal_line"] == 38
    assert rec["first_signal_book"] == "betonline"
    assert rec["first_line"] == 36
    assert rec["first_sent_at"] == A.utc_iso(MORNING)


def test_cfb_wide_opener_never_becomes_a_telegram_play():
    sig = signals.cfb_signal(6.7, 78.3, 0.0, -18.5, 955.4, 52.4, 47.2, weekday=1)
    assert sig.level == signals.NO

    alerts, _ = _fresh()
    maine = card(
        sport="cfb",
        game_id="cfb:2026:1:maine@appalachian-state",
        signal=sig.level,
    )
    maine["consensus"]["spread_open"] = -18.5
    maine["signal"]["drivers"] = list(sig.drivers)
    assert A.edge_candidates(maine, alerts, CFG) == []


def test_signal_slugs():
    assert A.signal_slug("Very High Impact") == "very_high" and A.signal_slug("High Impact") == "high"
    assert A.signal_slug("Mid Impact") == "mid" and A.signal_slug("Low Impact") == "low"
    assert A.signal_slug("Low (Rain)") == "low" and A.signal_slug("Low (Wind)") == "low" and A.signal_slug("Low (Temp)") == "low"
    assert A.signal_slug("No Impact") is None and A.signal_slug(None) is None and A.signal_slug("") is None


def test_play_is_the_largest_under_edge_any_book():
    alerts, _ = _fresh()
    c = A.edge_candidates(card([_edge(), _edge(book="betcris", line=41.0, edge_pts=9.0),
                                _edge(book="novig", line=38.5, edge_pts=3.9, tier="watch"),
                                _edge(book="fanduel", side="over", edge_pts=9.0),
                                _edge(book="kalshi", market="spread", side="home", line=-3.0, edge_pts=5.0, tier="strong")]),
                          alerts, CFG)
    assert [x.key for x in c] == [EKEY]
    assert c[0].record["book"] == "novig" and c[0].record["last_book"] == "novig"
    assert "<b>Under 38.5 (−110) · Novig</b>" in c[0].text
    assert "Betcris" not in c[0].text


def test_best_book_churn_keeps_one_stable_play_identity():
    alerts, tg = _fresh()
    first = A.edge_candidates(card([_edge(book="betonline", line=38.0, edge_pts=3.4)]), alerts, CFG)
    assert [c.key for c in first] == [EKEY]
    out, _, _ = _live(first, alerts, tg)
    assert out.n_sent == 1

    # Kalshi becomes the best recommendation, but a book change is not a new PLAY
    # and a sub-threshold line change is not an UPDATE. A larger BetCris edge is
    # not a recommendation.
    churned = card([_edge(book="betonline", line=38.0, edge_pts=3.4),
                    _edge(book="betcris", line=41.0, edge_pts=9.0),
                    _edge(book="novig", line=38.5, edge_pts=3.9)])
    assert A.edge_candidates(churned, alerts, CFG) == []
    assert [r["alert_key"] for r in pstate.open_edge_records(alerts, GID)] == [EKEY]



def test_consensus_entry_synthesised_when_no_under_edge():
    alerts, _ = _fresh()
    e = A.consensus_entry(card([], sport="cfb"))
    assert e["market"] == "total" and e["side"] == "under" and e["edge_prob"] is None and e["model_version"] == "v1"
    assert e["book"] == "consensus" and e["line"] == 37.5 and e["fair_line"] == 34.6 and e["edge_pts"] == 2.9
    assert A.edge_candidates(card([_edge(side="over", edge_pts=-3.4)]), alerts, CFG)[0].record["last_line"] is None


def test_no_line_posted_still_reports_weather():
    alerts, _ = _fresh()
    c = card([])
    c["consensus"]["total_now"] = None
    c["odds"] = {}
    assert "no posted price available" in A.edge_candidates(c, alerts, CFG)[0].text


def test_edge_key_roundtrip_unchanged():
    for book in ("betonline", "consensus", "best"):
        key = A.edge_key(2026, 3, GID, "total", "under", book, "v1")
        assert key == f"edge|2026|3|{GID}|total|under|{book}|v1"
        assert A.parse_edge_key(key) == {"season": "2026", "week": "3", "game_id": GID, "market": "total", "side": "under",
                                         "book": book, "model_version": "v1"}
    assert A.parse_edge_key("edge|2026|3|x") is None and A.parse_edge_key("wx|edge|2026|3|g|total|under|b|v1|sig-mid") is None


def test_edge_dedup_accepts_stable_and_legacy_book_markers():
    for marker in (EKEY, LEGACY_EKEY):
        alerts, _ = _fresh()
        pstate.mark_alert(alerts, marker, "t")
        assert A.edge_candidates(card([_edge(book="betcris")]), alerts, CFG) == []


def test_model_promotion_does_not_create_a_second_telegram_play():
    alerts, tg = _fresh()
    _live(A.edge_candidates(card(), alerts, CFG), alerts, tg)
    promoted = card([dict(_edge(book="betcris"), model_version="v2")])
    assert A.edge_candidates(promoted, alerts, CFG) == []


def test_mark_only_after_successful_send():
    alerts, tg = _fresh()
    c = A.edge_candidates(card(), alerts, CFG)
    out, rec, _ = _live(c, alerts, tg, ok=False)
    assert out.n_sent == 0 and out.failed and alerts["sent"] == {} and alerts.get("records", {}) == {}
    out2, rec2, _ = _live(c, alerts, tg, ok=True)
    assert out2.n_sent == 1 and rec2.sent[0][1] == "C0"
    r = pstate.get_alert_record(alerts, c[0].key)
    assert r["first_line"] == 38.0 and r["first_edge"] == 3.4 and r["sends"] == 1 and r["status"] == "open"
    assert alerts["feed"][-1]["alert_key"] == c[0].key and alerts["feed"][-1]["text_html"] == c[0].text
    # re-run: dedup, nothing sent
    out3, rec3, p3 = _live(A.edge_candidates(card(), alerts, CFG) + c, alerts, tg)
    assert out3.n_sent == 0 and rec3.sent == [] and not p3.send and not p3.digest


def test_sender_exception_is_a_failed_send():
    alerts, tg = _fresh()

    def boom(text: str, chat: str | None) -> bool:
        raise RuntimeError("telegram down")
    c = A.edge_candidates(card(), alerts, CFG)
    p = A.plan(c, alerts, tg, NOW, CFG)
    out = A.dispatch(p, alerts, boom, NOW, CFG)
    assert out.n_sent == 0 and out.failed and alerts["sent"] == {}


def test_chat_routing_per_sport_and_shared_routine_default():
    cfg = A.Config.from_env({"TELEGRAM_CHAT_ID": "D", "TELEGRAM_CHAT_ID_CFB": "CF", "BOARD_URL": "https://x/"})
    assert cfg.chat_for("cfb") == "CF" and cfg.chat_for("nfl") == "D" and cfg.chat_for(None) == "D"
    assert cfg.board_url == "https://x"
    alerts, tg = _fresh()
    cfb = A.edge_candidates(card(sport="cfb", game_id="cfb:2026:3:a@b"), alerts, cfg)[0]
    out, rec, _ = _live([cfb], alerts, tg, cfg=cfg)
    assert out.n_sent == 1 and rec.sent[0][1] == "D"  # Routine notices share the default chat.
    immediate = A.Plan(send=[cfb])
    routed = Recorder()
    A.dispatch(immediate, {}, routed, NOW, cfg)
    assert routed.sent[0][1] == "CF"



# ---- run_alerts: dry-run prints, live persists, rehydrate ----------------------------

def test_run_alerts_dry_run_prints_and_writes_nothing(tmp_path: Path, capsys):
    run = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, enabled=True, dry_run=True, cfg=CFG, now=NOW,
                       sender=Recorder())
    out = capsys.readouterr().out
    assert EKEY in out and "dry-run" in out
    assert run.n_alerts == 0 and not (tmp_path / "alerts.json").exists()
    run2 = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, enabled=False, dry_run=False, cfg=CFG, now=NOW,
                        sender=Recorder())
    assert "disabled" in capsys.readouterr().out and run2.candidates and not (tmp_path / "alerts.json").exists()


def test_run_alerts_live_persists_and_unchanged_rerun_is_silent(tmp_path: Path):
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW, sender=rec)
    assert run.n_alerts == 1 and len(rec.sent) == 1      # New signal only
    assert run.keys_for(GID) == [EKEY]
    saved = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert saved['schema_version'] == pstate.KIND_VERSIONS['alerts'] and len(saved['sent']) == 1 and len(saved['feed']) == 1
    assert (tmp_path / "telegram_state.json").exists()
    rec2 = Recorder()
    run2 = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW + timedelta(hours=1), sender=rec2)
    assert run2.n_alerts == 0 and len(rec2.sent) == 0 and run2.source == "r2"


def test_empty_light_and_playwright_runs_do_not_notify(tmp_path):
    sender = Recorder()
    for minutes in (0, 9):
        run = A.run_alerts(_ctx(), {"nfl": [], "cfb": []}, tmp_path, cfg=CFG,
                           now=NOW + timedelta(minutes=minutes), sender=sender)
        assert run.outcome.n_messages == 0
    assert sender.sent == []


def test_legacy_queue_loads_but_never_replays(tmp_path):
    legacy = {"schema_version": 1, "queue": [{"key": "legacy", "text": "stale price"}], "extra": "retained"}
    pstate.save_telegram_state(tmp_path, legacy)
    loaded = pstate.load_telegram_state(tmp_path)
    assert loaded["queue"] == legacy["queue"] and loaded["extra"] == "retained"
    sender = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": []}, tmp_path, cfg=CFG, now=NOW, sender=sender)
    assert sender.sent == [] and run.alerts["sent"] == {} and not run.alerts.get("feed")
    saved = pstate.load_telegram_state(tmp_path)
    assert saved["queue"] == [] and saved["extra"] == "retained"


def test_first_observation_persists_across_failed_send_and_restart(tmp_path: Path):
    first = card([_edge(line=49, edge_pts=.5)], sport="cfb", signal="Low (Wind)")
    run = A.run_alerts(_ctx(), {"cfb": [first]}, tmp_path, cfg=CFG, now=NOW, sender=Recorder(False))
    assert run.n_alerts == 0 and not pstate.alert_sent(run.alerts, EKEY)
    later = card([_edge(line=47, edge_pts=-1.5)], sport="cfb", signal="Low (Wind)")
    delivered = A.run_alerts(_ctx(), {"cfb": [later]}, tmp_path, cfg=CFG,
                             now=NOW + timedelta(hours=9), sender=Recorder())
    rec = pstate.get_alert_record(delivered.alerts, EKEY)
    assert delivered.n_alerts == 1
    assert (rec["first_signal_at"], rec["first_signal_line"], rec["first_line"]) == (A.utc_iso(NOW), 49, 47)
    assert rec["first_sent_at"] == A.utc_iso(NOW + timedelta(hours=9))


def test_initially_unpriced_signal_never_gains_a_retrospective_betting_entry():
    alerts, tg = _fresh()
    unpriced = card([])
    _live(A.edge_candidates(unpriced, alerts, CFG, now=NOW), alerts, tg)
    # The next routine reminder can display a price without rewriting the entry.
    later = NOW + timedelta(hours=9)
    reminders = A.collect_candidates(_ctx(), {"nfl": [card()]}, alerts, CFG, later)
    assert len(reminders) == 1 and reminders[0].family == "reminder"
    _live(reminders, alerts, tg, now=later)
    rec = pstate.get_alert_record(alerts, EKEY)
    assert rec["first_line"] is None and rec["first_odds"] is None and rec["first_signal_line"] is None
    assert rec["last_line"] is None and rec["status"] == "open"


def test_rehydrate_from_d1_export_when_alerts_json_missing(tmp_path: Path):
    key = LEGACY_EKEY
    rows = [{"alert_key": key, "family": "edge", "game_id": GID, "sport": "nfl", "season": 2026, "week": 3,
             "market": "total", "side": "under", "book": "betonline", "tier": "mid", "model_version": "v1",
             "first_sent_at": "2026-09-17T10:00:00Z", "last_sent_at": "2026-09-17T10:00:00Z", "sends": 1,
             "first_line": 38.0, "first_edge": 3.4, "last_line": 38.0, "last_odds": -110, "last_fair": 34.6,
             "last_edge": 3.4, "status": "open"}]
    (tmp_path / "alerts_export.json").write_text(json.dumps([{"results": rows}]), encoding="utf-8")
    alerts, source = pstate.load_alerts_rehydrated(tmp_path)
    assert source == "export" and pstate.alert_sent(alerts, key)
    assert pstate.open_edge_records(alerts, GID)[0]["last_line"] == 38.0
    # /api/alerts shape via fetch_rows, only when the export is absent
    (tmp_path / "alerts_export.json").unlink()
    alerts2, source2 = pstate.load_alerts_rehydrated(tmp_path, lambda: {"ok": True, "rows": rows})
    assert source2 == "api" and pstate.alert_sent(alerts2, key)
    alerts3, source3 = pstate.load_alerts_rehydrated(tmp_path, lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert source3 == "fresh" and alerts3["sent"] == {}
    # Legacy book-keyed markers still suppress a duplicate first notice.
    # At a routine slot the single policy emits a reminder instead.
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card([_edge(line=39.5, edge_pts=4.9)])]}, tmp_path, cfg=CFG, now=NOW,
                       sender=rec, fetch_rows=lambda: rows)
    assert run.source == "api" and [c.family for c in run.outcome.sent] == ["reminder"]


def test_alert_records_pruned_closed_first():
    alerts, _ = _fresh()
    for i in range(6):
        pstate.upsert_alert_record(alerts, f"k{i}", {"family": "edge", "status": "closed" if i < 2 else "open"}, f"t{i}")
    assert pstate.prune_alert_records(alerts, cap=3) == 3
    assert sorted(alerts["records"]) == ["k3", "k4", "k5"]


# ---- D1 mirror + board payloads ----------------------------------------------------

def test_d1_alert_rows_and_upsert_freeze_first_columns():
    alerts, tg = _fresh()
    c = A.edge_candidates(card(), alerts, CFG)
    out, _, _ = _live(c, alerts, tg)
    rows = d1_out.alert_rows(out.records + [{"bogus": 1}, dict(out.records[0], status="weird")])
    assert len(rows) == 1 and set(rows[0]) == set(d1_out.ALERT_COLS)
    assert rows[0]["status"] == "open" and "last_move_at" not in rows[0] and "kickoff_utc" not in rows[0]
    assert rows[0]["tier"] == "mid" and rows[0]["book"] == "betonline"                 # D1 tier = signal slug
    assert out.records[0]["last_signal"] == "Mid Impact" and out.records[0]["tier"] == "mid"   # alerts.json record
    sql = d1_out.alert_upsert_sql(rows)
    assert len(sql) == 1 and sql[0].startswith("INSERT INTO alerts (alert_key, family")
    assert "ON CONFLICT(alert_key) DO UPDATE SET" in sql[0]
    upd = sql[0].split("DO UPDATE SET", 1)[1]
    assert "first_line" not in upd and "first_sent_at" not in upd and "last_line=excluded.last_line" in upd
    assert "sends=excluded.sends" in upd and "status=excluded.status" in upd
    stmts = d1_out.build_statements(alerts=rows)
    assert len(stmts) == 1 and f"'{c[0].key}'" in stmts[0]


def test_alerts_feed_and_status_payloads(tmp_path: Path):
    alerts, tg = _fresh()
    c = A.edge_candidates(card(), alerts, CFG)
    _live(c, alerts, tg)
    alerts["records"][c[0].key]["clv_pts"] = 1.5
    ctx = _ctx()
    meta = json_out.build_meta(ctx, {"nfl": 1}, {"betonline": {"count": 10, "baseline": 10, "status": "green", "last_ok": "t"}},
                               season=2026, week=3, finished_at=NOW)
    feed = json_out.build_alerts_feed(alerts, meta)
    assert feed["meta"]["run_id"] == "r1" and feed["n_open"] == 1
    assert feed["alerts"][0]["alert_key"] == c[0].key and feed["alerts"][0]["clv_pts"] == 1.5
    assert feed["alerts"][0]["status"] == "open" and "<b>" in feed["alerts"][0]["text_html"]
    run = d1_out.run_row(ctx, season=2026, week=3, finished_at=NOW, n_games=1, n_lines=5, n_alerts=1)
    prev = {"runs": [{"run_id": "r0", "status": "ok"}] + [{"run_id": f"old{i}"} for i in range(25)]}
    status = json_out.build_status(meta, run, previous=prev)
    assert status["runs"][0]["run_id"] == "r1" and status["runs"][0]["n_alerts"] == 1
    assert status["runs"][1]["run_id"] == "r0" and len(status["runs"]) == json_out.STATUS_RUNS_CAP
    assert isinstance(status["runs"][0]["stage_timings"], dict) and status["books"]["betonline"]["status"] == "green"
    files = json_out.write_board(tmp_path, {"nfl": [card()]}, meta, alerts_feed=feed, status=status)
    keys = list(files)
    assert "board/alerts_feed.json" in keys and "board/status.json" in keys and keys[-1] == "board/meta.json"
    json.loads((tmp_path / "alerts_feed.json").read_text(encoding="utf-8"))


# ---- pipeline.build wiring -----------------------------------------------------------

def _sport_result(cards: list[dict[str, Any]], new_keys: list[str]) -> Any:
    from pipeline import build
    odds = build.OddsResult([], {}, pstate.migrate(None, "openers"), {}, [], {}, new_opener_keys=new_keys)
    return build.SportResult("nfl", [], [], cards, [], {}, {}, {}, {}, odds)


def test_build_alert_stage_stamps_cards_and_feeds_outputs(tmp_path: Path, monkeypatch, capsys):
    from pipeline import build
    monkeypatch.setattr(A, "default_sender", lambda: Recorder())
    monkeypatch.setattr(A, "Config", type("Cfg", (), {"from_env": staticmethod(lambda env=None: CFG)}))
    res = _sport_result([card()], [f"{GID}|total|over|betonline"])
    ctx = _ctx()
    run = build.run_alert_stage(ctx, [res], tmp_path, enabled=True, dry_run=False, now=NOW)
    assert run is not None and run.n_alerts == 1 and "alerts" in ctx.stage_timings
    assert res.cards[0]["alerts"] == [EKEY]
    board = tmp_path / "board"
    manifest = build.write_outputs(ctx, [res], ["betonline"], board_dir=board, snapshot_dir=tmp_path / "snap",
                                   state_dir=tmp_path, d1_sql=tmp_path / "d1.sql", raw_files={}, finished_at=NOW,
                                   alerts_run=run)
    keys = list(manifest)
    assert "board/alerts_feed.json" in keys and "board/status.json" in keys and keys[-1] == "board/meta.json"
    assert "board/alerts.json" in keys and "board/telegram_state.json" in keys      # state rides R2
    feed = json.loads((board / "alerts_feed.json").read_text(encoding="utf-8"))
    assert [a["alert_key"] for a in feed["alerts"]][-1].startswith("edge|2026|3|")
    status = json.loads((board / "status.json").read_text(encoding="utf-8"))
    assert status["runs"][0]["run_id"] == "r1" and status["runs"][0]["n_alerts"] == 1
    sql = (tmp_path / "d1.sql").read_text(encoding="utf-8")
    assert "INSERT INTO alerts (" in sql and "ON CONFLICT(alert_key) DO UPDATE" in sql and "n_alerts" in sql
    # dry-run path: prints candidates, stamps nothing, writes nothing
    res2 = _sport_result([card()], [])
    run2 = build.run_alert_stage(_ctx(), [res2], tmp_path / "dry", enabled=True, dry_run=True)
    assert run2 is not None and run2.n_alerts == 0 and res2.cards[0]["alerts"] == []
    assert "dry-run" in capsys.readouterr().out and not (tmp_path / "dry" / "alerts.json").exists()


def test_build_alert_stage_failure_is_a_warn_degradation(tmp_path: Path, monkeypatch):
    from pipeline import build

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("state corrupt")
    monkeypatch.setattr(A, "run_alerts", boom)
    ctx = _ctx()
    assert build.run_alert_stage(ctx, [_sport_result([card()], [])], tmp_path, enabled=True, dry_run=False) is None
    assert [d.component for d in ctx.degradations] == ["alerts"] and ctx.degradations[0].severity == "warn"
