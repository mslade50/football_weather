"""Historical backtest from the git line/forecast archive (docs/HISTORICAL_BACKTEST_SPEC.md).

Between 2024-09 and 2026-04 the legacy generator committed ``nfl_weather.csv`` and
``cfb_weather.xlsx`` several times a day. Every distinct blob is a snapshot of every
upcoming game: the forecast, the impact numbers and the lines as they stood. This
module turns that archive into one row per graded game::

    python -m pipeline.backtest_git [--sport nfl|cfb] [--cache-dir data/backtest]
                                    [--refresh-git] [--no-network]

Outputs (``--cache-dir``, gitignored): ``hist_snapshots.parquet`` (one row per game per
snapshot, with lead hours and the replayed v1 tier) and ``hist_games.parquet`` (one row
per game: alert snapshot, closing snapshot, forecasts at fixed leads, result, actual
kickoff-window weather).

Scope of this first cut: data + grading. Board/JSON integration (spec §3.3–3.5) is not
wired yet. Known archive quirks handled here:

* ``Timestamp`` is naive ET; ``Date`` (``SUN 11/09``) has no year, so the year comes from
  the run time with a Jan–Jul rollover.
* Lines are one book per sport (BetOnline NFL, FanDuel CFB): ``line_book`` says which.
* Spread *signs* in the archive are not reliable (opener and current disagree on the
  favourite), so only ``|spread|`` is kept; the favourite comes from the schedule
  source's closing line when a signed spread is needed.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.backtest import _window, window_stats
from pipeline.model import config as C
from pipeline.model.impact import compute_impact_v1
from pipeline.model.signals import NO, cfb_signal, nfl_signal
from pipeline.odds.teams import normalize_team
from pipeline.schedule.cfb import cfb_week

REPO = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
ARCHIVE_FILES: dict[str, tuple[str, str]] = {"nfl": ("nfl_weather.csv", ".csv"), "cfb": ("cfb_weather.xlsx", ".xlsx")}
LINE_BOOK = {"nfl": "betonline", "cfb": "fanduel"}
ALERT_MAX_LEAD_H = 240.0
CLOSE_MAX_LEAD_H = 6.0
MIN_LEAD_H = 0.5
FIXED_LEADS_H = (24, 48, 72, 120, 168)
FIXED_LEAD_TOL_H = 6.0
MATCH_TOL = 0.011  # pct points; archive values carry float noise at the 3rd decimal
NFLVERSE_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
CFBD_BASE = "https://api.collegefootballdata.com"
ERA5_URL = ("https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
            "&start_date={d0}&end_date={d1}&hourly=temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation,wind_direction_10m"
            "&wind_speed_unit=mph&temperature_unit=fahrenheit&timezone=UTC")

# archive column -> normalised name, per sport
_COLS: dict[str, dict[str, str]] = {
    "nfl": {"Total_now": "total", "Under_now": "under_odds", "Total_open": "open_total",
            "Spread_now": "spread_raw", "Spread_open": "open_spread_raw"},
    "cfb": {"FD_now": "total", "Odds_n": "under_odds", "Fd_open": "open_total",
            "Current": "spread_raw", "Open": "open_spread_raw"},
}
_NUM = ["wind_fg", "temp_fg", "rain_fg", "travel_alt", "home_temp", "away_temp", "gs_fg", "away_fg"]


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


# ---------------------------------------------------------------- snapshot parsing
def split_game(sport: str, game: Any) -> tuple[Optional[str], Optional[str]]:
    """(away, home) raw names. NFL ``"away vs home"``, CFB ``"Away @ Home"`` (``vs`` on the Other sheet)."""
    g = str(game or "").strip()
    for sep in (" @ ", " vs "):
        if sep in g:
            away, home = g.split(sep, 1)
            return away.strip() or None, home.strip() or None
    return None, None


def parse_run_ts(timestamp: Any, commit_date: str) -> datetime:
    """Run time in UTC: the archive ``Timestamp`` (naive ET) when parseable, else the commit date."""
    ts = pd.to_datetime(str(timestamp), errors="coerce") if timestamp not in (None, "", "nan") else pd.NaT
    if pd.notna(ts):
        return ts.to_pydatetime().replace(tzinfo=ET).astimezone(timezone.utc)
    return datetime.fromisoformat(commit_date).astimezone(timezone.utc)


def infer_kickoff(date_str: Any, time_str: Any, run_ts: datetime) -> Optional[datetime]:
    """Kickoff (UTC) from ``SUN 11/09`` + ``01:00 PM`` (ET), year taken from the run time.

    A game month earlier than the run month by more than six months is next year
    (December run, January playoff game); later by more than six is last year."""
    try:
        md = str(date_str).split()[-1]
        mon, day = (int(x) for x in md.split("/"))
        hm = datetime.strptime(str(time_str).strip(), "%I:%M %p")
    except (ValueError, IndexError):
        return None
    run_et = run_ts.astimezone(ET)
    year = run_et.year + (1 if mon - run_et.month < -6 else -1 if mon - run_et.month > 6 else 0)
    try:
        local = datetime(year, mon, day, hm.hour, hm.minute, tzinfo=ET)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def normalize_snapshots(raw: pd.DataFrame, sport: str) -> pd.DataFrame:
    """Pure: archive rows (legacy columns + sha/commit_date/sheet) -> normalised snapshot rows."""
    if raw.empty:
        return pd.DataFrame()
    df = raw.rename(columns=_COLS[sport]).copy()
    for c in _NUM + ["total", "under_odds", "open_total", "spread_raw", "open_spread_raw"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else float("nan")
    if sport == "nfl":  # NFL archive stores gs_fg/away_fg as fractions; CFB as percent
        df["gs_fg"] = df["gs_fg"] * 100.0
        df["away_fg"] = df["away_fg"] * 100.0
    names = [split_game(sport, g) for g in df["Game"]]
    df["away_raw"] = [a for a, _ in names]
    df["home_raw"] = [h for _, h in names]
    df["run_ts"] = [parse_run_ts(t, c) for t, c in zip(df.get("Timestamp", [None] * len(df)), df["commit_date"], strict=True)]
    df["kick_archive"] = [infer_kickoff(d, t, r) for d, t, r in zip(df["Date"], df["Time"], df["run_ts"], strict=True)]
    df["spread_abs"] = df["spread_raw"].abs()
    df["open_spread_abs"] = df["open_spread_raw"].abs()
    df["sport"] = sport
    df["line_book"] = LINE_BOOK[sport]
    keep = ["sport", "sheet", "sha", "commit_date", "run_ts", "kick_archive", "Game", "away_raw", "home_raw",
            *_NUM, "total", "under_odds", "open_total", "spread_abs", "open_spread_abs", "line_book"]
    return df[keep].rename(columns={"gs_fg": "gs_fg_archived", "away_fg": "away_fg_archived"})


def extract_archive(sport: str, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    """Every row of every distinct archive blob for ``sport`` (cached as parquet)."""
    cache = cache_dir / "git" / f"archive_{sport}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    sys.path.insert(0, str(REPO / "scripts"))
    from _git_history import iter_snapshots  # noqa: PLC0415  (scripts/ is not a package)

    path, suffix = ARCHIVE_FILES[sport]
    frames: list[pd.DataFrame] = []
    for snap, blob_file, first in iter_snapshots(path, suffix):
        if not first:
            continue
        try:
            sheets = {"csv": pd.read_csv(blob_file)} if suffix == ".csv" else pd.read_excel(blob_file, sheet_name=None)
        except Exception as exc:  # noqa: BLE001  (a corrupt blob is skipped, not fatal)
            print(f"  skip {snap.sha[:7]}: {exc}")
            continue
        for name, df in sheets.items():
            df = df.assign(sheet=name, sha=snap.sha, commit_date=snap.date.isoformat())
            frames.append(df.astype({c: str for c in df.columns if df[c].dtype == object}))
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache, index=False)
    return out


# ---------------------------------------------------------------- schedules + results
def nfl_schedule(csv_text: str, seasons: Iterable[int]) -> pd.DataFrame:
    g = pd.read_csv(io.StringIO(csv_text))
    g = g[g.season.isin(list(seasons))].copy()
    kick = pd.to_datetime(g.gameday + " " + g.gametime.fillna("13:00"), errors="coerce")
    return pd.DataFrame({
        "sport": "nfl", "season": g.season.astype(int), "week": g.week.astype(int),
        "home_id": g.home_team.str.lower(), "away_id": g.away_team.str.lower(),
        "kickoff": [k.to_pydatetime().replace(tzinfo=ET).astimezone(timezone.utc) if pd.notna(k) else None for k in kick],
        "home_score": g.home_score, "away_score": g.away_score,
        "close_spread_src": g.spread_line,      # nflverse: expected home margin (+ = home favoured)
        "close_total_src": g.total_line, "roof": g.roof, "neutral": g.location.eq("Neutral"),
        "venue_key": g.stadium_id, "wind_reported": g.wind, "temp_reported": g.temp,
    })


def cfb_schedule(games: Iterable[Mapping[str, Any]], lines: Iterable[Mapping[str, Any]] = ()) -> pd.DataFrame:
    close = {}
    for gm in lines:
        ln = next((x for x in gm.get("lines", []) if x.get("spread") is not None or x.get("overUnder") is not None), None)
        if ln:
            close[gm["id"]] = (ln.get("spread"), ln.get("overUnder"))
    rows = []
    seen: set[Any] = set()
    for gm in games:
        if gm.get("id") in seen:  # a seasonType=both cache overlaps the postseason pull
            continue
        seen.add(gm.get("id"))
        home = normalize_team("cfb", gm.get("homeTeam"), "cfbd")
        away = normalize_team("cfb", gm.get("awayTeam"), "cfbd")
        if not home or not away or not gm.get("startDate"):
            continue
        sp, tot = close.get(gm["id"], (None, None))
        rows.append({
            "sport": "cfb", "season": int(gm["season"]), "week": cfb_week(gm.get("week"), gm.get("seasonType")),
            "home_id": home, "away_id": away,
            "kickoff": datetime.fromisoformat(gm["startDate"].replace("Z", "+00:00")),
            "home_score": gm.get("homePoints"), "away_score": gm.get("awayPoints"),
            # CFBD spread is the home line (negative = home favoured) -> expected home margin = -spread
            "close_spread_src": -sp if sp is not None else None, "close_total_src": tot,
            "roof": None, "neutral": bool(gm.get("neutralSite")), "venue_key": gm.get("venueId"),
            "wind_reported": None, "temp_reported": None,
        })
    return pd.DataFrame(rows)


def attach_schedule(snaps: pd.DataFrame, sched: pd.DataFrame, tol_h: float = 36.0) -> tuple[pd.DataFrame, list[str]]:
    """Resolve team names and match each snapshot game to the schedule (same teams, kickoff within ``tol_h``).

    Returns (matched snapshots with game_id/season/week/kickoff, sorted unresolved game strings)."""
    if snaps.empty:
        return snaps, []
    sport = snaps["sport"].iat[0]
    names = pd.unique(pd.concat([snaps.away_raw, snaps.home_raw]).dropna())
    ids = {n: normalize_team(sport, n, LINE_BOOK[sport]) for n in names}
    s = snaps.assign(away_id=snaps.away_raw.map(ids), home_id=snaps.home_raw.map(ids))
    by_pair: dict[tuple[str, str], list[tuple[datetime, Mapping[str, Any]]]] = {}
    for r in sched.to_dict("records"):
        by_pair.setdefault((r["home_id"], r["away_id"]), []).append((r["kickoff"], r))
    keys: dict[tuple[str, str, datetime], Optional[Mapping[str, Any]]] = {}
    out_rows = []
    unresolved: set[str] = set()
    for r in s.to_dict("records"):
        k = (r["home_id"], r["away_id"], r["kick_archive"])
        if k not in keys:
            best = None
            cands = by_pair.get((r["home_id"], r["away_id"]), []) + by_pair.get((r["away_id"], r["home_id"]), [])
            for kick, row in cands:
                if r["kick_archive"] is not None and kick is not None:
                    dh = abs((kick - r["kick_archive"]).total_seconds()) / 3600
                    if dh <= tol_h and (best is None or dh < best[0]):
                        best = (dh, row)
            keys[k] = best[1] if best else None
        m = keys[k]
        if m is None:
            unresolved.add(str(r["Game"]))
            continue
        out_rows.append({**r, "season": m["season"], "week": m["week"], "kickoff": m["kickoff"],
                         "home_id": m["home_id"], "away_id": m["away_id"],
                         "game_id": f"{sport}:{m['season']}:{m['week']}:{m['away_id']}@{m['home_id']}"})
    return pd.DataFrame(out_rows), sorted(unresolved)


# ---------------------------------------------------------------- model replay + tiers
def replay(snaps: pd.DataFrame, home_elev: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Lead hours, recomputed v1 gs/away (percent) and the legacy signal tier per snapshot."""
    if snaps.empty:
        return snaps
    s = snaps.copy()
    s["lead_h"] = [(k - r).total_seconds() / 3600 for k, r in zip(s.kickoff, s.run_ts, strict=True)]
    s = s[s.lead_h >= MIN_LEAD_H].copy()
    elev = home_elev or {}
    gs, aw, tiers = [], [], []
    for r in s.to_dict("records"):
        run_et = r["run_ts"].astimezone(ET)
        imp = compute_impact_v1(r["sport"], run_et.month, _f(r["temp_fg"]), _f(r["wind_fg"]), _f(r["rain_fg"]),
                                _f(r["travel_alt"]), _f(r["away_temp"]), _f(r["home_temp"]),
                                home_elev_m=elev.get(r["home_id"]), era_date=run_et.date().isoformat())
        gs.append(imp.gs_fg_pct)
        aw.append(imp.away_fg_pct)
        if r["sport"] == "nfl":
            sig = nfl_signal(_f(r["wind_fg"]), _f(r["temp_fg"]), _f(r["rain_fg"]))
        else:  # archive opener is FanDuel-only: a labelled proxy for the consensus opener
            sig = cfb_signal(_f(r["wind_fg"]), _f(r["temp_fg"]), _f(r["rain_fg"]), _f(r["open_spread_abs"]),
                             _f(r["travel_alt"]), _f(r["home_temp"]), _f(r["away_temp"]), run_et.weekday())
        tiers.append(sig.level)
    s["gs_fg"] = gs
    s["away_fg"] = aw
    s["tier"] = tiers
    return s.sort_values(["game_id", "run_ts"]).reset_index(drop=True)


