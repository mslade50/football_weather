"""Odds merge: provisional book rows -> schedule games -> per-book board + openers.

ARCH §8 (last paragraph) / PLAN Phase 2. Every parser stamps a *provisional*
``game_id`` of the shape ``{sport}:raw:{stamp}:{away}@{home}`` where ``stamp`` is
``YYYYMMDD`` (betcris/betonline), ``YYYY-MM-DD`` (fanduel/kalshi) or
``YYYY-MM-DDTHH:MM`` UTC (novig/pinnacle/prophetx) and ``away``/``home`` are the
book's own team strings (slugged by some books). This module:

1. ``canonicalize`` — resolves both team strings via ``odds.teams.normalize_team``,
   matches ``(away_id, home_id)`` to a schedule ``Game`` with kickoff within
   ``WINDOW_H`` (36 h); when only the swapped pair matches (neutral-site books
   list the "home" side differently) the row's ``home``/``away`` sides are
   flipped so they refer to the *schedule* home/away. Unmatched / unresolved
   rows are dropped and reported.
2. ``pivot`` — per game -> per book -> per market ``{home, away | over, under,
   line}`` using one *main* line per (game, book, market): ``is_main`` rows
   first; ladders (Kalshi alternates) collapse to the rung nearest the
   cross-book consensus line, else the most balanced pair of prices.
3. ``update_openers`` / ``opener_for`` — first-seen line per
   ``game_id|market|side|book`` through ``pipeline.state`` (never overwritten;
   ``openers.json`` under ``data_dir``).

``merge_odds`` runs the whole thing and returns a ``MergeResult``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline import state as state_mod
from pipeline.contracts import Game, GameLine
from pipeline.odds import teams as teams_mod

logger = logging.getLogger(__name__)

WINDOW_H = 36.0
BOOK_WEIGHTS: dict[str, float] = {
    "pinnacle": 3.0, "betonline": 2.0, "betcris": 1.5, "fanduel": 1.0, "draftkings": 1.0,
    "kalshi": 1.0, "novig": 1.0, "prophetx": 0.75,
}
_PROV_RE = re.compile(
    r"^(?P<sport>nfl|cfb):raw:(?P<stamp>\d{8}|\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?|unknown):(?P<away>[^@]*)@(?P<home>.*)$"
)
_STAMP_DAY = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")
_STAMP_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")
_FLIP = {"home": "away", "away": "home"}


@dataclass(frozen=True)
class RawGame:
    """A book's view of one game, keyed by its provisional ``game_id``."""

    book: str
    game_id: str
    sport: str
    away: str
    home: str
    kickoff_utc: datetime | None = None
    neutral: bool = False
    date_only: bool = False   # kickoff_utc carries the day only (noon UTC placeholder)


@dataclass
class MergeResult:
    sport: str
    lines: list[GameLine] = field(default_factory=list)         # canonical rows
    board: dict[str, dict[str, dict[str, dict[str, Any]]]] = field(default_factory=dict)
    openers: dict[str, Any] = field(default_factory=dict)       # state dict (openers.json)
    new_openers: int = 0
    game_map: dict[str, tuple[str, bool]] = field(default_factory=dict)  # provisional -> (game_id, flipped)
    unmatched: list[str] = field(default_factory=list)          # 'book|provisional_id|reason'
    unresolved: list[str] = field(default_factory=list)         # 'sport|book|raw'
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # book -> market -> rows


# ---- provisional ids ------------------------------------------------------------

def parse_stamp(stamp: str) -> tuple[datetime | None, bool]:
    """``(kickoff_utc, date_only)`` for a provisional-id stamp; ``(None, False)`` if unknown."""
    m = _STAMP_DT.match(stamp or "")
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc), False
    m = _STAMP_DAY.match(stamp or "")
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d, 12, 0, tzinfo=timezone.utc), True
    return None, False


def parse_provisional(game_id: str, book: str = "") -> RawGame | None:
    m = _PROV_RE.match(game_id or "")
    if not m:
        return None
    kick, date_only = parse_stamp(m.group("stamp"))
    return RawGame(
        book=book, game_id=game_id, sport=m.group("sport"), away=m.group("away").strip(),
        home=m.group("home").strip(), kickoff_utc=kick, date_only=date_only,
    )


def is_provisional(game_id: str) -> bool:
    return bool(_PROV_RE.match(game_id or ""))


def raw_games_from_lines(lines: Iterable[GameLine]) -> dict[str, RawGame]:
    """Fallback registry built from the provisional ids alone."""
    out: dict[str, RawGame] = {}
    for ln in lines:
        if ln.game_id in out:
            continue
        rg = parse_provisional(ln.game_id, ln.book)
        if rg is not None:
            out[ln.game_id] = rg
    return out


