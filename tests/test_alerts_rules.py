"""pipeline/alerts.py rules (ARCH §10): thresholds per sport/market, weather gate,
move buckets, edge-gone, forecast move, quiet-hours queue/flush, 25-per-run cap +
digest, mark-only-after-send, dedup, rehydrate, D1 mirror, feed/status payloads."""

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
         kickoff: datetime = KICK, stadium: bool = True) -> dict[str, Any]:
    edges = [_edge()] if edges is None else edges
    return {
        "game_id": game_id, "sport": sport, "season": 2026, "week": 3, "kickoff_utc": kickoff.isoformat(),
        "kickoff_local": kickoff.isoformat(), "tz": "America/New_York", "date_label": "SUN 09/20", "time_label": "01:00 PM",
        "neutral": False, "status": "scheduled", "signal": {"label": "High Impact", "flags": []},
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


# ---- thresholds per sport / market (model.fair.tier, the gate EDGE alerts read) ----

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


def test_weather_gate_blocks_non_weather_edges():
    assert fair.tier("nfl", "total", 5.0, 0.1, 0.9, 10, is_weather_driven=False) == "watch"
    alerts, _ = _fresh()
    assert A.edge_candidates(card(weather_driven=False), alerts, CFG) == []
    assert A.edge_candidates(card(thin=True), alerts, CFG) == []
    assert A.edge_candidates(card([_edge(tier="watch")]), alerts, CFG) == []
    c = A.edge_candidates(card(), alerts, CFG)
    assert [x.key for x in c] == [f"edge|2026|3|{GID}|total|under|betonline|v1"]
    assert c[0].tier == "edge" and c[0].sport == "nfl" and c[0].kickoff_utc == KICK


def test_edge_dedup_after_marker():
    alerts, _ = _fresh()
    pstate.mark_alert(alerts, f"edge|2026|3|{GID}|total|under|betonline|v1", "t")
    assert A.edge_candidates(card(), alerts, CFG) == []


# ---- move buckets / gone / forecast move: only on games with an open EDGE ----

def _with_open_edge(line: float = 38.0, fair_line: float = 34.6, edge_pts: float = 3.4, wind: float = 18.0) -> dict:
    alerts, tg = _fresh()
    cands = A.edge_candidates(card([_edge(line=line, fair_line=fair_line, edge_pts=edge_pts)], wind=wind), alerts, CFG)
    out, _, _ = _live(cands, alerts, tg)
    assert out.n_sent == 1
    return alerts


def test_move_bucket_steps():
    assert A.move_bucket("total", 38.0, 38.5) == 0
    assert A.move_bucket("total", 39.0, 38.0) == 1
    assert A.move_bucket("total", 40.5, 38.0) == 2
    assert A.move_bucket("spread", -3.5, -3.0) == 1
    assert A.move_bucket("spread", -2.5, -3.5) == 2
    assert A.move_direction("total", "under", 37.0, 38.0, 34.6) == "toward fair"
    assert A.move_direction("total", "under", 39.0, 38.0, 34.6) == "away from fair"


def test_move_only_for_games_with_open_edge():
    alerts, _ = _fresh()
    moved = card([_edge(line=39.0, edge_pts=4.4)])
    assert [c for c in A.followup_candidates(moved, alerts, CFG, NOW) if c.family == "move"] == []
    alerts = _with_open_edge()
    half = card([_edge(line=38.5, edge_pts=3.9)])
    assert A.followup_candidates(half, alerts, CFG, NOW) == []
    c = A.followup_candidates(moved, alerts, CFG, NOW)
    assert [x.key for x in c] == [f"move|edge|2026|3|{GID}|total|under|betonline|v1|1"]
    assert "away from fair" in c[0].text and "38 → 39" in c[0].text


def test_move_cooldown_and_rebasing_after_send():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    c = A.followup_candidates(card([_edge(line=39.0, edge_pts=4.4)]), alerts, CFG, NOW)
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    ekey = f"edge|2026|3|{GID}|total|under|betonline|v1"
    assert pstate.get_alert_record(alerts, ekey)["last_line"] == 39.0
    # 1 h later, another point: within the 2 h cooldown -> nothing
    assert A.followup_candidates(card([_edge(line=40.0, edge_pts=5.4)]), alerts, CFG, NOW + timedelta(hours=1)) == []
    # 3 h later: bucket vs the FIRST alerted line (38 -> 40 = bucket 2, a new key); direction vs last sent (39)
    c2 = A.followup_candidates(card([_edge(line=40.0, edge_pts=5.4)]), alerts, CFG, NOW + timedelta(hours=3))
    assert [x.key for x in c2] == [f"move|{ekey}|2"] and "39 → 40" in c2[0].text
    # drifting back to 39 = bucket 1 again, already sent -> silent
    assert A.followup_candidates(card([_edge(line=39.0, edge_pts=4.4)]), alerts, CFG, NOW + timedelta(hours=6)) == []


def test_edge_gone_closes_record_and_suppresses_move():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    gone = card([_edge(line=35.0, edge_pts=0.4)])
    c = A.followup_candidates(gone, alerts, CFG, NOW)
    assert [x.family for x in c] == ["gone"]
    assert c[0].key == f"gone|edge|2026|3|{GID}|total|under|betonline|v1"
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    rec = pstate.get_alert_record(alerts, f"edge|2026|3|{GID}|total|under|betonline|v1")
    assert rec["status"] == "closed"
    assert pstate.open_edge_records(alerts, GID) == []
    assert A.followup_candidates(card([_edge(line=45.0, edge_pts=10.0)]), alerts, CFG, NOW) == []


def test_edge_gone_threshold_boundary():
    alerts = _with_open_edge()
    assert [x.family for x in A.followup_candidates(card([_edge(line=35.1, edge_pts=0.5)]), alerts, CFG, NOW)] != ["gone"]
    assert [x.family for x in A.followup_candidates(card([_edge(line=35.0, edge_pts=0.49)]), alerts, CFG, NOW)] == ["gone"]


def test_forecast_move_bucket_on_fair_line():
    alerts, tg = _fresh()
    alerts = _with_open_edge()
    same = card([_edge(fair_line=35.4, edge_pts=2.6)], wind=15.0)
    assert [x.family for x in A.followup_candidates(same, alerts, CFG, NOW)] == []
    moved = card([_edge(fair_line=36.1, edge_pts=1.9)], wind=13.0, rain=0.0)
    c = A.followup_candidates(moved, alerts, CFG, NOW)
    assert [x.key for x in c] == [f"wx|edge|2026|3|{GID}|total|under|betonline|v1|1"]
    assert "18 → 13 mph" in c[0].text and "0.8 → 0 mm" in c[0].text
    out, _, _ = _live(c, alerts, tg)
    assert out.n_sent == 1
    rec = pstate.get_alert_record(alerts, f"edge|2026|3|{GID}|total|under|betonline|v1")
    assert rec["last_fair"] == 36.1 and rec["last_wind"] == 13.0
    assert A.followup_candidates(moved, alerts, CFG, NOW) == []


# ---- quiet hours -----------------------------------------------------------------

def test_quiet_hours_window():
    assert A.in_quiet_hours(QUIET)
    assert A.in_quiet_hours(datetime(2026, 9, 19, 10, 59, tzinfo=timezone.utc))   # 06:59 ET
    assert not A.in_quiet_hours(datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc))  # 07:00 ET
    assert not A.in_quiet_hours(NOW)