def _pick_fixed_leads(g: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lead in FIXED_LEADS_H:
        d = (g.lead_h - lead).abs()
        if len(d) and d.min() <= FIXED_LEAD_TOL_H:
            r = g.loc[d.idxmin()]
            out[f"wind_fg_{lead}h"] = r.wind_fg
            out[f"total_{lead}h"] = r.total
            out[f"gs_fg_{lead}h"] = r.gs_fg
        else:
            out[f"wind_fg_{lead}h"] = out[f"total_{lead}h"] = out[f"gs_fg_{lead}h"] = None
    return out


def summarize_games(snaps: pd.DataFrame) -> pd.DataFrame:
    """One row per game: alert snapshot (first non-"No Impact" tier at lead <= 240 h with a total),
    closing snapshot (last at lead <= 6 h with a total, else last with a total), fixed-lead forecasts."""
    rows = []
    for gid, g in snaps.groupby("game_id", sort=False):
        g = g.sort_values("run_ts")
        priced = g[g.total.notna()]
        if priced.empty:
            continue
        near = priced[priced.lead_h <= CLOSE_MAX_LEAD_H]
        close = near.iloc[-1] if not near.empty else priced.iloc[-1]
        alert_pool = priced[(priced.lead_h <= ALERT_MAX_LEAD_H) & (priced.tier != NO)]
        alert = alert_pool.iloc[0] if not alert_pool.empty else None
        last = g.iloc[-1]
        row = {
            "game_id": gid, "sport": last.sport, "season": last.season, "week": last.week, "kickoff": last.kickoff,
            "home_id": last.home_id, "away_id": last.away_id, "line_book": last.line_book, "n_snapshots": len(g),
            "max_lead_h": g.lead_h.max(), "close_lead_h": close.lead_h, "close_total": close.total,
            "close_under_odds": close.under_odds, "close_spread_abs": close.spread_abs,
            "close_wind_fg": close.wind_fg, "close_temp_fg": close.temp_fg, "close_rain_fg": close.rain_fg,
            "close_gs_fg": close.gs_fg, "open_total": priced.iloc[0].open_total,
            "tier_at_close": close.tier, "tier_max": _max_tier(g.tier),
            "alert": alert is not None,
            "alert_lead_h": alert.lead_h if alert is not None else None,
            "alert_tier": alert.tier if alert is not None else None,
            "alert_total": alert.total if alert is not None else None,
            "alert_under_odds": alert.under_odds if alert is not None else None,
            "alert_spread_abs": alert.spread_abs if alert is not None else None,
            "alert_wind_fg": alert.wind_fg if alert is not None else None,
            "alert_temp_fg": alert.temp_fg if alert is not None else None,
            "alert_gs_fg": alert.gs_fg if alert is not None else None,
            "model_match": float(((g.gs_fg - g.gs_fg_archived).abs() <= MATCH_TOL).mean()),
            **_pick_fixed_leads(g),   # weather from every snapshot; the total there may be missing
        }
        rows.append(row)
    return pd.DataFrame(rows)


_TIER_RANK = {"No Impact": 0, "Low Impact": 1, "Mid Impact": 2, "High Impact": 3, "Very High Impact": 4}


def _max_tier(tiers: Iterable[str]) -> str:
    return max(tiers, key=lambda t: _TIER_RANK.get(t, 0), default=NO)


# ---------------------------------------------------------------- grading
def grade_under(line: Optional[float], actual_total: Optional[float]) -> Optional[str]:
    if line is None or actual_total is None or (isinstance(line, float) and math.isnan(line)) \
            or (isinstance(actual_total, float) and math.isnan(actual_total)):
        return None
    return "win" if actual_total < line else "loss" if actual_total > line else "push"


def unit_profit(result: Optional[str], odds: Optional[float]) -> Optional[float]:
    if result is None:
        return None
    if result == "push":
        return 0.0
    if result == "loss":
        return -1.0
    o = _f(odds) or -110.0
    return o / 100.0 if o > 0 else 100.0 / -o


def attach_results(games: pd.DataFrame, sched: pd.DataFrame) -> pd.DataFrame:
    keyed = sched.assign(game_id=lambda d: d.sport + ":" + d.season.astype(str) + ":" + d.week.astype(str)
                         + ":" + d.away_id + "@" + d.home_id)
    cols = ["game_id", "home_score", "away_score", "close_spread_src", "close_total_src", "roof", "neutral",
            "venue_key", "wind_reported", "temp_reported"]
    g = games.merge(keyed[cols], on="game_id", how="left")
    g["actual_total"] = g.home_score + g.away_score
    g["actual_margin"] = g.home_score - g.away_score
    for prefix in ("alert", "close"):
        res = [grade_under(_f(t), _f(a)) for t, a in zip(g[f"{prefix}_total"], g.actual_total, strict=True)]
        g[f"{prefix}_result"] = res
        g[f"{prefix}_profit"] = [unit_profit(r, o) for r, o in zip(res, g[f"{prefix}_under_odds"], strict=True)]
    g["clv_pts"] = g.alert_total - g.close_total           # > 0: the total fell after the alert
    return g


# ---------------------------------------------------------------- actual weather (ERA5)
def venue_coords(sport: str, stadiums: pd.DataFrame, cfbd_venues: Iterable[Mapping[str, Any]] = ()) -> dict[Any, tuple[float, float, str]]:
    out: dict[Any, tuple[float, float, str]] = {}
    key = "nflverse_stadium_id" if sport == "nfl" else "cfbd_venue_id"
    for r in stadiums.dropna(subset=[key]).to_dict("records"):
        k = r[key] if sport == "nfl" else int(r[key])
        out[k] = (float(r["lat"]), float(r["lon"]), str(r.get("roof_type") or "open"))
    if sport == "cfb":
        for v in cfbd_venues:
            if v.get("id") not in out and v.get("latitude") is not None:
                out[v["id"]] = (float(v["latitude"]), float(v["longitude"]), "dome" if v.get("dome") else "open")
    return out


def window_mean(hourly: Mapping[str, list], kick: datetime) -> dict[str, Optional[float]]:
    """Kickoff-window actuals using the existing backtest contract (``pipeline.backtest._window`` /
    ``window_stats``: 3 hourly samples kickoff..+2h, mean wind/gust/temp, summed precipitation)."""
    st = window_stats(hourly, *_window(kick))
    return {"wind_act": st["wind"], "gust_act": st["gust"], "temp_act": st["temp"], "rain_act_mm": st["rain"],
            "dir_act": st["dir"]}


def _http_json(url: str) -> Any:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(300)
            elif exc.code >= 500:
                time.sleep(20)
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}")


