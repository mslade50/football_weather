"""Board JSON payloads (ARCH §5): GameCard, ``games_{sport}.json``, ``board.json``,
``meta.json``, ``history.json`` / ``wx_history.json`` and per-run ``snapshots/``.

Every served file is written with ``allow_nan=False`` (a bare ``NaN`` token kills
``JSON.parse`` in the browser — a loud build failure is preferable) after
``sanitize()`` has turned NaN/inf into ``null`` and datetimes into ISO strings.

Payload shapes
--------------
``games_{sport}.json``   ``{"meta": <slim meta>, "games": [GameCard, ...]}``
``board.json``           ``{"meta": ..., "rows": [slim table row, ...]}`` (both sports)
``meta.json``            RunMeta + ``books`` status + ``next_run_eta`` (pushed LAST)
``history.json``         ``{"schema_version", "run_id", "series": {key: [[ts, line, odds]]},
                           "fair_series": {...}}`` — same object as the state file
``wx_history.json``      ``{"schema_version", "run_id", "series": {game_id: [[ts, lead_h,
                           wind, gust, temp, precip, pop, gs_fg]]}}`` change-only
``snapshots/{sport}/{season}/{week}/{run_id}.json``  full GameCard list per run

The book-status chips (``meta.books``) are green / amber / red:
green = rows this run and no volume drop, amber = rows but < 50 % of the
baseline peak, red = 0 rows for a requested book.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import state as pstate
from pipeline.contracts import Game, GameLine, Stadium, Team, WeatherForecast
from utils.timeutil import date_label, time_label, to_tz, utc_iso

PathLike = Union[str, Path]

BOARD_DIRNAME = "board"
SNAPSHOT_DIRNAME = "snapshots"
META_FILE = "meta.json"
BOARD_FILE = "board.json"
WX_HISTORY_FILE = "wx_history.json"
ALERTS_FEED_FILE = "alerts_feed.json"
STATUS_FILE = "status.json"
BACKTEST_FILE = "backtest.json"   # written by pipeline/backtest.py (Phase 6); carried through write_board when given
STATUS_RUNS_CAP = 20
WX_LAST_FILE = "wx_last.json"
WX_HISTORY_CAP = 120

# GitHub backstop schedule (pipeline.yml ``'17 9,14,20 * * *'`` UTC); the CF
# Worker cron sends its own ETA via NEXT_RUN_ETA when it dispatches.
BACKSTOP_UTC_HOURS = (9, 14, 20)
BACKSTOP_MINUTE = 17

VOLUME_AMBER_FRAC = 0.5


# ---- sanitising -------------------------------------------------------------------

def _finite(v: float) -> Optional[float]:
    return v if math.isfinite(v) else None


def sanitize(obj: Any) -> Any:
    """Recursively make ``obj`` JSON-safe: NaN/inf -> None, datetime -> ISO,
    dataclasses -> dict, sets/tuples -> lists. Dict keys are stringified."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return _finite(obj)
    if isinstance(obj, datetime):
        return utc_iso(obj) if obj.tzinfo is not None else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: sanitize(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [sanitize(v) for v in obj]
    try:  # numpy scalars
        if hasattr(obj, "item"):
            return sanitize(obj.item())
    except Exception:  # noqa: BLE001
        pass
    return str(obj)


def dump_json(path: PathLike, data: Any, indent: Optional[int] = None) -> Path:
    """Write sanitised JSON with ``allow_nan=False``. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(sanitize(data), allow_nan=False, indent=indent, ensure_ascii=False)
    p.write_text(text, encoding="utf-8")
    return p


# ---- next run ----------------------------------------------------------------------

def next_backstop(now: datetime) -> datetime:
    """Next GitHub backstop fire (UTC) strictly after ``now``."""
    n = now.astimezone(timezone.utc)
    for day in (0, 1):
        base = (n + timedelta(days=day)).replace(minute=BACKSTOP_MINUTE, second=0, microsecond=0)
        for h in BACKSTOP_UTC_HOURS:
            cand = base.replace(hour=h)
            if cand > n:
                return cand
    return n  # unreachable


def next_run_eta(now: datetime, env: Optional[dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    raw = (env.get("NEXT_RUN_ETA") or "").strip()
    if raw:
        return raw
    return utc_iso(next_backstop(now))


# ---- GameCard -----------------------------------------------------------------------

def _team_block(team: Optional[Team], team_id: str) -> dict[str, Any]:
    if team is None:
        return {"team_id": team_id, "name": team_id, "short": team_id.upper()}
    return {"team_id": team.team_id, "name": team.name or team_id, "short": team.short or team_id.upper()}


def _stadium_block(st: Optional[Stadium], roof_state: Optional[str], avg_wind: Optional[float], avg_wind_month: Optional[float]) -> Optional[dict[str, Any]]:
    if st is None:
        return None
    return {
        "stadium_id": st.stadium_id,
        "name": st.name,
        "lat": st.lat,
        "lon": st.lon,
        "city": st.city,
        "state": st.state,
        "country": st.country,
        "orient_deg": st.orientation_deg,
        "orient": st.orientation_bucket,
        "orient_src": st.orientation_src,
        "roof_type": st.roof_type,
        "roof_state": roof_state,
        "surface": st.surface,
        "elevation_m": st.elevation_m,
        "year_built": st.year_built,
        "timezone": st.timezone,
        "wind_vol_static": st.wind_vol_static,
        "wind_impact_static": st.wind_impact_static,
        "weakest_wind_effect": st.weakest_wind_effect,
        "avg_wind": avg_wind if avg_wind is not None else st.avg_wind_static,
        "avg_wind_month": avg_wind_month,
        "needs_review": bool(st.needs_review),
    }


def _hourly(fc: WeatherForecast) -> list[dict[str, Any]]:
    out = []
    for pt in fc.hourly or []:
        out.append({"t": pt.t, "temp": pt.temp, "wind": pt.wind, "gust": pt.gust, "dir": pt.dir,
                    "precip": pt.precip, "pop": pt.pop, "p10": pt.p10, "p90": pt.p90})
    return out


def _weather_block(fc: Optional[WeatherForecast], avg_wind: Optional[float]) -> Optional[dict[str, Any]]:
    if fc is None:
        return None
    wind_diff = None
    if fc.wind_fg is not None and avg_wind is not None:
        wind_diff = round(float(fc.wind_fg) - float(avg_wind), 1)
    return {
        "temp_fg": fc.temp_fg,
        "wind_fg": fc.wind_fg,
        "gust_fg": fc.gust_fg,
        "wind_dir_1h": fc.wind_dir_1h,
        "wind_dir_2h": fc.wind_dir_2h,
        "wind_dir_fg": fc.wind_dir_fg,
        "wind_dir_deg": fc.wind_dir_deg,
        "rain_fg": fc.rain_fg_mm,
        "precip_prob": fc.precip_prob,
        "precip_prob_ens": fc.precip_prob_ens,
        "wind_vol_fc": fc.wind_vol_fc,
        "wind_p10": fc.wind_p10,
        "wind_p50": fc.wind_p50,
        "wind_p90": fc.wind_p90,
        "wind_diff": wind_diff,
        "cross_mph": fc.cross_mph,
        "head_mph": fc.head_mph,
        "model_disagreement": fc.model_disagreement,
        "source": fc.source,
        "lead_hours": fc.lead_hours,
        "fetched_at": fc.run_time,
        "hourly": _hourly(fc),
    }


def _impact_v2_block(impact_v2: Any) -> Optional[dict[str, Any]]:
    if impact_v2 is None:
        return None
    g = getattr
    return {
        "gs_fg_pct": g(impact_v2, "gs_fg_pct", None),
        "away_fg_pct": g(impact_v2, "away_fg_pct", None),
        "roof_closed": bool(g(impact_v2, "roof_closed", False)),
        "components": {
            "wind": g(impact_v2, "wind_c", None),
            "cold": g(impact_v2, "cold_c", None),
            "heat": g(impact_v2, "heat_c", None),
            "rain": g(impact_v2, "rain_c", None),
            "alt": g(impact_v2, "alt_c", None),
            "heat_away": g(impact_v2, "heat_away", None),
            "cold_away": g(impact_v2, "cold_away", None),
        },
        "w_eff": g(impact_v2, "w_eff", None),
        "w_dir": g(impact_v2, "w_dir", None),
        "dir_mult": g(impact_v2, "dir_mult", None),
        "expected_mm": g(impact_v2, "expected_mm", None),
        "conf": g(impact_v2, "conf", None),
        "ensemble": bool(g(impact_v2, "ensemble", False)),
    }


def _impact_block(impact: Any, impact_v2: Any = None, model_version: Optional[str] = None) -> dict[str, Any]:
    """``model_version`` = the model driving edges/alerts (ALERT_MODEL); defaults to the v1 object's."""
    if impact is None:
        return {"v1": None, "v2": _impact_v2_block(impact_v2), "model_version": model_version or "v1"}
    g = getattr
    v1 = {
        "gs_fg_pct": g(impact, "gs_fg_pct", None),
        "away_fg_pct": g(impact, "away_fg_pct", None),
        "roof_closed": bool(g(impact, "roof_closed", False)),
        "components": {
            "wind": g(impact, "wind_c", None),
            "cold": g(impact, "cold_c", None),
            "heat": g(impact, "heat_c", None),
            "rain": g(impact, "rain_c", None),
            "alt": g(impact, "alt_c", None),
            "heat_away": g(impact, "heat_away", None),
            "cold_away": g(impact, "cold_away", None),
        },
    }
    return {"v1": v1, "v2": _impact_v2_block(impact_v2), "model_version": model_version or g(impact, "model_version", "v1")}


def _signal_block(sig: Any, flags: Sequence[str], dow_base: Optional[float] = None) -> dict[str, Any]:
    return {
        "label": getattr(sig, "label", None),
        "level": getattr(sig, "level", None),
        "color": getattr(sig, "color", None),
        "size": getattr(sig, "size", None),
        "flags": list(flags or []),
        "dow_base": dow_base,
    }


def _opener(openers: dict, key: str) -> dict:
    return pstate.get_opener(openers, key) or {}


def odds_block(game_id: str, lines: Iterable[GameLine], openers: dict) -> dict[str, dict[str, Any]]:
    """``{book: {spread, total, ml}}`` from this game's MAIN lines + the openers store."""
    per_book: dict[str, dict[str, dict[str, GameLine]]] = {}
    for ln in lines:
        if ln.game_id != game_id or not ln.is_main:
            continue
        per_book.setdefault(ln.book, {}).setdefault(ln.market, {})[ln.side] = ln
    out: dict[str, dict[str, Any]] = {}
    for book, markets in per_book.items():
        entry: dict[str, Any] = {}
        sp = markets.get("spread") or {}
        if sp:
            home, away = sp.get("home"), sp.get("away")
            ref = home or away
            op_home = _opener(openers, pstate.odds_key(game_id, "spread", "home", book))
            op_away = _opener(openers, pstate.odds_key(game_id, "spread", "away", book))
            home_line = home.line if home is not None else (-away.line if away is not None and away.line is not None else None)
            open_line = op_home.get("line")
            if open_line is None and op_away.get("line") is not None:
                open_line = -op_away["line"]
            entry["spread"] = {
                "home_line": home_line,
                "home_odds": home.odds if home else None,
                "away_odds": away.odds if away else None,
                "open_line": open_line,
                "open_odds": op_home.get("odds"),
                "updated_at": getattr(ref, "scraped_at", None),
            }
        to = markets.get("total") or {}
        if to:
            over, under = to.get("over"), to.get("under")
            ref = under or over
            op_under = _opener(openers, pstate.odds_key(game_id, "total", "under", book))
            op_over = _opener(openers, pstate.odds_key(game_id, "total", "over", book))
            entry["total"] = {
                "line": ref.line if ref else None,
                "over": over.odds if over else None,
                "under": under.odds if under else None,
                "open_line": op_under.get("line", op_over.get("line")),
                "open_under": op_under.get("odds"),
                "open_over": op_over.get("odds"),
                "updated_at": getattr(ref, "scraped_at", None),
            }
        ml = markets.get("ml") or {}
        if ml:
            home, away = ml.get("home"), ml.get("away")
            entry["ml"] = {
                "home": home.odds if home else None,
                "away": away.odds if away else None,
                "open_home": _opener(openers, pstate.odds_key(game_id, "ml", "home", book)).get("odds"),
                "open_away": _opener(openers, pstate.odds_key(game_id, "ml", "away", book)).get("odds"),
                "updated_at": getattr(home or away, "scraped_at", None),
            }
        if entry:
            out[book] = entry
    return out


def consensus_block(game_id: str, consensus: dict, openers: dict) -> dict[str, Any]:
    """``consensus`` = ``{(game_id, market): ConsensusLine}`` from ``pipeline.build``."""
    sp = consensus.get((game_id, "spread"))
    to = consensus.get((game_id, "total"))
    op_sp = _opener(openers, pstate.odds_key(game_id, "spread", "home", "consensus"))
    op_to = _opener(openers, pstate.odds_key(game_id, "total", "under", "consensus"))
    sp_now = getattr(sp, "line", None)
    to_now = getattr(to, "line", None)
    sp_open, to_open = op_sp.get("line"), op_to.get("line")
    n_books = max(getattr(sp, "n_books", 0) or 0, getattr(to, "n_books", 0) or 0)
    return {
        "spread_open": sp_open,
        "spread_now": sp_now,
        "total_open": to_open,
        "total_now": to_now,
        "move_s": (sp_now - sp_open) if sp_now is not None and sp_open is not None else None,
        "move_t": (to_now - to_open) if to_now is not None and to_open is not None else None,
        "ref_book": getattr(to, "ref_book", None) or getattr(sp, "ref_book", None),
        "n_books": n_books,
        "thin": n_books < 2,
    }


def fair_block(fair: Any, legacy: Optional[dict[str, Any]] = None, fair_v2: Any = None) -> dict[str, Any]:
    """From ``pipeline.model.fair.GameFair`` (may be None) + legacy derived cols.
    ``fair_v2`` (GameFair or FairV2) fills ``fair_total_v2``/``fair_spread_v2``/``confidence_v2``."""
    legacy = legacy or {}
    edges = []
    best_total = best_spread = None
    if fair is not None:
        for e in getattr(fair, "edges", []) or []:
            edges.append(sanitize(e))
        for market, slot in (("total", "best_total"), ("spread", "best_spread")):
            cands = [e for e in getattr(fair, "edges", []) or [] if e.market == market and e.edge_pts is not None]
            if cands:
                b = max(cands, key=lambda e: (e.edge_pts or 0.0, e.edge_prob or 0.0))
                if slot == "best_total":
                    best_total = sanitize(b)
                else:
                    best_spread = sanitize(b)
    return {
        "my_total": legacy.get("My_total"),
        "my_spread": legacy.get("My_spread"),
        "edge_legacy": legacy.get("Edge"),
        "edge_s_legacy": legacy.get("Edge_s"),
        "fair_total": getattr(fair, "fair_total", None),
        "fair_spread": getattr(fair, "fair_spread", None),
        "fair_total_v2": getattr(fair_v2, "fair_total", None),
        "fair_spread_v2": getattr(fair_v2, "fair_spread", None),
        "confidence_v2": getattr(fair_v2, "confidence", None),
        "confidence": getattr(fair, "confidence", None),
        "weather_driven": getattr(fair, "weather_driven", None),
        "edges": edges,
        "best_total": best_total,
        "best_spread": best_spread,
    }


def build_card(
    sport: str,
    game: Game,
    stadium: Optional[Stadium],
    home_team: Optional[Team],
    away_team: Optional[Team],
    fc: Optional[WeatherForecast],
    impact: Any,
    signal: Any,
    flags: Sequence[str],
    *,
    lines: Iterable[GameLine] = (),
    openers: Optional[dict] = None,
    consensus: Optional[dict] = None,
    fair: Any = None,
    legacy_derived: Optional[dict[str, Any]] = None,
    travel_alt: Optional[float] = None,
    home_temp: Optional[float] = None,
    away_temp: Optional[float] = None,
    roof_state: Optional[str] = None,
    avg_wind: Optional[float] = None,
    avg_wind_month: Optional[float] = None,
    alerts: Sequence[str] = (),
    run_id: Optional[str] = None,
    impact_v2: Any = None,
    fair_v2: Any = None,
    model_version: Optional[str] = None,
) -> dict[str, Any]:
    """One GameCard (ARCH §5). Pure: everything comes from the arguments.
    ``impact_v2``/``fair_v2`` fill the side-by-side v2 blocks; ``model_version`` stamps
    which model drives ``fair.edges`` (ALERT_MODEL)."""
    openers = openers or {}
    consensus = consensus or {}
    tz = game.tz or (stadium.timezone if stadium and stadium.timezone else "America/New_York")
    kickoff_local = game.kickoff_local if game.kickoff_local.tzinfo is not None else to_tz(game.kickoff_utc, tz)
    lines = [ln for ln in lines if ln.game_id == game.game_id]
    card = {
        "game_id": game.game_id,
        "sport": sport,
        "season": game.season,
        "week": game.week,
        "kickoff_utc": game.kickoff_utc,
        "kickoff_local": kickoff_local.isoformat(),
        "tz": tz,
        "date_label": date_label(kickoff_local),
        "time_label": time_label(kickoff_local),
        "home": _team_block(home_team, game.home_id),
        "away": _team_block(away_team, game.away_id),
        "neutral": bool(game.neutral),
        "status": game.status,
        "stadium": _stadium_block(stadium, roof_state, avg_wind, avg_wind_month),
        "travel_alt": travel_alt,
        "home_temp": home_temp,
        "away_temp": away_temp,
        "weather": _weather_block(fc, avg_wind if avg_wind is not None else (stadium.avg_wind_static if stadium else None)),
        "impact": _impact_block(impact, impact_v2, model_version),
        "signal": _signal_block(signal, flags),
        "odds": odds_block(game.game_id, lines, openers),
        "consensus": consensus_block(game.game_id, consensus, openers),
        "fair": fair_block(fair, legacy_derived, fair_v2),
        "alerts": list(alerts),
        "run_id": run_id,
    }
    return sanitize(card)


REQUIRED_CARD_KEYS = (
    "game_id", "sport", "season", "week", "kickoff_utc", "kickoff_local", "tz", "date_label", "time_label",
    "home", "away", "neutral", "status", "stadium", "travel_alt", "home_temp", "away_temp", "weather",
    "impact", "signal", "odds", "consensus", "fair", "alerts", "run_id",
)


# ---- slim table rows ---------------------------------------------------------------

def table_row(card: dict[str, Any]) -> dict[str, Any]:
    """Slim row for ``board.json`` / the Table view."""
    wx = card.get("weather") or {}
    st = card.get("stadium") or {}
    v1 = ((card.get("impact") or {}).get("v1")) or {}
    v2 = ((card.get("impact") or {}).get("v2")) or {}
    cons = card.get("consensus") or {}
    fair = card.get("fair") or {}
    best_t = fair.get("best_total") or {}
    best_s = fair.get("best_spread") or {}
    return {
        "game_id": card["game_id"],
        "sport": card["sport"],
        "season": card["season"],
        "week": card["week"],
        "kickoff_utc": card["kickoff_utc"],
        "date_label": card["date_label"],
        "time_label": card["time_label"],
        "game": f"{card['away']['short']} @ {card['home']['short']}",
        "home": card["home"]["name"],
        "away": card["away"]["name"],
        "neutral": card["neutral"],
        "stadium": st.get("name"),
        "roof_state": st.get("roof_state"),
        "temp_fg": wx.get("temp_fg"),
        "wind_fg": wx.get("wind_fg"),
        "gust_fg": wx.get("gust_fg"),
        "wind_dir_fg": wx.get("wind_dir_fg"),
        "rain_fg": wx.get("rain_fg"),
        "precip_prob": wx.get("precip_prob"),
        "gs_fg_pct": v1.get("gs_fg_pct"),
        "away_fg_pct": v1.get("away_fg_pct"),
        "signal": (card.get("signal") or {}).get("label"),
        "flags": (card.get("signal") or {}).get("flags") or [],
        "spread_open": cons.get("spread_open"),
        "spread_now": cons.get("spread_now"),
        "total_open": cons.get("total_open"),
        "total_now": cons.get("total_now"),
        "ref_book": cons.get("ref_book"),
        "n_books": cons.get("n_books"),
        "fair_total": fair.get("fair_total"),
        "fair_spread": fair.get("fair_spread"),
        "best_total_edge": best_t.get("edge_pts"),
        "best_total_book": best_t.get("book"),
        "best_spread_edge": best_s.get("edge_pts"),
        "best_spread_book": best_s.get("book"),
        "confidence": fair.get("confidence"),
        "gs_fg_v2": v2.get("gs_fg_pct"),
        "away_fg_v2": v2.get("away_fg_pct"),
        "fair_total_v2": fair.get("fair_total_v2"),
        "fair_spread_v2": fair.get("fair_spread_v2"),
        "wind_vol_fc": wx.get("wind_vol_fc"),
        "source": wx.get("source"),
        "lead_hours": wx.get("lead_hours"),
        "model_version": (card.get("impact") or {}).get("model_version"),
    }


# ---- meta ---------------------------------------------------------------------------

def books_status(
    counts: dict[str, dict[str, int]],
    requested: Sequence[str],
    baselines: Optional[dict[str, dict]] = None,
    now: Optional[str] = None,
    previous: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """``{book: {count, baseline, status, last_ok}}`` for the header chips.

    ``counts`` is ``RunContext.counts`` (``{book: {sport|sport.market: n}}``);
    ``baselines`` is ``{sport: scrape_baseline dict}`` (peaks keyed ``book|market``);
    ``previous`` is the last meta's ``books`` map (carries ``last_ok`` forward)."""
    previous = previous or {}
    out: dict[str, dict[str, Any]] = {}
    for book in requested:
        per = counts.get(book) or {}
        n = sum(v for k, v in per.items() if "." not in k)  # per-sport totals only
        peak = 0
        for bl in (baselines or {}).values():
            for key, p in (bl.get("peaks") or {}).items():
                if key.split("|", 1)[0] == book:
                    peak += int(p or 0)
        if n <= 0:
            status = "red"
        elif peak and n < VOLUME_AMBER_FRAC * peak:
            status = "amber"
        else:
            status = "green"
        prev = previous.get(book) or {}
        out[book] = {
            "count": n,
            "baseline": peak or None,
            "status": status,
            "last_ok": now if n > 0 else prev.get("last_ok"),
        }
    return out


def build_meta(
    ctx: Any,
    sport_counts: dict[str, int],
    books: dict[str, dict[str, Any]],
    *,
    season: Optional[int] = None,
    week: Optional[int] = None,
    model_version: str = "v1",
    finished_at: Optional[datetime] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """meta.json = RunMeta + books + next_run_eta (ARCH §5). ``ctx`` is a RunContext."""
    finished = finished_at or datetime.now(timezone.utc)
    meta = {
        "run_id": ctx.run_id,
        "sport": ctx.sport,
        "scope": ctx.scope,
        "season": season,
        "week": week,
        "git_sha": ctx.git_sha,
        "started_at": ctx.started_at,
        "finished_at": finished,
        "last_updated": utc_iso(finished),
        "duration_s": round((finished - ctx.started_at).total_seconds(), 3),
        "model_version": model_version,
        "sport_counts": dict(sport_counts),
        "stage_timings": dict(ctx.stage_timings),
        "counts": {k: dict(v) for k, v in ctx.counts.items()},
        "degradations": [sanitize(d) for d in ctx.degradations],
        "unresolved_names": list(ctx.unresolved_names),
        "books": books,
        "next_run_eta": next_run_eta(finished),
    }
    if extra:
        meta.update(extra)
    return sanitize(meta)


def slim_meta(meta: dict[str, Any]) -> dict[str, Any]:
    keys = ("run_id", "last_updated", "season", "week", "sport_counts", "git_sha", "model_version", "next_run_eta", "degradations")
    return {k: meta.get(k) for k in keys}


# ---- wx history (change-only) --------------------------------------------------------

def load_wx_history(state_dir: PathLike) -> dict:
    d = pstate._load(Path(state_dir) / WX_HISTORY_FILE)
    if not isinstance(d.get("series"), dict):
        d["series"] = {}
    d["schema_version"] = pstate.SCHEMA_VERSION
    return d


def wx_point(fc: Optional[WeatherForecast], impact: Any) -> Optional[list[Any]]:
    if fc is None:
        return None
    gs = getattr(impact, "gs_fg_pct", None)
    return [fc.lead_hours, fc.wind_fg, fc.gust_fg, fc.temp_fg, fc.rain_fg_mm, fc.precip_prob, gs]


def update_wx_history(history: dict, points: dict[str, list[Any]], now: str, cap: int = WX_HISTORY_CAP) -> list[str]:
    """Append ``[now, *point]`` for each game whose point differs from its last
    entry. Returns the game ids that changed (the D1 change-only set)."""
    store = history.setdefault("series", {})
    changed: list[str] = []
    for game_id, pt in points.items():
        pt = sanitize(pt)
        seq = store.setdefault(game_id, [])
        if not seq or seq[-1][1:] != pt:
            seq.append([now, *pt])
            changed.append(game_id)
            if len(seq) > cap:
                del seq[0:len(seq) - cap]
    return changed


def prune_wx_history(history: dict, active_game_ids: Iterable[str]) -> int:
    active = set(active_game_ids)
    store = history.setdefault("series", {})
    stale = [k for k in store if k not in active]
    for k in stale:
        del store[k]
    return len(stale)


def save_wx_history(state_dir: PathLike, history: dict) -> Path:
    history["schema_version"] = pstate.SCHEMA_VERSION
    return dump_json(Path(state_dir) / WX_HISTORY_FILE, history)


# ---- writers ------------------------------------------------------------------------

def snapshot_key(sport: str, season: Any, week: Any, run_id: str) -> str:
    return f"{SNAPSHOT_DIRNAME}/{sport}/{season}/{week}/{run_id}.json"


# ---- alerts feed + status (Phase 4) ------------------------------------------------

def build_alerts_feed(alerts_state: Optional[dict], meta: dict[str, Any], cap: int = pstate.FEED_CAP) -> dict[str, Any]:
    """``board/alerts_feed.json``: the last ``cap`` sent alerts (newest first),
    sharing ``alert_key`` with Telegram / D1; ``clv_pts`` and ``status`` joined
    from the alert records."""
    alerts_state = alerts_state or {}
    records = alerts_state.get("records") or {}
    items = []
    for it in list(alerts_state.get("feed") or [])[-cap:]:
        if not isinstance(it, dict):
            continue
        rec = records.get(it.get("alert_key")) if isinstance(records, dict) else None
        row = dict(it)
        if isinstance(rec, dict):
            row["clv_pts"] = rec.get("clv_pts", row.get("clv_pts"))
            row["status"] = rec.get("status", "open")
            row.setdefault("tier", rec.get("tier"))
        items.append(row)
    items.reverse()
    return sanitize({"meta": slim_meta(meta), "alerts": items, "n_open": sum(1 for r in records.values() if isinstance(r, dict) and r.get("status", "open") == "open")})


def status_run_row(run: dict[str, Any]) -> dict[str, Any]:
    """Slim ``runs`` entry from a d1_out.run_row dict (JSON columns decoded)."""
    out = {k: run.get(k) for k in ("run_id", "sport", "season", "week", "git_sha", "scope", "started_at", "finished_at",
                                   "duration_s", "status", "n_games", "n_lines", "n_alerts")}
    for col, key in (("stage_timings_json", "stage_timings"), ("counts_json", "counts"), ("degradations_json", "degradations")):
        v = run.get(col)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = None
        out[key] = v
    return out


def build_status(
    meta: dict[str, Any],
    run: dict[str, Any],
    *,
    previous: Optional[dict[str, Any]] = None,
    books: Optional[dict[str, dict[str, Any]]] = None,
    cap: int = STATUS_RUNS_CAP,
) -> dict[str, Any]:
    """``board/status.json``: last ``cap`` runs (this run first, carried from the
    previous status.json — the Status tab also has ``/api/runs`` for D1 truth),
    current degradations, unresolved names and per-book counts vs baseline."""
    prev_runs = [r for r in ((previous or {}).get("runs") or []) if isinstance(r, dict) and r.get("run_id") != run.get("run_id")]
    runs = [status_run_row(run), *prev_runs][:cap]
    return sanitize({
        "meta": slim_meta(meta),
        "runs": runs,
        "degradations": meta.get("degradations") or [],
        "unresolved_names": meta.get("unresolved_names") or [],
        "books": books if books is not None else (meta.get("books") or {}),
        "stage_timings": meta.get("stage_timings") or {},
        "counts": meta.get("counts") or {},
    })


def load_previous_status(state_dir: PathLike) -> Optional[dict[str, Any]]:
    d = pstate._load(Path(state_dir) / STATUS_FILE)
    return d or None


def season_week(cards: Sequence[dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    """(season, week) of the earliest kickoff among ``cards``."""
    if not cards:
        return None, None
    first = min(cards, key=lambda c: str(c.get("kickoff_utc") or ""))
    return first.get("season"), first.get("week")


def write_board(
    out_dir: PathLike,
    cards_by_sport: dict[str, list[dict[str, Any]]],
    meta: dict[str, Any],
    history: Optional[dict] = None,
    wx_history: Optional[dict] = None,
    snapshots_dir: Optional[PathLike] = None,
    alerts_feed: Optional[dict[str, Any]] = None,
    status: Optional[dict[str, Any]] = None,
    backtest: Optional[dict[str, Any]] = None,
) -> dict[str, Path]:
    """Write every board payload under ``out_dir`` (``board/`` prefix in R2) and one
    snapshot per sport under ``snapshots_dir``. Returns ``{r2_key: path}`` with
    ``board/meta.json`` LAST in insertion order (the publisher relies on it)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    slim = slim_meta(meta)
    rows: list[dict[str, Any]] = []
    for sport, cards in cards_by_sport.items():
        cards = sorted(cards, key=lambda c: (str(c.get("kickoff_utc") or ""), c.get("game_id") or ""))
        name = f"games_{sport}.json"
        written[f"{BOARD_DIRNAME}/{name}"] = dump_json(out / name, {"meta": slim, "games": cards})
        rows.extend(table_row(c) for c in cards)
        if snapshots_dir is not None and cards:
            season, week = season_week(cards)
            key = snapshot_key(sport, season, week, meta["run_id"])
            written[key] = dump_json(Path(snapshots_dir) / key.split("/", 1)[1], {"meta": slim, "games": cards})
    written[f"{BOARD_DIRNAME}/{BOARD_FILE}"] = dump_json(out / BOARD_FILE, {"meta": slim, "rows": rows})
    if history is not None:
        payload = {"schema_version": pstate.SCHEMA_VERSION, "run_id": meta["run_id"],
                   "series": history.get("series") or {}, "fair_series": history.get("fair_series") or {}}
        written[f"{BOARD_DIRNAME}/{pstate.HISTORY_FILE}"] = dump_json(out / pstate.HISTORY_FILE, payload)
    if wx_history is not None:
        payload = {"schema_version": pstate.SCHEMA_VERSION, "run_id": meta["run_id"], "series": wx_history.get("series") or {}}
        written[f"{BOARD_DIRNAME}/{WX_HISTORY_FILE}"] = dump_json(out / WX_HISTORY_FILE, payload)
    if alerts_feed is not None:
        written[f"{BOARD_DIRNAME}/{ALERTS_FEED_FILE}"] = dump_json(out / ALERTS_FEED_FILE, alerts_feed)
    if status is not None:
        written[f"{BOARD_DIRNAME}/{STATUS_FILE}"] = dump_json(out / STATUS_FILE, status)
    if backtest is not None:
        written[f"{BOARD_DIRNAME}/{BACKTEST_FILE}"] = dump_json(out / BACKTEST_FILE, backtest)
    written[f"{BOARD_DIRNAME}/{META_FILE}"] = dump_json(out / META_FILE, meta)
    return written


__all__ = [
    "BOARD_DIRNAME", "SNAPSHOT_DIRNAME", "META_FILE", "BOARD_FILE", "WX_HISTORY_FILE", "ALERTS_FEED_FILE", "STATUS_FILE",
    "BACKTEST_FILE",
    "REQUIRED_CARD_KEYS", "build_alerts_feed", "build_status", "status_run_row", "load_previous_status",
    "sanitize", "dump_json", "next_backstop", "next_run_eta",
    "build_card", "odds_block", "consensus_block", "fair_block", "table_row",
    "books_status", "build_meta", "slim_meta",
    "load_wx_history", "wx_point", "update_wx_history", "prune_wx_history", "save_wx_history",
    "snapshot_key", "season_week", "write_board",
]
