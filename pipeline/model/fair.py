"""Consensus / fair line / edge model (ARCHITECTURE §7.2, §7.3).

Odds math helpers are copied verbatim from ``golf_scraping/board/build.py``
(``american_to_prob``, ``prob_to_american``, ``american_to_decimal``, ``_best``,
``_median``). Everything else is football-specific:

* per-book devig (multiplicative two-way normalisation; exchanges use ``prob_raw``)
* Pinnacle-weighted consensus line (weighted median of main lines) and the
  weighted-mean vig-free probability at that line
* pts->prob shift tables (totals linear per point; spreads key-number aware)
* fair lines from the v1 impact percentages, per-(book, market, side) edges,
  confidence and tiers
* the legacy CFB derived columns (``My_total``/``Edge``/``My_spread``/``Edge_s``)

Sign conventions: spread lines are HOME-relative (negative = home favoured);
``line`` on an ``away`` spread row is the away number as the book prints it
(i.e. ``-home_line``). Totals: the same number on both ``over``/``under`` rows.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.contracts import Edge, GameLine
from pipeline.model import config as C

EXCHANGE_BOOKS = {"kalshi", "novig", "prophetx"}
DEFAULT_BOOK_WEIGHT = 0.5
LEGACY_NOW_BOOK: dict[str, str] = {"nfl": "betonline", "cfb": "fanduel"}
CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "data" / "calibration.json"

# Cumulative half-point shift tables (§7.3). Key ``k`` = probability gained by
# the side receiving points when its number moves from ``k-0.5`` to ``k``
# (magnitude space). Anything past the last key uses ``_TAIL``.
SPREAD_KEY_PROB: dict[str, dict[float, float]] = {
    "nfl": {
        0.5: 0.020, 1.0: 0.015, 1.5: 0.020, 2.0: 0.020, 2.5: 0.030, 3.0: 0.095, 3.5: 0.030,
        4.0: 0.020, 4.5: 0.020, 5.0: 0.020, 5.5: 0.020, 6.0: 0.040, 6.5: 0.030, 7.0: 0.075,
        7.5: 0.030, 8.0: 0.020, 8.5: 0.020, 9.0: 0.020, 9.5: 0.020, 10.0: 0.045, 10.5: 0.020,
        11.0: 0.015, 11.5: 0.015, 12.0: 0.015, 12.5: 0.015, 13.0: 0.020, 13.5: 0.020, 14.0: 0.040,
    },
    "cfb": {
        0.5: 0.017, 1.0: 0.015, 1.5: 0.017, 2.0: 0.017, 2.5: 0.022, 3.0: 0.060, 3.5: 0.022,
        4.0: 0.018, 4.5: 0.018, 5.0: 0.018, 5.5: 0.018, 6.0: 0.028, 6.5: 0.022, 7.0: 0.050,
        7.5: 0.022, 8.0: 0.017, 8.5: 0.017, 9.0: 0.017, 9.5: 0.017, 10.0: 0.030, 10.5: 0.017,
        11.0: 0.015, 11.5: 0.015, 12.0: 0.015, 12.5: 0.015, 13.0: 0.015, 13.5: 0.015, 14.0: 0.030,
    },
}
_TAIL: dict[str, float] = {"nfl": 0.010, "cfb": 0.010}
PROB_FLOOR = 0.01
PROB_CEIL = 0.99


# ---- odds math (copied from golf_scraping/board/build.py) --------------------
def american_to_prob(o: int) -> float:
    if not o:
        return 0.0
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)


def prob_to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        return 0
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def american_to_decimal(o: int) -> float:
    if not o:
        return 0.0
    return o / 100.0 + 1 if o > 0 else 100.0 / (-o) + 1


def _best(book_to_odds: dict) -> tuple:
    """Best (most favorable to bettor) odds across books -> (book, odds)."""
    best_book, best_dec = None, 0.0
    for bk, o in book_to_odds.items():
        d = american_to_decimal(o)
        if d > best_dec:
            best_book, best_dec = bk, d
    return best_book, best_dec


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2


# ---- devig ------------------------------------------------------------------
def _clamp(p: float, lo: float = PROB_FLOOR, hi: float = PROB_CEIL) -> float:
    if p != p or math.isinf(p):
        return 0.5
    return min(hi, max(lo, p))


def devig_pair(odds_a: int, odds_b: int) -> tuple[float, float]:
    """Multiplicative two-way devig -> (p_a, p_b) summing to 1."""
    pa, pb = american_to_prob(odds_a), american_to_prob(odds_b)
    s = pa + pb
    if s <= 0:
        return 0.5, 0.5
    return _clamp(pa / s), _clamp(pb / s)


def vigfree_prob(line: GameLine, other: GameLine | None) -> float:
    """Vig-free probability for ``line``'s side. Exchanges carry ``prob_raw``."""
    if line.book in EXCHANGE_BOOKS and line.prob_raw is not None:
        return _clamp(line.prob_raw)
    if other is None:
        return _clamp(american_to_prob(line.odds))
    if other.book in EXCHANGE_BOOKS and other.prob_raw is not None and line.prob_raw is None:
        return _clamp(1.0 - other.prob_raw)
    return devig_pair(line.odds, other.odds)[0]