def raw_games_from_scraper(scraper: Any, book: str, sport: str) -> dict[str, RawGame]:
    """Richer registry from a scraper's retained parse objects (``last_games`` /
    ``last_events``: anything with ``away``/``home`` and a kickoff), which keep the
    raw team strings and the neutral flag the slugged ids lose."""
    out: dict[str, RawGame] = {}
    for attr in ("last_games", "last_events", "last_raw"):
        items = getattr(scraper, attr, None)
        if not items:
            continue
        for g in items if not isinstance(items, dict) else items.values():
            away = getattr(g, "away", None)
            home = getattr(g, "home", None)
            if not away or not home:
                continue
            kick = getattr(g, "kickoff_utc", None) or getattr(g, "start_utc", None)
            gid = getattr(g, "game_id", None)
            if not gid:
                continue
            out[str(gid)] = RawGame(
                book=book, game_id=str(gid), sport=sport, away=str(away), home=str(home),
                kickoff_utc=kick, neutral=bool(getattr(g, "neutral", False)),
            )
    return out


# ---- schedule matching ----------------------------------------------------------

@dataclass(frozen=True)
class Match:
    game: Game
    flipped: bool
    delta_h: float | None


class GameMatcher:
    """Index schedule games by ``(away_id, home_id)``; match a ``RawGame``."""

    def __init__(
        self,
        sport: str,
        games: Sequence[Game],
        window_h: float = WINDOW_H,
        now: datetime | None = None,
        data_dir: Path = teams_mod.DATA_DIR,
    ):
        self.sport = sport
        self.window = timedelta(hours=window_h)
        self.now = now
        self.data_dir = data_dir
        self.by_pair: dict[tuple[str, str], list[Game]] = {}
        for g in games:
            if g.sport != sport:
                continue
            self.by_pair.setdefault((g.away_id, g.home_id), []).append(g)

    def resolve(self, raw: str, book: str) -> str | None:
        return teams_mod.normalize_team(self.sport, raw, book, data_dir=self.data_dir)

    def _pick(self, cands: list[Game], raw: RawGame) -> tuple[Game | None, float | None]:
        if raw.kickoff_utc is None:
            if len(cands) == 1:
                return cands[0], None
            if self.now is not None:
                # several meetings on the schedule and no kickoff: the next one
                future = [g for g in cands if g.kickoff_utc >= self.now - self.window]
                if future:
                    g = min(future, key=lambda g: g.kickoff_utc)
                    return g, None
            return None, None
        kick = raw.kickoff_utc if raw.kickoff_utc.tzinfo else raw.kickoff_utc.replace(tzinfo=timezone.utc)
        best, best_d = None, None
        for g in cands:
            d = abs((g.kickoff_utc - kick).total_seconds()) / 3600.0
            if d <= self.window.total_seconds() / 3600.0 and (best_d is None or d < best_d):
                best, best_d = g, d
        return best, best_d

    def match(self, raw: RawGame) -> Match | None:
        away_id = self.resolve(raw.away, raw.book)
        home_id = self.resolve(raw.home, raw.book)
        if not away_id or not home_id:
            return None
        direct, d1 = self._pick(self.by_pair.get((away_id, home_id), []), raw)
        if direct is not None:
            return Match(direct, False, d1)
        swapped, d2 = self._pick(self.by_pair.get((home_id, away_id), []), raw)
        if swapped is not None:
            return Match(swapped, True, d2)
        return None


def _flip_line(ln: GameLine, game_id: str) -> GameLine:
    side = _FLIP.get(ln.side, ln.side)
    return GameLine(
        sport=ln.sport, game_id=game_id, book=ln.book, market=ln.market, side=side, odds=ln.odds,
        line=ln.line, prob_raw=ln.prob_raw, is_main=ln.is_main, source_id=ln.source_id,
        scraped_at=ln.scraped_at, run_id=ln.run_id,
    )


def _rekey(ln: GameLine, game_id: str) -> GameLine:
    return GameLine(
        sport=ln.sport, game_id=game_id, book=ln.book, market=ln.market, side=ln.side, odds=ln.odds,
        line=ln.line, prob_raw=ln.prob_raw, is_main=ln.is_main, source_id=ln.source_id,
        scraped_at=ln.scraped_at, run_id=ln.run_id,
    )