def test_quiet_hours_queue_bypass_and_flush(tmp_path: Path):
    alerts, tg = _fresh()
    edge = A.edge_candidates(card(), alerts, CFG)[0]
    strong = A.edge_candidates(card([_edge(book="betcris", tier="strong", edge_pts=4.0)]), alerts, CFG)[0]
    soon_card = card([_edge(book="fanduel")], kickoff=QUIET + timedelta(hours=2))
    soon = A.edge_candidates(soon_card, alerts, CFG)[0]
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
    out2, _, p2 = _live([edge], alerts, tg2, now=QUIET + timedelta(hours=1))
    assert len(tg2["queue"]) == 1 and out2.n_sent == 0
    # 07:30 ET: released as ONE digest, then marked
    out3, rec3, p3 = _live([], alerts, tg2, now=MORNING)
    assert len(p3.flush) == 1 and out3.n_messages == 1 and out3.n_sent == 1
    assert rec3.sent[0][0].startswith("<b>Overnight alerts (1)</b>") and rec3.sent[0][1] == "CNFL"
    assert pstate.alert_sent(alerts, edge.key) and tg2["queue"] == []


def test_flush_skips_keys_sent_meanwhile():
    alerts, tg = _fresh()
    edge = A.edge_candidates(card(), alerts, CFG)[0]
    _live([edge], alerts, tg, now=QUIET)
    pstate.mark_alert(alerts, edge.key, "t")   # e.g. sent from the playwright job
    out, rec, p = _live([], alerts, tg, now=MORNING)
    assert p.flush == [] and rec.sent == [] and tg["queue"] == []


# ---- cap + digest ----------------------------------------------------------------

def _many(n: int) -> list[A.Candidate]:
    alerts, _ = _fresh()
    out = []
    for i in range(n):
        gid = f"nfl:2026:3:t{i}@ne"
        out += A.edge_candidates(card([_edge(book="betonline")], game_id=gid), alerts, CFG)
    assert len(out) == n
    return out