# ---- pts -> prob -------------------------------------------------------------
def load_calibration(path: Path = CALIBRATION_PATH) -> dict[str, Any]:
    """Optional overrides for the shipped tables; missing/invalid file -> {}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_calibration(cal: dict[str, Any]) -> None:
    for sport, tbl in (cal.get("spread_key_prob") or {}).items():
        if sport in SPREAD_KEY_PROB and isinstance(tbl, dict):
            SPREAD_KEY_PROB[sport] = {float(k): float(v) for k, v in tbl.items()}
    for sport, v in (cal.get("pts_prob_total") or {}).items():
        if sport in C.PTS_PROB_TOTAL:
            C.PTS_PROB_TOTAL[sport] = float(v)


def _half_steps(a: float, b: float) -> Iterable[float]:
    """Half-point step endpoints strictly after ``a`` up to and including ``b`` (a<b)."""
    n = int(round((b - a) * 2))
    for i in range(1, n + 1):
        yield round(a + i * 0.5, 1)


def spread_cumulative(sport: str, line: float) -> float:
    """S(line): cumulative cover-probability offset for the HOME side at a
    home-relative ``line``, relative to S(0)=0. Monotone non-decreasing in line
    (more points for home -> home more likely to cover)."""
    tbl = SPREAD_KEY_PROB[sport]
    tail = _TAIL[sport]
    mag = abs(line)
    total = 0.0
    for k in _half_steps(0.0, math.floor(mag * 2) / 2):
        total += tbl.get(k, tail)
    frac = mag - math.floor(mag * 2) / 2
    if frac > 0:
        k = round(math.floor(mag * 2) / 2 + 0.5, 1)
        total += tbl.get(k, tail) * (frac / 0.5)
    return total if line >= 0 else -total


def spread_shift(sport: str, from_line: float, to_line: float, side: str = "home") -> float:
    """Probability gained by ``side`` when the home-relative line moves
    from ``from_line`` to ``to_line``."""
    d = spread_cumulative(sport, to_line) - spread_cumulative(sport, from_line)
    return d if side == "home" else -d


def total_shift(sport: str, from_line: float, to_line: float, side: str = "over") -> float:
    """Probability gained by ``side`` when the total moves from ``from_line`` to ``to_line``."""
    d = -(to_line - from_line) * C.PTS_PROB_TOTAL[sport]
    return d if side == "over" else -d


def shift(sport: str, market: str, from_line: float, to_line: float, side: str) -> float:
    if market == "total":
        return total_shift(sport, from_line, to_line, side)
    if market == "spread":
        return spread_shift(sport, from_line, to_line, side)
    return 0.0


# ---- consensus ---------------------------------------------------------------
def book_weight(book: str) -> float:
    return float(C.BOOK_WEIGHTS.get(book, DEFAULT_BOOK_WEIGHT))


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights, strict=False))
    total = sum(w for _, w in pairs)
    if total <= 0 or not pairs:
        return _median(list(values))
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc >= total / 2.0:
            if math.isclose(acc, total / 2.0) and i + 1 < len(pairs):
                return (v + pairs[i + 1][0]) / 2.0
            return v
    return pairs[-1][0]


def _home_line(gl: GameLine) -> float:
    """Home-relative number for spread rows; raw number for totals."""
    if gl.market == "spread" and gl.side == "away":
        return -float(gl.line)  # type: ignore[arg-type]
    return float(gl.line)  # type: ignore[arg-type]


def _pair_key(gl: GameLine) -> tuple[str, str, float]:
    return gl.book, gl.market, _home_line(gl) if gl.market != "ml" else 0.0


_PRIMARY_SIDE = {"spread": "home", "total": "over", "ml": "home"}
_OPPOSITE = {"home": "away", "away": "home", "over": "under", "under": "over"}


def main_lines(lines: Iterable[GameLine], market: str) -> dict[str, dict[str, GameLine]]:
    """{book: {side: GameLine}} for the main line of ``market`` per book.

    Rows with ``is_main`` win; otherwise the first line seen per book. Two
    sides only pair when they refer to the same number."""
    by_book: dict[str, dict[str, GameLine]] = {}
    chosen_num: dict[str, float] = {}
    for gl in lines:
        if gl.market != market:
            continue
        num = _home_line(gl) if market != "ml" else 0.0
        cur = by_book.setdefault(gl.book, {})
        if gl.book in chosen_num:
            if num != chosen_num[gl.book]:
                if gl.is_main and not any(x.is_main for x in cur.values()):
                    by_book[gl.book] = {gl.side: gl}
                    chosen_num[gl.book] = num
                continue
            cur.setdefault(gl.side, gl)
        else:
            chosen_num[gl.book] = num
            cur[gl.side] = gl
    return by_book


@dataclass(frozen=True)
class Consensus:
    sport: str
    market: str
    line: float | None
    prob: float | None  # vig-free prob of the primary side (home / over) at ``line``
    n_books: int
    ref_book: str | None
    thin: bool
    books: dict[str, float] = field(default_factory=dict)  # book -> main line (home-relative)

    @property
    def primary_side(self) -> str:
        return _PRIMARY_SIDE[self.market]


def consensus(sport: str, lines: Iterable[GameLine], market: str) -> Consensus:
    per_book = main_lines(lines, market)
    primary = _PRIMARY_SIDE[market]
    nums: list[float] = []
    wts: list[float] = []
    books: dict[str, float] = {}
    for book, sides in per_book.items():
        gl = next(iter(sides.values()))
        num = _home_line(gl)
        books[book] = num
        nums.append(num)
        wts.append(book_weight(book))
    n = len(nums)
    if n == 0:
        return Consensus(sport, market, None, None, 0, None, True, {})
    line = weighted_median(nums, wts)
    ref_book = max(per_book, key=book_weight)
    # move every book's devigged primary-side prob to the consensus line
    probs: list[float] = []
    pw: list[float] = []
    for book, sides in per_book.items():
        a = sides.get(primary)
        b = sides.get(_OPPOSITE[primary])
        if a is None and b is None:
            continue
        if a is not None:
            p = vigfree_prob(a, b)
        else:
            p = 1.0 - vigfree_prob(b, None)  # type: ignore[arg-type]
        p += shift(sport, market, books[book], line, primary)
        probs.append(_clamp(p))
        pw.append(book_weight(book))
    prob = sum(p * w for p, w in zip(probs, pw, strict=False)) / sum(pw) if pw else None
    return Consensus(sport, market, line, prob, n, ref_book, n < 2, books)


# ---- fair lines --------------------------------------------------------------
def fair_total(consensus_total: float | None, gs_fg_pct: float | None) -> float | None:
    if consensus_total is None or gs_fg_pct is None or gs_fg_pct != gs_fg_pct:
        return consensus_total
    return consensus_total * (1.0 + gs_fg_pct / 100.0)


def fair_spread(consensus_spread: float | None, away_fg_pct: float | None) -> float | None:
    if consensus_spread is None or away_fg_pct is None or away_fg_pct != away_fg_pct:
        return consensus_spread
    return consensus_spread * (1.0 + away_fg_pct / 100.0)


def fair_line(market: str, cons: Consensus, gs_fg_pct: float | None, away_fg_pct: float | None) -> float | None:
    if market == "total":
        return fair_total(cons.line, gs_fg_pct)
    if market == "spread":
        return fair_spread(cons.line, away_fg_pct)
    return None


def fair_prob(sport: str, market: str, side: str, cons: Consensus, fair: float, at_line: float) -> float:
    """P(side wins at ``at_line``) when the true number is ``fair``.

    Consensus prob (primary side at consensus line) is re-centred on the fair
    line and then moved to the book's number with the pts->prob tables."""
    base = cons.prob if cons.prob is not None else 0.5
    primary = _PRIMARY_SIDE[market]
    p = base + shift(sport, market, fair, at_line, primary)
    if side != primary:
        p = 1.0 - p
    return _clamp(p)


