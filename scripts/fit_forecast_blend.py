"""Fit the lead-weighted climatology shrinkage curves (ARCH §6) from real forecast skill.

    python scripts/fit_forecast_blend.py --cache-dir CACHE [--seasons 2024,2025] [--sample-every 4]
        [--stadiums id,id,..] [--models best_match] [--throttle 1.5] [--out data/calibration.json] [--dry-run]

For a sample of ~40 stadiums (every k-th in-scope stadium sorted by id: NFL + FBS,
coast / inland / mountain mixed by construction; or ``--stadiums``) and the Sep–Dec
season(s):

* forecasts 1..7 days ahead for every hour from Open-Meteo's Previous Runs API
  (``hourly=wind_speed_10m_previous_dayN,temperature_2m_previous_dayN,
  precipitation_probability_previous_dayN`` — falls back to
  ``precipitation_previous_dayN >= 1 mm`` when the PoP variable is null);
* the truth from the ERA5 archive (hourly wind / temperature / precipitation, the
  same source the climatology cells come from);
* the base rate from the ``data/climatology.csv`` cells (stadium × ISO week × solar
  time-of-day bin) — run ``python -m pipeline.stadiums.climatology`` first.

Samples are 3-hour kickoff-style windows (every 3 h, all in-season hours). For each
variable and lead the MAE-optimal shrinkage w in ``w·fc + (1−w)·climo`` (grid 0..1
step 0.01) is found with stadiums pooled; the curve written to
``calibration.json["forecast_blend"]["weights"]`` keeps w = 1 up to 48 h and
follows the fitted points (monotone non-increasing) at 72..168 h, floor = w(168 h).
Fit stats (n, MAE forecast-only / blended / climatology-only per lead) go into
``forecast_blend.fit`` and are printed.

One request per stadium × season × source, 1.5 s throttle, json cache under
``--cache-dir`` (resume-safe; the archive cache keys are shared with
``stadiums/climatology.py`` when the span matches). If the previous-runs API is
unavailable or throttled, nothing is written and the defaults stay in force.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.stadiums.build_stadiums import Fetcher  # noqa: E402
from pipeline.stadiums.climatology import (  # noqa: E402
    ARCHIVE_URL,
    DEFAULT_END,
    DEFAULT_START,
    _stadium_points,
    hourly_params,
)
from pipeline.weather import climatology_blend as CB  # noqa: E402

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = tuple(range(1, 8))
KIND_VARS = {"wind": "wind_speed_10m", "temp": "temperature_2m", "rain_prob": "precipitation_probability"}
WINDOW_H = 3
SEASON_START = (9, 1)
SEASON_END = (12, 31)
FULL_H = 48.0
FLOOR_H = 168.0
GRID = [i / 100.0 for i in range(101)]
MIN_SAMPLES = 200


def previous_runs_params(lat: float, lon: float, start: str, end: str, models: str) -> dict[str, str]:
    hourly = [f"{var}_previous_day{n}" for var in KIND_VARS.values() for n in LEADS]
    hourly += [f"precipitation_previous_day{n}" for n in LEADS]
    return {
        "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}", "start_date": start, "end_date": end,
        "hourly": ",".join(hourly), "models": models,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "mm", "timezone": "UTC",
    }


def _parse_hour(s: str) -> dt.datetime:
    return dt.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), tzinfo=dt.timezone.utc)


def _mean(xs: list[Optional[float]]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if len(vals) == len(xs) and vals else None


def window_samples(
    sid: str, lon: float, fc_hourly: dict[str, Any], truth_hourly: dict[str, Any], table: CB.ClimoTable
) -> dict[str, dict[int, list[tuple[float, float, float]]]]:
    """-> {kind: {lead_days: [(forecast, climo, actual), ...]}} over 3-h windows every 3 h."""
    out: dict[str, dict[int, list[tuple[float, float, float]]]] = {k: defaultdict(list) for k in KIND_VARS}
    ft = {t: i for i, t in enumerate(fc_hourly.get("time") or [])}
    tt = {t: i for i, t in enumerate(truth_hourly.get("time") or [])}
    times = sorted(set(ft) & set(tt))
    if not times:
        return out
    pop_ok = {n: any(v is not None for v in (fc_hourly.get(f"precipitation_probability_previous_day{n}") or [])) for n in LEADS}
    for ts in times:
        t0 = _parse_hour(ts)
        if t0.hour % WINDOW_H:
            continue
        keys = [(t0 + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(WINDOW_H)]
        if not all(k in ft and k in tt for k in keys):
            continue
        cell = table.lookup(t0 + dt.timedelta(hours=1), stadium_id=sid)
        if cell is None:
            continue
        ti = [tt[k] for k in keys]
        fi = [ft[k] for k in keys]
        act_wind = _mean([truth_hourly["wind_speed_10m"][i] for i in ti])
        act_temp = _mean([truth_hourly["temperature_2m"][i] for i in ti])
        precs = [truth_hourly["precipitation"][i] for i in ti]
        act_rain = None if any(p is None for p in precs) else (1.0 if sum(precs) >= CB.RAIN_MM else 0.0)
        for n in LEADS:
            fw = _mean([fc_hourly.get(f"wind_speed_10m_previous_day{n}", [None])[i] if i < len(fc_hourly.get(f"wind_speed_10m_previous_day{n}", [])) else None for i in fi])
            if fw is not None and act_wind is not None and cell.wind_mean is not None:
                out["wind"][n].append((fw, cell.wind_mean, act_wind))
            ftp = _mean([fc_hourly.get(f"temperature_2m_previous_day{n}", [None])[i] if i < len(fc_hourly.get(f"temperature_2m_previous_day{n}", [])) else None for i in fi])
            if ftp is not None and act_temp is not None and cell.temp_mean is not None:
                out["temp"][n].append((ftp, cell.temp_mean, act_temp))
            if act_rain is None or cell.rain_freq is None:
                continue
            if pop_ok[n]:
                pp = _mean([fc_hourly[f"precipitation_probability_previous_day{n}"][i] for i in fi])
                fp = None if pp is None else max(0.0, min(1.0, pp / 100.0))
            else:
                col = fc_hourly.get(f"precipitation_previous_day{n}") or []
                amt = [col[i] if i < len(col) else None for i in fi]
                fp = None if any(a is None for a in amt) else (1.0 if sum(amt) >= CB.RAIN_MM else 0.0)
            if fp is not None:
                out["rain_prob"][n].append((fp, cell.rain_freq, act_rain))
    return out


def fit_weight(samples: list[tuple[float, float, float]], metric: str = "mae") -> dict[str, float]:
    """Loss-optimal w on the grid; returns n, w, mae_fc (w=1), mae_blend, mae_climo (w=0).
    ``metric`` = ``mae`` (wind / temp) or ``brier`` (rain_prob vs a 0/1 outcome: MAE is not a
    proper score for probabilities, so the probability curve is fitted on the Brier score;
    the MAE columns are still reported)."""
    n = len(samples)

    def mae(w: float) -> float:
        return sum(abs(w * f + (1.0 - w) * c - a) for f, c, a in samples) / n

    def brier(w: float) -> float:
        return sum((w * f + (1.0 - w) * c - a) ** 2 for f, c, a in samples) / n

    loss = brier if metric == "brier" else mae
    best_w, best = 1.0, loss(1.0)
    for w in GRID:
        m = loss(w)
        if m < best - 1e-12:
            best_w, best = w, m
    out = {"n": n, "w": best_w, "metric": metric, "mae_fc": round(mae(1.0), 4), "mae_blend": round(mae(best_w), 4), "mae_climo": round(mae(0.0), 4)}
    if metric == "brier":
        out.update({"brier_fc": round(brier(1.0), 4), "brier_blend": round(best, 4), "brier_climo": round(brier(0.0), 4)})
    return out


def curve_from_fit(by_lead: dict[int, dict[str, float]], default: CB.Curve) -> dict[str, Any]:
    """w=1 to 48 h, then the fitted (monotone non-increasing, capped at 1) points at 72..168 h."""
    pts: list[list[float]] = []
    running = 1.0
    for n in LEADS:
        lead_h = 24.0 * n
        if lead_h <= FULL_H or n not in by_lead or by_lead[n]["n"] < MIN_SAMPLES:
            continue
        running = min(running, min(1.0, by_lead[n]["w"]))
        pts.append([lead_h, round(running, 3)])
    if not pts:
        return {"full_h": default.full_h, "floor_h": default.floor_h, "floor": default.floor}
    return {"full_h": FULL_H, "floor_h": pts[-1][0], "floor": pts[-1][1], "points": pts}


def truth_hourly(fetcher: Fetcher, sid: str, lat: float, lon: float, start: str, end: str, throttle: float) -> Optional[dict[str, Any]]:
    """ERA5 hourly truth for the span: sliced out of the 10-year climatology cache when that
    stadium is there (no extra archive call), else one small archive request."""
    full = fetcher.cached(f"era5h_{DEFAULT_START}_{DEFAULT_END}_{sid}")
    if isinstance(full, dict) and isinstance(full.get("hourly"), dict):
        h = full["hourly"]
        idx = [i for i, t in enumerate(h.get("time") or []) if start <= str(t)[:10] <= end]
        if idx:
            return {k: [v[i] for i in idx] for k, v in h.items() if isinstance(v, list)}
    payload = fetcher.json(f"era5h_{start}_{end}_{sid}", "GET", ARCHIVE_URL, params=hourly_params(lat, lon, start, end), throttle=throttle, retries=4)
    if isinstance(payload, dict) and isinstance(payload.get("hourly"), dict):
        return payload["hourly"]
    return None


def season_span(season: int) -> tuple[str, str]:
    return f"{season}-{SEASON_START[0]:02d}-{SEASON_START[1]:02d}", f"{season}-{SEASON_END[0]:02d}-{SEASON_END[1]:02d}"


def sample_stadiums(every: int, explicit: Optional[set[str]]) -> dict[str, tuple[float, float]]:
    pts = _stadium_points(CB.DATA_DIR, explicit, False)
    if explicit:
        return pts
    ids = sorted(pts)
    return {sid: pts[sid] for sid in ids[::max(1, every)]}


def run(args: argparse.Namespace) -> int:
    table = CB.default_table(use_cache=False)
    if table is None:
        print("data/climatology.csv has no weekly cells; run: python -m pipeline.stadiums.climatology --cache-dir ...")
        return 2
    explicit = {s.strip() for s in args.stadiums.split(",") if s.strip()} or None
    points = {sid: p for sid, p in sample_stadiums(args.sample_every, explicit).items() if sid in table.stadium_ids()}
    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
    print(f"fit_forecast_blend: {len(points)} stadiums x seasons {seasons}, models={args.models}")
    fetcher = Fetcher(args.cache_dir, offline=args.offline, log=print)
    pooled: dict[str, dict[int, list[tuple[float, float, float]]]] = {k: defaultdict(list) for k in KIND_VARS}
    n_ok = n_fail = 0
    for season in seasons:
        start, end = season_span(season)
        for i, (sid, (lat, lon)) in enumerate(sorted(points.items()), 1):
            try:
                fc = fetcher.json(f"prevruns_{args.models}_{start}_{end}_{sid}", "GET", PREVIOUS_RUNS_URL,
                                  params=previous_runs_params(lat, lon, start, end, args.models), throttle=args.throttle, retries=4)
                truth = truth_hourly(fetcher, sid, lat, lon, start, end, args.throttle)
            except RuntimeError as exc:
                n_fail += 1
                print(f"  {season} {sid}: fetch failed ({exc}); skipped")
                continue
            if not isinstance(fc, dict) or not isinstance(fc.get("hourly"), dict) or truth is None:
                n_fail += 1
                print(f"  {season} {sid}: unexpected payload; skipped")
                continue
            got = window_samples(sid, lon, fc["hourly"], truth, table)
            for kind, by_lead in got.items():
                for n, rows in by_lead.items():
                    pooled[kind][n].extend(rows)
            n_ok += 1
            if i % 10 == 0 or i == len(points):
                print(f"  {season}: {i}/{len(points)} stadiums; wind samples so far {sum(len(v) for v in pooled['wind'].values())}")
    if n_ok == 0:
        print("no stadium/season fetched successfully (previous-runs API unavailable or throttled); defaults kept, nothing written")
        return 1

    fit: dict[str, Any] = {
        "fitted_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seasons": seasons, "stadiums": sorted(points), "n_series_ok": n_ok, "n_series_failed": n_fail, "models": args.models,
        "window_h": WINDOW_H, "leads_days": list(LEADS), "by_kind": {},
    }
    weights: dict[str, Any] = {}
    cfg_now = CB.load_blend_config(use_cache=False)
    print(f"\n{'kind':10s} {'lead':>6s} {'n':>7s} {'w*':>5s} {'MAE fc':>8s} {'MAE blend':>10s} {'MAE climo':>10s}  (rain_prob: w* on Brier)")
    for kind in KIND_VARS:
        by_lead: dict[int, dict[str, float]] = {}
        for n in LEADS:
            rows = pooled[kind].get(n) or []
            if len(rows) < MIN_SAMPLES:
                continue
            by_lead[n] = fit_weight(rows, metric="brier" if kind == "rain_prob" else "mae")
            r = by_lead[n]
            extra = f"  brier fc/blend/climo {r['brier_fc']:.4f}/{r['brier_blend']:.4f}/{r['brier_climo']:.4f}" if "brier_fc" in r else ""
            print(f"{kind:10s} {24 * n:>5.0f}h {r['n']:>7d} {r['w']:>5.2f} {r['mae_fc']:>8.3f} {r['mae_blend']:>10.3f} {r['mae_climo']:>10.3f}{extra}")
        fit["by_kind"][kind] = {str(24 * n): v for n, v in by_lead.items()}
        weights[kind] = curve_from_fit(by_lead, cfg_now.curves[kind])
    print("\nweights:", json.dumps(weights))
    if args.dry_run:
        print("dry-run: calibration.json not written")
        return 0
    path = args.out
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    block = data.get("forecast_blend") if isinstance(data.get("forecast_blend"), dict) else {}
    block.setdefault("medium_range_weights", dict(CB.DEFAULT_MEDIUM_WEIGHTS))
    block.setdefault("medium_range_start_h", CB.DEFAULT_MEDIUM_START_H)
    block["weights"] = weights
    block["fit"] = fit
    data["forecast_blend"] = block
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache-dir", type=Path, default=None, help="json cache for previous-runs + ERA5 payloads (resume-safe)")
    ap.add_argument("--seasons", default="2024,2025")
    ap.add_argument("--sample-every", type=int, default=4, help="take every k-th in-scope stadium (default 4 -> ~43)")
    ap.add_argument("--stadiums", default="", help="explicit comma-separated stadium ids (overrides the sample)")
    ap.add_argument("--models", default="best_match", help="previous-runs model id(s)")
    ap.add_argument("--throttle", type=float, default=1.5)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", type=Path, default=CB.CALIBRATION_PATH)
    ap.add_argument("--dry-run", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
