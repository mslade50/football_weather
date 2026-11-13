"""Pure parser for Betcris public football odds JSON.

Preserves provider prices, observation times, expiry, and UTC kickoffs.
Provisional game ids are resolved against the schedule by odds/merge.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pipeline.contracts import GameLine, game_line
from pipeline.odds.ids import provisional_game_id as build_game_id
from pipeline.odds.parsers.common import dedupe_games as dedupe_games
from pipeline.odds.parsers.common import finite_number as _public_number
from pipeline.odds.parsers.common import parse_utc, valid_lines

BOOK = "betcris"
# How long a public print stays on the board, measured from feed_fetched_at.
# CFB refreshes every few minutes and uses DISPLAY_MAX_AGE_S. The provider TTL
# still decides whether that price may create an edge; this ceiling only
# decides whether it is shown. NFL league/1.json (tier "later") refreshes about
# once an hour, so its ceiling is min(provider TTL, NFL_CARRY_MAX_AGE_S).
# NFL_CARRY_MAX_AGE_S matches pipeline.build.ODDS_CARRY_MAX_AGE.
DISPLAY_MAX_AGE_S = 1800
NFL_CARRY_MAX_AGE_S = 3600
PUBLIC_LEAGUES = {"nfl": 1, "cfb": 2}


class StaleObservationError(ValueError):
    """A valid league observation exceeded its existing freshness deadline."""


@dataclass(frozen=True)
class BetcrisGame:
    number: int
    sport: str
    page: str
    away: str
    home: str
    kickoff_utc: datetime | None
    venue: str | None
    neutral: bool
    preseason: bool
    away_spread: float | None
    home_spread: float | None
    total: float | None
    away_ml: int | None
    home_ml: int | None

    @property
    def game_id(self) -> str:
        return raw_game_id(self.sport, self.away, self.home, self.kickoff_utc)

    @property
    def source_id(self) -> str:
        return f"{self.page}:{self.number}"


def raw_game_id(sport: str, away: str, home: str, start_utc: datetime | None) -> str:
    return build_game_id(sport, away, home, start_utc, precision="compact", slug="hyphen")


def _public_timestamp(value: object) -> datetime | None:
    return parse_utc(value, naive_utc=False)


def parse_public(payload: dict, sport: str, *, now: datetime,
                 market: str | None = None, run_id: str | None = None) -> list[GameLine]:
    """Betcris's public /assets/odds/v1 league JSON -> open pregame prices.

    Keep the upstream observation time, not our download time. The publisher
    retains closed games and partial-game markets, so neither is current odds.
    JSON kickoff strings carry their actual UTC/offset time.
    """
    if sport not in PUBLIC_LEAGUES:
        raise ValueError(f"unknown sport {sport!r}")
    if not isinstance(payload, dict) or payload.get("feed_ok") is not True:
        raise ValueError("Betcris public feed is unavailable")
    if type(payload.get("version")) is not int or payload.get("version") != 1:
        raise ValueError("Betcris public feed has an unsupported schema version")
    league = payload.get("league")
    expected_name = "NFL" if sport == "nfl" else "COLLEGE FOOTBALL"
    if (not isinstance(league, dict) or type(league.get("id")) is not int
            or league.get("id") != PUBLIC_LEAGUES[sport]
            or league.get("name") != expected_name or league.get("sport") != "FOOTBALL"):
        raise ValueError(f"Betcris public feed is not the {sport} full-game league")
    observed = _public_timestamp(payload.get("feed_fetched_at"))
    ttl = _public_number(payload.get("stale_after_seconds"))
    # Global catalog activity and local download time cannot confirm a retained
    # league revision. Its own observation clock remains the freshness gate.
    if observed is None or ttl is None or ttl <= 0 or now < observed:
        raise ValueError("Betcris public feed has a stale or invalid observation timestamp")
    age = (now - observed).total_seconds()
    # A run at :01 can see an NFL file 30-60 minutes old. Keep that print until
    # the earlier of the provider TTL and the one-hour carry window. CFB stays
    # on the 30-minute ceiling; a tighter provider TTL does not hide it.
    max_age = min(ttl, float(NFL_CARRY_MAX_AGE_S)) if sport == "nfl" else float(DISPLAY_MAX_AGE_S)
    effective_ttl = max_age
    if age > effective_ttl:
        raise StaleObservationError(
            "Betcris public feed has a stale or invalid observation timestamp: "
            f"observed={observed.isoformat()}, age_seconds={age:.0f}, "
            f"effective_ttl_seconds={effective_ttl:g}, provider_ttl_seconds={ttl:g}")
    display_until = observed + timedelta(seconds=max_age)
    actionable_until = observed + timedelta(seconds=ttl)
    games = payload.get("games")
    if not isinstance(games, list):
        raise ValueError("Betcris public feed is missing its games list")
    out: list[GameLine] = []
    for game in games:
        if (not isinstance(game, dict) or game.get("live") is not False
                or game.get("period_no") != 0 or game.get("parent") is not None):
            continue
        start = _public_timestamp(game.get("starts_at"))
        if start is None:
            continue
        # October captures match the canonical schedule in UTC, including the
        # London 13:30 kickoff. Do not add a Pacific offset to current feeds.
        kickoff = start
        if kickoff <= now:
            continue
        home, away = game.get("home"), game.get("visitor")
        if not isinstance(home, str) or not home.strip() or not isinstance(away, str) or not away.strip():
            continue
        gid = build_game_id(sport, away, home, kickoff, slug="hyphen")
        markets = game.get("markets")
        if not isinstance(markets, dict):
            continue
        for source_market, target_market, sides in (
            ("moneyline", "ml", {"home": "home", "visitor": "away"}),
            ("spread", "spread", {"home": "home", "visitor": "away"}),
            ("total", "total", {"over": "over", "under": "under"}),
        ):
            quotes = markets.get(source_market)
            if (market is not None and market != target_market) or not isinstance(quotes, dict):
                continue
            for source_side, side in sides.items():
                quote = quotes.get(source_side)
                if not isinstance(quote, dict) or quote.get("status") != "open":
                    continue
                price = _public_number(quote.get("price"))
                line = _public_number(quote.get("line")) if target_market != "ml" else None
                if price is None or abs(price) < 100 or not price.is_integer():
                    continue
                if target_market != "ml" and line is None:
                    continue
                out.append(game_line(sport=sport, game_id=gid, book=BOOK, market=target_market,
                                    side=side, line=line, odds=int(price), scraped_at=observed,
                                    source_id=f"public:{game.get('id')}", run_id=run_id,
                                    source_updated_at=observed,
                                    expires_at=display_until,
                                    actionable_until=actionable_until))
    out = valid_lines(out)
    return out


def parse(payload: dict, sport: str, *, now: datetime,
          market: str | None = None, run_id: str | None = None) -> list[GameLine]:
    """Parse a decoded public JSON object; HTML and other inputs are unsupported."""
    if not isinstance(payload, dict):
        raise ValueError("Betcris parser requires a decoded public JSON object; HTML is unsupported")
    return parse_public(payload, sport, now=now, market=market, run_id=run_id)