def canonicalize(
    sport: str,
    lines: Iterable[GameLine],
    games: Sequence[Game],
    raw_games: dict[str, RawGame] | None = None,
    *,
    window_h: float = WINDOW_H,
    now: datetime | None = None,
    data_dir: Path = teams_mod.DATA_DIR,
    result: MergeResult | None = None,
) -> MergeResult:
    """Re-key every provisional row onto its schedule ``game_id`` (flipping sides
    for swapped neutral listings). Rows already carrying a schedule id pass
    through untouched."""
    res = result or MergeResult(sport=sport)
    lines = [ln for ln in lines if ln.sport == sport]
    registry = dict(raw_games or {})
    for gid, rg in raw_games_from_lines(lines).items():
        registry.setdefault(gid, rg)
    matcher = GameMatcher(sport, games, window_h=window_h, now=now, data_dir=data_dir)
    known = {g.game_id for g in games if g.sport == sport}
    teams_mod.reset_unresolved(sport)

    cache: dict[str, tuple[str, bool] | None] = {}
    for ln in lines:
        pid = ln.game_id
        if pid in known:
            res.lines.append(ln)
            continue
        if pid not in cache:
            rg = registry.get(pid)
            m = matcher.match(rg) if rg is not None else None
            if m is None:
                cache[pid] = None
                reason = "unparsed" if rg is None else "no-schedule-match"
                res.unmatched.append(f"{ln.book}|{pid}|{reason}")
            else:
                cache[pid] = (m.game.game_id, m.flipped)
                res.game_map[pid] = cache[pid]
        hit = cache[pid]
        if hit is None:
            continue
        gid, flipped = hit
        res.lines.append(_flip_line(ln, gid) if flipped else _rekey(ln, gid))

    res.unresolved = teams_mod.unresolved_names(sport)
    for ln in res.lines:
        res.counts.setdefault(ln.book, {}).setdefault(ln.market, 0)
        res.counts[ln.book][ln.market] += 1
    return res


# ---- main-line selection + pivot ------------------------------------------------

def _home_line(ln: GameLine) -> float | None:
    """Home-relative (spread) / over (total) line key for pairing the two sides."""
    if ln.line is None:
        return None
    if ln.market == "spread" and ln.side == "away":
        return -ln.line
    return ln.line


def _prob(ln: GameLine) -> float:
    if ln.prob_raw is not None:
        return ln.prob_raw
    o = ln.odds
    return 100.0 / (o + 100.0) if o > 0 else -o / (-o + 100.0)


def _pair_lines(rows: Sequence[GameLine]) -> dict[float | None, dict[str, GameLine]]:
    """``{home_line: {side: row}}`` — one candidate pair per distinct line."""
    pairs: dict[float | None, dict[str, GameLine]] = {}
    for ln in rows:
        key = _home_line(ln) if ln.market != "ml" else None
        key = round(key, 2) if key is not None else None
        pairs.setdefault(key, {}).setdefault(ln.side, ln)
    return pairs


def select_main(rows: Sequence[GameLine], consensus: float | None = None) -> dict[str, GameLine]:
    """One main pair of sides for a (game, book, market) row group."""
    if not rows:
        return {}
    main_rows = [ln for ln in rows if ln.is_main]
    pairs = _pair_lines(main_rows or rows)
    if len(pairs) == 1:
        return next(iter(pairs.values()))
    # Ladder: nearest consensus, else the most balanced two-way price.
    if consensus is not None:
        keyed = [(k, v) for k, v in pairs.items() if k is not None]
        if keyed:
            k, v = min(keyed, key=lambda kv: (abs(kv[0] - consensus), abs(_balance(kv[1]))))
            return v

    def score(item: tuple[float | None, dict[str, GameLine]]) -> tuple[float, float]:
        return (abs(_balance(item[1])), abs(item[0] or 0.0))

    return min(pairs.items(), key=score)[1]


def _balance(sides: dict[str, GameLine]) -> float:
    """Distance of the pair from a coin-flip (0 = perfectly balanced). Single-sided
    pairs are penalised so a complete pair always wins."""
    vals = list(sides.values())
    if len(vals) < 2:
        return 10.0
    return _prob(vals[0]) - _prob(vals[1])


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    total = sum(w for _, w in values)
    acc = 0.0
    for v, w in values:
        acc += w
        if acc >= total / 2.0:
            return v
    return values[-1][0]


def consensus_lines(lines: Iterable[GameLine], weights: dict[str, float] = BOOK_WEIGHTS) -> dict[tuple[str, str], float]:
    """``{(game_id, market): weighted-median home/over main line}`` (spread/total only)."""
    groups: dict[tuple[str, str, str], list[GameLine]] = {}
    for ln in lines:
        if ln.market == "ml":
            continue
        groups.setdefault((ln.game_id, ln.book, ln.market), []).append(ln)
    per_game: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for (gid, book, market), rows in groups.items():
        sides = select_main(rows)
        key = next((k for k in map(_home_line, sides.values()) if k is not None), None)
        if key is None:
            continue
        per_game.setdefault((gid, market), []).append((float(key), weights.get(book, 1.0)))
    return {k: v for k, v in ((k, _weighted_median(v)) for k, v in per_game.items()) if v is not None}