def edge_pts(market: str, side: str, fair: float, book_line: float) -> float:
    """Signed points in favour of ``side``; ``book_line`` is the home-relative
    number for spreads, the total for totals."""
    if market == "total":
        return (book_line - fair) if side == "under" else (fair - book_line)
    if market == "spread":
        return (book_line - fair) if side == "home" else (fair - book_line)
    return 0.0


# ---- confidence / tiers ------------------------------------------------------
def confidence(
    wind_vol_fc: float | None,
    model_disagreement: float | None,
    lead_hours: float | None,
    wind_vol_static: str | None = None,
) -> float:
    wv = wind_vol_fc
    if wv is None or wv != wv:
        key = (wind_vol_static or "").strip().lower()
        wv = C.WIND_VOL_STATIC_TO_FC.get(key, 0.5) * 15.0
    md = 0.0 if model_disagreement is None or model_disagreement != model_disagreement else model_disagreement
    lh = 0.0 if lead_hours is None or lead_hours != lead_hours else lead_hours
    c = 1.0 - 0.5 * min(1.0, wv / 15.0) - 0.3 * min(1.0, md / 8.0) - 0.2 * min(1.0, max(0.0, lh - 36.0) / (168.0 - 36.0))
    return min(1.0, max(0.0, c))


def weather_driven(gs_fg_pct: float | None, rain_c: float | None, away_fg_pct: float | None) -> bool:
    gs = gs_fg_pct if gs_fg_pct is not None and gs_fg_pct == gs_fg_pct else 0.0
    aw = away_fg_pct if away_fg_pct is not None and away_fg_pct == away_fg_pct else 0.0
    rc = rain_c if rain_c is not None and rain_c == rain_c else 0.0
    return gs <= C.WEATHER_DRIVEN_GS_MAX or rc > 0 or aw <= C.WEATHER_DRIVEN_AWAY_MAX


