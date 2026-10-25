"""Team-game stats feed for the team-total model (``pipeline.model.team_totals``).

Per sport, completed team-games for this season and the last two, turned into
:class:`~pipeline.model.team_totals.TeamGame` rows and cached in ``state/team_stats.json``
(schema-versioned, synced with R2 like the other state files). Raw payloads are captured
verbatim under ``raw_dir/teamstats/`` before parsing; parsers are pure.

Sources:
* NFL: nflverse ``stats_team_week_{season}.csv`` (EPA, plays) + nflverse ``games.csv`` (points).
  No drives or success-rate feed, so those metrics stay empty for NFL.
* CFB: CFBD ``/stats/game/advanced?year=`` (PPA, success rate, plays, drives) + ``/games?year=``
  (points, FBS classification) + ``/player/returning?year=`` (early-season prior shrink).

Quota: CFBD calls are shared with the live schedule. The current season is re-fetched on a fixed
cadence while games are scheduled (``MIN_REFRESH_H``: NFL 12 h, free; CFB 48 h, 2 calls each,
~30 calls a month); a fixed cadence also picks up stats a feed publishes late. Past seasons are
fetched once and kept; returning production once per season; a failed fetch backs off ``RETRY_H``.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline import state as pstate
from pipeline.model.team_totals import (
    Rating,
    TeamGame,
    blend_weight,
    build_ratings,
    features,
    league_total,
    predict_total,
)
from pipeline.odds.teams import get_resolver
from pipeline.schedule.cfb import cfb_week
from pipeline.stadiums.loader import slug

NFLVERSE_TEAM_WEEK_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CFBD_BASE = "https://api.collegefootballdata.com"
MIN_REFRESH_H = {"nfl": 12.0, "cfb": 48.0}   # CFBD calls are shared with the live schedule
GAME_DONE_H = 4.0            # a game counts as finished this long after kickoff
RETRY_H = 6.0                # after a failed fetch, wait this long before trying that season again
HISTORY_SEASONS = 2          # seasons kept before the current one


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


# ---------------------------------------------------------------- pure parsers

def parse_nfl(stats_csv: str, games_csv: str, season: int) -> list[TeamGame]:
    """nflverse team-week stats + games.csv -> TeamGame rows (REG + POST) for ``season``."""
    pts: dict[tuple[str, str], tuple[float, float]] = {}
    for g in csv.DictReader(io.StringIO(games_csv)):
        hs, as_ = _f(g.get("home_score")), _f(g.get("away_score"))
        if hs is None or as_ is None or str(g.get("season")) != str(season):
            continue
        pts[(g["game_id"], g["home_team"].lower())] = (hs, as_)
        pts[(g["game_id"], g["away_team"].lower())] = (as_, hs)
    off: dict[tuple[str, str], dict[str, Any]] = {}
    for r in csv.DictReader(io.StringIO(stats_csv)):
        if str(r.get("season")) != str(season) or (r.get("season_type") or "REG") not in ("REG", "POST"):
            continue
        plays = sum(_f(r.get(c)) or 0.0 for c in ("attempts", "sacks_suffered", "carries"))
        epa = (_f(r.get("passing_epa")) or 0.0) + (_f(r.get("rushing_epa")) or 0.0)
        off[(r["game_id"], r["team"].lower())] = {"opp": (r.get("opponent_team") or "").lower(),
                                                  "week": int(float(r.get("week") or 0)),
                                                  "plays": plays or None, "eff": epa / plays if plays else None}
    rows = []
    for (gid, team), o in off.items():
        d = off.get((gid, o["opp"]), {})
        pf, pa = pts.get((gid, team), (None, None))
        if pf is None:
            continue
        rows.append(TeamGame("nfl", season, o["week"], gid, team, o["opp"], eff=o["eff"], plays=o["plays"], pf=pf,
                             d_eff=d.get("eff"), d_plays=d.get("plays"), pa=pa))
    return rows


def parse_cfb(advanced: Sequence[Mapping[str, Any]], games: Sequence[Mapping[str, Any]], season: int,
              resolve: Callable[[str], Optional[str]]) -> tuple[list[TeamGame], set[str]]:
    """CFBD advanced game stats + games -> (TeamGame rows, FBS team ids) for ``season``."""
    pts: dict[tuple[Any, str], tuple[float, float]] = {}
    week_of: dict[Any, int] = {}
    fbs: set[str] = set()
    for g in games:
        if g.get("homePoints") is None or g.get("awayPoints") is None:
            continue
        week_of[g["id"]] = cfb_week(g.get("week"), g.get("seasonType"))
        for side, other in (("home", "away"), ("away", "home")):
            tid = resolve(g.get(f"{side}Team") or "")
            if tid:
                pts[(g["id"], tid)] = (float(g[f"{side}Points"]), float(g[f"{other}Points"]))
                if g.get(f"{side}Classification") == "fbs":
                    fbs.add(tid)
    rows = []
    for x in advanced:
        team, opp = resolve(x.get("team") or ""), resolve(x.get("opponent") or "")
        if not team or not opp or (x.get("gameId"), team) not in pts:
            continue
        o, d = x.get("offense") or {}, x.get("defense") or {}
        pf, pa = pts[(x["gameId"], team)]
        rows.append(TeamGame("cfb", season, week_of.get(x["gameId"], int(x.get("week") or 0)), str(x["gameId"]), team, opp,
                             eff=_f(o.get("ppa")), sr=_f(o.get("successRate")), plays=_f(o.get("plays")),
                             drives=_f(o.get("drives")), pf=pf, d_eff=_f(d.get("ppa")), d_sr=_f(d.get("successRate")),
                             d_plays=_f(d.get("plays")), d_drives=_f(d.get("drives")), pa=pa))
    return rows, fbs


def parse_returning(payload: Sequence[Mapping[str, Any]], resolve: Callable[[str], Optional[str]]) -> dict[str, float]:
    out = {}
    for r in payload:
        tid, v = resolve(r.get("team") or ""), _f(r.get("percentPPA"))
        if tid and v is not None:
            out[tid] = min(1.0, max(0.0, v))
    return out


# ---------------------------------------------------------------- cache

def load_cache(state_dir: Path) -> dict:
    return pstate.load_team_stats(state_dir)


def save_cache(state_dir: Path, data: dict) -> None:
    pstate.save_team_stats(state_dir, data)


def resolve_team(sport: str, name: str) -> Optional[str]:
    """Exact alias match, else a slug (FCS opponents keep a stable id without fuzzy guessing,
    and nothing is registered as unresolved: these names never price a game)."""
    if not name:
        return None
    return get_resolver(sport).resolve(name, fuzzy=False) or slug(name)


def _rows_to_json(rows: Iterable[TeamGame]) -> list[dict]:
    return [{k: v for k, v in asdict(r).items() if v is not None} for r in rows]


def rows_from_cache(cache: dict, sport: str) -> tuple[list[TeamGame], dict[int, dict[str, float]], set[str]]:
    sp = cache.get("sports", {}).get(sport, {})
    rows = [TeamGame(**r) for s in sp.get("seasons", {}).values() for r in s.get("rows", [])]
    returning = {int(s): v.get("returning", {}) for s, v in sp.get("seasons", {}).items() if v.get("returning")}
    members = set(sp.get("members", []))
    return rows, returning, members


def needs_refresh(entry: Optional[Mapping[str, Any]], now: datetime, *, final: bool, min_hours: float) -> bool:
    """Fetch when we have nothing; a finished season never again; the current season every
    ``min_hours``. A failed attempt (``failed_at``) blocks retries for ``RETRY_H``."""
    failed = (entry or {}).get("failed_at")
    if failed and now - datetime.fromisoformat(failed) < timedelta(hours=RETRY_H):
        return False
    if not entry or not entry.get("fetched_at"):
        return True
    if final:
        return False
    return now - datetime.fromisoformat(entry["fetched_at"]) >= timedelta(hours=min_hours)


# ---------------------------------------------------------------- fetch (raw-first)

def _http_text(url: str, headers: Optional[dict[str, str]] = None, timeout: float = 60.0) -> str:
    import httpx

    r = httpx.get(url, headers=headers or {}, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _capture(raw_dir: Optional[Path], name: str, text: str) -> None:
    if raw_dir is not None:
        out = Path(raw_dir) / "teamstats"
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text, encoding="utf-8")


def fetch_season(sport: str, season: int, *, raw_dir: Optional[Path] = None,
                 get: Callable[..., str] = _http_text, api_key: Optional[str] = None,
                 returning: Optional[Mapping[str, float]] = None) -> dict[str, Any]:
    """One season's cache entry: {"rows": [...], "returning": {...}, "members": [...]}."""
    if sport == "nfl":
        stats = get(NFLVERSE_TEAM_WEEK_URL.format(season=season))
        _capture(raw_dir, f"nfl_stats_team_week_{season}.csv", stats)
        games = get(NFLVERSE_GAMES_URL)
        _capture(raw_dir, "nfl_games.csv", games)
        return {"rows": _rows_to_json(parse_nfl(stats, games, season))}
    key = api_key or os.environ.get("CFBD_API_KEY", "")
    if not key:
        raise RuntimeError("CFBD_API_KEY not set")
    hdr = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    payloads = {}
    calls = [("advanced", f"/stats/game/advanced?year={season}"),
             ("games", f"/games?year={season}&seasonType=both&classification=fbs")]
    if not returning:  # once per season: it does not change during the season
        calls.append(("returning", f"/player/returning?year={season}"))
    for name, path in calls:
        try:
            text = get(f"{CFBD_BASE}{path}", hdr)
            _capture(raw_dir, f"cfb_{name}_{season}.json", text)
            payloads[name] = json.loads(text)
        except Exception:
            if name == "returning":   # optional: the prior shrink falls back to 0.55; retried next refresh
                continue
            raise

    def resolve(name: str) -> Optional[str]:
        return resolve_team("cfb", name)

    rows, fbs = parse_cfb(payloads["advanced"], payloads["games"], season, resolve)
    ret = dict(returning or {})
    if "returning" in payloads:
        try:
            ret = parse_returning(payloads["returning"], resolve)
        except Exception:  # noqa: BLE001  (malformed optional payload: keep the rows, default the prior)
            pass
    return {"rows": _rows_to_json(rows), "returning": ret, "members": sorted(fbs)}


