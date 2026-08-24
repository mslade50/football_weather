"""Frozen dataclass contracts (ARCHITECTURE §4.2).

pydantic is not installed in this environment, so these are stdlib
``dataclasses`` with ``frozen=True``. Validation that pydantic would have done
lives in ``__post_init__`` for the few invariants that matter downstream
(sport / market / side vocabularies, probability bounds).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Optional

SPORTS = ("nfl", "cfb")
MARKETS = ("ml", "spread", "total")
SIDES = ("home", "away", "over", "under")
ROOF_STATES = ("outdoors", "dome", "closed", "open")
TIERS = ("strong", "edge", "watch", "none")
SEVERITIES = ("info", "warn", "error")


def _check_in(value: Any, allowed: tuple, label: str) -> None:
    if value not in allowed:
        raise ValueError(f"{label}={value!r} not in {allowed}")


def make_game_id(sport: str, season: int, week: int, away_id: str, home_id: str) -> str:
    return f"{sport}:{season}:{week}:{away_id}@{home_id}"


def odds_key(game_id: str, market: str, side: str, book: str) -> str:
    return f"{game_id}|{market}|{side}|{book}"


class _AsDict:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]  # type: ignore[arg-type]


@dataclass(frozen=True)
class Game(_AsDict):
    game_id: str
    sport: str
    season: int
    week: int
    kickoff_utc: datetime
    kickoff_local: datetime
    tz: str
    home_id: str
    away_id: str
    stadium_id: Optional[str]
    neutral: bool = False
    roof_state: Optional[str] = None
    status: str = "scheduled"
    source: str = ""

    def __post_init__(self) -> None:
        _check_in(self.sport, SPORTS, "sport")
        if self.roof_state is not None:
            _check_in(self.roof_state, ROOF_STATES, "roof_state")
        if not self.game_id:
            raise ValueError("game_id required")

    @property
    def month(self) -> int:
        return self.kickoff_local.month


@dataclass(frozen=True)
class Stadium(_AsDict):
    stadium_id: str
    name: str
    lat: float
    lon: float
    aliases: list[str] = field(default_factory=list)
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    elevation_m: Optional[float] = None
    timezone: Optional[str] = None
    orientation_deg: Optional[float] = None
    orientation_bucket: Optional[str] = None
    orientation_src: Optional[str] = None
    roof_type: Optional[str] = None
    surface: Optional[str] = None
    capacity: Optional[int] = None
    year_built: Optional[int] = None
    avg_wind_static: Optional[float] = None
    wind_vol_static: Optional[str] = None
    wind_impact_static: Optional[str] = None
    weakest_wind_effect: Optional[str] = None
    avg_wind_by_month: dict[str, float] = field(default_factory=dict)
    avg_temp_f: Optional[float] = None
    wikidata_qid: Optional[str] = None
    osm_way_id: Optional[str] = None
    cfbd_venue_id: Optional[str] = None
    espn_venue_id: Optional[str] = None
    nflverse_stadium_id: Optional[str] = None
    needs_review: bool = False

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lon <= 180.0):
            raise ValueError(f"bad coordinates for {self.stadium_id}: {self.lat},{self.lon}")

    @property
    def is_dome(self) -> bool:
        return self.roof_type == "dome"


@dataclass(frozen=True)
class Team(_AsDict):
    team_id: str
    sport: str
    name: str
    short: Optional[str] = None
    home_stadium_id: Optional[str] = None
    avg_temp_f: Optional[float] = None
    conference: Optional[str] = None
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _check_in(self.sport, SPORTS, "sport")


@dataclass(frozen=True)
class WeatherPoint(_AsDict):
    """One hourly sample inside the game window."""

    t: datetime
    temp: Optional[float] = None
    wind: Optional[float] = None
    gust: Optional[float] = None
    dir: Optional[float] = None
    precip: Optional[float] = None
    pop: Optional[float] = None
    p10: Optional[float] = None
    p90: Optional[float] = None


@dataclass(frozen=True)
class WeatherForecast(_AsDict):
    game_id: str
    source: str
    run_time: Optional[datetime] = None
    lead_hours: Optional[float] = None
    temp_fg: Optional[float] = None
    wind_fg: Optional[float] = None
    gust_fg: Optional[float] = None
    wind_dir_1h: Optional[str] = None
    wind_dir_2h: Optional[str] = None
    wind_dir_fg: Optional[str] = None
    wind_dir_deg: Optional[float] = None
    rain_fg_mm: Optional[float] = None
    precip_prob: Optional[float] = None
    wind_vol_fc: Optional[float] = None
    wind_p10: Optional[float] = None
    wind_p50: Optional[float] = None
    wind_p90: Optional[float] = None
    cross_mph: Optional[float] = None
    head_mph: Optional[float] = None
    model_disagreement: Optional[float] = None
    roof_state: Optional[str] = None
    hourly: list[WeatherPoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.roof_state is not None:
            _check_in(self.roof_state, ROOF_STATES, "roof_state")


# Alias used in some docs/prompts.
Forecast = WeatherForecast


@dataclass(frozen=True)
class GameLine(_AsDict):
    sport: str
    game_id: str
    book: str
    market: str
    side: str
    odds: int
    line: Optional[float] = None
    prob_raw: Optional[float] = None
    is_main: bool = True
    source_id: Optional[str] = None
    scraped_at: Optional[datetime] = None
    run_id: Optional[str] = None

    def __post_init__(self) -> None:
        _check_in(self.sport, SPORTS, "sport")
        _check_in(self.market, MARKETS, "market")
        _check_in(self.side, SIDES, "side")
        if self.market == "ml" and self.line is not None:
            raise ValueError("moneyline has no line")
        if self.market != "ml" and self.line is None:
            raise ValueError(f"{self.market} requires a line")
        if self.prob_raw is not None and not (0.0 < self.prob_raw < 1.0):
            raise ValueError(f"prob_raw must be in (0,1), got {self.prob_raw}")

    @property
    def key(self) -> str:
        return odds_key(self.game_id, self.market, self.side, self.book)


@dataclass(frozen=True)
class Edge(_AsDict):
    game_id: str
    book: str
    market: str
    side: str
    line: Optional[float]
    odds: int
    fair_line: Optional[float]
    fair_prob: Optional[float]
    vigfree_prob: Optional[float]
    edge_pts: Optional[float]
    edge_prob: Optional[float]
    confidence: float
    tier: str
    model_version: str = "v1"
    ref_book: Optional[str] = None
    n_books: int = 0

    def __post_init__(self) -> None:
        _check_in(self.market, MARKETS, "market")
        _check_in(self.side, SIDES, "side")
        _check_in(self.tier, TIERS, "tier")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence out of range: {self.confidence}")


@dataclass(frozen=True)
class Degradation(_AsDict):
    component: str
    reason: str
    severity: str = "warn"
    run_id: Optional[str] = None
    ts: Optional[datetime] = None

    def __post_init__(self) -> None:
        _check_in(self.severity, SEVERITIES, "severity")


@dataclass(frozen=True)
class RunMeta(_AsDict):
    run_id: str
    sport: str
    season: Optional[int] = None
    week: Optional[int] = None
    git_sha: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    stage_timings: dict[str, float] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    degradations: list[Degradation] = field(default_factory=list)
    unresolved_names: list[str] = field(default_factory=list)
    next_run_eta: Optional[datetime] = None


__all__ = [
    "SPORTS",
    "MARKETS",
    "SIDES",
    "ROOF_STATES",
    "TIERS",
    "SEVERITIES",
    "make_game_id",
    "odds_key",
    "Game",
    "Stadium",
    "Team",
    "WeatherPoint",
    "WeatherForecast",
    "Forecast",
    "GameLine",
    "Edge",
    "Degradation",
    "RunMeta",
]