def _covered(venue_roof: Optional[str], game_roof: Optional[str]) -> bool:
    """No outdoor weather for fixed domes or a game played with the roof closed (schedule roof state)."""
    return (venue_roof or "").lower() == "dome" or (game_roof or "").lower() in C.CLOSED_ROOF_STATES


def attach_actuals(games: pd.DataFrame, coords: Mapping[Any, tuple[float, float, str]], cache_dir: Path, *,
                   network: bool = True, get: Callable[[str], Any] = _http_json, pause_s: float = 0.4) -> pd.DataFrame:
    """ERA5 kickoff-window weather per game. The raw hourly payload is cached verbatim under
    ``cache_dir/era5_hourly`` (resume-safe) and the window is aggregated from it on every run."""
    cdir = cache_dir / "era5_hourly"
    cdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in games.to_dict("records"):
        c = coords.get(r.get("venue_key"))
        roof = r.get("roof") if isinstance(r.get("roof"), str) else None
        rec: dict[str, Any] = {"game_id": r["game_id"], "venue_roof": c[2] if c else None,
                               **dict.fromkeys(("wind_act", "gust_act", "temp_act", "rain_act_mm", "dir_act"))}
        if c and not _covered(c[2], roof):
            path = cdir / f"{r['game_id'].replace(':', '_').replace('@', '-')}.json"
            payload = json.loads(path.read_text()) if path.exists() else None
            if payload is None and network:
                kick: datetime = r["kickoff"]
                url = ERA5_URL.format(lat=c[0], lon=c[1], d0=kick.date(), d1=(kick + timedelta(hours=4)).date())
                payload = get(url)
                path.write_text(json.dumps(payload))
                time.sleep(pause_s)
            if payload is not None:
                rec.update(window_mean(payload["hourly"], r["kickoff"]))
        rows.append(rec)
    return games.merge(pd.DataFrame(rows), on="game_id", how="left")