def refresh(sport: str, season: int, cache: dict, kickoffs: Sequence[datetime], now: Optional[datetime] = None, *,
            raw_dir: Optional[Path] = None, get: Callable[..., str] = _http_text,
            on_error: Optional[Callable[[str], None]] = None) -> bool:
    """Bring ``cache`` up to date for ``sport`` (current + ``HISTORY_SEASONS`` back). ``kickoffs`` =
    the scheduled kickoffs we know of; with none (off-season) a cached current season is left alone.
    True if anything changed (so the caller saves)."""
    now = now or datetime.now(timezone.utc)
    sp = cache.setdefault("sports", {}).setdefault(sport, {"seasons": {}})
    seasons = sp.setdefault("seasons", {})
    changed = False
    for s in range(season - HISTORY_SEASONS, season + 1):
        entry = seasons.get(str(s))
        if s == season and entry and entry.get("fetched_at") and not kickoffs:
            continue   # off-season: nothing new to fetch
        if not needs_refresh(entry, now, final=s < season, min_hours=MIN_REFRESH_H.get(sport, 12.0)):
            continue
        try:
            fresh = fetch_season(sport, s, raw_dir=raw_dir, get=get, returning=(entry or {}).get("returning"))
        except Exception as exc:  # noqa: BLE001  (keep the cached season; the board still renders)
            if on_error is not None:
                on_error(f"{sport} {s}: {type(exc).__name__}: {exc}")
            seasons[str(s)] = {**(entry or {}), "failed_at": now.isoformat()}   # persisted -> backoff across runs
            changed = True
            continue
        fresh["fetched_at"] = now.isoformat()
        seasons[str(s)] = fresh
        changed = True
    for s in [k for k in seasons if int(k) < season - HISTORY_SEASONS]:
        del seasons[s]
        changed = True
    members: set[str] = set()
    for v in seasons.values():
        members |= set(v.get("members", []))
    sp["members"] = sorted(members)
    return changed


