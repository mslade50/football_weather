"""Prospective, paper-only weather study. No pricing/alert module imports this.

All selection uses the current capture. Results and closes only grade frozen
entries. Immutable captures are the audit log; the checkpoint is a small index
of first crossings, their contemporaneous controls and pregame closing quotes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

VERSION = 1
POLICY = {
    "id": "ifs-first-observed-v1",
    "wind": "mean of four complete IFS hours, UTC kickoff hour floor through +3h",
    "lead_hours": [24, 144], "thresholds_mph": [12, 15],
    "forecast_max_age_hours": 6, "model_init_max_age_hours": 24,
    "quote_max_age_minutes": 60, "close_max_age_minutes": 60,
    "books": ["fanduel", "betonline", "draftkings", "pinnacle"],
    "fixed_books": {"cfb": "fanduel", "nfl": "betonline"},
    "best": "secondary: highest under total, then highest decimal return; odds >= -120",
    "controls": "same capture, sport, season, week, lead-day bin, book; valid wind below threshold",
    "missing_quote": "first weather crossing is frozen and missed; never enter later",
    "entry_capture": "only runs requesting sport's fixed book; other runs archived without entries",
    "reschedule": "exclude entry or control if authoritative kickoff changes",
    "stake": "one unit risked at recorded American odds; pushes return zero",
    "promotion": "none; no live strategy or stakes affected",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


POLICY_HASH = digest(POLICY)


def stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    return float(value)


def empty() -> dict:
    return {"schema_version": VERSION, "policy": POLICY, "policy_hash": POLICY_HASH,
            "runs": {}, "games": {}, "flags": {}, "scores": {}, "gaps": [], "started_at": None, "last_capture": None}


def validate(state: dict) -> None:
    if (state.get("schema_version") != VERSION or state.get("policy_hash") != POLICY_HASH
            or state.get("policy") != POLICY):
        raise ValueError("Paper checkpoint schema/policy mismatch; explicit migration required")
    for name in ("runs", "games", "flags", "scores"):
        if not isinstance(state.get(name), dict):
            raise ValueError(f"Paper checkpoint missing {name}")


def decimal(odds: float) -> float:
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


def quotes(card: dict, now: datetime) -> tuple[dict, dict]:
    """Require matching, fresh over/under observations from an allowed sportsbook."""
    out, rejected = {}, {}
    for book in POLICY["books"]:
        rows = [q for q in card.get("paper_quotes", []) if q.get("book") == book
                and q.get("game_id") == card["game_id"] and q.get("sport") == card["sport"]
                and q.get("market") == "total" and q.get("is_main") is True]
        valid = []
        for under in rows:
            if under.get("side") != "under":
                continue
            for over in rows:
                if over.get("side") != "over" or over.get("line") != under.get("line"):
                    continue
                ok = True
                for q in (under, over):
                    observed = stamp(q.get("scraped_at"))
                    odds, line = number(q.get("odds")), number(q.get("line"))
                    if (observed is None or not timedelta(0) <= now - observed <= timedelta(minutes=60)
                            or odds is None or abs(odds) < 100 or line is None or not 0 < line < 150):
                        ok = False
                    for field in ("source_updated_at", "expires_at", "actionable_until"):
                        if q.get(field) is None:
                            continue
                        dt = stamp(q[field])
                        if dt is None or (field == "source_updated_at" and not timedelta(0) <= now - dt <= timedelta(minutes=60)):
                            ok = False
                        if field != "source_updated_at" and (dt is None or dt <= now):
                            ok = False
                if ok and under.get("scraped_at") == over.get("scraped_at"):
                    valid.append({"book": book, "line": under["line"], "odds": under["odds"],
                                  "over_odds": over["odds"], "observed_at": iso(stamp(under["scraped_at"])),
                                  "source_updated_at": under.get("source_updated_at"),
                                  "source_id": under.get("source_id"), "run_id": under.get("run_id")})
        if len(valid) == 1:
            out[book] = valid[0]
        else:
            rejected[book] = "ambiguous_pairs" if valid else "no_fresh_paired_total"
    return out, rejected


def observation(card: dict, now: datetime) -> dict:
    wx, stadium = card.get("weather") or {}, card.get("stadium") or {}
    kick = stamp(card.get("kickoff_utc"))
    lead = (kick - now).total_seconds() / 3600 if kick else None
    reasons = []
    if kick is None or lead is None or not 24 <= lead <= 144:
        reasons.append("outside_24_to_144h_window")
    if card.get("status") not in ("scheduled", "pre", "STATUS_SCHEDULED"):
        reasons.append("not_scheduled")
    if stadium.get("roof_state") not in ("outdoors", "open") or stadium.get("roof_type") == "dome":
        reasons.append("roof_not_confirmed_exposed")
    fetched = stamp(wx.get("fetched_at"))
    if fetched is None or not timedelta(0) <= now - fetched <= timedelta(hours=6) or wx.get("point_aged") is not False:
        reasons.append("forecast_missing_stale_or_future")
    init = (wx.get("point_source_updated_at") or {}).get("ecmwf_opendata")
    if init and (stamp(init) is None or not timedelta(0) <= now - stamp(init) <= timedelta(hours=24)):
        reasons.append("model_init_stale_or_future")
    samples = wx.get("paper_ifs") or []
    expected = [kick.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i) for i in range(4)] if kick else []
    wind = None
    if (len(samples) == 4 and [stamp(s.get("t")) for s in samples] == expected
            and all(number(s.get("wind_mph")) is not None and 0 <= s["wind_mph"] < 200 for s in samples)):
        wind = mean(s["wind_mph"] for s in samples)
    else:
        reasons.append("ifs_four_hour_window_incomplete")
    paired, quote_rejections = quotes(card, now)
    return {"game_id": card["game_id"], "sport": card["sport"], "season": card.get("season"),
            "week": card.get("week"), "home_id": (card.get("home") or {}).get("team_id"),
            "away_id": (card.get("away") or {}).get("team_id"), "kickoff_utc": iso(kick) if kick else None,
            "observed_at": iso(now), "lead_hours": lead, "lead_day": int(lead // 24) if lead is not None else None,
            "wind_mph": wind, "reasons": reasons, "quotes": paired, "quote_rejections": quote_rejections,
            "raw_quotes": copy.deepcopy(card.get("paper_quotes") or []),
            "forecast": {"source": wx.get("ecmwf_source"), "fetched_at": wx.get("fetched_at"),
                         "model_init": init, "publication_at": None, "samples": samples},
            "other_model_windows": wx.get("models") or [],
            "roof_state": stadium.get("roof_state"), "stadium_id": stadium.get("stadium_id")}


def capture(state: dict, cards: list[dict], run_id: str, now: datetime, *, requested_books: list[str]) -> tuple[dict, dict]:
    validate(state)
    if not run_id or not cards or len({c["game_id"] for c in cards}) != len(cards):
        raise ValueError("Capture requires a run ID and nonempty, unique game cards")
    obs = [observation(card, now) for card in sorted(cards, key=lambda c: c["game_id"])]
    for o in obs:
        if POLICY["fixed_books"][o["sport"]] not in requested_books:
            o["reasons"].append("reference_book_not_requested_this_run")
    event = {"schema_version": VERSION, "policy_hash": POLICY_HASH, "run_id": run_id,
             "observed_at": iso(now), "requested_books": sorted(set(requested_books)), "observations": obs}
    fingerprint = digest(event)
    if run_id in state["runs"]:
        if state["runs"][run_id] != fingerprint:
            raise ValueError("Run ID was reused with different paper observations")
        return copy.deepcopy(state), event
    if state["last_capture"] and now < stamp(state["last_capture"]):
        raise ValueError("Out-of-order paper capture; audit replay must be chronological")
    out = copy.deepcopy(state)
    out["started_at"] = out["started_at"] or iso(now)
    out["last_capture"] = iso(now)
    out["runs"][run_id] = fingerprint
    for o in obs:
        gid = o["game_id"]
        game = out["games"].setdefault(gid, {"closes": {}})
        # Keep candidate quotes even outside the entry weather/lead gates. A
        # closing quote is never inferred from a change-only history timestamp.
        if game.get("kickoff_utc") != o["kickoff_utc"]:
            if game.get("kickoff_utc") is not None:
                game["rescheduled"] = True
            game["closes"] = {}
        game.update({k: o[k] for k in ("game_id", "sport", "season", "week", "home_id", "away_id", "kickoff_utc")})
        kick = stamp(o["kickoff_utc"])
        if kick and now < kick:
            for book, q in o["quotes"].items():
                previous = game["closes"].get(book)
                if (stamp(q["observed_at"]) < kick and
                        (not previous or stamp(q["observed_at"]) > stamp(previous["observed_at"]))):
                    game["closes"][book] = copy.deepcopy(q)
        if o["reasons"]:
            continue
        for threshold in POLICY["thresholds_mph"]:
            key = f"{gid}|{threshold}"
            if o["wind_mph"] < threshold or key in out["flags"]:
                continue
            fixed = POLICY["fixed_books"][o["sport"]]
            best = [q for q in o["quotes"].values() if q["odds"] >= -120]
            best_quote = max(best, key=lambda q: (q["line"], decimal(q["odds"]), q["book"])) if best else None
            variants = {}
            for name, q in (("fixed", o["quotes"].get(fixed)), ("best", best_quote)):
                book = q["book"] if q else fixed
                controls = [{"game_id": c["game_id"], "kickoff_utc": c["kickoff_utc"],
                             "observed_at": c["observed_at"], "wind_mph": c["wind_mph"],
                             "quotes": {book: copy.deepcopy(c["quotes"][book])}}
                            for c in obs if c["game_id"] != gid and not c["reasons"]
                            and all(c[k] == o[k] for k in ("sport", "season", "week", "lead_day"))
                            and c["wind_mph"] < threshold and book in c["quotes"]]
                variants[name] = {"quote": copy.deepcopy(q), "controls": controls,
                                  "status": "entered" if q else "missed_no_quote"}
            out["flags"][key] = {"threshold": threshold, "run_id": run_id, "observation": copy.deepcopy(o),
                                 "variants": variants}
    return out, event


def grade(state: dict, o: dict, quote: dict | None, now: datetime) -> dict:
    if quote is None:
        return {"status": "missed_no_quote", "clv": None, "profit": None}
    game = state["games"][o["game_id"]]
    kick = stamp(o["kickoff_utc"])
    result = state["scores"].get(o["game_id"], {})
    if (game.get("rescheduled") or game["kickoff_utc"] != o["kickoff_utc"]
            or (result.get("kickoff_utc") and result["kickoff_utc"] != o["kickoff_utc"])):
        return {"status": "excluded_reschedule", "clv": None, "profit": None}
    if not kick or now < kick:
        return {"status": "pregame", "clv": None, "profit": None}
    close = game["closes"].get(quote["book"])
    fresh = close and timedelta(0) < kick - stamp(close["observed_at"]) <= timedelta(minutes=60)
    clv = quote["line"] - close["line"] if fresh else None
    total = number(result.get("actual_total")) if result.get("src_result") else None
    profit = None if total is None else 0.0 if total == quote["line"] else decimal(quote["odds"]) - 1 if total < quote["line"] else -1.0
    return {"status": "settled" if profit is not None else "pending_result", "clv": clv, "profit": profit,
            "result": None if profit is None else "P" if profit == 0 else "W" if profit > 0 else "L",
            "close_status": "fresh" if fresh else "missing_fresh_close", "close": close,
            "result_source": result.get("src_result"), "actual_total": total}


def cluster_interval(rows: list[dict], field: str) -> list[float] | None:
    groups = defaultdict(list)
    for row in rows:
        if row.get(field) is not None:
            groups[row["week_key"]].append(row[field])
    # Descriptive percentile bootstrap; no p-values, no promotion or tuning.
    if len(groups) < 8:
        return None
    values = list(groups.values())
    rng = random.Random(143)
    draws = sorted(mean(x for group in rng.choices(values, k=len(values)) for x in group) for _ in range(1000))
    return [draws[24], draws[974]]


def summary(rows: list[dict]) -> dict:
    clv = [r["clv"] for r in rows if r.get("clv") is not None]
    excess = [r["excess_clv"] for r in rows if r.get("excess_clv") is not None]
    profits = [r["profit"] for r in rows if r.get("profit") is not None]
    return {"entries": len(rows), "clv_n": len(clv), "raw_clv": mean(clv) if clv else None,
            "matched_n": len(excess), "excess_clv": mean(excess) if excess else None,
            "settled_n": len(profits), "roi": mean(profits) if profits else None,
            "record": dict(Counter(r.get("result") for r in rows if r.get("result"))),
            "weeks": len({r["week_key"] for r in rows}),
            "raw_clv_interval": cluster_interval(rows, "clv"),
            "excess_clv_interval": cluster_interval(rows, "excess_clv"),
            "roi_interval": cluster_interval(rows, "profit")}


def report(state: dict, now: datetime) -> dict:
    validate(state)
    rows, missed = [], Counter()
    for flag in state["flags"].values():
        o = flag["observation"]
        for variant, v in flag["variants"].items():
            if v["quote"] is None:
                missed[f'{o["sport"]}|{flag["threshold"]}|{variant}'] += 1
                continue
            g = grade(state, o, v["quote"], now)
            control_grades = [grade(state, c, c["quotes"][v["quote"]["book"]], now) for c in v["controls"]]
            matched = [c["clv"] for c in control_grades if c["clv"] is not None]
            # Require every frozen control's close; partial grading must not
            # silently change the originally selected comparison set.
            baseline = mean(matched) if matched and len(matched) == len(control_grades) else None
            rows.append({"game_id": o["game_id"], "sport": o["sport"], "threshold": flag["threshold"],
                         "variant": variant, "week_key": f'{o["season"]}:{o["week"]}', "entry": v["quote"],
                         "entry_at": o["observed_at"], "wind_mph": o["wind_mph"], **g,
                         "control_n": len(control_grades), "control_clv_n": len(matched), "baseline_clv": baseline,
                         "excess_clv": g["clv"] - baseline if g["clv"] is not None and baseline is not None else None})
    groups, weeks = defaultdict(list), defaultdict(list)
    for row in rows:
        key = f'{row["sport"]}|{row["threshold"]}|{row["variant"]}'
        groups[key].append(row)
        weeks[f'{key}|{row["week_key"]}'].append(row)
    for sport in ("cfb", "nfl"):
        for threshold in (12, 15):
            for variant in ("fixed", "best"):
                groups.setdefault(f"{sport}|{threshold}|{variant}", [])
    return {"schema_version": VERSION, "policy": POLICY, "policy_hash": POLICY_HASH,
            "generated_at": iso(now), "started_at": state["started_at"], "last_capture": state["last_capture"],
            "last_grade": state.get("last_grade"), "capture_runs": len(state["runs"]), "games": len(state["games"]),
            "capture_gaps": state.get("gaps", []),
            "missed": dict(missed), "pending": dict(Counter(r["status"] for r in rows)),
            "missing_closes": sum(r.get("close_status") == "missing_fresh_close" for r in rows),
            "groups": {k: summary(v) for k, v in groups.items()}, "weeks": {k: summary(v) for k, v in weeks.items()},
            "rows": rows, "interpretation": "Paper only. No established edge. Intervals require 8 observed weeks; no automatic promotion."}
