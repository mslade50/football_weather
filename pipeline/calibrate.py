"""Refit the v2 impact coefficients against closing lines (PLAN Phase 6, ARCH §7.5).

    python -m pipeline.calibrate [--input data/backtest/games.parquet ...] [--out data/calibration.json]
                                 [--min-weeks 4] [--backtest data/board/backtest.json] [--dry-run] [--force]

What is refit (the ``v2`` block of ``data/calibration.json`` only):

    wind curve        wind_offset_mph, wind_coeff, wind_exp, wind_cap
    gust blend        gust_blend
    rain threshold    rain_prob_min
    altitude slope    alt_slope_per_m
    heat-away delta   heat_away_delta_f

``head_weight``, ``dir_mult_weak``, ``alt_base_m`` and ``alt_cap`` are carried through
unchanged. **v1 constants in ``pipeline/model/config.py`` are never touched** — this
module only reads them (through ``impact.compute_impact_v1``) to report v1's error
next to v2's; it never imports ``config`` for writing and never edits any ``.py``.

Objective: the closing-total error of the v2 fair line,

    fair_total = total_open * (1 + gs_fg_v2 / 100)      -> (fair_total - total_close)^2
    fair_spread = spread_open * (1 + away_fg_v2 / 100)  -> (fair_spread - spread_close)^2

summed over every matched game (mean squared, totals + spreads), with a mild ridge
toward the coefficients we started from (``--ridge``) so a thin sample cannot fling a
coefficient to a bound. The fit is a bounded coordinate descent (no scipy): each
coefficient is nudged by a shrinking step while the loss improves. Deterministic.

Data: per-game rows with the forecast at the lead the alerts fire on and the
opening/closing lines — ``pipeline.backtest`` writes them to ``data/backtest/games.parquet``
and as ``games[]`` inside ``board/backtest.json``. Flat rows or GameCard-shaped rows
(``weather.*``, ``stadium.*``, ``consensus.*``, ``closing.*``) both load. v1's error is
scored with the RUN month (``_run_month``: ``src_forecast`` snapshot stamp / ``fetched_at`` /
kickoff − lead), falling back to the game month when the row carries no run timestamp.

Guard: at least ``--min-weeks`` (default 4) distinct (season, week) pairs with a closing
total, else nothing is written (exit 2) unless ``--force``.

Promotion rule (manual, documented here and in ``model/config.py``): set
``ALERT_MODEL = "v2"`` in ``pipeline/model/config.py`` ONLY when v2 CLV >= v1 CLV over
>= 4 weeks of alerts. ``--backtest`` reads ``clv.by_model`` from ``backtest.json`` and
writes the verdict into ``calibration.json["promotion"]`` — it never flips the flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline.model import config as C
from pipeline.model import impact as I  # noqa: N812
from utils.env import load_repo_dotenv

SCHEMA_VERSION = 1
MIN_WEEKS = 4
DEFAULT_OUT = I.CALIBRATION_PATH
DEFAULT_INPUTS = (
    Path("data/backtest/games.parquet"),
    Path("data/backtest/games.csv"),
    Path("data/board/backtest.json"),
    Path("site/web/data/backtest.json"),
)
NOTE = ("v2 impact coefficients (ARCH 7.5). Refit weekly by calibrate.yml; v1 constants in "
        "pipeline/model/config.py are never refit. spread_key_prob / pts_prob_total may be added here "
        "to override the shipped pts->prob tables.")
PROMOTION_RULE = ("Set ALERT_MODEL = \"v2\" in pipeline/model/config.py only when v2 CLV >= v1 CLV over "
                  ">= 4 weeks of alerts (manual merge; calibrate never edits config.py).")

# (lower, upper) bounds per refit coefficient; everything else in V2_DEFAULTS is carried through.
BOUNDS: dict[str, tuple[float, float]] = {
    "gust_blend": (0.3, 1.0),
    "wind_offset_mph": (6.0, 14.0),
    "wind_coeff": (0.2, 1.5),
    "wind_exp": (0.8, 1.6),
    "wind_cap": (6.0, 16.0),
    "rain_prob_min": (0.2, 0.8),
    "alt_slope_per_m": (0.001, 0.008),
    "heat_away_delta_f": (5.0, 25.0),
}
REFIT_KEYS: tuple[str, ...] = tuple(BOUNDS)
TOTAL_KEYS = ("gust_blend", "wind_offset_mph", "wind_coeff", "wind_exp", "wind_cap", "rain_prob_min")
SPREAD_KEYS = ("alt_slope_per_m", "heat_away_delta_f")

SPORT_ALIASES = {"nfl": "nfl", "cfb": "cfb", "ncaaf": "cfb", "ncaa": "cfb", "college": "cfb"}


# ---- rows ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    sport: str
    season: Optional[int]
    week: Optional[int]
    month: Optional[int]
    temp_fg: Optional[float]
    wind_fg: Optional[float]
    gust_fg: Optional[float]
    rain_fg: Optional[float]
    precip_prob: Optional[float]
    precip_prob_ens: Optional[float]
    wind_dir_deg: Optional[float]
    wind_dir_fg: Optional[str]
    orientation_deg: Optional[float]
    weakest_wind_effect: Optional[str]
    travel_alt: Optional[float]
    home_temp: Optional[float]
    away_temp: Optional[float]
    roof_state: Optional[str]
    total_open: Optional[float]
    total_close: Optional[float]
    spread_open: Optional[float]
    spread_close: Optional[float]
    # month of the RUN that produced the forecast (v1 rain suppression keys on it); None -> game month
    run_month: Optional[int] = None

    @property
    def has_total(self) -> bool:
        return self.total_open is not None and self.total_close is not None

    @property
    def has_spread(self) -> bool:
        return self.spread_open is not None and self.spread_close is not None and self.spread_open != 0.0


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    try:  # numpy scalars
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _str(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


def _get(d: dict[str, Any], *names: str) -> Any:
    """First present key among ``names``; dotted names walk nested dicts (``weather.wind_fg``)."""
    for name in names:
        cur: Any = d
        ok = True
        for part in name.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None and not (isinstance(cur, float) and math.isnan(cur)):
            return cur
    return None


def _month(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.month
    s = _str(v)
    if s and len(s) >= 7 and s[4] == "-":
        try:
            return int(s[5:7])
        except ValueError:
            return None
    return _int(v) if _int(v) is not None and 1 <= (_int(v) or 0) <= 12 else None


def _parse_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = _str(v)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_month(d: dict[str, Any]) -> Optional[int]:
    """Month of the RUN that produced the forecast. v1's September rain suppression keys on
    the generator's clock, not the kickoff (``impact.compute_impact_v1`` ``month``), so an
    October game forecast in a September run must be scored with month=9.

    Sources, first hit wins: an explicit ``run_month`` / ``commit_date`` / ``run_date`` /
    ``run_time`` column; a GameCard's ``weather.fetched_at`` (the merge ``run_time``);
    the snapshot timestamp ``pipeline.backtest`` stamps into ``src_forecast``
    (``snapshot:<iso>``); else kickoff minus the forecast lead (``lead_fc`` /
    ``lead_hours``). None when nothing is available — ``_v1_pcts`` then falls back to
    the game month, which is only wrong for October kickoffs forecast in September."""
    m = _month(_get(d, "run_month", "commit_date", "run_date", "run_time", "fetched_at", "weather.fetched_at"))
    if m is not None:
        return m
    src = _str(_get(d, "src_forecast"))
    if src and src.startswith("snapshot:"):
        m = _month(src.split(":", 1)[1])
        if m is not None:
            return m
    kick = _parse_dt(_get(d, "kickoff_utc", "kickoff"))
    lead = _num(_get(d, "lead_fc", "lead_hours", "weather.lead_hours"))
    if kick is not None and lead is not None and lead >= 0:
        return (kick - timedelta(hours=lead)).month
    return None


def row_from_dict(d: dict[str, Any]) -> Optional[Row]:
    """Flat backtest row or GameCard-shaped row -> ``Row`` (None when the sport is unknown)."""
    sport = SPORT_ALIASES.get(str(_get(d, "sport") or "").strip().lower())
    if sport is None:
        return None
    return Row(
        sport=sport,
        season=_int(_get(d, "season")),
        week=_int(_get(d, "week")),
        month=_month(_get(d, "month", "kickoff_utc", "kickoff", "date")),
        temp_fg=_num(_get(d, "temp_fg", "temp_fc", "weather.temp_fg")),
        wind_fg=_num(_get(d, "wind_fg", "wind_fc", "weather.wind_fg")),
        gust_fg=_num(_get(d, "gust_fg", "gust_fc", "weather.gust_fg")),
        rain_fg=_num(_get(d, "rain_fg", "rain_fc", "rain_fg_mm", "weather.rain_fg")),
        precip_prob=_num(_get(d, "precip_prob", "weather.precip_prob")),
        precip_prob_ens=_num(_get(d, "precip_prob_ens", "weather.precip_prob_ens")),
        wind_dir_deg=_num(_get(d, "wind_dir_deg", "weather.wind_dir_deg")),
        wind_dir_fg=_str(_get(d, "wind_dir_fg", "wind_dir_fc", "weather.wind_dir_fg")),
        orientation_deg=_num(_get(d, "orientation_deg", "orient_deg", "stadium.orient_deg", "stadium.orientation_deg")),
        weakest_wind_effect=_str(_get(d, "weakest_wind_effect", "stadium.weakest_wind_effect")),
        travel_alt=_num(_get(d, "travel_alt", "travel_alt_m")),
        home_temp=_num(_get(d, "home_temp")),
        away_temp=_num(_get(d, "away_temp")),
        roof_state=_str(_get(d, "roof_state", "stadium.roof_state")),
        total_open=_num(_get(d, "total_open", "consensus.total_open", "total_at_forecast")),
        total_close=_num(_get(d, "total_close", "closing_total", "closing.total", "consensus.total_close")),
        spread_open=_num(_get(d, "spread_open", "consensus.spread_open")),
        spread_close=_num(_get(d, "spread_close", "closing_spread", "closing.spread", "consensus.spread_close")),
        run_month=_run_month(d),
    )


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("games", "rows", "matched_games", "items"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def load_rows(paths: Sequence[Path]) -> list[Row]:
    """Rows from ``.parquet`` / ``.csv`` (pandas) or ``.json`` (list or ``{games: [...]}``)."""
    out: list[Row] = []
    for p in paths:
        p = Path(p)
        if not p.is_file():
            continue
        if p.suffix.lower() == ".json":
            raw = _rows_from_payload(json.loads(p.read_text(encoding="utf-8")))
        else:
            import pandas as pd

            df = pd.read_parquet(p) if p.suffix.lower() == ".parquet" else pd.read_csv(p)
            raw = df.to_dict(orient="records")
        for d in raw:
            r = row_from_dict(d)
            if r is not None:
                out.append(r)
    return out


def usable(rows: Iterable[Row]) -> list[Row]:
    return [r for r in rows if r.has_total and r.wind_fg is not None and r.temp_fg is not None]


def weeks_of(rows: Iterable[Row]) -> list[tuple[int, int]]:
    return sorted({(r.season or 0, r.week or 0) for r in rows if r.has_total})


# ---- model evaluation --------------------------------------------------------------------

def _v2(r: Row, cal: dict[str, float]) -> I.ImpactV2:
    return I.compute_impact_v2(
        r.sport, r.temp_fg, r.wind_fg, r.gust_fg, r.rain_fg, r.precip_prob, r.travel_alt, r.home_temp, r.away_temp,
        wind_dir_deg=r.wind_dir_deg, wind_dir_fg=r.wind_dir_fg, orientation_deg=r.orientation_deg,
        weakest_wind_effect=r.weakest_wind_effect, precip_prob_ens=r.precip_prob_ens, roof_state=r.roof_state, cal=cal,
    )


def _v1_pcts(r: Row) -> tuple[float, float]:
    # v1 rain suppression keys on the RUN month (see _run_month); the game month is the
    # documented fallback when the row carries no run timestamp.
    month = r.run_month if r.run_month is not None else r.month
    imp = I.compute_impact_v1(r.sport, month, r.temp_fg, r.wind_fg, r.rain_fg or 0.0, r.travel_alt, r.away_temp,
                              roof_state=r.roof_state)
    return imp.gs_fg_pct, imp.away_fg_pct


def _sq_err(rows: Sequence[Row], gs_of: Any, away_of: Any) -> tuple[float, float, int, int]:
    """(mean sq total err, mean sq spread err, n_total, n_spread) for a percent-impact function pair."""
    st = ss = 0.0
    nt = ns = 0
    for r in rows:
        if r.has_total:
            gs = gs_of(r)
            st += (r.total_open * (1.0 + gs / 100.0) - r.total_close) ** 2
            nt += 1
        if r.has_spread:
            away = away_of(r)
            ss += (r.spread_open * (1.0 + away / 100.0) - r.spread_close) ** 2
            ns += 1
    return (st / nt if nt else 0.0), (ss / ns if ns else 0.0), nt, ns


def loss_v2(rows: Sequence[Row], cal: dict[str, float]) -> tuple[float, float]:
    """(total mse, spread mse) of v2 under ``cal``."""
    cache: dict[int, I.ImpactV2] = {}

    def imp(r: Row) -> I.ImpactV2:
        k = id(r)
        if k not in cache:
            cache[k] = _v2(r, cal)
        return cache[k]

    t, s, _, _ = _sq_err(rows, lambda r: imp(r).gs_fg_pct, lambda r: imp(r).away_fg_pct)
    return t, s


def loss_v1(rows: Sequence[Row]) -> tuple[float, float]:
    t, s, _, _ = _sq_err(rows, lambda r: _v1_pcts(r)[0], lambda r: _v1_pcts(r)[1])
    return t, s


def loss_baseline(rows: Sequence[Row]) -> tuple[float, float]:
    """No weather adjustment at all (fair == open)."""
    t, s, _, _ = _sq_err(rows, lambda r: 0.0, lambda r: 0.0)
    return t, s


def _ridge(cal: dict[str, float], start: dict[str, float], keys: Sequence[str]) -> float:
    acc = 0.0
    for k in keys:
        lo, hi = BOUNDS[k]
        acc += ((cal[k] - start[k]) / (hi - lo)) ** 2
    return acc


def objective(rows: Sequence[Row], cal: dict[str, float], start: dict[str, float], ridge: float,
              keys: Sequence[str] = REFIT_KEYS) -> float:
    t, s = loss_v2(rows, cal)
    return (t + s) * (1.0 + ridge * _ridge(cal, start, keys))


def clamp(k: str, v: float) -> float:
    lo, hi = BOUNDS[k]
    return min(hi, max(lo, v))


@dataclass
class FitResult:
    cal: dict[str, float]
    start: dict[str, float]
    loss_before: float
    loss_after: float
    rounds: int
    evaluations: int
    changed: list[str]


def fit(rows: Sequence[Row], start: dict[str, float], *, keys: Sequence[str] = REFIT_KEYS, ridge: float = 0.1,
        rounds: int = 40, tol: float = 1e-9) -> FitResult:
    """Bounded coordinate descent with a halving step. Only ``keys`` move; the rest of
    ``start`` is carried through untouched."""
    cal = dict(start)
    have_spread = any(r.has_spread for r in rows)
    active = [k for k in keys if have_spread or k not in SPREAD_KEYS]
    steps = {k: (BOUNDS[k][1] - BOUNDS[k][0]) / 8.0 for k in active}
    evals = 0

    def J(c: dict[str, float]) -> float:
        nonlocal evals
        evals += 1
        return objective(rows, c, start, ridge, keys)

    best = J(cal)
    before = best
    n_rounds = 0
    for _ in range(rounds):
        n_rounds += 1
        improved = False
        for k in active:
            for sign in (1.0, -1.0):
                trial = dict(cal)
                trial[k] = clamp(k, cal[k] + sign * steps[k])
                if trial[k] == cal[k]:
                    continue
                val = J(trial)
                if val < best - tol:
                    cal, best, improved = trial, val, True
                    break
        if not improved:
            for k in active:
                steps[k] *= 0.5
            if all(s < 1e-4 * (BOUNDS[k][1] - BOUNDS[k][0]) for k, s in steps.items()):
                break
    changed = [k for k in active if abs(cal[k] - start[k]) > 1e-12]
    return FitResult({k: round(float(v), 6) for k, v in cal.items()}, dict(start), before, best, n_rounds, evals, changed)


# ---- promotion verdict (from backtest.json alerts_clv.by_model) -----------------------------

def clv_block(backtest: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalise the CLV block of ``board/backtest.json`` (``alerts_clv`` from
    ``pipeline.backtest.alerts_clv``, or an older ``clv`` block) to
    ``{"n", "weeks", "by_model": {model: {"n", "avg_clv", "pos_frac"}}}``. ``by_model`` may
    be a list of ``{key, n, avg_clv, pos_frac}`` rows or a dict keyed by model; ``weeks``
    falls back to the distinct (season, week) pairs of the settled alerts."""
    out: dict[str, Any] = {"n": 0, "weeks": 0, "by_model": {}}
    if not isinstance(backtest, dict):
        return out
    clv = backtest.get("alerts_clv")
    if not isinstance(clv, dict):
        clv = backtest.get("clv")
    if not isinstance(clv, dict):
        return out
    raw = clv.get("by_model")
    models: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("key") is not None:
                models[str(row["key"])] = row
    elif isinstance(raw, dict):
        models = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    for k, m in models.items():
        out["by_model"][k] = {"n": _int(m.get("n")), "avg_clv": _num(m.get("avg_clv", m.get("avg"))),
                              "pos_frac": _num(m.get("pos_frac", m.get("pos_rate", m.get("pos_pct"))))}
    out["n"] = _int(clv.get("n")) or sum(v["n"] or 0 for v in out["by_model"].values())
    weeks = _int(clv.get("weeks"))
    if weeks is None:
        pairs = {(a.get("season"), a.get("week")) for a in (clv.get("alerts") or []) if isinstance(a, dict) and a.get("week") is not None}
        weeks = len(pairs)
    out["weeks"] = weeks
    return out


