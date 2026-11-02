"""Fit the team-total points model and write the ``team_totals`` block of data/calibration.json.

Uses the SAME rating code the live pipeline runs (``pipeline.model.team_totals.walk_ratings``
over ``pipeline.teamstats`` parsers), so the coefficients match what the board computes.

    python scripts/fit_team_model.py --nfl-dir <dir with st{season}.csv + games.csv>
                                     --cfbd-dir <dir with adv_/games_/returning_/lines_{season}.json>
                                     [--seasons-nfl 1999-2025] [--seasons-cfb 2016-2025] [--dry-run]

Reports, per sport, a strict forward test (each season predicted by a model fitted only on
earlier seasons): RMSE of ours vs the closing total, and the slope of (actual - close) on
(ours - close). Offline only; never called by the pipeline.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.model import team_totals as TT  # noqa: E402,N812
from pipeline.teamstats import parse_cfb, parse_nfl, parse_returning, resolve_team  # noqa: E402

FEATURES = {
    "nfl": ["lg", "pts_match", "eff_match", "sum_pace"],
    "cfb": ["lg", "pts_match", "eff_match", "ppd_match", "sr_match", "sum_pace", "sum_drives"],
}
CAL_PATH = Path(__file__).resolve().parents[1] / "data" / "calibration.json"


def _span(s: str) -> list[int]:
    a, b = (int(x) for x in s.split("-"))
    return list(range(a, b + 1))


def load_nfl(d: Path, seasons: list[int]) -> tuple[list[TT.TeamGame], dict[str, float]]:
    games = (d / "games.csv").read_text(encoding="utf-8")
    rows: list[TT.TeamGame] = []
    for s in seasons:
        p = d / f"st{s}.csv"
        if p.exists():
            rows += parse_nfl(p.read_text(encoding="utf-8"), games, s)
    import csv
    import io

    lines = {r["game_id"]: float(r["total_line"]) for r in csv.DictReader(io.StringIO(games)) if r.get("total_line")}
    return rows, lines


def load_cfb(d: Path, seasons: list[int]) -> tuple[list[TT.TeamGame], dict[str, float], dict[int, dict[str, float]], set[str]]:
    rows: list[TT.TeamGame] = []
    members: set[str] = set()
    returning: dict[int, dict[str, float]] = {}
    lines: dict[str, float] = {}

    def resolve(name: str) -> str | None:
        return resolve_team("cfb", name)

    for s in seasons:
        adv, gms = d / f"adv_{s}.json", d / f"games_{s}.json"
        if not (adv.exists() and gms.exists()):
            continue
        r, fbs = parse_cfb(json.loads(adv.read_text()), json.loads(gms.read_text()), s, resolve)
        rows += r
        members |= fbs
        ret = d / f"returning_{s}.json"
        if ret.exists():
            returning[s] = parse_returning(json.loads(ret.read_text()), resolve)
        ln = d / f"lines_{s}.json"
        if ln.exists():
            for g in json.loads(ln.read_text()):
                tot = next((x.get("overUnder") for x in g.get("lines", []) if x.get("overUnder")), None)
                if tot:
                    lines[str(g["id"])] = float(tot)
    return rows, lines, returning, members


def game_table(sport: str, rows: list[TT.TeamGame], returning: dict, members: set[str] | None) -> list[dict[str, Any]]:
    """One row per game: features from both teams' pre-game ratings + the actual total."""
    by_week: dict[tuple[int, int], dict[str, list[TT.TeamGame]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_week[(r.season, r.week)][r.game_key].append(r)
    out = []
    lg_by = {s: TT.league_total(rows, s, members or None) for s in {r.season for r in rows}}
    for s, w, snap in TT.walk_ratings(sport, rows, returning=returning, members=members or None):
        lg = lg_by[s]
        if lg is None:
            continue  # no previous season in the data: cannot rate this season pre-game
        for key, pair in by_week[(s, w)].items():
            if len(pair) != 2:
                continue
            a, b = pair
            if members and not (a.team in members and b.team in members):
                continue
            if a.team not in snap or b.team not in snap:
                continue
            f = TT.features(snap[a.team], snap[b.team], lg)
            out.append({"season": s, "week": w, "game_key": key, "total": a.pf + a.pa,
                        "n_prior": min(snap[a.team].n_games, snap[b.team].n_games), **f})
    return out


def ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.linalg.lstsq(np.column_stack([np.ones(len(X)), X]), y, rcond=None)[0]


def forward_test(table: list[dict], feats: list[str], lines: dict[str, float]) -> dict[str, Any]:
    seasons = sorted({t["season"] for t in table})
    preds, acts, lns, early = [], [], [], []
    for s in seasons[2:]:  # need >= 2 earlier seasons to fit
        tr = [t for t in table if t["season"] < s]
        te = [t for t in table if t["season"] == s and t["game_key"] in lines]
        if not te:
            continue
        b = ols(np.array([[t[f] for f in feats] for t in tr]), np.array([t["total"] for t in tr]))
        for t in te:
            preds.append(b[0] + sum(c * t[f] for c, f in zip(b[1:], feats, strict=True)))
            acts.append(t["total"])
            lns.append(lines[t["game_key"]])
            early.append(t["n_prior"] < 3)
    p, a, ln = np.array(preds), np.array(acts), np.array(lns)
    gap, resid = p - ln, a - ln

    def slope(mask: np.ndarray) -> tuple[float, float]:
        g, r = gap[mask], resid[mask]
        gc = g - g.mean()
        b = float((gc * (r - r.mean())).sum() / (gc ** 2).sum())
        e = r - r.mean() - b * gc
        se = math.sqrt(float(((gc ** 2) * e ** 2).sum()) / float((gc ** 2).sum()) ** 2)
        return round(b, 3), round(se, 3)

    all_mask = np.ones(len(p), bool)
    return {"n": int(len(p)), "rmse_model": round(float(np.sqrt(((a - p) ** 2).mean())), 3),
            "rmse_close": round(float(np.sqrt(((a - ln) ** 2).mean())), 3),
            "gap_slope": slope(all_mask), "gap_slope_early": slope(np.array(early)) if any(early) else None,
            "sd_gap": round(float(gap.std()), 3)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nfl-dir", type=Path)
    ap.add_argument("--cfbd-dir", type=Path)
    ap.add_argument("--seasons-nfl", default="1999-2025")
    ap.add_argument("--seasons-cfb", default="2016-2025")
    ap.add_argument("--out", type=Path, default=CAL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    block: dict[str, Any] = {}
    for sport in ("nfl", "cfb"):
        if sport == "nfl" and args.nfl_dir:
            rows, lines = load_nfl(args.nfl_dir, _span(args.seasons_nfl))
            returning, members = {}, set()
        elif sport == "cfb" and args.cfbd_dir:
            rows, lines, returning, members = load_cfb(args.cfbd_dir, _span(args.seasons_cfb))
        else:
            continue
        table = game_table(sport, rows, returning, members)
        feats = FEATURES[sport]
        test = forward_test(table, feats, lines)
        b = ols(np.array([[t[f] for f in feats] for t in table]), np.array([t["total"] for t in table]))
        coef = {"intercept": round(float(b[0]), 6), **{f: round(float(c), 6) for f, c in zip(feats, b[1:], strict=True)}}
        seasons = sorted({t["season"] for t in table})
        block[sport] = {"coef": coef, "features": feats, "seasons": [seasons[0], seasons[-1]], "n_games": len(table),
                        "forward_test": test}
        print(f"{sport}: {len(table)} games {seasons[0]}-{seasons[-1]}  forward test {test}")
        print(f"   coef {coef}")
    if args.dry_run or not block:
        print("dry-run: nothing written" if block else "no data")
        return 0
    data = json.loads(args.out.read_text(encoding="utf-8"))
    data["team_totals"] = {**data.get("team_totals", {}), **block}
    args.out.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"wrote team_totals -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
