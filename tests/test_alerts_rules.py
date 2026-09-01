"""pipeline/alerts.py rules: actionable PLAY gating, stable play identity, consolidated
follow-ups, quiet-hours revalidation, low-volume batching, persistence, and output wiring."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.model import fair
from pipeline.outputs import d1_out, json_out
from pipeline.run_context import RunContext

KICK = datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)      # Sun 1:00p ET
NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)        # Fri 11:00a ET (not quiet)
QUIET = datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc)      # Fri 23:30 ET
MORNING = datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc)   # Sat 07:30 ET
GID = "nfl:2026:3:sea@ne"
EKEY = f"edge|2026|3|{GID}|total|under|best|v1"
LEGACY_EKEY = f"edge|2026|3|{GID}|total|under|betonline|v1"
CFG = A.Config(board_url="https://board.test", chat_default="C0", chat_by_sport={"nfl": "CNFL"})


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
        "consensus": {"total_now": 37.5, "spread_now": -3.0, "ref_book": "pinnacle", "n_books": 6, "thin": thin},
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


# ---- PLAY gate: Mid+, real posted price, and at least one point by default ----

def test_notification_policy_defaults_and_env_overrides():
    assert CFG.max_per_run == 4 and CFG.min_tier == "mid" and CFG.min_edge_pts == 1.0
    assert not CFG.include_openers
    cfg = A.Config.from_env({"TELEGRAM_MIN_TIER": "high", "TELEGRAM_MIN_EDGE_PTS": "2.5",
                             "TELEGRAM_MAX_PER_RUN": "7", "TELEGRAM_INCLUDE_OPENERS": "true"})
    assert cfg.min_tier == "high" and cfg.min_edge_pts == 2.5 and cfg.max_per_run == 7 and cfg.include_openers


def test_default_play_gate_requires_actionable_mid_plus_real_book_price():
    # Board-only metadata and fair.tier do not veto an otherwise actionable play.
    assert fair.tier("nfl", "total", 5.0, 0.1, 0.9, 10, is_weather_driven=False) == "watch"
    alerts, _ = _fresh()
    for c in (card(weather_driven=False), card(thin=True), card([_edge(tier="watch")]),
              card([_edge(tier="none")])):
        got = A.edge_candidates(c, alerts, CFG)
        assert [x.key for x in got] == [EKEY]

    # Clarity policy: do not page on Low, sub-one-point value, consensus-only
    # estimates, or a recommendation without both a posted line and price.
    assert A.edge_candidates(card(signal="Low Impact"), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(edge_pts=0.99)]), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(side="over")]), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(line=None)]), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(odds=None)]), alerts, CFG) == []

    c = A.edge_candidates(card(), alerts, CFG)[0]
    assert c.tier == "mid" and c.sport == "nfl" and c.kickoff_utc == KICK
    assert c.record["tier"] == "mid" and c.record["last_signal"] == "Mid Impact"
    assert c.record["book"] == "betonline" and c.record["last_book"] == "betonline"


def test_no_edge_before_the_betting_week_opens():
    """Never bet into next week's line: no EDGE before Monday 00:00 ET of the game's own week.
    The board still carries the card (the weather window runs 10 days) — only the alert waits."""
    from utils.timeutil import bet_week_open

    alerts, _ = _fresh()
    monday = bet_week_open(KICK)                                  # Mon 2026-09-14 04:00Z
    assert A.edge_candidates(card(), alerts, CFG, None, monday - timedelta(seconds=1)) == []
    assert A.edge_candidates(card(), alerts, CFG, None, monday - timedelta(days=2)) == []   # prev Saturday
    assert len(A.edge_candidates(card(), alerts, CFG, None, monday)) == 1                   # opens on the dot
    assert len(A.edge_candidates(card(), alerts, CFG, None, NOW)) == 1                      # Fri of that week
    # a Monday-nighter gets a Monday-only window, not the previous Monday's
    mnf = datetime(2026, 9, 22, 0, 15, tzinfo=timezone.utc)
    assert bet_week_open(mnf) == datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    assert A.edge_candidates(card(kickoff=mnf), alerts, CFG, None, NOW) == []
    # no clock passed -> no gate (direct callers/tests); an unknown kickoff never alerts
    assert len(A.edge_candidates(card(), alerts, CFG)) == 1
    blind = card()
    blind["kickoff_utc"] = None
    assert A.edge_candidates(blind, alerts, CFG, None, NOW) == []


def test_betting_notifications_stop_at_kickoff():
    alerts, _ = _fresh()
    assert A.edge_candidates(card(), alerts, CFG, None, KICK - timedelta(seconds=1))
    assert A.edge_candidates(card(), alerts, CFG, None, KICK) == []
    assert A.edge_candidates(card(), alerts, CFG, None, KICK + timedelta(hours=2)) == []

    open_alerts = _with_open_edge()
    pulled = card([_edge(line=None, odds=None)], kickoff=KICK)
    assert A.followup_candidates(pulled, open_alerts, CFG, KICK) == []


def test_collect_candidates_applies_the_week_gate():
    alerts, _ = _fresh()
    cards = {"nfl": [card()]}
    early = A.collect_candidates(_ctx(), cards, alerts, CFG, KICK - timedelta(days=9), include_ops=False)
    assert [c.family for c in early] == []
    inweek = A.collect_candidates(_ctx(), cards, alerts, CFG, NOW, include_ops=False)
    assert "edge" in {c.family for c in inweek}


def test_no_impact_never_alerts():
    alerts, _ = _fresh()
    assert A.edge_candidates(card(signal="No Impact"), alerts, CFG) == []
    assert A.edge_candidates(card(signal=None), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(edge_pts=9.0, tier="strong")], signal="No Impact"), alerts, CFG) == []
    assert A._alertable_edges(card(signal="")) == []


def test_signal_slugs():
    assert A.signal_slug("Very High Impact") == "very_high" and A.signal_slug("High Impact") == "high"
    assert A.signal_slug("Mid Impact") == "mid" and A.signal_slug("Low Impact") == "low"
    assert A.signal_slug("Low (Rain)") == "low" and A.signal_slug("Low (Wind)") == "low" and A.signal_slug("Low (Temp)") == "low"
    assert A.signal_slug("No Impact") is None and A.signal_slug(None) is None and A.signal_slug("") is None


def test_play_is_the_largest_under_edge_any_book():
    alerts, _ = _fresh()
    c = A.edge_candidates(card([_edge(), _edge(book="betcris", line=38.5, edge_pts=3.9, tier="watch"),
                                _edge(book="fanduel", side="over", edge_pts=9.0),
                                _edge(book="novig", market="spread", side="home", line=-3.0, edge_pts=5.0, tier="strong")]),
                          alerts, CFG)
    assert [x.key for x in c] == [EKEY]
    assert c[0].record["book"] == "betcris" and c[0].record["last_book"] == "betcris"
    assert "<b>Under 38.5 (−110) · Betcris</b>" in c[0].text


def test_best_book_churn_keeps_one_stable_play_identity():
    alerts, tg = _fresh()
    first = A.edge_candidates(card([_edge(book="betonline", line=38.0, edge_pts=3.4)]), alerts, CFG)
    assert [c.key for c in first] == [EKEY]
    out, _, _ = _live(first, alerts, tg)
    assert out.n_sent == 1

    # Betcris becomes the best recommendation, but a book change is not a new PLAY
    # and a sub-threshold line change is not an UPDATE.
    churned = card([_edge(book="betonline", line=38.0, edge_pts=3.4),
                    _edge(book="betcris", line=38.5, edge_pts=3.9)])
    assert A.edge_candidates(churned, alerts, CFG) == []
    assert A.followup_candidates(churned, alerts, CFG, NOW + timedelta(hours=1)) == []
    assert [r["alert_key"] for r in pstate.open_edge_records(alerts, GID)] == [EKEY]

    # A material change in the best available offer is useful, but it must not
    # be described as though one book's market line moved.
    better = card([_edge(book="betonline", line=38.0, edge_pts=3.4),
                   _edge(book="betcris", line=40.0, edge_pts=5.4)])
    update = A.followup_candidates(better, alerts, CFG, NOW + timedelta(hours=5))
    assert len(update) == 1 and update[0].family == "move"
    assert "Best price: Under 38 (−110) · BetOnline → Under 40 (−110) · Betcris" in update[0].text
    assert "Line:" not in update[0].text
    assert "Best price:" in update[0].summary and "BetOnline → Under 40" in update[0].summary


def test_consensus_entry_synthesised_when_no_under_edge():
    alerts, _ = _fresh()
    e = A.consensus_entry(card([], sport="cfb"))
    assert e["market"] == "total" and e["side"] == "under" and e["edge_prob"] is None and e["model_version"] == "v1"
    assert e["book"] == "consensus" and e["line"] == 37.5 and e["fair_line"] == 34.6 and e["edge_pts"] == 2.9
    assert A.edge_candidates(card([_edge(side="over", edge_pts=-3.4)]), alerts, CFG) == []


def test_no_line_posted_does_not_page():
    alerts, _ = _fresh()
    c = card([])
    c["consensus"]["total_now"] = None
    c["odds"] = {}
    assert A.edge_candidates(c, alerts, CFG) == []


def test_bypass_quiet_for_high_tiers_and_gone():
    mk = lambda tier, fam="edge": A.Candidate("k", fam, "nfl", "<b>x</b>\ny", tier=tier)  # noqa: E731
    assert mk("high").bypass_quiet and mk("very_high").bypass_quiet
    assert not mk("mid").bypass_quiet and not mk("low").bypass_quiet and not mk(None).bypass_quiet
    assert mk("low", "gone").bypass_quiet
    alerts, _ = _fresh()
    assert A.edge_candidates(card(signal="Very High Impact"), alerts, CFG)[0].tier == "very_high"
    assert A.edge_candidates(card(signal="High Impact"), alerts, CFG)[0].bypass_quiet


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
    alerts = _with_open_edge()
    promoted = card([dict(_edge(book="betcris"), model_version="v2")])
    assert A.edge_candidates(promoted, alerts, CFG) == []

    v2_key = A.edge_key(2026, 3, GID, "total", "under", "best", "v2")
    duplicate = dict(pstate.get_alert_record(alerts, EKEY) or {})
    duplicate.update({"alert_key": v2_key, "model_version": "v2"})
    alerts["records"][v2_key] = duplicate
    alerts["sent"][v2_key] = "2026-09-18T14:05:00Z"
    assert len(A._canonical_open_edges(alerts, GID)) == 1


# ---- move buckets / gone / signal change / forecast move: only on games with an open EDGE ----

def _with_open_edge(line: float = 38.0, fair_line: float = 34.6, edge_pts: float = 3.4, wind: float = 18.0,
                    signal: str = "Mid Impact", edges: list[dict[str, Any]] | None = None) -> dict:
    alerts, tg = _fresh()
    edges = [_edge(line=line, fair_line=fair_line, edge_pts=edge_pts)] if edges is None else edges
    cands = A.edge_candidates(card(edges, wind=wind, signal=signal), alerts, CFG)
    out, _, _ = _live(cands, alerts, tg)
    assert out.n_sent == 1
    return alerts


def test_move_bucket_steps():
    assert A.move_bucket("total", 38.0, 38.5) == 0
    assert A.move_bucket("total", 39.49, 38.0) == 0
    assert A.move_bucket("total", 39.5, 38.0) == 1
    assert A.move_bucket("total", 41.0, 38.0) == 2
    assert A.move_bucket("spread", -3.99, -3.0) == 0
    assert A.move_bucket("spread", -4.0, -3.0) == 1
    assert A.move_bucket("spread", -5.0, -3.0) == 2
    assert A.move_direction("total", "under", 37.0, 38.0, 34.6) == "toward fair"
    assert A.move_direction("total", "under", 39.0, 38.0, 34.6) == "away from fair"


def test_move_only_for_games_with_open_edge():
    alerts, _ = _fresh()
    moved = card([_edge(line=39.5, edge_pts=4.9)])
    assert [c for c in A.followup_candidates(moved, alerts, CFG, NOW) if c.family == "move"] == []
    alerts = _with_open_edge()
    below_step = card([_edge(line=39.49, edge_pts=4.89)])
    assert A.followup_candidates(below_step, alerts, CFG, NOW) == []
    c = A.followup_candidates(moved, alerts, CFG, NOW)
    assert [x.key for x in c] == [f"move|{EKEY}|1"]
    assert "Line: Under 38 → 39.5 · BetOnline −110" in c[0].text
    assert c[0].record["direction"] == "away from fair"


def test_move_cooldown_and_rebasing_after_send():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    c = A.followup_candidates(card([_edge(line=39.5, edge_pts=4.9)]), alerts, CFG, NOW)
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    assert pstate.get_alert_record(alerts, EKEY)["last_line"] == 39.5
    second_bucket = card([_edge(line=41.0, edge_pts=6.4)])
    assert A.followup_candidates(second_bucket, alerts, CFG, NOW + timedelta(hours=3, minutes=59)) == []
    c2 = A.followup_candidates(second_bucket, alerts, CFG, NOW + timedelta(hours=4))
    assert [x.key for x in c2] == [f"move|{EKEY}|2"] and "39.5 → 41" in c2[0].text
    _live(c2, alerts, tg, now=NOW + timedelta(hours=4))
    # Drifting back to the first bucket is silent because that bucket was already sent.
    assert A.followup_candidates(card([_edge(line=39.5, edge_pts=4.9)]), alerts, CFG,
                                 NOW + timedelta(hours=8)) == []


def test_signal_gone_closes_record_and_suppresses_move():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    gone = card([_edge(line=35.0, edge_pts=0.4)], signal="No Impact", wind=6.0)
    c = A.followup_candidates(gone, alerts, CFG, NOW)
    assert [x.family for x in c] == ["gone"] and c[0].bypass_quiet
    assert c[0].key == f"gone|{EKEY}"
    assert "Reason: Signal Mid Impact → No Impact; below MID" in c[0].text
    assert "Was: Under 38 · Now: 35 (+0.4 pts vs fair)" in c[0].text
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    rec = pstate.get_alert_record(alerts, EKEY)
    assert rec["status"] == "closed" and rec["last_signal"] == "No Impact"
    assert pstate.open_edge_records(alerts, GID) == []
    assert A.followup_candidates(card([_edge(line=45.0, edge_pts=10.0)]), alerts, CFG, NOW) == []


def test_value_below_one_point_closes_an_actionable_play():
    alerts = _with_open_edge()
    c = A.followup_candidates(card([_edge(line=35.0, edge_pts=0.4)]), alerts, CFG, NOW)
    assert [x.family for x in c] == ["gone"]
    assert "Value fell to +0.4 pts (minimum +1)" in c[0].text


def test_signal_change_key_message_and_no_duplicate():
    alerts, tg = _fresh()
    alerts = _with_open_edge(signal="Mid Impact")
    assert pstate.get_alert_record(alerts, EKEY)["last_signal"] == "Mid Impact"
    assert A.followup_candidates(card(signal="Mid Impact"), alerts, CFG, NOW) == []
    c = A.followup_candidates(card(signal="High Impact", wind=17.0), alerts, CFG, NOW)
    assert [x.key for x in c] == [f"wx|{EKEY}|sig-high"] and c[0].family == "wx" and c[0].tier == "high"
    assert "Signal: <b>Mid Impact → High Impact</b>" in c[0].text
    assert "<b>Play: Under 38 (−110) · BetOnline</b>" in c[0].text and c[0].bypass_quiet
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    rec = pstate.get_alert_record(alerts, EKEY)
    assert rec["last_signal"] == "High Impact" and rec["status"] == "open" and rec["tier"] == "high"
    assert A.followup_candidates(card(signal="High Impact"), alerts, CFG, NOW) == []
    back = A.followup_candidates(card(signal="Mid Impact"), alerts, CFG, NOW)
    assert [x.key for x in back] == [f"wx|{EKEY}|sig-mid"]
    _live(back, alerts, tg)
    assert A.followup_candidates(card(signal="High Impact"), alerts, CFG, NOW) == []  # sig-high already sent
    # a record without last_signal (pre-change rows / D1 rehydrate) never guesses a change
    rec.pop("last_signal")
    assert A.followup_candidates(card(signal="High Impact"), alerts, CFG, NOW) == []


def test_simultaneous_signal_fair_and_line_change_produces_one_message():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    changed = card([_edge(line=39.5, fair_line=36.6, edge_pts=2.9)], signal="High Impact", wind=13.0, rain=0.0)
    c = A.followup_candidates(changed, alerts, CFG, NOW)
    assert len(c) == 1 and c[0].key == f"wx|{EKEY}|sig-high"
    assert "Signal: <b>Mid Impact → High Impact</b>" in c[0].text
    assert "Line:" not in c[0].text and "Forecast:" not in c[0].text
    out, rec, _ = _live(c, alerts, tg)
    assert out.n_sent == 1 and out.n_messages == 1 and len(rec.sent) == 1


def test_legacy_duplicate_parents_collapse_to_one_followup_and_close_together():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    legacy = dict(pstate.get_alert_record(alerts, EKEY) or {})
    legacy.update({"alert_key": LEGACY_EKEY, "book": "betonline", "last_book": "betonline"})
    alerts["records"][LEGACY_EKEY] = legacy
    alerts["sent"][LEGACY_EKEY] = "2026-09-18T14:00:00Z"

    c = A.followup_candidates(card(signal="No Impact"), alerts, CFG, NOW)
    assert len(c) == 1 and c[0].key == f"gone|{EKEY}"
    assert c[0].record["related_edge_keys"] == [LEGACY_EKEY]
    out, _, _ = _live(c, alerts, tg)
    assert out.n_messages == 1
    assert pstate.get_alert_record(alerts, EKEY)["status"] == "closed"
    assert pstate.get_alert_record(alerts, LEGACY_EKEY)["status"] == "closed"


def test_forecast_move_bucket_on_fair_line():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    same = card([_edge(fair_line=36.59, edge_pts=1.41)], wind=15.0)
    assert [x.family for x in A.followup_candidates(same, alerts, CFG, NOW)] == []
    moved = card([_edge(fair_line=36.6, edge_pts=1.4)], wind=13.0, rain=0.0)
    c = A.followup_candidates(moved, alerts, CFG, NOW)
    assert [x.key for x in c] == [f"wx|{EKEY}|1"]
    assert "Forecast: fair total 34.6 → 36.6" in c[0].text
    assert "18 → 13 mph" in c[0].text and "0.8 → 0 mm" in c[0].text
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    rec = pstate.get_alert_record(alerts, EKEY)
    assert rec["last_fair"] == 36.6 and rec["last_wind"] == 13.0
    assert A.followup_candidates(moved, alerts, CFG, NOW) == []


# ---- quiet hours -----------------------------------------------------------------

def test_quiet_hours_window():
    assert A.in_quiet_hours(QUIET)
    assert A.in_quiet_hours(datetime(2026, 9, 19, 10, 59, tzinfo=timezone.utc))   # 06:59 ET
    assert not A.in_quiet_hours(datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc))  # 07:00 ET
    assert not A.in_quiet_hours(NOW)


def test_quiet_hours_queue_bypass_and_flush(tmp_path: Path):
    alerts, tg = _fresh()
    edge = A.edge_candidates(card(), alerts, CFG)[0]  # Mid → queued
    strong = A.edge_candidates(card([_edge(book="betcris", edge_pts=4.0)], signal="Very High Impact",
                                    game_id="nfl:2026:3:a@b"), alerts, CFG)[0]
    soon_card = card([_edge(book="fanduel")], kickoff=QUIET + timedelta(hours=2), game_id="nfl:2026:3:c@d")
    soon = A.edge_candidates(soon_card, alerts, CFG)[0]
    assert strong.tier == "very_high" and strong.bypass_quiet and not edge.bypass_quiet
    out, rec, p = _live([edge, strong, soon], alerts, tg, now=QUIET)
    assert {c.key for c in p.send} == {strong.key, soon.key}
    assert [c.key for c in p.queued] == [edge.key]
    assert out.n_sent == 2 and not pstate.alert_sent(alerts, edge.key)
    assert [q["key"] for q in tg["queue"]] == [edge.key]
    # persisted + reloaded queue survives the R2 round-trip
    pstate.save_telegram_state(tmp_path, tg)
    tg2 = pstate.load_telegram_state(tmp_path)
    assert tg2["queue"][0]["key"] == edge.key and tg2["schema_version"] == pstate.SCHEMA_VERSION
    # re-queue during the night replaces (latest text), never duplicates
    out2, _, _ = _live([edge], alerts, tg2, now=QUIET + timedelta(hours=1))
    assert len(tg2["queue"]) == 1 and out2.n_sent == 0
    # 07:30 ET: the still-current key validates the snapshot; it is released as one digest.
    out3, rec3, p3 = _live([edge], alerts, tg2, now=MORNING)
    assert len(p3.flush) == 1 and out3.n_messages == 1 and out3.n_sent == 1
    assert rec3.sent[0][0].startswith("<b>MORNING SUMMARY (1)</b>") and rec3.sent[0][1] == "CNFL"
    assert pstate.alert_sent(alerts, edge.key) and tg2["queue"] == []


def test_quiet_queue_drops_snapshot_when_signal_is_no_longer_current():
    alerts, tg = _fresh()
    edge = A.edge_candidates(card(), alerts, CFG)[0]
    _live([edge], alerts, tg, now=QUIET)
    assert [q["key"] for q in tg["queue"]] == [EKEY]

    # The morning collection has no candidate for this key (for example the
    # signal fell below Mid overnight), so stale PLAY text must not be delivered.
    out, rec, p = _live([], alerts, tg, now=MORNING)
    assert p.flush == [] and out.n_sent == 0 and rec.sent == [] and tg["queue"] == []


def test_morning_summary_uses_current_price_not_overnight_snapshot():
    alerts, tg = _fresh()
    overnight = A.edge_candidates(card([_edge(line=38.0, edge_pts=3.4)]), alerts, CFG)[0]
    _live([overnight], alerts, tg, now=QUIET)

    current = A.edge_candidates(card([_edge(line=39.0, edge_pts=4.4)]), alerts, CFG)[0]
    out, rec, p = _live([current], alerts, tg, now=MORNING)
    assert len(p.flush) == 1 and out.n_messages == 1
    assert "Under 39" in rec.sent[0][0] and "Under 38" not in rec.sent[0][0]
    assert pstate.get_alert_record(alerts, EKEY)["first_line"] == 39.0


def test_flush_skips_keys_sent_meanwhile():
    alerts, tg = _fresh()
    edge = A.edge_candidates(card(), alerts, CFG)[0]
    _live([edge], alerts, tg, now=QUIET)
    pstate.mark_alert(alerts, edge.key, "t")   # e.g. sent from the playwright job
    out, rec, p = _live([], alerts, tg, now=MORNING)
    assert p.flush == [] and rec.sent == [] and tg["queue"] == []


# ---- low-volume run cap + summary -------------------------------------------------

def _many(n: int) -> list[A.Candidate]:
    alerts, _ = _fresh()
    out = []
    for i in range(n):
        gid = f"nfl:2026:3:t{i}@ne"
        out += A.edge_candidates(card([_edge(book="betonline")], game_id=gid), alerts, CFG)
    assert len(out) == n
    return out


def test_default_cap_is_three_individuals_then_one_summary():
    alerts, tg = _fresh()
    out, rec, p = _live(_many(40), alerts, tg)
    assert len(p.send) == 3 and len(p.digest) == 37
    assert out.n_messages == 4 and out.n_sent == 40
    assert rec.sent[-1][0].startswith("<b>SUMMARY (37)</b>")
    assert all(pstate.alert_sent(alerts, c.key) for c in p.send + p.digest)
    assert len(alerts["feed"]) == 40


def test_under_cap_sends_individually():
    alerts, tg = _fresh()
    out, _, p = _live(_many(3), alerts, tg)
    assert len(p.send) == 3 and p.digest == [] and out.n_messages == 3


def test_tight_cap_defers_fresh_play_when_morning_and_system_groups_use_budget():
    alerts, tg = _fresh()
    queued = A.edge_candidates(card(), alerts, CFG)[0]
    _live([queued], alerts, tg, now=QUIET)
    current = A.edge_candidates(card(), alerts, CFG)[0]
    fresh = A.edge_candidates(card(game_id="nfl:2026:3:new@game"), alerts, CFG)[0]
    ops = A.Candidate("ops|x", "ops", "", "⚠️ <b>DATA ISSUE</b>",
                      record={"family": "ops"}, summary="⚠️ DATA ISSUE")
    cfg = A.Config(board_url=CFG.board_url, chat_default=CFG.chat_default,
                   chat_by_sport=CFG.chat_by_sport, max_per_run=2)

    out, _, p = _live([current, fresh, ops], alerts, tg, now=MORNING, cfg=cfg)
    assert out.n_messages == 2 and len(p.flush) == 1 and len(p.ops) == 1
    assert p.send == [] and p.digest == []
    assert [q["key"] for q in tg["queue"]] == [fresh.key]
    assert not pstate.alert_sent(alerts, fresh.key)


def test_high_tier_and_imminent_prioritised_before_cap():
    cands = _many(30)
    late = A.edge_candidates(card([_edge(book="novig", edge_pts=5.0)], game_id="nfl:2026:3:zz@ne", signal="High Impact"),
                             _fresh()[0], CFG)[0]
    alerts, tg = _fresh()
    _, _, p = _live(cands + [late], alerts, tg)
    assert p.send[0].key == late.key


def test_digest_failure_marks_nothing():
    alerts, tg = _fresh()
    out, rec, p = _live(_many(30), alerts, tg, ok=False)
    assert out.n_sent == 0 and out.n_messages == 0 and alerts["sent"] == {}
    assert len(out.failed) == 30


# ---- mark only after send ------------------------------------------------------

def test_mark_only_after_successful_send():
    alerts, tg = _fresh()
    c = A.edge_candidates(card(), alerts, CFG)
    out, rec, _ = _live(c, alerts, tg, ok=False)
    assert out.n_sent == 0 and out.failed and alerts["sent"] == {} and alerts.get("records", {}) == {}
    out2, rec2, _ = _live(c, alerts, tg, ok=True)
    assert out2.n_sent == 1 and rec2.sent[0][1] == "CNFL"
    r = pstate.get_alert_record(alerts, c[0].key)
    assert r["first_line"] == 38.0 and r["first_edge"] == 3.4 and r["sends"] == 1 and r["status"] == "open"
    assert alerts["feed"][-1]["alert_key"] == c[0].key and alerts["feed"][-1]["text_html"] == c[0].text
    # re-run: dedup, nothing sent
    out3, rec3, p3 = _live(A.edge_candidates(card(), alerts, CFG) + c, alerts, tg)
    assert out3.n_sent == 0 and rec3.sent == [] and p3.skipped == [c[0].key]


def test_sender_exception_is_a_failed_send():
    alerts, tg = _fresh()

    def boom(text: str, chat: str | None) -> bool:
        raise RuntimeError("telegram down")
    c = A.edge_candidates(card(), alerts, CFG)
    p = A.plan(c, alerts, tg, NOW, CFG)
    out = A.dispatch(p, alerts, boom, NOW, CFG)
    assert out.n_sent == 0 and out.failed and alerts["sent"] == {}


def test_chat_routing_per_sport_and_ops_default():
    cfg = A.Config.from_env({"TELEGRAM_CHAT_ID": "D", "TELEGRAM_CHAT_ID_CFB": "CF", "BOARD_URL": "https://x/"})
    assert cfg.chat_for("cfb") == "CF" and cfg.chat_for("nfl") == "D" and cfg.chat_for(None) == "D"
    assert cfg.board_url == "https://x"
    alerts, tg = _fresh()
    cfb = A.edge_candidates(card(sport="cfb", game_id="cfb:2026:3:a@b"), alerts, cfg)[0]
    ops = A.Candidate("degr|x|y|2026-09-18", "ops", "cfb", "t")
    out, rec, _ = _live([cfb, ops], alerts, tg, cfg=cfg)
    chats = {c.key: chat for c, (_, chat) in zip(out.sent, rec.sent, strict=True)}
    assert chats[cfb.key] == "CF" and chats[ops.key] == "D"


# ---- openers digest / ops ------------------------------------------------------------

def test_openers_are_disabled_by_default_and_available_by_opt_in():
    alerts, _ = _fresh()
    calm = card(game_id="nfl:2026:3:a@b", wind=5.0, gs=-1.0)
    windy = card(game_id="nfl:2026:3:c@d", wind=12.0, gs=-1.0)
    cold = card(game_id="nfl:2026:3:e@f", wind=3.0, gs=-2.0)
    keys = ["nfl:2026:3:a@b|total|over|betcris", "nfl:2026:3:c@d|total|over|betcris", "nfl:2026:3:e@f|spread|home|fanduel",
            "nfl:2026:3:c@d|total|under|consensus"]
    assert A.opener_candidates("nfl", [calm, windy, cold], keys, alerts, CFG, NOW) == []
    enabled = A.Config(board_url=CFG.board_url, chat_default=CFG.chat_default,
                       chat_by_sport=CFG.chat_by_sport, include_openers=True)
    c = A.opener_candidates("nfl", [calm, windy, cold], keys, alerts, enabled, NOW)
    assert len(c) == 1 and c[0].key == "openers|nfl|2026|3|2026-09-18"
    assert "2 weather game" in c[0].text and "Betcris" in c[0].text and "Consensus" not in c[0].text
    assert A.opener_candidates("nfl", [calm], keys[:1], alerts, enabled, NOW) == []
    assert A.opener_candidates("nfl", [windy], [], alerts, enabled, NOW) == []
    pstate.mark_alert(alerts, c[0].key, "t")
    assert A.opener_candidates("nfl", [windy], keys, alerts, enabled, NOW) == []


def test_ops_candidates_keys():
    ctx = _ctx()
    ctx.degrade("weather", "open-meteo 503 for 3 games", "warn")
    ctx.degrade("odds.merge", "info only", "info")
    ctx.unresolved_names.extend(["betcris:Ohio St", "betcris:Ohio St", "nfl:2026:3:x@y:stadium"])
    alerts, _ = _fresh()
    cards = {"nfl": [card(stadium=False)]}
    c = A.ops_candidates(ctx, cards, alerts, NOW, heartbeat_ts=NOW - timedelta(hours=21), prev_meta_ts=NOW - timedelta(hours=1))
    keys = sorted(x.key for x in c)
    assert keys == sorted([
        "degr|weather|open-meteo-for-games|2026-09-18",   # counts stripped → stable across runs
        "heartbeat|cf-cron-heartbeat|2026-09-18",
        "names|betcris|2026-09-18",
        f"stadium|{GID}",
    ])
    assert all(x.family == "ops" for x in c)
    empty = {"nfl": [dict(card(), consensus={"total_now": None, "thin": True})]}
    c2 = A.ops_candidates(_ctx(), empty, alerts, NOW)
    assert "noref|nfl|2026-09-18" in {x.key for x in c2}


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


def test_run_alerts_live_persists_and_second_run_is_silent(tmp_path: Path):
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW, sender=rec,
                       new_keys_by_sport={"nfl": [f"{GID}|total|over|betonline"]})
    assert run.n_alerts == 1 and len(rec.sent) == 1      # PLAY only; openers are opt-in
    assert run.keys_for(GID) == [EKEY]
    saved = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1 and len(saved["sent"]) == 1 and len(saved["feed"]) == 1
    assert (tmp_path / "telegram_state.json").exists()
    rec2 = Recorder()
    run2 = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW + timedelta(hours=1), sender=rec2,
                        new_keys_by_sport={"nfl": [f"{GID}|total|over|betonline"]})
    assert run2.n_alerts == 0 and rec2.sent == [] and run2.source == "r2"


def test_rehydrate_from_d1_export_when_alerts_json_missing(tmp_path: Path):
    key = LEGACY_EKEY
    rows = [{"alert_key": key, "family": "edge", "game_id": GID, "sport": "nfl", "season": 2026, "week": 3,
             "market": "total", "side": "under", "book": "betonline", "tier": "mid", "model_version": "v1",
             "first_sent_at": "2026-09-17T10:00:00Z", "last_sent_at": "2026-09-17T10:00:00Z", "sends": 1,
             "first_line": 38.0, "first_edge": 3.4, "last_line": 38.0, "last_fair": 34.6,
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
    # The legacy book-keyed marker dedupes a new stable PLAY while its record
    # still drives follow-up detection at the new 1.5-point threshold.
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card([_edge(line=39.5, edge_pts=4.9)])]}, tmp_path, cfg=CFG, now=NOW,
                       sender=rec, fetch_rows=lambda: rows)
    assert run.source == "api" and [c.family for c in run.outcome.sent] == ["move"]


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
