"""Historical backtest from the git line/forecast archive (docs/HISTORICAL_BACKTEST_SPEC.md).

The old generator committed ``nfl_weather.csv`` / ``cfb_weather.xlsx`` ~3x a day between
2024-09 and 2026-04. Every commit is a snapshot of every upcoming game at that moment: the
forecast the model saw at that lead, its impact numbers, and the book's line. Replaying those
blobs against the rebuilt model grades the 2024 and 2025 seasons **now** instead of waiting
for 2026 to accrue::

    python -m pipeline.backtest --from-git --seasons 2024,2025 [--sport nfl|cfb] [--no-network]

Stages (each resumable from a cache; ``--no-network`` runs entirely offline):

1. **snapshots** – ``extract_git_snapshots`` walks ``git log`` per file with
   ``scripts/_git_history.py`` (blob-hash dedupe, blobs materialised once), parses every
   distinct blob into one row per game and caches them as
   ``data/backtest/git/{sport}_snapshots.parquet``.
2. **identity** – ``load_schedule`` joins the archive rows to nflverse ``games.csv`` (NFL) and
   CFBD ``/games`` (CFB) for the canonical ``game_id`` (ARCH §4.1), kickoff, week and final
   score. Team strings resolve through ``pipeline/odds/teams.py`` + ``data/aliases``; the
   legacy ``Date`` has no year, so it is read as the MM/DD nearest the run's ET date. Names
   that never resolve, and matchups with no schedule row (the workbook's ``Other`` sheet is
   FCS-vs-FCS and unpriced), are counted in ``meta.hist.unresolved`` / ``unmatched``.
3. **replay** – per game the snapshots are sorted by run time and each one is re-scored with
   ``compute_impact_v1`` (era-aware) and the legacy signal tiers. The *alert snapshot* is the
   first tier != "No Impact" within ``ALERT_MAX_LEAD_H``; the *closing* snapshot is the last
   one within ``CLOSE_MAX_LEAD_H``.
4. **actuals** – ERA5 hourly archive over the kickoff window (``pipeline.backtest._window``)
   from ``data/backtest/era5`` (the same cache layout ``pipeline.stadiums.climatology`` writes,
   so a partial pull resumes); a window cache keeps re-runs instant.
5. **grade** – the UNDER at the alert total (``alert_*``) and, side by side, the UNDER at the
   closing total (``close_*``), so timing edge and weather edge can be told apart. Buckets are
   the 118 legacy definitions, aggregated per season into ``by_season`` on every grid row.

Everything downstream (bucket matching, ``_stats``, ``stadium_results``) is
``pipeline.backtest``'s; this module only produces its ``GameRow``s and the aggregates
``BacktestResult.payload`` merges.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import backtest as bt
from pipeline.model import clv as clv_mod
from pipeline.model import impact as impact_mod
from pipeline.model import signals as sig_mod
from pipeline.run_context import REPO_ROOT
from utils.timeutil import ET, UTC, bet_week_open, ensure_utc, parse_iso, to_et, utc_iso

PathLike = Union[str, Path]

# ---- constants ---------------------------------------------------------------------------

ARCHIVE_FILE = {"nfl": "nfl_weather.csv", "cfb": "cfb_weather.xlsx"}
ARCHIVE_SUFFIX = {"nfl": ".csv", "cfb": ".xlsx"}
# one book per sport in the archive, not the 3-book consensus the live board tracks now
LINE_BOOK = {"nfl": "betonline", "cfb": "fanduel"}

DEFAULT_GIT_CACHE = REPO_ROOT / "data" / "backtest" / "git"
DEFAULT_ERA5_CACHE = REPO_ROOT / "data" / "backtest" / "era5"

MIN_LEAD_H = 0.5           # snapshots inside the last half hour are not a bettable lead
ALERT_MAX_LEAD_H = 240.0   # a tier further out than 10 days never fired an alert
CLOSE_MAX_LEAD_H = 6.0     # "closing" line = the last snapshot inside 6 h of kickoff
ERR_LEADS = (24, 48, 72, 120, 168)
# 24/72/120 h are the GameRow's existing lead-1/3/5 columns; 48/168 get their own
ERR_LEAD_FIELD = {24: "wind_lead1", 48: "wind_l48", 72: "wind_lead3", 120: "wind_lead5", 168: "wind_l168"}
ERR_LEAD_N = {24: 1, 72: 3, 120: 5}
ERR_LEAD_TOL_H = 6.0
LEAD_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<=48h", 0.0, 48.0), ("48-120h", 48.0, 120.0), (">120h", 120.0, math.inf),
)
DEFAULT_ODDS = -110.0
# archived gs_fg precision: NFL is a full-precision fraction, CFB is rounded to 1 dp percent
MODEL_TOL = {"nfl": 1e-4, "cfb": 0.051}
ALL_HIST = "all_hist"

NFLVERSE_GAMES_URL = bt.NFLVERSE_GAMES_URL
CFBD_BASE = bt.CFBD_BASE
ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"
ERA5_THROTTLE_S = 1.5

MARKET_COLS: dict[str, dict[str, str]] = {
    # BetOnline in the NFL csv, FanDuel in the CFB workbook
    "nfl": {"total_now": "Total_now", "under_now": "Under_now", "spread_now": "Spread_now",
            "total_open": "Total_open", "under_open": "Under_open", "spread_open": "Spread_open"},
    "cfb": {"total_now": "FD_now", "under_now": "Odds_n", "spread_now": "Current",
            "total_open": "Fd_open", "under_open": "Odds_o", "spread_open": "Open"},
}
NUMERIC_COLS = ("temp_fg", "wind_fg", "rain_fg", "travel_alt", "home_temp", "away_temp",
                "gs_fg", "away_fg", "wind_avg", "year_built")
SNAP_COLUMNS = (
    "sport", "sheet", "sha", "commit_date", "run_ts", "run_month", "game", "date_label", "time_label",
    "away_raw", "home_raw", "stadium", "game_loc", "wind_dir_fg", "weakest_wind_effect", "orient",
    *NUMERIC_COLS, "total_now", "under_now", "spread_now", "total_open", "under_open", "spread_open",
)
STAT_KEYS = ("Wins", "Losses", "Push", "Sample", "Margin", "ROI", "+ CLV", "CLV %")   # backtest._stats

_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
_FRACTION_RE = re.compile(r"\.(\d+)")
ERA5_NAME_RE = re.compile(r"^era5h_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})_(.+)$")


# ---- small helpers -----------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    return bt._num(v)


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def _round(v: Optional[float], nd: int = 3) -> Optional[float]:
    return None if v is None else round(v, nd)


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def american_payout(odds: Optional[float]) -> float:
    """Profit per 1 unit staked at American odds (``None`` -> the -110 default)."""
    o = _num(odds)
    if o is None or o == 0:
        o = DEFAULT_ODDS
    return o / 100.0 if o > 0 else 100.0 / abs(o)


def roi_of(result: Optional[str], odds: Optional[float]) -> Optional[float]:
    if result == "W":
        return american_payout(odds)
    if result == "L":
        return -1.0
    if result == "P":
        return 0.0
    return None


def lead_band(lead_h: Optional[float]) -> Optional[str]:
    if lead_h is None:
        return None
    for name, lo, hi in LEAD_BANDS:
        if lo <= lead_h < hi:
            return name
    return LEAD_BANDS[-1][0]


TIER_RANK = {sig_mod.NO: 0, sig_mod.LOW: 1, sig_mod.MID: 2, sig_mod.HIGH: 3, sig_mod.VERY_HIGH: 4}


def tier_rank(tier: Optional[str]) -> int:
    """Ordering for 'did the tier survive to kickoff' (labels carry a suffix: 'Low (Rain)')."""
    if not tier:
        return 0
    for label, rank in sorted(TIER_RANK.items(), key=lambda kv: -len(kv[0])):
        if tier.startswith(label.split(" Impact")[0]):
            return rank
    return 0


# ---- 1. snapshot extraction ----------------------------------------------------------------

def _git_history_module() -> Any:
    """``scripts/_git_history.py`` (a script, not a package) imported by path.

    Registered in ``sys.modules`` before it executes: its ``@dataclass`` resolves annotations
    through ``sys.modules[cls.__module__]``."""
    import importlib.util
    import sys

    name = "_git_history"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


def _split_game(rec: Mapping[str, Any]) -> tuple[str, str]:
    """(away, home) from the legacy ``Game`` string, or the Other sheet's own columns.

    NFL ``'philadelphia vs green bay'``, CFB FBS ``'Kansas State @ Oklahoma State'``,
    CFB Other ``'Yale vs Princeton'`` (+ explicit Home Team / Away Team)."""
    home, away = _text(rec.get("Home Team")), _text(rec.get("Away Team"))
    if home and away:
        return away, home
    g = _text(rec.get("Game")) or ""
    for sep in (" @ ", " vs. ", " vs ", " VS "):
        if sep in g:
            a, h = g.split(sep, 1)
            return a.strip(), h.strip()
    return "", ""


def _run_ts(rec: Mapping[str, Any], commit_dt: datetime) -> datetime:
    """The legacy ``Timestamp`` is naive ET; sheets without one (Other) fall back to the commit.

    Fractional seconds are normalised first: ``datetime.fromisoformat`` on 3.10 only accepts 3 or
    6 digits, and the archive is not consistent about it."""
    raw = rec.get("Timestamp")
    if isinstance(raw, datetime):
        return ensure_utc(raw.replace(tzinfo=ET) if raw.tzinfo is None else raw)
    s = _text(raw)
    if s:
        s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
        try:
            return ensure_utc(parse_iso(s, default_tz=ET))
        except ValueError:
            pass
    return ensure_utc(commit_dt)


def snapshot_rows(df: Any, *, sport: str, sheet: str, sha: str, commit_date: str) -> list[dict[str, Any]]:
    """One archive frame (a csv blob or one workbook sheet) -> normalised snapshot rows."""
    if "gs_fg" not in getattr(df, "columns", ()):
        return []
    commit_dt = ensure_utc(parse_iso(commit_date))
    cols = MARKET_COLS[sport]
    out: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        away, home = _split_game(rec)
        if not away or not home:
            continue
        run_ts = _run_ts(rec, commit_dt)
        row: dict[str, Any] = {
            "sport": sport, "sheet": sheet, "sha": sha, "commit_date": commit_date,
            "run_ts": utc_iso(run_ts), "run_month": to_et(run_ts).month,
            "game": _text(rec.get("Game")), "date_label": _text(rec.get("Date")), "time_label": _text(rec.get("Time")),
            "away_raw": away, "home_raw": home, "stadium": _text(rec.get("stadium")),
            "game_loc": _text(rec.get("game_loc")), "wind_dir_fg": _text(rec.get("wind_dir_fg")),
            "weakest_wind_effect": _text(rec.get("weakest_wind_effect")), "orient": _text(rec.get("orient")),
        }
        for col in NUMERIC_COLS:
            row[col] = _num(rec.get(col))
        for key, col in cols.items():
            row[key] = _num(rec.get(col))
        out.append(row)
    return out


def _read_frames(path: Path, sport: str) -> Iterator[tuple[str, Any]]:
    """(sheet, DataFrame) for one materialised blob."""
    import pandas as pd

    if sport == "nfl":
        yield "csv", pd.read_csv(path)
        return
    for name, df in pd.read_excel(path, sheet_name=None).items():
        yield str(name), df


def extract_from_files(paths: Iterable[tuple[str, str, Path]], sport: str) -> list[dict[str, Any]]:
    """``(sha, commit_date, blob_path)`` triples -> snapshot rows (the git walk's pure half;
    the tests drive this from ``tests/fixtures/git_archive``)."""
    rows: list[dict[str, Any]] = []
    for sha, commit_date, path in paths:
        for sheet, df in _read_frames(Path(path), sport):
            rows.extend(snapshot_rows(df, sport=sport, sheet=sheet, sha=sha, commit_date=commit_date))
    return rows


def snapshots_path(cache_dir: PathLike, sport: str) -> Path:
    return Path(cache_dir) / f"{sport}_snapshots.parquet"


def extract_git_snapshots(sport: str, *, cache_dir: PathLike = DEFAULT_GIT_CACHE, refresh: bool = False,
                          no_network: bool = False, log: Callable[[str], None] = print) -> Any:
    """Every distinct blob of the sport's archive file -> a DataFrame of snapshot rows.

    Cached as ``{cache_dir}/{sport}_snapshots.parquet``; ``refresh`` re-walks the history.
    ``no_network`` here means "no git either": the cache must exist."""
    import pandas as pd

    cache = snapshots_path(cache_dir, sport)
    if cache.is_file() and not refresh:
        df = pd.read_parquet(cache)
        log(f"  git[{sport}]: {len(df)} snapshot rows from {cache.name}")
        return df
    if no_network:
        raise RuntimeError(f"--no-network: {cache} missing (run once without it to build the git cache)")
    gh = _git_history_module()
    rows: list[dict[str, Any]] = []
    blobs = 0
    for snap, blob_file, first in gh.iter_snapshots(ARCHIVE_FILE[sport], ARCHIVE_SUFFIX[sport]):
        if not first:
            continue
        blobs += 1
        try:
            frames = list(_read_frames(Path(blob_file), sport))
        except Exception as exc:  # noqa: BLE001 - a corrupt blob must not stop the walk
            log(f"  skip {snap.sha[:7]}: {exc}")
            continue
        for sheet, df in frames:
            rows.extend(snapshot_rows(df, sport=sport, sheet=sheet, sha=snap.sha[:7],
                                      commit_date=snap.date.isoformat()))
    out = pd.DataFrame(rows, columns=list(SNAP_COLUMNS))
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache, index=False)
    log(f"  git[{sport}]: {blobs} distinct blobs -> {len(out)} snapshot rows -> {cache}")
    return out


# ---- 2. schedule, results, identity ---------------------------------------------------------

@dataclass(frozen=True)
class SchedGame:
    game_id: str
    sport: str
    season: int
    week: int
    kickoff_utc: datetime
    home_id: str
    away_id: str
    home_name: Optional[str] = None
    away_name: Optional[str] = None
    stadium_id: Optional[str] = None
    stadium_name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    elevation_m: Optional[float] = None
    orientation_deg: Optional[float] = None
    weakest_wind_effect: Optional[str] = None
    roof_state: Optional[str] = None
    neutral: bool = False
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    source: str = ""


def _stadium_bits(book: Any, *keys: Optional[str]) -> dict[str, Any]:
    st = None
    if book is not None:
        for k in keys:
            if k:
                st = book.find_stadium(k)
                if st is not None:
                    break
    if st is None:
        return {}
    return {
        "stadium_id": st.stadium_id, "stadium_name": st.name, "lat": _num(st.lat), "lon": _num(st.lon),
        "elevation_m": _num(st.elevation_m), "orientation_deg": _num(getattr(st, "orientation_deg", None)),
        "weakest_wind_effect": getattr(st, "weakest_wind_effect", None),
        "roof_state": {"dome": "dome", "open": "outdoors"}.get(getattr(st, "roof_type", None) or ""),
    }


def _cache_text(path: Path, fetch: Callable[[], str], *, no_network: bool, label: str) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if no_network:
        raise RuntimeError(f"--no-network: {label} cache missing ({path})")
    text = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def load_nflverse_csv(cache_dir: PathLike, *, no_network: bool = False, url: str = NFLVERSE_GAMES_URL) -> str:
    def fetch() -> str:
        import httpx

        return httpx.get(url, timeout=60.0, headers={"User-Agent": bt.USER_AGENT}).text

    return _cache_text(Path(cache_dir) / "nflverse_games.csv", fetch, no_network=no_network, label="nflverse games.csv")


def load_cfbd_games(season: int, cache_dir: PathLike, *, no_network: bool = False,
                    api_key: Optional[str] = None) -> list[dict[str, Any]]:
    """CFBD ``/games?year=&seasonType=both&classification=fbs`` (FBS-involved games, incl. bowls)."""
    def fetch() -> str:
        import httpx

        key = api_key or os.environ.get("CFBD_API_KEY") or ""
        r = httpx.get(f"{CFBD_BASE}/games", params={"year": season, "seasonType": "both", "classification": "fbs"},
                      headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=60.0)
        r.raise_for_status()
        return json.dumps(r.json())

    text = _cache_text(Path(cache_dir) / f"cfbd_games_{season}.json", fetch, no_network=no_network,
                       label=f"CFBD games {season}")
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("games") or []
    return [g for g in payload if isinstance(g, dict)]


def nfl_schedule(csv_text: str, seasons: Sequence[int], book: Any = None) -> list[SchedGame]:
    import csv
    import io

    from pipeline.contracts import make_game_id
    from pipeline.schedule.nfl import _kickoff_et, _week

    want = {int(s) for s in seasons}
    out: list[SchedGame] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            season = int(float(row.get("season") or 0))
        except ValueError:
            continue
        if season not in want:
            continue
        kick_et = _kickoff_et(row)
        home = (row.get("home_team") or "").strip().lower()
        away = (row.get("away_team") or "").strip().lower()
        if kick_et is None or not home or not away:
            continue
        week = _week(row)
        roof = (row.get("roof") or "").strip().lower() or None
        bits = _stadium_bits(book, (row.get("stadium_id") or "").strip() or None, _text(row.get("stadium")))
        out.append(SchedGame(
            game_id=make_game_id("nfl", season, week, away, home), sport="nfl", season=season, week=week,
            kickoff_utc=kick_et.astimezone(UTC), home_id=home, away_id=away,
            home_name=_text(row.get("home_team")), away_name=_text(row.get("away_team")),
            neutral=(row.get("location") or "").strip().lower() == "neutral",
            home_score=int(float(row["home_score"])) if _text(row.get("home_score")) else None,
            away_score=int(float(row["away_score"])) if _text(row.get("away_score")) else None,
            source="nflverse", **{**bits, "roof_state": bits.get("roof_state") or roof},
        ))
    return out


def cfb_schedule(payload: Iterable[Mapping[str, Any]], season: int, book: Any = None) -> list[SchedGame]:
    from pipeline.contracts import make_game_id
    from pipeline.schedule.cfb import cfb_week
    from pipeline.stadiums.loader import slug

    out: list[SchedGame] = []
    for g in payload or []:
        start = g.get("startDate") or g.get("start_date")
        if not start:
            continue
        try:
            kick = ensure_utc(parse_iso(str(start)))
        except (TypeError, ValueError):
            continue
        home_name = _text(g.get("homeTeam") or g.get("home_team"))
        away_name = _text(g.get("awayTeam") or g.get("away_team"))
        if not home_name or not away_name:
            continue
        home_id = (book.resolve_team("cfb", home_name, fuzzy=False) if book is not None else None) or slug(home_name)
        away_id = (book.resolve_team("cfb", away_name, fuzzy=False) if book is not None else None) or slug(away_name)
        week = cfb_week(g.get("week"), g.get("seasonType") or g.get("season_type"))
        vid = g.get("venueId") if g.get("venueId") is not None else g.get("venue_id")
        bits = _stadium_bits(book, str(vid) if vid is not None else None, _text(g.get("venue")))
        hp = g.get("homePoints", g.get("home_points"))
        ap = g.get("awayPoints", g.get("away_points"))
        out.append(SchedGame(
            game_id=make_game_id("cfb", season, week, away_id, home_id), sport="cfb", season=season, week=week,
            kickoff_utc=kick, home_id=home_id, away_id=away_id, home_name=home_name, away_name=away_name,
            neutral=bool(g.get("neutralSite") or g.get("neutral_site")),
            home_score=int(hp) if hp is not None else None, away_score=int(ap) if ap is not None else None,
            source="cfbd", **bits,
        ))
    return out


def load_schedule(sport: str, seasons: Sequence[int], *, cache_dir: PathLike, book: Any = None,
                  no_network: bool = False, cfbd_key: Optional[str] = None) -> list[SchedGame]:
    if sport == "nfl":
        return nfl_schedule(load_nflverse_csv(cache_dir, no_network=no_network), seasons, book)
    out: list[SchedGame] = []
    for season in seasons:
        payload = load_cfbd_games(int(season), cache_dir, no_network=no_network, api_key=cfbd_key)
        out.extend(cfb_schedule(payload, int(season), book))
    return out


def game_date(date_label: Optional[str], run_ts: datetime) -> Optional[date]:
    """The legacy ``Date`` ('SUN 11/09') carries no year: the occurrence of MM/DD nearest the
    run's ET day. Nearest, not next -- the generator kept a game listed for a day or two after
    kickoff, and a strictly forward rule would push those into the following season."""
    m = _DATE_RE.search(str(date_label or ""))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    base = to_et(run_ts).date()
    cands = []
    for year in (base.year - 1, base.year, base.year + 1):
        try:
            cands.append(date(year, month, day))
        except ValueError:   # 02/29 in a non-leap year
            continue
    return min(cands, key=lambda d: abs((d - base).days)) if cands else None


class GameIndex:
    """Archive (away, home, date) -> SchedGame, with the team resolution the pipeline uses."""

    def __init__(self, sport: str, sched: Iterable[SchedGame], *, data_dir: Optional[Path] = None):
        self.sport = sport
        self.by_pair: dict[tuple[str, str], list[SchedGame]] = defaultdict(list)
        self.games: dict[str, SchedGame] = {}
        self._resolved: dict[str, Optional[str]] = {}
        self._data_dir = data_dir
        for g in sched:
            self.by_pair[(g.away_id, g.home_id)].append(g)
            self.games[g.game_id] = g
        self.unresolved: dict[str, int] = defaultdict(int)
        self.unmatched: dict[str, int] = defaultdict(int)

    def team(self, raw: str) -> Optional[str]:
        if raw not in self._resolved:
            from pipeline.odds import teams as teams_mod

            kwargs = {"data_dir": self._data_dir} if self._data_dir is not None else {}
            self._resolved[raw] = teams_mod.normalize_team(self.sport, raw, book="git_archive", **kwargs)
        return self._resolved[raw]

    def match(self, away_raw: str, home_raw: str, when: Optional[date], run_ts: datetime) -> Optional[SchedGame]:
        away, home = self.team(away_raw), self.team(home_raw)
        if away is None:
            self.unresolved[f"{self.sport}|{away_raw}"] += 1
        if home is None:
            self.unresolved[f"{self.sport}|{home_raw}"] += 1
        if away is None or home is None:
            return None
        cands = self.by_pair.get((away, home)) or self.by_pair.get((home, away)) or []
        if not cands:
            self.unmatched[f"{self.sport}|{away}@{home}"] += 1
            return None
        if when is not None:
            near = [g for g in cands if abs((to_et(g.kickoff_utc).date() - when).days) <= 1]
            if near:
                return min(near, key=lambda g: abs((to_et(g.kickoff_utc).date() - when).days))
        ahead = [g for g in cands if g.kickoff_utc >= run_ts]
        if ahead:
            return min(ahead, key=lambda g: g.kickoff_utc)
        self.unmatched[f"{self.sport}|{away}@{home}"] += 1
        return None


# ---- 3. per-snapshot model replay -------------------------------------------------------------

@dataclass
class Replay:
    """One archived snapshot of one game after the v1 replay."""
    run_ts: datetime
    lead_h: float
    sha: str
    sheet: str
    temp_fg: Optional[float]
    wind_fg: Optional[float]
    rain_fg: Optional[float]
    gs_fg: Optional[float]
    away_fg: Optional[float]
    gs_fg_archived: Optional[float]
    matched: Optional[bool]
    gs_fg_v2: Optional[float] = None
    away_fg_v2: Optional[float] = None
    tier: str = sig_mod.NO
    # everything cfb_signal needs besides wind/temp/rain, so the same tier can be recomputed on
    # the ERA5 actuals ("did the weather the alert fired on actually show up?")
    sig_inputs: dict[str, Any] = field(default_factory=dict)
    total_now: Optional[float] = None
    under_now: Optional[float] = None
    spread_now: Optional[float] = None
    total_open: Optional[float] = None
    under_open: Optional[float] = None
    spread_open: Optional[float] = None
    wind_dir_fg: Optional[str] = None


def replay_snapshot(rec: Mapping[str, Any], sched: SchedGame) -> Replay:
    """Re-score one archived row with ``compute_impact_v1`` (era of the commit) + the legacy tier."""
    sport = str(rec["sport"])
    run_ts = ensure_utc(parse_iso(str(rec["run_ts"])))
    lead_h = (sched.kickoff_utc - run_ts) / timedelta(hours=1)
    temp, wind, rain = _num(rec.get("temp_fg")), _num(rec.get("wind_fg")), _num(rec.get("rain_fg"))
    month = int(rec.get("run_month") or to_et(run_ts).month)
    era = str(rec.get("commit_date") or "")[:10] or None
    v1 = impact_mod.compute_impact_v1(
        sport, month, temp, wind, rain, _num(rec.get("travel_alt")), _num(rec.get("away_temp")),
        home_temp=_num(rec.get("home_temp")), roof_state=None,
        home_elev_m=sched.elevation_m, era_date=era,
    )
    gs = impact_mod.legacy_scale(v1.gs_fg_pct, sport)
    archived = _num(rec.get("gs_fg"))
    matched = None if archived is None else abs(gs - archived) <= MODEL_TOL[sport]
    weak = _text(rec.get("weakest_wind_effect")) or sched.weakest_wind_effect
    v2 = impact_mod.compute_impact_v2(
        sport, temp, wind, None, rain, None, _num(rec.get("travel_alt")),
        _num(rec.get("home_temp")), _num(rec.get("away_temp")),
        wind_dir_fg=_text(rec.get("wind_dir_fg")), orientation_deg=sched.orientation_deg,
        weakest_wind_effect=weak, roof_state=None,
    )
    if sport == "nfl":
        tier = sig_mod.nfl_signal(wind, temp, rain).label
    else:
        tier = sig_mod.cfb_signal(wind, temp, rain, _num(rec.get("spread_open")), _num(rec.get("travel_alt")),
                                  _num(rec.get("home_temp")), _num(rec.get("away_temp")),
                                  to_et(run_ts).weekday()).label
    return Replay(
        run_ts=run_ts, lead_h=lead_h, sha=str(rec.get("sha") or ""), sheet=str(rec.get("sheet") or ""),
        temp_fg=temp, wind_fg=wind, rain_fg=rain, gs_fg=gs,
        away_fg=impact_mod.legacy_scale(v1.away_fg_pct, sport), gs_fg_archived=archived, matched=matched,
        gs_fg_v2=impact_mod.legacy_scale(v2.gs_fg_pct, sport), away_fg_v2=impact_mod.legacy_scale(v2.away_fg_pct, sport),
        tier=tier, wind_dir_fg=_text(rec.get("wind_dir_fg")),
        sig_inputs={"open_spread": _num(rec.get("spread_open")), "travel_alt": _num(rec.get("travel_alt")),
                    "home_temp": _num(rec.get("home_temp")), "away_temp": _num(rec.get("away_temp")),
                    "weekday": to_et(run_ts).weekday()},
        **{k: _num(rec.get(k)) for k in ("total_now", "under_now", "spread_now", "total_open", "under_open", "spread_open")},
    )


def tier_for(sport: str, wind: Optional[float], temp: Optional[float], rain: Optional[float],
             sig_inputs: Mapping[str, Any]) -> str:
    """The legacy tier for one set of weather numbers. Swap in the ERA5 actuals and the same
    function answers "would this have fired if we had known the weather?"."""
    if sport == "nfl":
        return sig_mod.nfl_signal(wind, temp, rain).label
    return sig_mod.cfb_signal(wind, temp, rain, sig_inputs.get("open_spread"), sig_inputs.get("travel_alt"),
                              sig_inputs.get("home_temp"), sig_inputs.get("away_temp"),
                              int(sig_inputs.get("weekday") or 0)).label


def peak_snapshot(series: Sequence[Replay]) -> Optional[Replay]:
    """The first snapshot at the worst tier the game ever reached — the escalation bet."""
    if not series:
        return None
    top = max(tier_rank(r.tier) for r in series)
    if top < 1:
        return None
    return next(r for r in series if tier_rank(r.tier) == top)


def alert_snapshot(series: Sequence[Replay], max_lead_h: float = ALERT_MAX_LEAD_H) -> Optional[Replay]:
    """The alert pipeline/alerts.py would have fired: the first snapshot in a signal tier
    inside the horizon (one alert per game, earliest wins)."""
    for r in series:
        if r.lead_h <= max_lead_h and r.tier != sig_mod.NO and r.total_now is not None:
            return r
    return None


def closing_snapshot(series: Sequence[Replay], max_lead_h: float = CLOSE_MAX_LEAD_H) -> Optional[Replay]:
    """Last snapshot inside ``max_lead_h`` of kickoff, else the last one seen (flagged by lead)."""
    priced = [r for r in series if r.total_now is not None]
    if not priced:
        return None
    inside = [r for r in priced if r.lead_h <= max_lead_h]
    return inside[-1] if inside else priced[-1]


def nearest_lead(series: Sequence[Replay], lead: float, tol: float = ERR_LEAD_TOL_H) -> Optional[Replay]:
    cands = [r for r in series if abs(r.lead_h - lead) <= tol]
    return min(cands, key=lambda r: abs(r.lead_h - lead)) if cands else None


# ---- 4. ERA5 actuals --------------------------------------------------------------------------

def era5_index(cache_dir: PathLike) -> dict[str, list[tuple[str, str, Path]]]:
    """``{stadium_id: [(start, end, path), ...]}`` from the climatology cache layout."""
    out: dict[str, list[tuple[str, str, Path]]] = defaultdict(list)
    root = Path(cache_dir)
    if not root.is_dir():
        return {}
    for p in sorted(root.glob("era5h_*.json")):
        m = ERA5_NAME_RE.match(p.stem)
        if m:
            out[m.group(3)].append((m.group(1), m.group(2), p))
    return dict(out)


def era5_covers(entries: Sequence[tuple[str, str, Path]], day: date) -> Optional[Path]:
    iso = day.isoformat()
    for start, end, path in entries:
        if start <= iso <= end:
            return path
    return None


def half_year_window(day: date) -> tuple[str, str]:
    """The canonical ERA5 pull window a date belongs to (halves keep one request small)."""
    if day.month <= 6:
        return f"{day.year}-01-01", f"{day.year}-06-30"
    return f"{day.year}-07-01", f"{day.year}-12-31"


def missing_era5(rows: Iterable[bt.GameRow], index: Mapping[str, list[tuple[str, str, Path]]]
                 ) -> list[tuple[str, str, str]]:
    """``(stadium_id, start, end)`` windows the cache does not cover, newest first."""
    need: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in rows:
        kick = bt._dt(r.kickoff_utc)
        if kick is None or not r.stadium_id or r.lat is None or r.lon is None:
            continue
        day = kick.date()
        if era5_covers(index.get(r.stadium_id) or (), day) is not None:
            continue
        start, end = half_year_window(day)
        need[(r.stadium_id, start, end)] += 1
    return [k for k, _ in sorted(need.items(), key=lambda kv: (-kv[1], kv[0]))]


def fetch_era5(windows: Sequence[tuple[str, str, str]], points: Mapping[str, tuple[float, float]], *,
               cache_dir: PathLike, limit: int = 0, throttle_s: float = ERA5_THROTTLE_S,
               log: Callable[[str], None] = print) -> int:
    """One hourly ERA5 request per (stadium, window) into the same cache the climatology build
    uses, so a 429 just means "resume next run". ``limit`` caps requests per run."""
    from pipeline.stadiums.build_stadiums import Fetcher
    from pipeline.stadiums.climatology import hourly_params

    todo = list(windows)[:limit] if limit else list(windows)
    if not todo:
        return 0
    fetcher = Fetcher(Path(cache_dir), log=log)
    n = 0
    for i, (sid, start, end) in enumerate(todo, 1):
        pt = points.get(sid)
        if pt is None:
            continue
        log(f"  era5 {i}/{len(todo)} {sid} {start}..{end}")
        try:
            fetcher.json(f"era5h_{start}_{end}_{sid}", "GET", ERA5_URL,
                         params=hourly_params(pt[0], pt[1], start, end), throttle=throttle_s, retries=4)
            n += 1
        except RuntimeError as exc:
            log(f"    failed ({exc}); the next run resumes")
    return n


def window_cache_path(cache_dir: PathLike) -> Path:
    return Path(cache_dir) / "windows.parquet"


def load_window_cache(cache_dir: PathLike) -> dict[tuple[str, str], dict[str, Any]]:
    import pandas as pd

    p = window_cache_path(cache_dir)
    if not p.is_file():
        return {}
    df = pd.read_parquet(p)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in df.to_dict(orient="records"):
        out[(str(rec["stadium_id"]), str(rec["start"]))] = rec
    return out


def save_window_cache(cache_dir: PathLike, cache: Mapping[tuple[str, str], dict[str, Any]]) -> None:
    import pandas as pd

    if not cache:
        return
    p = window_cache_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(cache.values())).to_parquet(p, index=False)


def fill_actuals(rows: Sequence[bt.GameRow], *, cache_dir: PathLike, log: Callable[[str], None] = print) -> int:
    """ERA5 mean over ``[kick, kick+2h]`` per game, one stadium file opened at a time.
    Hits are memoised in ``{cache_dir}/windows.parquet`` so later runs never re-read the archive."""
    index = era5_index(cache_dir)
    cache = load_window_cache(cache_dir)
    by_stadium: dict[str, list[bt.GameRow]] = defaultdict(list)
    filled = 0
    for r in rows:
        kick = bt._dt(r.kickoff_utc)
        if kick is None or not r.stadium_id:
            continue
        start, end = bt._window(kick)
        hit = cache.get((r.stadium_id, start.isoformat()))
        if hit is not None:
            _apply_window(r, hit)
            filled += 1
            continue
        by_stadium[r.stadium_id].append(r)
    for sid, grp in sorted(by_stadium.items()):
        entries = index.get(sid) or []
        if not entries:
            continue
        wanted: dict[Path, list[bt.GameRow]] = defaultdict(list)
        for r in grp:
            kick = bt._dt(r.kickoff_utc)
            path = era5_covers(entries, kick.date()) if kick is not None else None
            if path is not None:
                wanted[path].append(r)
        for path, members in wanted.items():
            try:
                hourly = (json.loads(path.read_text(encoding="utf-8")) or {}).get("hourly") or {}
            except (OSError, ValueError) as exc:
                log(f"  era5 {path.name}: {exc}")
                continue
            for r in members:
                kick = bt._dt(r.kickoff_utc)
                start, end = bt._window(kick)
                st = bt.window_stats(hourly, start, end)
                if st["wind"] is None and st["temp"] is None:
                    continue
                rec = {"stadium_id": sid, "start": start.isoformat(), "temp": st["temp"], "wind": st["wind"],
                       "gust": st["gust"], "rain": st["rain"], "dir": st["dir"], "src": path.stem}
                cache[(sid, start.isoformat())] = rec
                _apply_window(r, rec)
                filled += 1
    save_window_cache(cache_dir, cache)
    return filled


def _apply_window(r: bt.GameRow, rec: Mapping[str, Any]) -> None:
    r.temp_act, r.wind_act = _num(rec.get("temp")), _num(rec.get("wind"))
    r.gust_act, r.rain_act = _num(rec.get("gust")), _num(rec.get("rain"))
    r.wind_dir_act = _text(rec.get("dir"))
    r.src_actual = f"era5:{rec.get('src')}"


# ---- 5. rows ------------------------------------------------------------------------------------

def build_game_row(sched: SchedGame, series: Sequence[Replay], *, week_gate: bool = True) -> Optional[bt.GameRow]:
    """One game's snapshot series -> the graded ``GameRow`` (alert bet, escalation bet, closing bet).

    ``week_gate`` drops every snapshot before Monday 00:00 ET of the game's own week, matching the
    live rule in ``alerts.py`` (never bet into next week's line)."""
    opened = bet_week_open(sched.kickoff_utc)
    series = sorted((r for r in series if r.lead_h >= MIN_LEAD_H and (not week_gate or r.run_ts >= opened)),
                    key=lambda r: r.run_ts)
    if not series:
        return None
    alert = alert_snapshot(series)
    peak = peak_snapshot([r for r in series if r.total_now is not None])
    close = closing_snapshot(series)
    first = series[0]
    # temp_fc / wind_fc are the CLOSING forecast for every game, the same basis
    # ``row_from_snapshots`` uses for the 2026 columns: one bucket assignment per game, so the
    # alert-bet and close-bet column groups describe the same set of games. The forecast the
    # alert actually saw is kept beside it as ``*_alert``.
    fc = close or alert or first
    matched = [r.matched for r in series if r.matched is not None]
    row = bt.GameRow(
        game_id=sched.game_id, sport=sched.sport, season=sched.season, week=sched.week,
        kickoff_utc=utc_iso(sched.kickoff_utc), home_id=sched.home_id, away_id=sched.away_id,
        home_name=sched.home_name, away_name=sched.away_name, stadium_id=sched.stadium_id,
        stadium_name=sched.stadium_name, neutral=sched.neutral, roof_state=sched.roof_state,
        lat=sched.lat, lon=sched.lon,
        temp_fc=fc.temp_fg, wind_fc=fc.wind_fg, rain_fc=fc.rain_fg, wind_dir_fc=fc.wind_dir_fg,
        lead_fc=_round(fc.lead_h, 2), gs_fg_v1=fc.gs_fg, away_fg_v1=fc.away_fg,
        gs_fg_v2=fc.gs_fg_v2, away_fg_v2=fc.away_fg_v2,
        total_open=first.total_open if first.total_open is not None else (close.total_open if close else None),
        total_close=close.total_now if close else None,
        spread_open=first.spread_open if first.spread_open is not None else (close.spread_open if close else None),
        spread_close=close.spread_now if close else None,
        ref_book=LINE_BOOK[sched.sport],
        home_score=sched.home_score, away_score=sched.away_score,
        src_forecast=f"git:{fc.sha}", src_result=("nflverse" if sched.sport == "nfl" else "cfbd"),
        hist=True, line_book=LINE_BOOK[sched.sport], sheet=fc.sheet, n_snapshots=len(series),
        tier_at_kick=series[-1].tier, gs_fg_archived=fc.gs_fg_archived,
        model_match_rate=_round(sum(1 for m in matched if m) / len(matched), 4) if matched else None,
    )
    for lead in ERR_LEADS:
        near = nearest_lead(series, float(lead))
        if near is None:
            continue
        setattr(row, ERR_LEAD_FIELD[lead], _round(near.wind_fg, 2))
        if lead in ERR_LEAD_N:
            setattr(row, f"temp_lead{ERR_LEAD_N[lead]}", _round(near.temp_fg, 2))
            setattr(row, f"rain_lead{ERR_LEAD_N[lead]}", _round(near.rain_fg, 2))
    if alert is not None:
        row.alert_tier, row.alert_lead_h = alert.tier, _round(alert.lead_h, 2)
        row.alert_total, row.alert_under_odds, row.alert_spread = alert.total_now, alert.under_now, alert.spread_now
        row.temp_alert, row.wind_alert, row.rain_alert = alert.temp_fg, alert.wind_fg, alert.rain_fg
    if peak is not None:
        row.peak_tier, row.peak_lead_h = peak.tier, _round(peak.lead_h, 2)
        row.peak_total, row.peak_under_odds = peak.total_now, peak.under_now
    if close is not None:
        row.close_lead_h, row.close_under_odds = _round(close.lead_h, 2), close.under_now
    return finalize_hist_row(row)


def finalize_hist_row(r: bt.GameRow) -> bt.GameRow:
    """Grade the under twice: at the alert total (the bet) and at the closing total."""
    if r.home_score is not None and r.away_score is not None:
        r.actual_total = float(r.home_score + r.away_score)
        r.result = float(r.home_score - r.away_score)
    r.clv_status = clv_mod.clv_status(r.total_open, r.total_close)
    r.under_result = bt.grade_under(r.alert_total, r.actual_total)
    r.margin = _round(r.alert_total - r.actual_total, 3) if r.alert_total is not None and r.actual_total is not None else None
    r.roi_alert = roi_of(r.under_result, r.alert_under_odds)
    r.peak_result = bt.grade_under(r.peak_total, r.actual_total)
    r.peak_margin = _round(r.peak_total - r.actual_total, 3) if r.peak_total is not None and r.actual_total is not None else None
    r.roi_peak = roi_of(r.peak_result, r.peak_under_odds)
    r.close_result = bt.grade_under(r.total_close, r.actual_total)
    r.close_margin = _round(r.total_close - r.actual_total, 3) if r.total_close is not None and r.actual_total is not None else None
    r.roi_close = roi_of(r.close_result, r.close_under_odds)
    if r.alert_total is not None and r.total_close is not None:
        r.clv_pts = _round(r.alert_total - r.total_close, 2)
    if r.wind_alert is not None and r.wind_act is not None:
        r.wind_err_alert = _round(abs(r.wind_alert - r.wind_act), 2)
    return r


def lead_error_rows(rows: Iterable[bt.GameRow]) -> list[dict[str, Any]]:
    """Long-format forecast error per lead (``data/backtest/hist_leads.parquet``)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.wind_act is None:
            continue
        pairs: list[tuple[str, Optional[float]]] = [("alert", r.wind_alert), ("close", r.wind_fc)]
        pairs += [(f"l{lead}", getattr(r, ERR_LEAD_FIELD[lead], None)) for lead in ERR_LEADS]
        for label, wind in pairs:
            if wind is None:
                continue
            out.append({"game_id": r.game_id, "sport": r.sport, "season": r.season, "lead": label,
                        "wind_fg": wind, "wind_act": r.wind_act, "wind_err": _round(abs(wind - r.wind_act), 2)})
    return out


# ---- 6. aggregation ------------------------------------------------------------------------------

def season_key(r: bt.GameRow) -> Optional[str]:
    return str(r.season) if r.season is not None else None


def season_grid(rows: Sequence[bt.GameRow], defs: Sequence[bt.Bucket], on: str = "forecast", *,
                result_field: str = "under_result", margin_field: str = "margin") -> dict[int, dict[str, dict[str, Any]]]:
    """``{bucket_id: {"2024": stats, "2025": stats, "all_hist": stats}}`` over the legacy buckets."""
    seasons = sorted({s for s in (season_key(r) for r in rows) if s})
    out: dict[int, dict[str, dict[str, Any]]] = {}
    for b in defs:
        members = [] if b.is_separator else [
            r for r in rows if bt.bucket_matches(b, r.sport, *bt.bucket_inputs(r, on), r.spread_abs, r.clv_status)
        ]
        block = {s: bt._stats([r for r in members if season_key(r) == s], result_field=result_field,
                              margin_field=margin_field) for s in seasons}
        block[ALL_HIST] = bt._stats(members, result_field=result_field, margin_field=margin_field)
        out[b.id] = block
    return out


def tier_scorecard(rows: Sequence[bt.GameRow]) -> list[dict[str, Any]]:
    """Per sport x tier x lead band: n, win %, ROI, CLV %, mean |wind error|, persistence and the
    evaporation rate.

    Keyed on ``peak_tier`` — the worst tier the game ever reached — and grading the escalation bet
    taken at that snapshot. Keying on ``alert_tier`` (the FIRST tier of any kind) files a game that
    opened Low and became Very High under Low, which undercounted the severe tiers roughly 4x.

    ``evaporated`` is the share whose ERA5 actuals would not have fired any tier: the bet was on
    weather that never showed up. It is the single most useful number next to a Low row."""
    groups: dict[tuple[str, str, str], list[bt.GameRow]] = defaultdict(list)
    for r in rows:
        if not r.peak_tier or r.peak_result is None:
            continue
        band = lead_band(r.peak_lead_h)
        if band is None:
            continue
        groups[(r.sport, r.peak_tier, band)].append(r)
    order = {name: i for i, (name, _, _) in enumerate(LEAD_BANDS)}
    out = []
    for (sport, tier, band), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0], -tier_rank(kv[0][1]), order.get(kv[0][2], 9))
    ):
        wins = sum(1 for r in members if r.peak_result == "W")
        losses = sum(1 for r in members if r.peak_result == "L")
        rois = [r.roi_peak for r in members if r.roi_peak is not None]
        clv = [r for r in members if r.clv_pts is not None]
        persist = [r for r in members if r.tier_at_kick]
        known = [r for r in members if r.tier_on_actual]
        out.append({
            "sport": sport, "tier": tier, "tier_slug": _tier_slug(tier), "lead_band": band, "n": len(members),
            "wins": wins, "losses": losses, "push": sum(1 for r in members if r.peak_result == "P"),
            "win_pct": _round(wins / (wins + losses), 4) if wins + losses else None,
            "roi": _round(sum(rois) / len(rois), 4) if rois else None,
            "clv_pct": _round(sum(1 for r in clv if (r.clv_pts or 0) > 0) / len(clv), 4) if clv else None,
            "avg_clv": _round(_mean([r.clv_pts for r in clv]), 3) if clv else None,
            "wind_err": _round(_mean([r.wind_err_alert for r in members]), 2),
            "persistence": _round(sum(1 for r in persist if tier_rank(r.tier_at_kick) >= tier_rank(r.peak_tier))
                                  / len(persist), 4) if persist else None,
            "evaporated": _round(sum(1 for r in known if r.tier_on_actual == sig_mod.NO) / len(known), 4) if known else None,
            "n_actual": len(known),
        })
    return out


def _tier_slug(label: Optional[str]) -> Optional[str]:
    from pipeline.alerts import signal_slug

    return signal_slug(label)


def hist_game_rows(rows: Sequence[bt.GameRow], defs: Sequence[bt.Bucket], on: str = "forecast") -> list[dict[str, Any]]:
    """One slim row per graded historical game (the Backtest tab's 'graded games' list)."""
    out = []
    for r in rows:
        if r.close_result is None and r.under_result is None:
            continue
        b = bt.first_match(defs, r.sport, *bt.bucket_inputs(r, on), r.spread_abs, r.clv_status)
        out.append({
            "game_id": r.game_id, "sport": r.sport, "season": r.season, "week": r.week, "kickoff_utc": r.kickoff_utc,
            "away_name": r.away_name, "home_name": r.home_name, "stadium_name": r.stadium_name,
            "alert_tier": r.alert_tier, "tier_at_kick": r.tier_at_kick, "alert_lead_h": r.alert_lead_h,
            "peak_tier": r.peak_tier, "peak_lead_h": r.peak_lead_h, "peak_total": r.peak_total,
            "peak_result": r.peak_result, "roi_peak": r.roi_peak, "tier_on_actual": r.tier_on_actual,
            "alert_total": r.alert_total, "alert_under_odds": r.alert_under_odds, "close_total": r.total_close,
            "close_lead_h": r.close_lead_h, "total_open": r.total_open, "actual_total": r.actual_total,
            "under_result": r.under_result, "close_result": r.close_result, "roi_alert": r.roi_alert,
            "clv_pts": r.clv_pts, "clv_status": r.clv_status, "wind_fc": r.wind_fc, "wind_act": r.wind_act,
            "temp_fc": r.temp_fc, "temp_act": r.temp_act, "spread_open": r.spread_open,
            "wind_alert": r.wind_alert, "temp_alert": r.temp_alert, "wind_err_alert": r.wind_err_alert,
            "gs_fg_v1": r.gs_fg_v1, "gs_fg_v2": r.gs_fg_v2, "line_book": r.line_book, "Signal": b.id if b else None,
        })
    out.sort(key=lambda d: (d["kickoff_utc"] or "", d["game_id"]))
    return out


def coverage(rows: Sequence[bt.GameRow]) -> list[dict[str, Any]]:
    """Season x week: snapshots replayed, games seen, games graded."""
    seen: dict[tuple[str, int, int], list[bt.GameRow]] = defaultdict(list)
    for r in rows:
        seen[(r.sport, int(r.season or 0), int(r.week or 0))].append(r)
    out = []
    for (sport, season, week), members in sorted(seen.items()):
        out.append({
            "sport": sport, "season": season, "week": week,
            "snapshots": sum(r.n_snapshots or 0 for r in members),
            "games": len(members),
            "priced": sum(1 for r in members if r.total_close is not None),
            "alerted": sum(1 for r in members if r.alert_tier),
            "graded": sum(1 for r in members if r.close_result is not None),
            "bet_graded": sum(1 for r in members if r.under_result is not None),
            "with_actual": sum(1 for r in members if r.wind_act is not None),
        })
    return out


# ---- 7. orchestration ------------------------------------------------------------------------------

@dataclass
class HistResult:
    rows: list[bt.GameRow] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    by_season: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    by_season_close: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    scorecard: list[dict[str, Any]] = field(default_factory=list)
    games: list[dict[str, Any]] = field(default_factory=list)
    stadiums: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def block(self) -> dict[str, Any]:
        """What ``BacktestResult.hist`` carries into ``board/backtest.json``."""
        return {
            "by_season": {str(k): v for k, v in self.by_season.items()},
            "by_season_close": {str(k): v for k, v in self.by_season_close.items()},
            "stadium_results": self.stadiums, "tier_scorecard": self.scorecard,
            "hist_games": self.games, "leads": self.leads, "meta": self.meta,
        }


def build_rows(sport: str, seasons: Sequence[int], *, snapshots: Any, sched: Sequence[SchedGame],
               data_dir: Optional[Path] = None, log: Callable[[str], None] = print
               ) -> tuple[list[bt.GameRow], GameIndex, dict[str, int], dict[str, dict[str, Any]]]:
    """Snapshot rows + schedule -> one graded ``GameRow`` per matched game.

    The third return value counts rows per source sheet and how many of them found no schedule
    game: the workbook's ``Other`` sheet is FCS-vs-FCS and carries no lines, so most of it is
    expected to miss the FBS schedule."""
    want = {int(s) for s in seasons}
    index = GameIndex(sport, [g for g in sched if g.season in want], data_dir=data_dir)
    per_game: dict[str, list[Replay]] = defaultdict(list)
    pair_cache: dict[tuple[str, str, Optional[str]], Optional[SchedGame]] = {}
    counts: dict[str, int] = defaultdict(int)
    for rec in snapshots.to_dict(orient="records"):
        sheet = str(rec.get("sheet") or "?")
        counts["rows"] += 1
        counts[f"rows:{sheet}"] += 1
        run_ts = ensure_utc(parse_iso(str(rec["run_ts"])))
        when = game_date(rec.get("date_label"), run_ts)
        key = (str(rec.get("away_raw")), str(rec.get("home_raw")), when.isoformat() if when else None)
        if key not in pair_cache:
            pair_cache[key] = index.match(key[0], key[1], when, run_ts)
        game = pair_cache[key]
        if game is None or game.season not in want:
            counts[f"unmatched:{sheet}"] += 1
            continue
        per_game[game.game_id].append(replay_snapshot(rec, game))
    rows, sig_ctx = [], {}
    for gid, series in per_game.items():
        row = build_game_row(index.games[gid], series)
        if row is None:
            continue
        rows.append(row)
        usable = sorted((r for r in series if r.total_now is not None), key=lambda r: r.run_ts)
        ref = peak_snapshot(usable) or (usable[-1] if usable else None)
        if ref is not None:
            sig_ctx[gid] = ref.sig_inputs
    rows.sort(key=lambda r: (r.kickoff_utc or "", r.game_id))
    sheets = sorted(k.split(":", 1)[1] for k in counts if k.startswith("rows:"))
    misses = ", ".join(f"{sh} {counts.get('unmatched:' + sh, 0)}/{counts['rows:' + sh]}" for sh in sheets)
    log(f"  {sport}: {counts['rows']} snapshot rows -> {len(rows)} games "
        f"({sum(1 for r in rows if r.alert_tier)} alerted, {sum(1 for r in rows if r.close_result)} graded); "
        f"rows with no schedule game: {misses}")
    return rows, index, dict(counts), sig_ctx


def run(*, seasons: Sequence[int], defs: Sequence[bt.Bucket], sport: Optional[str] = None,
        git_cache: PathLike = DEFAULT_GIT_CACHE, era5_cache: PathLike = DEFAULT_ERA5_CACHE,
        no_network: bool = False, refresh_git: bool = False, era5_max_fetch: int = 0,
        bucket_on: str = "forecast", now: Optional[datetime] = None, cfbd_key: Optional[str] = None,
        data_dir: Optional[Path] = None, book: Any = None, log: Callable[[str], None] = print) -> HistResult:
    """The whole ``--from-git`` mode: extract, join, replay, grade, aggregate."""
    sports = [sport] if sport else ["nfl", "cfb"]
    seasons = [int(s) for s in seasons]
    if book is None:
        try:
            from pipeline.stadiums.loader import load_stadium_book

            book = load_stadium_book(data_dir) if data_dir else load_stadium_book()
        except Exception as exc:  # noqa: BLE001 - the join still works off raw ids
            log(f"  stadium book unavailable ({exc}); stadium fields will be empty")
    rows: list[bt.GameRow] = []
    unresolved: dict[str, int] = {}
    unmatched: dict[str, int] = {}
    snap_counts: dict[str, int] = {}
    sheet_counts: dict[str, int] = {}
    sig_by_game: dict[str, dict[str, Any]] = {}
    for sp in sports:
        snaps = extract_git_snapshots(sp, cache_dir=git_cache, refresh=refresh_git,
                                      no_network=no_network and not refresh_git, log=log)
        sched = load_schedule(sp, seasons, cache_dir=git_cache, book=book, no_network=no_network, cfbd_key=cfbd_key)
        log(f"  {sp}: {len(sched)} scheduled games in {seasons}")
        sp_rows, index, counts, sig_ctx = build_rows(sp, seasons, snapshots=snaps, sched=sched,
                                                    data_dir=data_dir, log=log)
        rows.extend(sp_rows)
        sig_by_game.update(sig_ctx)
        snap_counts[sp] = counts["rows"]
        sheet_counts.update({f"{sp}:{k}": v for k, v in counts.items() if k != "rows"})
        unresolved.update(index.unresolved)
        unmatched.update(index.unmatched)

    index_era5 = era5_index(era5_cache)
    missing = missing_era5(rows, index_era5)
    if missing and not no_network and era5_max_fetch:
        got = fetch_era5(missing, {r.stadium_id: (r.lat, r.lon) for r in rows
                                   if r.stadium_id and r.lat is not None and r.lon is not None},
                         cache_dir=era5_cache, limit=era5_max_fetch, log=log)
        log(f"  era5: fetched {got}/{len(missing)} missing window(s)")
    n_act = fill_actuals(rows, cache_dir=era5_cache, log=log)
    for r in rows:
        if r.wind_act is not None and r.game_id in sig_by_game:
            r.tier_on_actual = tier_for(r.sport, r.wind_act, r.temp_act, r.rain_act, sig_by_game[r.game_id])
        finalize_hist_row(r)
    # only the games still without an actual are a gap: once windows.parquet has a game's mean the
    # hourly file it came from is not needed again (and CI restores the reduction, not the archive)
    still_missing = missing_era5([r for r in rows if r.wind_act is None], era5_index(era5_cache))
    log(f"  era5: {n_act}/{len(rows)} games with actuals; {len(still_missing)} window(s) to fetch")

    graded = [r for r in rows if r.close_result is not None]
    bet_graded = [r for r in rows if r.under_result is not None]
    matched_rates = [r.model_match_rate for r in rows if r.model_match_rate is not None]
    res = HistResult(
        rows=rows, leads=lead_error_rows(rows),
        by_season=season_grid(rows, defs, bucket_on),
        by_season_close=season_grid(rows, defs, bucket_on, result_field="close_result", margin_field="close_margin"),
        scorecard=tier_scorecard(rows), games=hist_game_rows(rows, defs, bucket_on),
        stadiums=bt.stadium_results(rows, utc_iso(now) if now else None,
                                    result_field="close_result", margin_field="close_margin"),
        meta={
            "seasons": seasons, "sports": sports, "n_snapshots": sum(snap_counts.values()),
            "n_games": len(rows), "n_alerted": sum(1 for r in rows if r.alert_tier),
            "n_graded": len(graded), "n_bet_graded": len(bet_graded),
            "n_with_actual": sum(1 for r in rows if r.wind_act is not None),
            "model_match_rate": _round(_mean(matched_rates), 4),
            "n_peak_graded": sum(1 for r in rows if r.peak_result is not None),
            "evaporated": _round(sum(1 for r in rows if r.peak_tier and r.tier_on_actual == sig_mod.NO)
                                 / max(1, sum(1 for r in rows if r.peak_tier and r.tier_on_actual)), 4),
            "line_book": LINE_BOOK, "bucket_on": bucket_on,
            "unresolved": [f"{k} x{v}" for k, v in sorted(unresolved.items(), key=lambda kv: -kv[1])][:100],
            "n_unresolved": len(unresolved),
            "unmatched": [f"{k} x{v}" for k, v in sorted(unmatched.items(), key=lambda kv: -kv[1])][:100],
            "n_unmatched": len(unmatched), "rows_by_sheet": sheet_counts,
            "coverage": coverage(rows),
            "era5_windows_missing": len(still_missing),
        },
    )
    return res


def print_report(res: HistResult, log: Callable[[str], None] = print) -> None:
    """Coverage table + tier scorecard (the CLI's stdout contract, spec §3.2)."""
    m = res.meta
    log(f"historical: {m['n_games']} games, {m['n_graded']} graded at the close, {m['n_alerted']} alerted "
        f"({m['n_bet_graded']} alert bets graded), {m['n_with_actual']} with ERA5 actuals; "
        f"model match {m['model_match_rate']}")
    log("  coverage per season (weeks, then totals)")
    by_season: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for c in m["coverage"]:
        by_season[(c["sport"], c["season"])].append(c)
    for (sport, season), cells in sorted(by_season.items()):
        weeks = ",".join(str(c["week"]) for c in sorted(cells, key=lambda c: c["week"]))
        log(f"    {sport} {season}: weeks [{weeks}]")
        log(f"      games={sum(c['games'] for c in cells)} priced={sum(c['priced'] for c in cells)} "
            f"alerted={sum(c['alerted'] for c in cells)} graded={sum(c['graded'] for c in cells)} "
            f"bets={sum(c['bet_graded'] for c in cells)} actuals={sum(c['with_actual'] for c in cells)} "
            f"snapshots={sum(c['snapshots'] for c in cells)}")
    log("  tier scorecard, keyed on the PEAK tier (sport tier band: n win% roi clv% err persist evaporated)")
    for s in res.scorecard:
        log(f"    {s['sport']:<3} {s['tier']:<18} {s['lead_band']:<9} n={s['n']:<4} win={s['win_pct']} "
            f"roi={s['roi']} clv={s['clv_pct']} err={s['wind_err']} persist={s['persistence']} "
            f"evap={s['evaporated']}")
    if m["n_unresolved"]:
        log(f"  unresolved names: {m['n_unresolved']} -> {m['unresolved'][:5]}")
    if m["n_unmatched"]:
        log(f"  unmatched matchups: {m['n_unmatched']} -> {m['unmatched'][:5]}")


__all__ = [
    "ALERT_MAX_LEAD_H", "ALL_HIST", "CLOSE_MAX_LEAD_H", "DEFAULT_ERA5_CACHE", "DEFAULT_GIT_CACHE", "ERR_LEADS",
    "ERR_LEAD_FIELD", "LINE_BOOK", "MIN_LEAD_H", "STAT_KEYS", "GameIndex", "HistResult", "Replay", "SchedGame", "alert_snapshot",
    "american_payout", "build_game_row", "build_rows", "cfb_schedule", "closing_snapshot", "coverage",
    "era5_covers", "era5_index", "extract_from_files", "extract_git_snapshots", "fetch_era5", "fill_actuals",
    "finalize_hist_row", "game_date", "half_year_window", "hist_game_rows", "lead_band", "lead_error_rows",
    "load_cfbd_games", "load_nflverse_csv", "load_schedule", "missing_era5", "nearest_lead", "nfl_schedule",
    "peak_snapshot", "print_report", "replay_snapshot", "roi_of", "run", "season_grid", "snapshot_rows", "snapshots_path",
    "tier_for", "tier_rank", "tier_scorecard",
]