def tier(
    sport: str,
    market: str,
    e_pts: float | None,
    e_prob: float | None,
    conf: float,
    lead_hours: float | None,
    thin: bool = False,
    is_weather_driven: bool = True,
) -> str:
    if market not in ("spread", "total") or e_pts is None or thin:
        return "none"
    ep = e_prob if e_prob is not None else 0.0
    lh = lead_hours if lead_hours is not None else 0.0
    edge_t = C.EDGE_THRESH[sport][market]
    strong_t = C.STRONG_THRESH[sport][market]
    if is_weather_driven and e_pts >= strong_t and conf >= 0.5 and ep >= 0.03:
        return "strong"
    if is_weather_driven and e_pts >= edge_t and (conf >= 0.5 or lh <= 36.0) and ep >= 0.03:
        return "edge"
    if e_pts >= C.WATCH_FRACTION * edge_t:
        return "watch"
    return "none"


# ---- per-game evaluation -----------------------------------------------------
@dataclass(frozen=True)
class GameFair:
    game_id: str
    sport: str
    total: Consensus
    spread: Consensus
    fair_total: float | None
    fair_spread: float | None
    confidence: float
    weather_driven: bool
    edges: list[Edge] = field(default_factory=list)

    def best(self, market: str, side: str) -> Edge | None:
        cands = [e for e in self.edges if e.market == market and e.side == side and e.edge_pts is not None]
        if not cands:
            return None
        return max(cands, key=lambda e: (e.edge_pts or 0.0, e.edge_prob or 0.0))