def promotion_verdict(backtest: Optional[dict[str, Any]], min_weeks: int = MIN_WEEKS) -> dict[str, Any]:
    out: dict[str, Any] = {"rule": PROMOTION_RULE, "alert_model_now": C.alert_model(), "eligible": False,
                           "v1_clv_avg": None, "v2_clv_avg": None, "weeks": 0}
    blk = clv_block(backtest)
    if not blk["by_model"]:
        return out
    v1, v2 = blk["by_model"].get("v1") or {}, blk["by_model"].get("v2") or {}
    a1, a2 = v1.get("avg_clv"), v2.get("avg_clv")
    weeks = blk["weeks"] or 0
    out.update(v1_clv_avg=a1, v2_clv_avg=a2, weeks=weeks, v1_n=v1.get("n"), v2_n=v2.get("n"))
    out["eligible"] = bool(a1 is not None and a2 is not None and weeks >= min_weeks and a2 >= a1)
    return out


# ---- output ------------------------------------------------------------------------------

def build_calibration(res: FitResult, rows: Sequence[Row], *, inputs: Sequence[Path], ridge: float,
                      backtest: Optional[dict[str, Any]] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    lt_b, ls_b = loss_baseline(rows)
    lt_1, ls_1 = loss_v1(rows)
    lt_0, ls_0 = loss_v2(rows, res.start)
    lt_2, ls_2 = loss_v2(rows, res.cal)
    n_total = sum(1 for r in rows if r.has_total)
    n_spread = sum(1 for r in rows if r.has_spread)
    wk = weeks_of(rows)
    v2_block = {k: float(res.cal.get(k, C.V2_DEFAULTS[k])) for k in C.V2_DEFAULTS}
    return {
        "schema_version": SCHEMA_VERSION,
        "note": NOTE,
        "v2": v2_block,
        "fit": {
            "fitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inputs": [str(p) for p in inputs],
            "n_games": len(rows), "n_total": n_total, "n_spread": n_spread,
            "n_weeks": len(wk), "weeks": [f"{s}-{w}" for s, w in wk],
            "refit_keys": list(REFIT_KEYS), "changed": res.changed, "bounds": {k: list(v) for k, v in BOUNDS.items()},
            "ridge": ridge, "rounds": res.rounds, "evaluations": res.evaluations,
            "objective_before": res.loss_before, "objective_after": res.loss_after,
            "mse": {
                "baseline": {"total": lt_b, "spread": ls_b},
                "v1": {"total": lt_1, "spread": ls_1},
                "v2_before": {"total": lt_0, "spread": ls_0},
                "v2_after": {"total": lt_2, "spread": ls_2},
            },
            "start": {k: res.start[k] for k in REFIT_KEYS},
        },
        "promotion": promotion_verdict(backtest),
    }


def validate_calibration(data: dict[str, Any]) -> list[str]:
    """Schema problems (empty list == valid). Mirrors what ``impact.load_v2_calibration`` needs."""
    errs: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errs.append("schema_version")
    v2 = data.get("v2")
    if not isinstance(v2, dict):
        return errs + ["v2 block missing"]
    if set(v2) != set(C.V2_DEFAULTS):
        errs.append(f"v2 keys {sorted(set(v2) ^ set(C.V2_DEFAULTS))}")
    for k, v in v2.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            errs.append(f"v2.{k} not numeric")
        elif k in BOUNDS and not (BOUNDS[k][0] <= float(v) <= BOUNDS[k][1]):
            errs.append(f"v2.{k} out of bounds")
    return errs


def write_calibration(path: Path, data: dict[str, Any]) -> Path:
    errs = validate_calibration(data)
    if errs:
        raise ValueError(f"refusing to write invalid calibration: {errs}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def config_fingerprint() -> str:
    """sha256 of pipeline/model/config.py — tests assert it is identical before/after a run."""
    return hashlib.sha256(Path(C.__file__).read_bytes()).hexdigest()


# ---- CLI ----------------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.calibrate", description=__doc__.split("\n\n")[0])
    p.add_argument("--input", type=Path, action="append", default=None,
                   help="backtest rows (.parquet/.csv/.json); repeatable; default: first existing of "
                        + ", ".join(str(x) for x in DEFAULT_INPUTS))
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--start", type=Path, default=None, help="calibration.json to start from (default: --out if it exists)")
    p.add_argument("--backtest", type=Path, default=None, help="board/backtest.json for the CLV promotion verdict")
    p.add_argument("--min-weeks", type=int, default=MIN_WEEKS)
    p.add_argument("--ridge", type=float, default=0.1)
    p.add_argument("--rounds", type=int, default=40)
    p.add_argument("--force", action="store_true", help="fit even with fewer than --min-weeks weeks")
    p.add_argument("--dry-run", action="store_true", help="print the fit, write nothing")
    return p.parse_args(argv)


def _load_json(path: Optional[Path]) -> Optional[dict[str, Any]]:
    if path is None or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_repo_dotenv()
    args = parse_args(argv)
    fp_before = config_fingerprint()
    inputs = list(args.input) if args.input else [p for p in DEFAULT_INPUTS if p.is_file()][:1]
    rows = usable(load_rows(inputs))
    wk = weeks_of(rows)
    print(f"  calibrate: {len(rows)} usable game(s) over {len(wk)} week(s) from {[str(p) for p in inputs] or 'nothing'}")
    if not rows:
        print("  calibrate: no rows with forecast + opening + closing total; nothing to fit")
        return 2
    if len(wk) < args.min_weeks and not args.force:
        print(f"  calibrate: need >= {args.min_weeks} weeks (have {len(wk)}); refusing to refit (use --force)")
        return 2
    start_path = args.start or (args.out if Path(args.out).is_file() else None)
    start = I.load_v2_calibration(start_path, use_cache=False) if start_path else dict(C.V2_DEFAULTS)
    res = fit(rows, start, ridge=args.ridge, rounds=args.rounds)
    data = build_calibration(res, rows, inputs=inputs, ridge=args.ridge, backtest=_load_json(args.backtest))
    mse = data["fit"]["mse"]
    print(f"  objective {res.loss_before:.4f} -> {res.loss_after:.4f} in {res.rounds} round(s) / {res.evaluations} eval(s)")
    print(f"  total mse: baseline {mse['baseline']['total']:.3f} · v1 {mse['v1']['total']:.3f} · "
          f"v2 {mse['v2_before']['total']:.3f} -> {mse['v2_after']['total']:.3f}")
    for k in REFIT_KEYS:
        flag = " *" if k in res.changed else ""
        print(f"    {k:<18} {start[k]:>10.5f} -> {res.cal[k]:>10.5f}{flag}")
    promo = data["promotion"]
    print(f"  promotion: eligible={promo['eligible']} v1 {promo['v1_clv_avg']} v2 {promo['v2_clv_avg']} "
          f"weeks {promo['weeks']} (ALERT_MODEL now {promo['alert_model_now']}) — {PROMOTION_RULE}")
    if args.dry_run:
        print("  dry-run: nothing written")
    else:
        write_calibration(args.out, data)
        print(f"  wrote {args.out}")
    assert config_fingerprint() == fp_before, "pipeline/model/config.py changed during calibrate — v1 is frozen"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
