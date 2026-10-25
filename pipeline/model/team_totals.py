"""Our own game total from team ratings (owner decision 2026-10-24), weather layered on top elsewhere.

Pure functions, no I/O. Two halves:

* **Ratings.** From completed team-game rows (:class:`TeamGame`), each team's pre-game
  rating per metric = last season's mean (shrunk toward the league by ``a + b*returning``)
  blended with the season-to-date mean at prior weight ``k`` games. Values are centred on
  the league mean. CFB values are opponent-adjusted first: a game's offensive value minus
  what that opponent's defence normally allows (its own pre-game rating), and vice versa.
* **Points.** ``predict_total`` maps the two teams' ratings to an expected total with the
  linear coefficients fitted by ``scripts/fit_team_model.py`` and stored in the
  ``team_totals`` block of ``data/calibration.json``. The model has no weather term: the
  v2 weather shade is applied on top in ``pipeline.model.fair``.

Only the relative signal is used: ``pipeline.teamstats.relative_to_slate`` removes the slate's
median (ours - line) gap first, because our level drifts by a point or two a season, and freezes
the remaining per-game gap in points (``gap_rel``). How much of it the fair total takes is
``blend_weight(sport, lead_hours)``
(CFB 0.35 three days out, 0.15 at the close; NFL 0.05, i.e. shown but barely trusted).
Evidence and method: ``docs/notes/team-style-weather-study.md``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "data" / "calibration.json"

OFF = ("eff", "sr", "ppd", "pf")          # offence: efficiency/play, success rate, points/drive, points
DEF = ("d_eff", "d_sr", "d_ppd", "pa")    # the same allowed
PACE = ("pace", "drives")                 # plays in the team's games, own drives
METRICS = OFF + DEF + PACE
MIRROR = dict(zip(OFF + DEF, DEF + OFF, strict=True))   # offence metric <-> the defence metric it faces

# (k prior games, a, b): prior = last season's mean * clip(a + b*returning, 0, 1). From the
# 1999-2025 NFL / 2017-2025 CFB tuning (tmp/rushpass/{nfl,cfb}_team_model.py).
DEFAULT_PARAMS: dict[str, dict[str, tuple[float, float, float]]] = {
    "nfl": {"eff": (6, 0.4, 0.0), "sr": (6, 0.4, 0.0), "ppd": (8, 0.4, 0.0), "pf": (8, 0.4, 0.0),
            "d_eff": (12, 0.2, 0.0), "d_sr": (12, 0.2, 0.0), "d_ppd": (12, 0.2, 0.0), "pa": (12, 0.2, 0.0),
            "pace": (12, 0.2, 0.0), "drives": (12, 0.2, 0.0)},
    "cfb": {"eff": (6, 0.5, 0.0), "sr": (6, 0.5, 0.0), "ppd": (6, 0.3, 0.4), "pf": (6, 0.5, 0.0),
            "d_eff": (6, 0.3, 0.0), "d_sr": (6, 0.3, 0.0), "d_ppd": (6, 0.5, 0.0), "pa": (6, 0.5, 0.0),
            "pace": (6, 0.3, 0.0), "drives": (6, 0.3, 0.0)},
}
OPPONENT_ADJUST = {"nfl": False, "cfb": True}
# Share of (our total - line) the v2 fair total takes, by lead. CFB measured on the 2024-25
# archive (0.36-0.40 at ~72 h, p~0.01) and 2017-2025 closes (0.15-0.17); NFL showed nothing since 2013,
# so it is shown at a token weight until live results say otherwise.
BLEND_WEIGHTS: dict[str, tuple[float, float]] = {"nfl": (0.05, 0.05), "cfb": (0.15, 0.35)}  # (at close, >= 72 h)
BLEND_CLOSE_H, BLEND_FAR_H = 6.0, 72.0


@dataclass(frozen=True)
class TeamGame:
    """One team's completed game. Any metric may be None (e.g. NFL has no drives feed)."""

    sport: str
    season: int
    week: int
    game_key: str
    team: str
    opp: str
    eff: Optional[float] = None
    sr: Optional[float] = None
    plays: Optional[float] = None
    drives: Optional[float] = None
    pf: Optional[float] = None
    d_eff: Optional[float] = None
    d_sr: Optional[float] = None
    d_plays: Optional[float] = None
    d_drives: Optional[float] = None
    pa: Optional[float] = None

    def value(self, m: str) -> Optional[float]:
        if m == "ppd":
            return self.pf / self.drives if self.pf is not None and self.drives else None
        if m == "d_ppd":
            return self.pa / self.d_drives if self.pa is not None and self.d_drives else None
        if m == "pace":
            return self.plays + self.d_plays if self.plays is not None and self.d_plays is not None else None
        return getattr(self, m)