def team_totals_for_games(sport: str, games: Sequence[Any], cache: dict, model: Mapping[str, Any],
                          now: datetime) -> dict[str, dict[str, Any]]:
    """Our expected total per game_id (no weather): {"total", "weight", "n_games", "model"}.

    Skips games where either team has no rating, CFB games not between two FBS teams, and
    sports with no fitted coefficients. ``weight`` = share of (total - line) the v2 fair takes."""
    coef = (model.get(sport) or {}).get("coef")
    rows, returning, members = rows_from_cache(cache, sport)
    if not coef or not rows:
        return {}
    out: dict[str, dict[str, Any]] = {}
    lg_by: dict[int, Optional[float]] = {}
    by_week: dict[tuple[int, int], dict[str, Rating]] = {}
    for g in games:
        if sport == "cfb" and members and not (g.home_id in members and g.away_id in members):
            continue
        key = (int(g.season), int(g.week))
        if key not in by_week:
            by_week[key] = build_ratings(sport, rows, key[0], key[1], returning=returning, members=members or None)
        h, a = by_week[key].get(g.home_id), by_week[key].get(g.away_id)
        if key[0] not in lg_by:
            lg_by[key[0]] = league_total(rows, key[0], members or None)
        if h is None or a is None or lg_by[key[0]] is None:   # no last season cached -> no pre-game rating
            continue
        total = predict_total(coef, features(h, a, lg_by[key[0]]))
        lead = (g.kickoff_utc - now).total_seconds() / 3600.0
        out[g.game_id] = {"total": round(total, 1), "weight": round(blend_weight(sport, lead), 3),
                          "n_games": min(h.n_games, a.n_games), "model": "team_v1"}
    return out