def test_cap_25_per_run_with_digest():
    alerts, tg = _fresh()
    out, rec, p = _live(_many(40), alerts, tg)
    assert len(p.send) == 24 and len(p.digest) == 16
    assert out.n_messages == 25 and out.n_sent == 40
    assert rec.sent[-1][0].startswith("<b>Alert digest (run cap) (16)</b>")
    assert all(pstate.alert_sent(alerts, c.key) for c in p.send + p.digest)
    assert len(alerts["feed"]) == 40


def test_under_cap_sends_individually():
    alerts, tg = _fresh()
    out, rec, p = _live(_many(25), alerts, tg)
    assert len(p.send) == 25 and p.digest == [] and out.n_messages == 25


def test_strong_and_imminent_prioritised_before_cap():
    cands = _many(30)
    late = A.edge_candidates(card([_edge(book="novig", tier="strong", edge_pts=5.0)], game_id="nfl:2026:3:zz@ne"), _fresh()[0], CFG)[0]
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

def test_openers_digest_weather_gate_and_daily_key():
    alerts, _ = _fresh()
    calm = card(game_id="nfl:2026:3:a@b", wind=5.0, gs=-1.0)
    windy = card(game_id="nfl:2026:3:c@d", wind=12.0, gs=-1.0)
    cold = card(game_id="nfl:2026:3:e@f", wind=3.0, gs=-2.0)
    keys = ["nfl:2026:3:a@b|total|over|betcris", "nfl:2026:3:c@d|total|over|betcris", "nfl:2026:3:e@f|spread|home|fanduel",
            "nfl:2026:3:c@d|total|under|consensus"]
    c = A.opener_candidates("nfl", [calm, windy, cold], keys, alerts, CFG, NOW)
    assert len(c) == 1 and c[0].key == "openers|nfl|2026|3|2026-09-18"
    assert "2 weather game" in c[0].text and "Betcris" in c[0].text and "Consensus" not in c[0].text
    assert A.opener_candidates("nfl", [calm], keys[:1], alerts, CFG, NOW) == []
    assert A.opener_candidates("nfl", [windy], [], alerts, CFG, NOW) == []
    pstate.mark_alert(alerts, c[0].key, "t")
    assert A.opener_candidates("nfl", [windy], keys, alerts, CFG, NOW) == []


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
        "degr|weather|open-meteo-503-for-3-games|2026-09-18",
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
    assert f"edge|2026|3|{GID}|total|under|betonline|v1" in out and "dry-run" in out
    assert run.n_alerts == 0 and not (tmp_path / "alerts.json").exists()
    run2 = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, enabled=False, dry_run=False, cfg=CFG, now=NOW,
                        sender=Recorder())
    assert "disabled" in capsys.readouterr().out and run2.candidates and not (tmp_path / "alerts.json").exists()


def test_run_alerts_live_persists_and_second_run_is_silent(tmp_path: Path):
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW, sender=rec,
                       new_keys_by_sport={"nfl": [f"{GID}|total|over|betonline"]})
    assert run.n_alerts == 2 and len(rec.sent) == 2      # EDGE + openers digest
    assert run.keys_for(GID) == [f"edge|2026|3|{GID}|total|under|betonline|v1"]
    saved = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1 and len(saved["sent"]) == 2 and len(saved["feed"]) == 2
    assert (tmp_path / "telegram_state.json").exists()
    rec2 = Recorder()
    run2 = A.run_alerts(_ctx(), {"nfl": [card()]}, tmp_path, cfg=CFG, now=NOW + timedelta(hours=1), sender=rec2,
                        new_keys_by_sport={"nfl": [f"{GID}|total|over|betonline"]})
    assert run2.n_alerts == 0 and rec2.sent == [] and run2.source == "r2"


def test_rehydrate_from_d1_export_when_alerts_json_missing(tmp_path: Path):
    key = f"edge|2026|3|{GID}|total|under|betonline|v1"
    rows = [{"alert_key": key, "family": "edge", "game_id": GID, "sport": "nfl", "season": 2026, "week": 3,
             "market": "total", "side": "under", "book": "betonline", "tier": "edge", "model_version": "v1",
             "first_sent_at": "2026-09-17T10:00:00Z", "last_sent_at": "2026-09-17T10:00:00Z", "sends": 1,
             "first_line": 38.0, "last_line": 38.0, "last_fair": 34.6, "last_edge": 3.4, "status": "open"}]
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
    # the rehydrated EDGE record drives MOVE detection on the next run
    rec = Recorder()
    run = A.run_alerts(_ctx(), {"nfl": [card([_edge(line=39.0, edge_pts=4.4)])]}, tmp_path, cfg=CFG, now=NOW,
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
    assert run is not None and run.n_alerts == 2 and "alerts" in ctx.stage_timings
    assert res.cards[0]["alerts"] == [f"edge|2026|3|{GID}|total|under|betonline|v1"]
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
    assert status["runs"][0]["run_id"] == "r1" and status["runs"][0]["n_alerts"] == 2
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
