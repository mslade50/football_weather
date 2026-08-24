"""Pure parser for the bookmaker.eu (Betcris) public lines viewer, football pages.

Input is the server-rendered HTML of
``https://lines.bookmaker.eu/en/sports/football/{nfl,nfl-preseason,college-football}/``
(no login, no JS needed). One ``<table class="oddsTable">`` per page:

* ``<tr><th class="oddsSubTitle">LEAGUE<br>extra</th></tr>`` section rows. The
  ``extra`` line is either ``Preseason`` or a neutral/international venue
  (``Aviva Stadium - Dublin, Ireland``). A subtitle applies to the games that
  follow it until the next subtitle.
* Row pairs ``<tr id="vTeam_N">`` (away) / ``<tr id="hTeam_N">`` (home) with cells
  ``vN_/hN_`` team, ``vS_/hS_`` spread, ``vT_/hT_`` total, ``vM_/hM_`` moneyline.
  ``½`` is used for half points; ``-`` means off the board.
* ``<td id="GameN_Time" title="START 8/29 9:00am PT">`` kickoff in Pacific time
  (no year). The page footer script carries the same kickoff with a year:
  ``var gameN_start= new Date('2026-08-29 09:00:00.00')`` — preferred when present.

The viewer publishes spread/total *lines only* (no juice). Those rows get
``DEFAULT_JUICE`` (-110, bookmaker's standard football price) so ``GameLine.odds``
is populated; ``prob_raw`` stays None so downstream can tell it was assumed.

Scrapers do not know the schedule, so ``game_id`` is provisional
(``{sport}:raw:{YYYYMMDD}:{away-slug}@{home-slug}``, same shape as the other
parsers); ``odds/merge.py`` resolves it against the schedule ``Game``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from pipeline.contracts import GameLine

BOOK = "betcris"
DEFAULT_JUICE = -110
PT = ZoneInfo("America/Los_Angeles")

# sport -> page slugs under /en/sports/football/
PAGES: dict[str, tuple[str, ...]] = {
    "nfl": ("nfl", "nfl-preseason"),
    "cfb": ("college-football",),
}

_START_RE = re.compile(r"START\s+(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*([ap]m)\s*PT", re.IGNORECASE)
_SCRIPT_START_RE = re.compile(r"var\s+game(\d+)_start\s*=\s*new\s+Date\('(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


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


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def raw_game_id(sport: str, away: str, home: str, start_utc: datetime | None) -> str:
    day = start_utc.strftime("%Y%m%d") if start_utc else "unknown"
    return f"{sport}:raw:{day}:{_slug(away)}@{_slug(home)}"


def parse_number(text: str | None) -> float | None:
    """'+9½' -> 9.5, '-3' -> -3.0, 'PK'/'EV' -> 0.0, '-'/'' -> None."""
    if text is None:
        return None
    t = text.strip().replace("½", ".5")
    if not t or t in ("-", "–", "—"):
        return None
    if t.lower() in ("pk", "pick", "ev", "even"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return None


def parse_odds(text: str | None) -> int | None:
    """American price: '+245' -> 245, '-300' -> -300, 'EVEN' -> 100, '-' -> None."""
    if text is None:
        return None
    t = text.strip()
    if not t or t in ("-", "–", "—"):
        return None
    if t.lower() in ("even", "ev", "pk", "pick"):
        return 100
    try:
        return int(t.replace("+", ""))
    except ValueError:
        return None


def parse_start_title(title: str | None, season: int) -> datetime | None:
    """'START 8/29 9:00am PT' -> aware UTC datetime; Jan/Feb dates roll into season+1."""
    if not title:
        return None
    m = _START_RE.search(title)
    if not m:
        return None
    month, day, hour, minute, ampm = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5].lower()
    hour = hour % 12 + (12 if ampm == "pm" else 0)
    year = season + 1 if month <= 2 else season
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=PT)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def parse_script_starts(html: str) -> dict[int, datetime]:
    """{game_number: kickoff UTC} from the footer ``var gameN_start`` lines (PT wall clock)."""
    out: dict[int, datetime] = {}
    for num, stamp in _SCRIPT_START_RE.findall(html):
        try:
            local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PT)
        except ValueError:
            continue
        out[int(num)] = local.astimezone(timezone.utc)
    return out


def parse_subtitle(th) -> tuple[str | None, bool]:
    """(venue, preseason) from an ``oddsSubTitle`` cell; venue is None for plain league rows."""
    lines = [s.strip() for s in th.get_text("\n", strip=True).split("\n") if s.strip()]
    venue: str | None = None
    preseason = False
    for extra in lines[1:]:
        if extra.lower() == "preseason":
            preseason = True
        else:
            venue = extra
    if len(lines) == 1 and "preseason" in lines[0].lower():
        preseason = True
    return venue, preseason


def _cell_text(scope, cell_id: str) -> str | None:
    el = scope.find(id=cell_id)
    return el.get_text(" ", strip=True) if el else None


def parse_games(html: str, sport: str, *, page: str = "", season: int | None = None) -> list[BetcrisGame]:
    """Every vTeam/hTeam pair on the page, priced or not."""
    if sport not in PAGES:
        raise ValueError(f"unknown sport {sport!r}")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="oddsTable")
    if table is None:
        return []
    starts = parse_script_starts(html)
    if season is None:
        season = _infer_season(starts)

    games: list[BetcrisGame] = []
    venue: str | None = None
    preseason = False
    for row in table.find_all("tr"):
        sub = row.find("th", class_="oddsSubTitle")
        if sub is not None:
            venue, preseason = parse_subtitle(sub)
            continue
        rid = row.get("id") or ""
        if not rid.startswith("vTeam_"):
            continue
        n = int(rid[len("vTeam_"):])
        home_row = table.find("tr", id=f"hTeam_{n}")
        away = _cell_text(row, f"vN_{n}")
        home = _cell_text(home_row, f"hN_{n}") if home_row is not None else None
        if not away or not home:
            continue
        time_el = row.find(id=f"Game{n}_Time")
        kickoff = starts.get(n) or parse_start_title(time_el.get("title") if time_el else None, season)
        games.append(BetcrisGame(
            number=n,
            sport=sport,
            page=page,
            away=away,
            home=home,
            kickoff_utc=kickoff,
            venue=venue,
            neutral=venue is not None,
            preseason=preseason,
            away_spread=parse_number(_cell_text(row, f"vS_{n}")),
            home_spread=parse_number(_cell_text(home_row, f"hS_{n}")),
            total=parse_number(_cell_text(row, f"vT_{n}")),
            away_ml=parse_odds(_cell_text(row, f"vM_{n}")),
            home_ml=parse_odds(_cell_text(home_row, f"hM_{n}")),
        ))
    return games


def _infer_season(starts: dict[int, datetime]) -> int:
    if starts:
        first = min(starts.values()).astimezone(PT)
        return first.year - 1 if first.month <= 2 else first.year
    now = datetime.now(timezone.utc)
    return now.year - 1 if now.month <= 2 else now.year


def game_lines(
    g: BetcrisGame,
    *,
    market: str | None = None,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    common = dict(sport=g.sport, game_id=g.game_id, book=BOOK, source_id=g.source_id,
                  scraped_at=scraped_at, run_id=run_id)
    out: list[GameLine] = []
    want = lambda m: market is None or market == m  # noqa: E731

    if want("spread"):
        away_sp = g.away_spread
        home_sp = g.home_spread if g.home_spread is not None else (-away_sp if away_sp is not None else None)
        if away_sp is None and home_sp is not None:
            away_sp = -home_sp
        if away_sp is not None and home_sp is not None:
            out.append(GameLine(market="spread", side="away", line=away_sp, odds=DEFAULT_JUICE, **common))
            out.append(GameLine(market="spread", side="home", line=home_sp, odds=DEFAULT_JUICE, **common))
    if want("total") and g.total is not None:
        out.append(GameLine(market="total", side="over", line=g.total, odds=DEFAULT_JUICE, **common))
        out.append(GameLine(market="total", side="under", line=g.total, odds=DEFAULT_JUICE, **common))
    if want("ml") and g.away_ml is not None and g.home_ml is not None:
        out.append(GameLine(market="ml", side="away", odds=g.away_ml, **common))
        out.append(GameLine(market="ml", side="home", odds=g.home_ml, **common))
    return out


def parse(
    html: str,
    sport: str,
    *,
    page: str = "",
    season: int | None = None,
    market: str | None = None,
    scraped_at: datetime | None = None,
    run_id: str | None = None,
) -> list[GameLine]:
    """HTML of one lines-viewer page -> GameLine rows (unpriced games contribute nothing)."""
    lines: list[GameLine] = []
    for g in parse_games(html, sport, page=page, season=season):
        lines.extend(game_lines(g, market=market, scraped_at=scraped_at, run_id=run_id))
    return lines


def dedupe_games(games: list[BetcrisGame]) -> list[BetcrisGame]:
    """Drop repeats across pages (the nfl page also lists upcoming preseason games)."""
    seen: set[str] = set()
    out: list[BetcrisGame] = []
    for g in games:
        if g.game_id in seen:
            continue
        seen.add(g.game_id)
        out.append(g)
    return out
