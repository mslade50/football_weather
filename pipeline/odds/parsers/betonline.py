"""Pure parser for BetOnline's ``offering-by-league`` football JSON.

Payload (recon 2026-08-23, ``POST api-offering.betonline.ag/api/offering/Sports/offering-by-league``
with ``{"Sport":"football","League":"nfl"|"nfl-preseason"|"ncaa","filterTime":0}``)::

    GameOffering.GamesDescription[] = {
      GameDate: "09/13/2026",
      Game: {
        GameId, AwayTeam, HomeTeam, AwayRotation, HomeRotation,
        WagerCutOff: "2026-09-13T13:00:00",   # kickoff, in the request's utc-offset (240 -> UTC-4)
        GameDateTime: "0001-01-01T00:00:00",  # always unset -- use WagerCutOff
        Comments: "" | "Neutral Field" | "Melbourne Cricket Ground, Australia",
        ScheduleText: "",
        AwayLine: {SpreadLine: {Point, Line}, MoneyLine: {Line}, TotalLine: {...zeros...}},
        HomeLine: {SpreadLine: {Point, Line}, MoneyLine: {Line}, ...},
        TotalLine: {TotalLine: {Point, Over: {Line}, Under: {Line}}},   # game total lives HERE
        DrawLine: {...zeros...},
      }}

* Off-the-board markets are all zeros: spread ``Point==0 and Line==0``, moneyline
  ``Line==0``, total ``Point==0``. A pick'em spread is ``Point 0`` with a *non-zero*
  ``Line`` (home side comes back as ``-0.0``) and is kept as ``line=0.0``.
* Moneylines are frequently missing for big favourites (both sides 0) -- rows dropped.
* ``Comments`` non-empty means neutral site (``"Neutral Field"`` or a venue string).
* Slug for CFB is ``ncaa`` (menu ``League:'NCAA'``); ``college-football`` returns
  ``GameOffering: null``. ``nfl-preseason`` returns null once preseason ends.

Scrapers do not know the schedule, so ``game_id`` is provisional
(``{sport}:raw:{YYYYMMDD}:{away-slug}@{home-slug}``, same shape as the betcris
parser); ``odds/merge.py`` resolves it against the schedule ``Game``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.contracts import GameLine

BOOK = "betonline"

# sport -> League slugs to request (each answered independently; null offerings are skipped)
LEAGUES: dict[str, tuple[str, ...]] = {
    "nfl": ("nfl", "nfl-preseason"),
    "cfb": ("ncaa",),
}

# ``utc-offset: 240`` request header -> WagerCutOff is UTC minus 240 minutes, year-round.
UTC_OFFSET_MINUTES = 240


@dataclass(frozen=True)
class BetOnlineGame:
    game_id_src: int
    sport: str
    league: str
    away: str
    home: str
    kickoff_utc: datetime | None
    venue: str | None
    neutral: bool
    away_spread: float | None
    away_spread_odds: int | None
    home_spread: float | None
    home_spread_odds: int | None
    total: float | None
    over_odds: int | None
    under_odds: int | None
    away_ml: int | None
    home_ml: int | None

    @property
    def game_id(self) -> str:
        return raw_game_id(self.sport, self.away, self.home, self.kickoff_utc)

    @property
    def source_id(self) -> str:
        return f"{self.league}:{self.game_id_src}"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def raw_game_id(sport: str, away: str, home: str, start_utc: datetime | None) -> str:
    day = start_utc.strftime("%Y%m%d") if start_utc else "unknown"
    return f"{sport}:raw:{day}:{_slug(away)}@{_slug(home)}"


def parse_cutoff(value: str | None, offset_minutes: int = UTC_OFFSET_MINUTES) -> datetime | None:
    """'2026-09-13T13:00:00' (UTC-offset local) -> aware UTC datetime; None when unset."""
    if not value or value.startswith("0001-"):
        return None
    try:
        local = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (local + timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _odds(value: Any) -> int | None:
    n = int(_num(value))
    return n or None


def _point(value: Any) -> float:
    p = _num(value)
    return 0.0 if p == 0 else p  # -0.0 -> 0.0


def parse_game(entry: dict, sport: str, league: str = "") -> BetOnlineGame | None:
    g = entry.get("Game") or {}
    away = (g.get("AwayTeam") or "").strip()
    home = (g.get("HomeTeam") or "").strip()
    if not away or not home:
        return None

    away_ln = g.get("AwayLine") or {}
    home_ln = g.get("HomeLine") or {}
    a_sp = away_ln.get("SpreadLine") or {}
    h_sp = home_ln.get("SpreadLine") or {}
    tot = (g.get("TotalLine") or {}).get("TotalLine") or {}

    a_sp_odds = _odds(a_sp.get("Line"))
    h_sp_odds = _odds(h_sp.get("Line"))
    spread_on = a_sp_odds is not None and h_sp_odds is not None
    away_spread = _point(a_sp.get("Point")) if spread_on else None
    home_spread = _point(h_sp.get("Point")) if spread_on else None
    if spread_on and away_spread is not None and home_spread is not None and away_spread != -home_spread:
        # Book always mirrors the points; trust the away side if they ever disagree.
        home_spread = -away_spread

    total_pt = _num(tot.get("Point"))
    over_odds = _odds((tot.get("Over") or {}).get("Line"))
    under_odds = _odds((tot.get("Under") or {}).get("Line"))
    total_on = total_pt > 0 and over_odds is not None and under_odds is not None

    comments = (g.get("Comments") or "").strip()
    return BetOnlineGame(
        game_id_src=int(g.get("GameId") or 0),
        sport=sport,
        league=league,
        away=away,
        home=home,
        kickoff_utc=parse_cutoff(g.get("WagerCutOff")),
        venue=comments or None,
        neutral=bool(comments),
        away_spread=away_spread,
        away_spread_odds=a_sp_odds if spread_on else None,
        home_spread=home_spread,
        home_spread_odds=h_sp_odds if spread_on else None,
        total=total_pt if total_on else None,
        over_odds=over_odds if total_on else None,
        under_odds=under_odds if total_on else None,
        away_ml=_odds((away_ln.get("MoneyLine") or {}).get("Line")),
        home_ml=_odds((home_ln.get("MoneyLine") or {}).get("Line")),
    )


def parse_games(payload: dict | None, sport: str, *, league: str = "") -> list[BetOnlineGame]:
    """One offering-by-league response -> games (null offering / error -> [])."""
    if not isinstance(payload, dict) or payload.get("IsError"):
        return []
    offering = payload.get("GameOffering") or {}
    league = league or str(offering.get("League") or "").lower()
    out: list[BetOnlineGame] = []
    for entry in offering.get("GamesDescription") or []:
        g = parse_game(entry, sport, league)
        if g is not None:
            out.append(g)
    return out


def game_lines(
    g: BetOnlineGame,
    *,
    market: str | None = None,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    common = dict(sport=g.sport, game_id=g.game_id, book=BOOK, source_id=g.source_id,
                  scraped_at=scraped_at, run_id=run_id)
    out: list[GameLine] = []
    want = lambda m: market is None or market == m  # noqa: E731

    if want("spread") and g.away_spread is not None and g.home_spread is not None:
        out.append(GameLine(market="spread", side="away", line=g.away_spread, odds=g.away_spread_odds, **common))
        out.append(GameLine(market="spread", side="home", line=g.home_spread, odds=g.home_spread_odds, **common))
    if want("total") and g.total is not None:
        out.append(GameLine(market="total", side="over", line=g.total, odds=g.over_odds, **common))
        out.append(GameLine(market="total", side="under", line=g.total, odds=g.under_odds, **common))
    if want("ml") and g.away_ml is not None and g.home_ml is not None:
        out.append(GameLine(market="ml", side="away", odds=g.away_ml, **common))
        out.append(GameLine(market="ml", side="home", odds=g.home_ml, **common))
    return out


def dedupe_games(games: list[BetOnlineGame]) -> list[BetOnlineGame]:
    """Drop repeats across leagues (nfl + nfl-preseason) by provisional game_id."""
    seen: set[str] = set()
    out: list[BetOnlineGame] = []
    for g in games:
        if g.game_id in seen:
            continue
        seen.add(g.game_id)
        out.append(g)
    return out


def parse(
    payload: dict | None,
    sport: str,
    *,
    league: str = "",
    market: str | None = None,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    """One offering-by-league response -> GameLine rows (off-board markets contribute nothing)."""
    lines: list[GameLine] = []
    for g in parse_games(payload, sport, league=league):
        lines.extend(game_lines(g, market=market, scraped_at=scraped_at, run_id=run_id))
    return lines


__all__ = [
    "BOOK",
    "LEAGUES",
    "UTC_OFFSET_MINUTES",
    "BetOnlineGame",
    "dedupe_games",
    "game_lines",
    "parse",
    "parse_cutoff",
    "parse_game",
    "parse_games",
    "raw_game_id",
]