def evaluate_game(
    sport: str,
    game_id: str,
    lines: Sequence[GameLine],
    gs_fg_pct: float | None,
    away_fg_pct: float | None,
    rain_c: float | None = None,
    wind_vol_fc: float | None = None,
    wind_vol_static: str | None = None,
    model_disagreement: float | None = None,
    lead_hours: float | None = None,
    model_version: str = C.MODEL_VERSION_V1,
) -> GameFair:
    rows = [gl for gl in lines if gl.game_id == game_id]
    cons = {m: consensus(sport, rows, m) for m in ("total", "spread")}
    fairs = {m: fair_line(m, cons[m], gs_fg_pct, away_fg_pct) for m in ("total", "spread")}
    conf = confidence(wind_vol_fc, model_disagreement, lead_hours, wind_vol_static)
    wd = weather_driven(gs_fg_pct, rain_c, away_fg_pct)
    edges: list[Edge] = []
    for market in ("total", "spread"):
        cn, fr = cons[market], fairs[market]
        per_book = main_lines(rows, market)
        for book, sides in per_book.items():
            for side, gl in sides.items():
                other = sides.get(_OPPOSITE[side])
                vf = vigfree_prob(gl, other)
                num = _home_line(gl)
                if fr is None or cn.thin:
                    ep = epr = fp = None
                else:
                    ep = edge_pts(market, side, fr, num)
                    fp = fair_prob(sport, market, side, cn, fr, num)
                    epr = fp - vf
                edges.append(
                    Edge(
                        game_id=game_id,
                        book=book,
                        market=market,
                        side=side,
                        line=gl.line,
                        odds=gl.odds,
                        fair_line=fr,
                        fair_prob=fp,
                        vigfree_prob=vf,
                        edge_pts=ep,
                        edge_prob=epr,
                        confidence=conf,
                        tier=tier(sport, market, ep, epr, conf, lead_hours, cn.thin, wd),
                        model_version=model_version,
                        ref_book=cn.ref_book,
                        n_books=cn.n_books,
                    )
                )
    return GameFair(game_id, sport, cons["total"], cons["spread"], fairs["total"], fairs["spread"], conf, wd, edges)


# ---- v2 (§7.5) ------------------------------------------------------------------
@dataclass(frozen=True)
class FairV2:
    """v2 fair numbers for a game even when no odds exist (fair_* stay None then)."""

    fair_total: float | None
    fair_spread: float | None
    confidence: float
    weather_driven: bool
    ensemble: bool  # confidence came from a live wind_vol_fc rather than the static bucket


def fair_v2(
    sport: str,
    cons_total: float | None,
    cons_spread: float | None,
    impact_v2: Any,
    wind_vol_fc: float | None = None,
    wind_vol_static: str | None = None,
    model_disagreement: float | None = None,
    lead_hours: float | None = None,
) -> FairV2:
    gs = getattr(impact_v2, "gs_fg_pct", None)
    aw = getattr(impact_v2, "away_fg_pct", None)
    rc = getattr(impact_v2, "rain_c", None)
    live = wind_vol_fc is not None and wind_vol_fc == wind_vol_fc
    return FairV2(
        fair_total=fair_total(cons_total, gs),
        fair_spread=fair_spread(cons_spread, aw),
        confidence=confidence(wind_vol_fc, model_disagreement, lead_hours, wind_vol_static),
        weather_driven=weather_driven(gs, rc, aw),
        ensemble=live,
    )


def evaluate_game_v2(
    sport: str,
    game_id: str,
    lines: Sequence[GameLine],
    impact_v2: Any,
    wind_vol_fc: float | None = None,
    wind_vol_static: str | None = None,
    model_disagreement: float | None = None,
    lead_hours: float | None = None,
) -> GameFair:
    """Same edge machinery as v1 but fed by the v2 components; edges carry ``model_version='v2'``."""
    return evaluate_game(
        sport,
        game_id,
        lines,
        getattr(impact_v2, "gs_fg_pct", None),
        getattr(impact_v2, "away_fg_pct", None),
        rain_c=getattr(impact_v2, "rain_c", None),
        wind_vol_fc=wind_vol_fc,
        wind_vol_static=wind_vol_static,
        model_disagreement=model_disagreement,
        lead_hours=lead_hours,
        model_version=C.MODEL_VERSION_V2,
    )