# ---------------------------------------------------------------- orchestration
def _read_url(url: str, dest: Path, network: bool, headers: Optional[dict[str, str]] = None) -> bytes:
    if dest.exists():
        return dest.read_bytes()
    if not network:
        raise FileNotFoundError(f"{dest} missing and --no-network set")
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return body


def load_schedule(sport: str, seasons: list[int], cache_dir: Path, network: bool) -> tuple[pd.DataFrame, list]:
    if sport == "nfl":
        body = _read_url(NFLVERSE_GAMES_URL, cache_dir / "nflverse_games.csv", network)
        return nfl_schedule(body.decode(), seasons), []
    import os

    key = os.environ.get("CFBD_API_KEY", "").strip()
    hdr = {"Authorization": f"Bearer {key}"} if key else {}
    net = network and bool(key)
    games: list = []
    lines: list = []
    for y in seasons:
        for st, tag in (("regular", ""), ("postseason", "post_")):
            games += json.loads(_read_url(f"{CFBD_BASE}/games?year={y}&seasonType={st}&classification=fbs",
                                          cache_dir / "cfbd" / f"games_{tag}{y}.json", net, hdr))
            try:
                lines += json.loads(_read_url(f"{CFBD_BASE}/lines?year={y}&seasonType={st}&provider=DraftKings",
                                              cache_dir / "cfbd" / f"lines_{tag}{y}.json", net, hdr))
            except (FileNotFoundError, urllib.error.HTTPError):
                pass  # closing lines only sign the spread; games still grade on the archive total
    try:
        venues = json.loads(_read_url(f"{CFBD_BASE}/venues", cache_dir / "cfbd" / "venues.json", net, hdr))
    except (FileNotFoundError, urllib.error.HTTPError):
        venues = []
    return cfb_schedule(games, lines), venues


