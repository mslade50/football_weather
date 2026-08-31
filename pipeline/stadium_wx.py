"""Per-stadium under records conditioned on the ACTUAL weather (HISTORICAL_BACKTEST_SPEC §8).

The hand-built CFB "Stadiums" sheet, rebuilt for both sports over ~10 seasons out of data the
repo already holds::

    python -m pipeline.stadium_wx --seasons 2015-2024 [--sport nfl|cfb] [--no-network]

* **ERA5** hourly 2015–2024 for 173 stadiums — the `stadiums/climatology.py` cache under
  ``data/backtest/era5``, reused through ``backtest_git.fill_actuals`` (and its window memo, so a
  second run never re-reads the hourly files).
* **NFL** — nflverse ``games.csv``: ``total_line`` / ``under_odds`` / final ``total`` / ``roof`` /
  ``stadium_id``, complete back to 1999.
* **CFB** — CFBD ``/games`` (for ``venueId``) joined on the game id to ``/lines`` (for the total);
  one request per season, cached under the git cache dir.

Keyed on **ERA5 wind at kickoff, not forecast wind**. A venue prior should answer "when it is
genuinely windy here, do unders hit?", which is a different question from "can we forecast it" —
and it keeps the forecast error measured in §7 out of the stadium numbers entirely.

Bands: ERA5 10 m wind reads *below* the forecast wind the signal thresholds are written in
(fitted on the 2024–25 replay: ``actual = 0.824 * forecast + 0.74``, r = 0.81), so a 15 mph
*forecast* is ~13 mph of ERA5 and true ERA5 ≥ 15 is the 97th percentile — about one game per
stadium per decade. Every band in ``WIND_BANDS`` is therefore computed, plus the venue's **own
top quartile**, which is self-normalising (Soldier Field's windy games are windier than Miami's)
and always has a quarter of the stadium's sample in it.

Writes ``data/backtest/stadium_wx.parquet`` (one row per stadium × sport) and
``stadium_wx_games.parquet`` (per game); ``pipeline.backtest`` merges the records into
``board/backtest.json`` as ``stadium_wx`` for the hover card.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import backtest as bt
from pipeline import backtest_git as bg
from pipeline.run_context import REPO_ROOT
from utils.env import load_repo_dotenv
from utils.timeutil import ensure_utc, parse_iso, utc_iso

PathLike = Union[str, Path]

DEFAULT_SEASONS = (2015, 2024)          # the window the local ERA5 hourly cache covers
DEFAULT_OUT = REPO_ROOT / "data" / "backtest"
# absolute ERA5 bands; see the module docstring on why 15 is thin
WIND_BANDS: tuple[tuple[str, float], ...] = (("wind10", 10.0), ("wind12", 12.0), ("wind15", 15.0))
QUARTILE_MIN_N = 12                     # a venue needs this many graded games for its own quartile
MIN_N = 8                               # below this a stadium row is emitted but flagged thin
CLOSED_ROOFS = {"dome", "closed", "retractable_closed"}
CFBD_BASE = bg.CFBD_BASE


# ---- inputs -------------------------------------------------------------------------------------

def load_cfbd_lines(season: int, cache_dir: PathLike, *, no_network: bool = False,
                    api_key: Optional[str] = None) -> list[dict[str, Any]]:
    """CFBD ``/lines?year=`` — one request per season (~900 games, 99 % carry a total)."""
    def fetch() -> str:
        import httpx

        key = api_key or os.environ.get("CFBD_API_KEY") or ""
        r = httpx.get(f"{CFBD_BASE}/lines", params={"year": season},
                      headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=120.0)
        r.raise_for_status()
        return json.dumps(r.json())

    text = bg._cache_text(Path(cache_dir) / f"cfbd_lines_{season}.json", fetch,
                          no_network=no_network, label=f"CFBD lines {season}")
    payload = json.loads(text)
    return [g for g in payload if isinstance(g, dict)]


def consensus_total(lines: Iterable[Mapping[str, Any]]) -> tuple[Optional[float], Optional[float], int]:
    """(median total, median open, n books) across the providers CFBD returns for one game."""
    totals = [bt._num(x.get("overUnder")) for x in lines or []]
    opens = [bt._num(x.get("overUnderOpen")) for x in lines or []]
    totals = [t for t in totals if t is not None]
    opens = [o for o in opens if o is not None]
    return (statistics.median(totals) if totals else None,
            statistics.median(opens) if opens else None,
            len(totals))


def _row(sport: str, season: int, week: Any, kickoff: Any, home_id: str, away_id: str, *,
         total: Optional[float], total_open: Optional[float], odds: Optional[float],
         home_score: Optional[int], away_score: Optional[int], bits: Mapping[str, Any],
         roof: Optional[str], source: str) -> Optional[bt.GameRow]:
    try:
        kick_dt = ensure_utc(parse_iso(str(kickoff)))
    except (TypeError, ValueError):
        return None
    if total is None or home_score is None or away_score is None:
        return None
    from pipeline.contracts import make_game_id

    r = bt.GameRow(
        game_id=make_game_id(sport, int(season), int(week or 0), away_id, home_id),
        sport=sport, season=int(season), week=int(week or 0), kickoff_utc=utc_iso(kick_dt),
        home_id=home_id, away_id=away_id, roof_state=roof,
        total_open=total_open, total_close=total, close_under_odds=odds,
        home_score=int(home_score), away_score=int(away_score),
        hist=True, src_result=source,
        **{k: v for k, v in bits.items() if k in ("stadium_id", "stadium_name", "lat", "lon")},
    )
    return r


def nfl_rows(csv_text: str, seasons: Sequence[int], book: Any = None) -> list[bt.GameRow]:
    """nflverse games.csv -> graded rows (outdoor only; the closing total is ``total_line``)."""
    import csv
    import io

    from pipeline.schedule.nfl import _kickoff_et, _week

    want = {int(s) for s in seasons}
    out: list[bt.GameRow] = []
    for rec in csv.DictReader(io.StringIO(csv_text)):
        try:
            season = int(float(rec.get("season") or 0))
        except ValueError:
            continue
        if season not in want:
            continue
        roof = (rec.get("roof") or "").strip().lower() or None
        if roof in CLOSED_ROOFS:
            continue
        kick_et = _kickoff_et(rec)
        home = (rec.get("home_team") or "").strip().lower()
        away = (rec.get("away_team") or "").strip().lower()
        if kick_et is None or not home or not away:
            continue
        bits = bg._stadium_bits(book, (rec.get("stadium_id") or "").strip() or None, bg._text(rec.get("stadium")))
        r = _row("nfl", season, _week(rec), kick_et.astimezone(bg.UTC).isoformat(), home, away,
                 total=bt._num(rec.get("total_line")), total_open=None, odds=bt._num(rec.get("under_odds")),
                 home_score=bt._num(rec.get("home_score")), away_score=bt._num(rec.get("away_score")),
                 bits=bits, roof=roof, source="nflverse")
        if r is not None:
            out.append(r)
    return out


def cfb_rows(games: Iterable[Mapping[str, Any]], lines: Iterable[Mapping[str, Any]], season: int,
             book: Any = None) -> list[bt.GameRow]:
    """CFBD /games (venue) joined on the game id to /lines (totals)."""
    from pipeline.schedule.cfb import cfb_week
    from pipeline.stadiums.loader import slug

    venue_by_id = {}
    for g in games or []:
        gid = g.get("id")
        if gid is not None:
            vid = g.get("venueId") if g.get("venueId") is not None else g.get("venue_id")
            venue_by_id[str(gid)] = (str(vid) if vid is not None else None, bg._text(g.get("venue")))
    out: list[bt.GameRow] = []
    for g in lines or []:
        total, total_open, n_books = consensus_total(g.get("lines") or [])
        home_name, away_name = bg._text(g.get("homeTeam")), bg._text(g.get("awayTeam"))
        if total is None or not home_name or not away_name:
            continue
        home_id = (book.resolve_team("cfb", home_name, fuzzy=False) if book is not None else None) or slug(home_name)
        away_id = (book.resolve_team("cfb", away_name, fuzzy=False) if book is not None else None) or slug(away_name)
        vid, vname = venue_by_id.get(str(g.get("id")), (None, None))
        bits = bg._stadium_bits(book, vid, vname)
        roof = bits.get("roof_state")
        if roof in CLOSED_ROOFS:
            continue
        r = _row("cfb", season, cfb_week(g.get("week"), g.get("seasonType") or g.get("season_type")),
                 g.get("startDate") or g.get("start_date"), home_id, away_id,
                 total=total, total_open=total_open, odds=None,
                 home_score=bt._num(g.get("homeScore")), away_score=bt._num(g.get("awayScore")),
                 bits=bits, roof=roof, source="cfbd")
        if r is not None:
            r.ref_book = f"cfbd:{n_books}"
            out.append(r)
    return out


# ---- ERA5 actuals, indexed ------------------------------------------------------------------------

def _hour_index(hourly: Mapping[str, Any]) -> dict[str, int]:
    """``'2015-11-08T18:00' -> row``, built once per stadium file."""
    return {str(t)[:16]: i for i, t in enumerate(hourly.get("time") or [])}


def _window_from_index(hourly: Mapping[str, Any], index: Mapping[str, int], start: Any) -> dict[str, Any]:
    """Mean over ``[start, start+2h]`` by direct lookup instead of scanning the year.

    ``backtest.window_stats`` parses every timestamp in the file for every game — fine for the
    1.8k games of the git replay, ~10^9 parses for ten seasons against a 10-year hourly file."""
    rows = [index[k] for k in ((start + bg.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in (0, 1, 2))
            if k in index]
    if not rows:
        return {}

    def col(name: str) -> list[float]:
        arr = hourly.get(name) or []
        return [float(arr[i]) for i in rows if i < len(arr) and arr[i] is not None]

    from pipeline.weather.merge import mean3

    precip = col("precipitation")
    return {"temp": mean3(col("temperature_2m")), "wind": mean3(col("wind_speed_10m")),
            "gust": mean3(col("wind_gusts_10m")), "rain": sum(precip) if precip else None, "dir": None}


def fill_actuals(rows: Sequence[bt.GameRow], *, cache_dir: PathLike, log: Callable[[str], None] = print) -> int:
    """ERA5 kickoff-window mean per game, one stadium file opened at a time and indexed by hour.
    Shares ``windows.parquet`` with ``backtest_git.fill_actuals``, so either path warms the other."""
    index = bg.era5_index(cache_dir)
    cache = bg.load_window_cache(cache_dir)
    todo: dict[str, list[bt.GameRow]] = defaultdict(list)
    filled = 0
    for r in rows:
        kick = bt._dt(r.kickoff_utc)
        if kick is None or not r.stadium_id:
            continue
        start, _ = bt._window(kick)
        hit = cache.get((r.stadium_id, start.isoformat()))
        if hit is not None:
            bg._apply_window(r, hit)
            filled += 1
        else:
            todo[r.stadium_id].append(r)
    for n, (sid, members) in enumerate(sorted(todo.items()), 1):
        entries = index.get(sid) or []
        if not entries:
            continue
        by_file: dict[Path, list[bt.GameRow]] = defaultdict(list)
        for r in members:
            kick = bt._dt(r.kickoff_utc)
            path = bg.era5_covers(entries, kick.date()) if kick is not None else None
            if path is not None:
                by_file[path].append(r)
        if n % 25 == 0:
            log(f"  era5 {n}/{len(todo)} stadiums")
        for path, group in by_file.items():
            try:
                hourly = (json.loads(path.read_text(encoding="utf-8")) or {}).get("hourly") or {}
            except (OSError, ValueError) as exc:
                log(f"  era5 {path.name}: {exc}")
                continue
            idx = _hour_index(hourly)
            for r in group:
                start, _ = bt._window(bt._dt(r.kickoff_utc))
                st = _window_from_index(hourly, idx, start)
                if not st or (st.get("wind") is None and st.get("temp") is None):
                    continue
                rec = {"stadium_id": sid, "start": start.isoformat(), "src": path.stem, **st}
                cache[(sid, start.isoformat())] = rec
                bg._apply_window(r, rec)
                filled += 1
    bg.save_window_cache(cache_dir, cache)
    return filled


# ---- aggregation --------------------------------------------------------------------------------

def _stats(rows: Sequence[bt.GameRow]) -> dict[str, Any]:
    graded = [r for r in rows if r.close_result is not None]
    w = sum(1 for r in graded if r.close_result == "W")
    losses = sum(1 for r in graded if r.close_result == "L")
    p = sum(1 for r in graded if r.close_result == "P")
    rois = [r.roi_close for r in graded if r.roi_close is not None]
    n = w + losses
    return {
        "n": len(graded), "w": w, "l": losses, "p": p,
        "record": f"{w}-{losses}-{p}",
        "win_pct": bg._round(w / n, 4) if n else None,
        "roi": bg._round(sum(rois) / len(rois), 4) if rois else None,
        "margin": bg._round(bg._mean([r.close_margin for r in graded]), 2),
    }


def stadium_records(rows: Sequence[bt.GameRow], *, bands: Sequence[tuple[str, float]] = WIND_BANDS,
                    min_n: int = MIN_N) -> list[dict[str, Any]]:
    """One row per (stadium, sport): the all-games under record, each absolute wind band, and the
    venue's own top-quartile wind band. ``split`` is the first/second half of the seasons covered,
    a cheap check that a venue effect is not just one stale era."""
    groups: dict[tuple[str, str], list[bt.GameRow]] = defaultdict(list)
    for r in rows:
        if r.stadium_id and r.close_result is not None:
            groups[(r.stadium_id, r.sport)].append(r)
    out = []
    for (sid, sport), members in sorted(groups.items()):
        winds = [r.wind_act for r in members if r.wind_act is not None]
        seasons = sorted({r.season for r in members if r.season is not None})
        mid = seasons[len(seasons) // 2] if seasons else None
        rec: dict[str, Any] = {
            "stadium_id": sid, "sport": sport,
            "stadium": next((r.stadium_name for r in members if r.stadium_name), sid),
            "team": ", ".join(sorted({r.home_id for r in members if r.home_id})[:3]) or None,
            "seasons": f"{seasons[0]}-{seasons[-1]}" if seasons else None,
            "n_with_wind": len(winds),
            "thin": len(members) < min_n,
            **{f"all_{k}": v for k, v in _stats(members).items()},
        }
        for name, lo in bands:
            hits = [r for r in members if r.wind_act is not None and r.wind_act >= lo]
            rec.update({f"{name}_{k}": v for k, v in _stats(hits).items()})
        # the venue's own windiest quarter — always populated, and it is the honest venue question
        if len(winds) >= QUARTILE_MIN_N:
            p75 = statistics.quantiles(winds, n=4)[2]
            top = [r for r in members if r.wind_act is not None and r.wind_act >= p75]
            rec["wind_p75"] = bg._round(p75, 2)
            rec.update({f"top25_{k}": v for k, v in _stats(top).items()})
        else:
            rec["wind_p75"] = None
        if mid is not None:
            rec.update({f"early_{k}": v for k, v in _stats([r for r in members if (r.season or 0) < mid]).items()})
            rec.update({f"late_{k}": v for k, v in _stats([r for r in members if (r.season or 0) >= mid]).items()})
        rec["mean_wind"] = bg._round(bg._mean(winds), 2)
        out.append(rec)
    return out


def wind_band_table(rows: Sequence[bt.GameRow], *, bands: Sequence[tuple[str, float]] = WIND_BANDS
                    ) -> list[dict[str, Any]]:
    """The result that survives its own test: the under record by ABSOLUTE ERA5 wind, pooled.

    Per-venue splits do not survive (see ``venue_noise_check``) — venue-to-venue ROI spread is
    indistinguishable from coin flips and a venue's first half does not predict its second. The
    wind effect itself is strong, monotonic and stable across 11 seasons, so this is the table the
    board should reason from."""
    out = []
    for sport in ("nfl", "cfb", "all"):
        pool = [r for r in rows if r.wind_act is not None and (sport == "all" or r.sport == sport)]
        if not pool:
            continue
        for name, lo in (("under10", None), *bands):
            hits = [r for r in pool if (r.wind_act < 10.0 if lo is None else r.wind_act >= lo)]
            st = _stats(hits)
            if st["n"]:
                out.append({"sport": sport, "band": name,
                            "wind_min": lo, "wind_max": 10.0 if lo is None else None, **st})
    return out


def venue_noise_check(records: Sequence[Mapping[str, Any]], min_n: int = 8) -> dict[str, Any]:
    """Is the per-venue leaderboard signal or sampling noise?

    Compares the observed spread of per-venue top-quartile ROI against the spread a set of pure
    coin flips of the same sample sizes would produce. A ratio near 1.0 means the venue table is
    describing noise and must not be presented as an edge."""
    rows = [r for r in records if (r.get("top25_n") or 0) >= min_n and r.get("top25_roi") is not None]
    if len(rows) < 10:
        return {}
    rois = [float(r["top25_roi"]) for r in rows]
    ns = [float(r["top25_n"]) for r in rows]
    observed = statistics.stdev(rois)
    median_n = statistics.median(ns)
    noise = 1.909 * math.sqrt(0.25 / median_n)     # sd of ROI for a coin flip at that sample size
    return {
        "n_venues": len(rows), "median_n": median_n,
        "mean_roi": bg._round(statistics.mean(rois), 4), "sd_roi": bg._round(observed, 4),
        "sd_if_noise": bg._round(noise, 4), "signal_ratio": bg._round(observed / noise, 3),
        "verdict": "venue spread exceeds sampling noise" if observed / noise > 1.15
                   else "indistinguishable from sampling noise",
    }


# ---- orchestration -------------------------------------------------------------------------------

@dataclass
class StadiumWxResult:
    rows: list[bt.GameRow] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    bands: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def run(*, seasons: Sequence[int], sport: Optional[str] = None, git_cache: PathLike = bg.DEFAULT_GIT_CACHE,
        era5_cache: PathLike = bg.DEFAULT_ERA5_CACHE, no_network: bool = False,
        cfbd_key: Optional[str] = None, book: Any = None, log: Callable[[str], None] = print) -> StadiumWxResult:
    sports = [sport] if sport else ["nfl", "cfb"]
    seasons = [int(s) for s in seasons]
    if book is None:
        from pipeline.stadiums.loader import load_stadium_book

        book = load_stadium_book()
    rows: list[bt.GameRow] = []
    for sp in sports:
        if sp == "nfl":
            got = nfl_rows(bg.load_nflverse_csv(git_cache, no_network=no_network), seasons, book)
        else:
            got = []
            for season in seasons:
                games = bg.load_cfbd_games(season, git_cache, no_network=no_network, api_key=cfbd_key)
                lines = load_cfbd_lines(season, git_cache, no_network=no_network, api_key=cfbd_key)
                got.extend(cfb_rows(games, lines, season, book))
                log(f"  cfb {season}: {len(games)} games, {len(lines)} line rows")
        log(f"  {sp}: {len(got)} gradeable outdoor games in {seasons[0]}-{seasons[-1]}")
        rows.extend(got)

    n_act = fill_actuals(rows, cache_dir=era5_cache, log=log)
    for r in rows:
        bg.finalize_hist_row(r)
    log(f"  era5: {n_act}/{len(rows)} games with actuals")
    records = stadium_records(rows)
    bands = wind_band_table(rows)
    graded = [r for r in rows if r.close_result is not None]
    winds = [r.wind_act for r in rows if r.wind_act is not None]
    meta = {
        "seasons": seasons, "sports": sports, "n_games": len(rows), "n_graded": len(graded),
        "n_with_wind": len(winds), "n_stadiums": len(records),
        "generated_at": utc_iso(),
        "bands": {name: bg._round(sum(1 for w in winds if w >= lo) / len(winds), 4) if winds else None
                  for name, lo in WIND_BANDS},
        "wind_pctiles": {str(p): bg._round(statistics.quantiles(winds, n=100)[p - 1], 2)
                         for p in (50, 75, 90, 95)} if len(winds) > 100 else {},
        "venue_noise": venue_noise_check(records),
    }
    return StadiumWxResult(rows=rows, records=records, bands=bands, meta=meta)


def write_outputs(res: StadiumWxResult, out_dir: PathLike = DEFAULT_OUT) -> dict[str, Path]:
    import pandas as pd

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, df in (("stadium_wx", pd.DataFrame(res.records)),
                     ("stadium_wx_bands", pd.DataFrame(res.bands)),
                     ("stadium_wx_games", pd.DataFrame([r.to_dict() for r in res.rows]))):
        p = out / f"{name}.parquet"
        df.to_parquet(p, index=False)
        written[name] = p
    (out / "stadium_wx_meta.json").write_text(json.dumps(res.meta, indent=1), encoding="utf-8")
    written["meta"] = out / "stadium_wx_meta.json"
    return written


def load_records(path: PathLike = DEFAULT_OUT / "stadium_wx.parquet") -> list[dict[str, Any]]:
    """The published records, for ``pipeline.backtest`` to merge into board/backtest.json."""
    import pandas as pd

    p = Path(path)
    if not p.is_file():
        return []
    df = pd.read_parquet(p)
    return [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in rec.items()}
            for rec in df.to_dict(orient="records")]


def load_bands(path: PathLike = DEFAULT_OUT / "stadium_wx_bands.parquet") -> list[dict[str, Any]]:
    return load_records(path)


def print_report(res: StadiumWxResult, top: int = 12, log: Callable[[str], None] = print) -> None:
    m = res.meta
    log(f"stadium weather records: {m['n_graded']} graded games, {m['n_with_wind']} with ERA5 wind, "
        f"{m['n_stadiums']} stadium rows, {m['seasons'][0]}-{m['seasons'][-1]}")
    log("  share of games at each band: " + ", ".join(f"{k} {v:.1%}" for k, v in m["bands"].items() if v is not None))
    if m["wind_pctiles"]:
        log("  ERA5 wind percentiles: " + ", ".join(f"p{k} {v}" for k, v in m["wind_pctiles"].items()))
    log("  under record by ABSOLUTE ERA5 wind at kickoff (the result that holds up):")
    log(f"    {'sport':<6}{'band':<10}{'record':>12}{'n':>7}{'win%':>8}{'ROI':>9}")
    for b in res.bands:
        log(f"    {b['sport']:<6}{b['band']:<10}{b['record']:>12}{b['n']:>7}"
            f"{(b['win_pct'] or 0):>8.3f}{(b['roi'] or 0):>+9.3f}")
    nz = res.meta.get("venue_noise") or {}
    if nz:
        log(f"  per-venue check: {nz['n_venues']} venues, top-quartile ROI sd {nz['sd_roi']} vs "
            f"{nz['sd_if_noise']} expected from pure noise -> ratio {nz['signal_ratio']} "
            f"({nz['verdict']})")
    ranked = sorted((r for r in res.records if (r.get("top25_n") or 0) >= 8),
                    key=lambda r: -(r.get("top25_roi") or -9))
    log(f"  windiest-quarter under records (top {top} by ROI, n>=8):")
    log(f"    {'stadium':<34}{'sport':<6}{'all games':>14}{'top quartile':>16}{'p75 wind':>10}")
    for r in ranked[:top]:
        log(f"    {str(r['stadium'])[:33]:<34}{r['sport']:<6}"
            f"{r['all_record'] + ' ' + str(r['all_roi']):>14}"
            f"{r['top25_record'] + ' ' + str(r['top25_roi']):>16}{str(r['wind_p75']):>10}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.stadium_wx", description=__doc__.split("\n\n")[0])
    p.add_argument("--seasons", default=f"{DEFAULT_SEASONS[0]}-{DEFAULT_SEASONS[1]}",
                   help="inclusive range '2015-2024' or a comma list")
    p.add_argument("--sport", choices=("nfl", "cfb"), default=None)
    p.add_argument("--git-cache", type=Path, default=bg.DEFAULT_GIT_CACHE)
    p.add_argument("--era5-cache", type=Path, default=bg.DEFAULT_ERA5_CACHE)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--no-network", action="store_true")
    return p.parse_args(argv)


def parse_seasons(spec: str) -> list[int]:
    spec = str(spec).replace(" ", "")
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(s) for s in spec.split(",") if s]


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_repo_dotenv()
    args = parse_args(argv)
    res = run(seasons=parse_seasons(args.seasons), sport=args.sport, git_cache=args.git_cache,
              era5_cache=args.era5_cache, no_network=args.no_network)
    print_report(res)
    for name, p in write_outputs(res, args.out_dir).items():
        print(f"  wrote {name} -> {p}")
    return 0


__all__ = [
    "WIND_BANDS", "StadiumWxResult", "cfb_rows", "consensus_total", "load_cfbd_lines", "load_records",
    "fill_actuals", "main", "nfl_rows", "parse_args", "parse_seasons", "print_report", "run",
    "stadium_records", "venue_noise_check",
    "wind_band_table", "load_bands", "write_outputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