MIN_SLATE_GAMES = 8   # fewer priced games than this -> no blend (the level bias cannot be removed)


def relative_to_slate(totals: Mapping[str, Mapping[str, Any]], lines: Mapping[str, float],
                      min_games: int = MIN_SLATE_GAMES) -> dict[str, dict[str, Any]]:
    """Keep only the RELATIVE signal: subtract this slate's median (ours - consensus) gap.

    Our level drifts by +-1-2 pts a season against the market (2025 CFB: +2.2 with results on
    the line), while the game-to-game differences carry the information (2024-25 archive, 72 h
    out: slope 0.43 de-meaned vs 0.36 raw). Adds ``slate_offset`` and ``gap_rel`` = total - offset -
    line, in points, frozen now: later re-pricing (expired quotes) adds this same gap to whatever
    the consensus becomes, so a quote that has since expired cannot leak back in through a stale
    offset. Games without a line, or a slate too small to estimate the offset, get no ``gap_rel``
    and therefore no blend."""
    gaps = sorted(v["total"] - lines[g] for g, v in totals.items() if g in lines and v.get("total") is not None)
    out = {g: dict(v) for g, v in totals.items()}
    if len(gaps) < min_games:
        return out
    n = len(gaps)
    offset = gaps[n // 2] if n % 2 else 0.5 * (gaps[n // 2 - 1] + gaps[n // 2])
    for g, v in out.items():
        v["slate_offset"] = round(offset, 2)
        if g in lines and v.get("total") is not None:
            v["gap_rel"] = round(v["total"] - offset - lines[g], 2)
    return out


__all__ = ["fetch_season", "load_cache", "needs_refresh", "parse_cfb", "parse_nfl",
           "parse_returning", "refresh", "resolve_team", "relative_to_slate", "rows_from_cache", "save_cache",
           "team_totals_for_games"]