def run(sport: str, seasons: list[int], cache_dir: Path, *, refresh_git: bool = False, network: bool = True) -> dict[str, Any]:
    # CFB keeps both sheets: "Other" holds FBS-vs-FCS games (gradeable when priced). The schedule is
    # FBS-involved games only, so an Other-sheet FCS-vs-FCS row cannot match and is counted, not graded.
    snaps = normalize_snapshots(extract_archive(sport, cache_dir, refresh_git), sport)
    sched, venues = load_schedule(sport, seasons, cache_dir, network)
    matched, unresolved = attach_schedule(snaps, sched)
    matched = matched[matched.season.isin(seasons)]
    stad = pd.read_csv(REPO / "data" / "stadiums.csv")
    teams = pd.read_csv(REPO / "data" / "teams.csv")
    elev_by_stadium = dict(zip(stad.stadium_id, stad.elevation_m, strict=True))
    home_elev = {t: elev_by_stadium.get(s) for t, s in zip(teams.team_id, teams.home_stadium_id, strict=True)}
    rep = replay(matched, home_elev if sport == "cfb" else None)
    games = attach_results(summarize_games(rep), sched)
    games = attach_actuals(games, venue_coords(sport, stad, venues), cache_dir, network=network)
    cache_dir.mkdir(parents=True, exist_ok=True)
    rep.to_parquet(cache_dir / f"hist_snapshots_{sport}.parquet", index=False)
    games.to_parquet(cache_dir / f"hist_games_{sport}.parquet", index=False)
    other = set(snaps.loc[snaps.sheet == "Other", "Game"]) - set(snaps.loc[snaps.sheet != "Other", "Game"])
    unresolved_main = [u for u in unresolved if u not in other]
    n_arch = snaps.loc[snaps.sheet != "Other", "Game"].nunique() if not snaps.empty else 0
    return {"sport": sport, "seasons": seasons, "n_snapshot_rows": len(rep), "n_games": len(games),
            "n_graded": int(games.close_result.notna().sum()), "n_alerts": int(games.alert.sum()),
            "unresolved": unresolved_main, "unresolved_share": round(len(unresolved_main) / max(n_arch, 1), 4),
            "other_sheet_unmatched": len(set(unresolved) & other),
            "model_match_rate": round(float(((rep.gs_fg - rep.gs_fg_archived).abs() <= MATCH_TOL).mean()), 4) if len(rep) else None}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sport", choices=["nfl", "cfb"], action="append")
    ap.add_argument("--seasons", default="2024,2025")
    ap.add_argument("--cache-dir", type=Path, default=REPO / "data" / "backtest")
    ap.add_argument("--refresh-git", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    a = ap.parse_args(argv)
    from utils.env import load_repo_dotenv

    load_repo_dotenv()
    seasons = [int(s) for s in a.seasons.split(",")]
    for sport in a.sport or ["nfl", "cfb"]:
        meta = run(sport, seasons, a.cache_dir, refresh_git=a.refresh_git, network=not a.no_network)
        print(json.dumps({k: (v if k != "unresolved" else v[:20]) for k, v in meta.items()}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