# ---- legacy columns (§7.2) ---------------------------------------------------
def _num(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        return None if math.isnan(x) else float(x)
    except TypeError:
        return None


def legacy_derived(
    total_proj: float | None,
    spread: float | None,
    fd_now: float | None,
    gs_fg_pct: float | None,
    away_fg_pct: float | None,
) -> dict[str, float | None]:
    """My_total / Edge / My_spread / Edge_s exactly as the 2024 CFB file.

    Inputs are PERCENT impact values (CFB legacy scale). ``spread`` is the
    ``Spread`` column (reference spread, same sign convention as the file)."""
    tp, sp, fd = _num(total_proj), _num(spread), _num(fd_now)
    gs = _num(gs_fg_pct) or 0.0
    aw = _num(away_fg_pct) or 0.0
    my_total = tp * (1.0 + gs / 100.0) if tp is not None else None
    edge = (fd - my_total) / my_total if (my_total not in (None, 0.0) and fd is not None) else None
    my_spread = sp * (1.0 + aw / 100.0) if sp is not None else None
    edge_s = sp - my_spread if (sp is not None and my_spread is not None) else None
    return {"My_total": my_total, "Edge": edge, "My_spread": my_spread, "Edge_s": edge_s}


@dataclass(frozen=True)
class LegacyNow:
    """'Now' numbers for the legacy files: the sport's designated book, else consensus."""

    total: float | None
    total_odds: int | None  # under price (Under_now / Odds_n)
    spread: float | None  # home-relative
    spread_odds: int | None  # home price (Odds_now)
    book: str | None


def legacy_now(sport: str, lines: Sequence[GameLine], cons_total: Consensus, cons_spread: Consensus) -> LegacyNow:
    pref = LEGACY_NOW_BOOK.get(sport)
    tot = main_lines(lines, "total").get(pref or "", {})
    spr = main_lines(lines, "spread").get(pref or "", {})
    if tot or spr:
        t = next(iter(tot.values()), None)
        h = spr.get("home") or spr.get("away")
        return LegacyNow(
            total=float(t.line) if t is not None else cons_total.line,
            total_odds=tot["under"].odds if "under" in tot else None,
            spread=_home_line(h) if h is not None else cons_spread.line,
            spread_odds=spr["home"].odds if "home" in spr else None,
            book=pref,
        )
    return LegacyNow(cons_total.line, None, cons_spread.line, None, cons_total.ref_book or cons_spread.ref_book)


def legacy_columns(
    sport: str,
    lines: Sequence[GameLine],
    gs_fg_pct: float | None,
    away_fg_pct: float | None,
) -> dict[str, Any]:
    """Odds-side legacy columns for one game: Spread/Total_proj (consensus ref),
    FD_now/Odds_n/Current/Odds_now equivalents, My_total/Edge/My_spread/Edge_s,
    plus ``ref_book`` for meta. Openers/moves come from ``pipeline.state``."""
    ct = consensus(sport, lines, "total")
    cs = consensus(sport, lines, "spread")
    now = legacy_now(sport, lines, ct, cs)
    derived = legacy_derived(ct.line, cs.line, now.total, gs_fg_pct, away_fg_pct)
    return {
        "Spread": cs.line,
        "Total_proj": ct.line,
        "ref_book": ct.ref_book or cs.ref_book,
        "n_books": max(ct.n_books, cs.n_books),
        "thin": ct.thin and cs.thin,
        "Total_now": now.total,
        "Under_now": now.total_odds,
        "Spread_now": now.spread,
        "Odds_now": now.spread_odds,
        "now_book": now.book,
        **derived,
    }


__all__ = [
    "american_to_prob",
    "prob_to_american",
    "american_to_decimal",
    "devig_pair",
    "vigfree_prob",
    "SPREAD_KEY_PROB",
    "load_calibration",
    "apply_calibration",
    "spread_cumulative",
    "spread_shift",
    "total_shift",
    "weighted_median",
    "main_lines",
    "Consensus",
    "consensus",
    "fair_total",
    "fair_spread",
    "fair_prob",
    "edge_pts",
    "confidence",
    "weather_driven",
    "tier",
    "GameFair",
    "evaluate_game",
    "FairV2",
    "fair_v2",
    "evaluate_game_v2",
    "legacy_derived",
    "LegacyNow",
    "legacy_now",
    "legacy_columns",
]