@dataclass(frozen=True)
class Rating:
    team: str
    n_games: int                       # games played this season before the rated week
    values: dict[str, float] = field(default_factory=dict)   # centred, pre-game


def _mean(xs: Iterable[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def league_means(rows: Sequence[TeamGame], season: int, members: Optional[set[str]] = None) -> dict[str, float]:
    sel = [r for r in rows if r.season == season and (members is None or r.team in members)]
    return {m: mu for m in METRICS if (mu := _mean(r.value(m) for r in sel)) is not None}


def _blend(prior: Optional[float], prior_scale: float, todate: Sequence[float], k: float) -> float:
    p = (prior or 0.0) * prior_scale
    n = len(todate)
    return (k * p + sum(todate)) / (k + n)


def walk_ratings(
    sport: str,
    rows: Sequence[TeamGame],
    *,
    returning: Optional[Mapping[int, Mapping[str, float]]] = None,
    members: Optional[set[str]] = None,
    params: Optional[Mapping[str, tuple[float, float, float]]] = None,
) -> Iterator[tuple[int, int, dict[str, Rating]]]:
    """Chronological single pass: yields ``(season, week, ratings)`` with every team's rating
    BEFORE that week's games, for every (season, week) present in ``rows``.

    ``returning[season][team]`` = share of last season's production returning (CFB; default
    0.55). ``members`` limits the league means (CFB: FBS teams)."""
    params = params or DEFAULT_PARAMS[sport]
    returning = returning or {}
    rows = sorted(rows, key=lambda r: (r.season, r.week, r.game_key, r.team))
    seasons = sorted({r.season for r in rows})
    lg = {s: league_means(rows, s, members) for s in seasons}
    adjust = OPPONENT_ADJUST.get(sport, False)
    hist: dict[tuple[int, str, str], list[float]] = defaultdict(list)
    prev_mean: dict[tuple[int, str, str], float] = {}

    def centre(s: int, m: str, v: Optional[float]) -> Optional[float]:
        # last season's league mean. A season with no predecessor in ``rows`` is centred on its own
        # mean: it only builds the prior for the next season and is never rated itself (has_prior).
        base = lg.get(s - 1, {}).get(m, lg.get(s, {}).get(m))
        return None if v is None or base is None else v - base

    def rating(s: int, team: str) -> Rating:
        ret = returning.get(s, {}).get(team, 0.55)
        vals = {}
        for m in METRICS:
            k, a, b = params[m]
            vals[m] = _blend(prev_mean.get((s, team, m)), min(1.0, max(0.0, a + b * ret)), hist[(s, team, m)], k)
        return Rating(team, len(hist[(s, team, "pf")]), vals)

    by_week: dict[tuple[int, int], list[TeamGame]] = defaultdict(list)
    for r in rows:
        by_week[(r.season, r.week)].append(r)
    cur: Optional[int] = None
    for (s, w) in sorted(by_week):
        if s != cur:
            if cur is not None:
                for (ss, team, m), vals in list(hist.items()):
                    if ss == cur and vals:
                        prev_mean[(s, team, m)] = sum(vals) / len(vals)
            cur = s
        week_rows = by_week[(s, w)]
        teams_now = {r.team for r in week_rows} | {r.opp for r in week_rows}
        known = {t for (ss, t, _m) in hist if ss in (s, s - 1)} | {t for (ss, t, _m) in prev_mean if ss == s}
        snap = {t: rating(s, t) for t in teams_now | known}
        yield s, w, snap
        for r in week_rows:
            for m in METRICS:
                v = centre(s, m, r.value(m))
                if v is None:
                    continue
                if adjust and m in MIRROR:
                    opp = snap.get(r.opp)
                    v -= opp.values.get(MIRROR[m], 0.0) if opp else 0.0
                hist[(s, r.team, m)].append(v)


def build_ratings(
    sport: str,
    rows: Sequence[TeamGame],
    season: int,
    before_week: int,
    *,
    returning: Optional[Mapping[int, Mapping[str, float]]] = None,
    members: Optional[set[str]] = None,
    params: Optional[Mapping[str, tuple[float, float, float]]] = None,
) -> dict[str, Rating]:
    """Ratings as of the start of ``before_week`` of ``season`` (no game from that week on is used).

    ``rows`` should hold this season and the previous one (two back for CFB, so last season's
    opponent adjustment has its own prior). Works before the season's first game too: the
    ratings are then last season's means, shrunk."""
    past = [r for r in rows if (r.season, r.week) < (season, before_week)]
    # sentinel week so the walk yields the state after every past game
    sentinel = TeamGame(sport, season, before_week, "__sentinel__", "__none__", "__none__")
    out: dict[str, Rating] = {}
    for s, w, snap in walk_ratings(sport, [*past, sentinel], returning=returning, members=members, params=params):
        if (s, w) == (season, before_week):
            out = {t: r for t, r in snap.items() if t != "__none__"}
    return out


# ---------------------------------------------------------------- points model

def league_total(rows: Sequence[TeamGame], season: int, members: Optional[set[str]] = None) -> Optional[float]:
    """League-average game total known before ``season``: last season's mean points x 2.
    None when last season is not in ``rows``: that season cannot be rated pre-game (its own
    results would leak in), so callers skip it."""
    prev = league_means(rows, season - 1, members).get("pf")
    return 2.0 * prev if prev is not None else None


def has_prior(rows: Sequence[TeamGame], season: int) -> bool:
    """True when ``season - 1`` is present, i.e. ``season`` can be rated without its own results."""
    return any(r.season == season - 1 for r in rows)


def features(home: Rating, away: Rating, league_total: float) -> dict[str, float]:
    """Matchup features: each offence against the other defence, plus pace."""
    h, a = home.values, away.values

    def match(off: str, de: str) -> float:
        return h.get(off, 0.0) + a.get(de, 0.0) + a.get(off, 0.0) + h.get(de, 0.0)

    return {
        "lg": league_total,
        "pts_match": match("pf", "pa"),
        "eff_match": match("eff", "d_eff"),
        "ppd_match": match("ppd", "d_ppd"),
        "sr_match": match("sr", "d_sr"),
        "sum_pace": h.get("pace", 0.0) + a.get("pace", 0.0),
        "sum_drives": h.get("drives", 0.0) + a.get("drives", 0.0),
    }


def predict_total(coef: Mapping[str, float], feats: Mapping[str, float]) -> float:
    return coef.get("intercept", 0.0) + sum(coef.get(k, 0.0) * v for k, v in feats.items())


def blend_weight(sport: str, lead_hours: Optional[float]) -> float:
    """Share of (our total - line) taken into the fair total; linear from the close to 72 h out."""
    close, far = BLEND_WEIGHTS.get(sport, (0.0, 0.0))
    if lead_hours is None:
        return close
    t = min(1.0, max(0.0, (lead_hours - BLEND_CLOSE_H) / (BLEND_FAR_H - BLEND_CLOSE_H)))
    return close + (far - close) * t


def load_model(path: Path = CALIBRATION_PATH) -> dict[str, dict]:
    """``team_totals`` block of calibration.json: {sport: {"coef": {...}, "fitted": {...}}}; {} when absent."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = data.get("team_totals") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


__all__ = ["BLEND_WEIGHTS", "DEFAULT_PARAMS", "METRICS", "Rating", "TeamGame", "blend_weight", "build_ratings",
           "features", "has_prior", "league_means", "league_total", "load_model", "predict_total", "walk_ratings"]