def _side_cell(ln: GameLine) -> dict[str, Any]:
    return {"line": ln.line, "odds": ln.odds, "prob_raw": ln.prob_raw, "is_main": ln.is_main,
            "scraped_at": ln.scraped_at.isoformat() if ln.scraped_at else None}


def pivot(lines: Iterable[GameLine]) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """``board[game_id][book][market] = {"line": home/over line, "home"/"away" |
    "over"/"under": {line, odds, prob_raw, ...}, "n_alt": alternates dropped}``."""
    lines = list(lines)
    cons = consensus_lines(lines)
    groups: dict[tuple[str, str, str], list[GameLine]] = {}
    for ln in lines:
        groups.setdefault((ln.game_id, ln.book, ln.market), []).append(ln)
    board: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for (gid, book, market), rows in sorted(groups.items()):
        sides = select_main(rows, cons.get((gid, market)))
        if not sides:
            continue
        cell: dict[str, Any] = {s: _side_cell(ln) for s, ln in sides.items()}
        cell["line"] = next((k for k in map(_home_line, sides.values()) if k is not None), None)
        cell["n_alt"] = len(_pair_lines(rows)) - 1
        board.setdefault(gid, {}).setdefault(book, {})[market] = cell
    return board


# ---- openers --------------------------------------------------------------------

def update_openers(openers: dict[str, Any], lines: Iterable[GameLine], now: str) -> int:
    """Record first-seen (line, odds) per ``game|market|side|book`` for the *main*
    lines only (ladder alternates never become openers). Returns new count."""
    main = [ln for ln in lines if ln.is_main and not is_provisional(ln.game_id)]
    return state_mod.record_openers(openers, main, now)


def opener_for(openers: dict[str, Any], game_id: str, market: str, book: str) -> dict[str, Any]:
    """``{"line": home/over opener line, "<side>": {line, odds, ts}}`` or ``{}``."""
    out: dict[str, Any] = {}
    for side in ("home", "away", "over", "under"):
        rec = state_mod.get_opener(openers, state_mod.odds_key(game_id, market, side, book))
        if rec:
            out[side] = dict(rec)
    if not out:
        return {}
    if market == "spread":
        if "home" in out and out["home"].get("line") is not None:
            out["line"] = out["home"]["line"]
        elif "away" in out and out["away"].get("line") is not None:
            out["line"] = -out["away"]["line"]
    elif market == "total":
        first = out.get("over") or out.get("under") or {}
        out["line"] = first.get("line")
    else:
        out["line"] = None
    return out


# ---- entry point ----------------------------------------------------------------

def merge_odds(
    sport: str,
    games: Sequence[Game],
    lines: Iterable[GameLine],
    raw_games: dict[str, RawGame] | None = None,
    *,
    data_dir: Path | None = None,
    openers: dict[str, Any] | None = None,
    now: datetime | None = None,
    window_h: float = WINDOW_H,
    aliases_dir: Path = teams_mod.DATA_DIR,
    save: bool = True,
) -> MergeResult:
    """Canonicalize + pivot + openers. ``data_dir`` is where ``openers.json``
    lives (loaded when ``openers`` is not passed; saved back when ``save``)."""
    now = now or datetime.now(timezone.utc)
    res = canonicalize(sport, lines, games, raw_games, window_h=window_h, now=now, data_dir=aliases_dir)
    if openers is None:
        openers = state_mod.load_openers(data_dir) if data_dir is not None else state_mod.migrate({}, "openers")
    res.openers = openers
    res.new_openers = update_openers(openers, res.lines, now.isoformat())
    res.board = pivot(res.lines)
    if data_dir is not None and save:
        state_mod.save_openers(data_dir, openers)
    if res.unmatched:
        logger.info(f"[merge] {sport}: {len(res.unmatched)} unmatched book rows dropped")
    return res


def board_summary(res: MergeResult) -> dict[str, Any]:
    """Compact counts for build output / RunMeta."""
    n_games = len(res.board)
    books = sorted({b for g in res.board.values() for b in g})
    return {
        "games": n_games, "books": books, "counts": res.counts, "new_openers": res.new_openers,
        "unmatched": len(res.unmatched), "unresolved": len(res.unresolved),
    }


__all__ = [
    "BOOK_WEIGHTS", "WINDOW_H", "RawGame", "Match", "MergeResult", "GameMatcher",
    "parse_provisional", "parse_stamp", "is_provisional", "raw_games_from_lines",
    "raw_games_from_scraper", "canonicalize", "select_main", "consensus_lines", "pivot",
    "update_openers", "opener_for", "merge_odds", "board_summary",
]
