"""Schedule sources: nflverse (NFL), CFBD with ESPN fallback (CFB)."""

from pipeline.schedule.cfb import fetch_cfb_schedule, parse_cfbd_games
from pipeline.schedule.espn import fetch_espn_scoreboard, parse_espn_scoreboard
from pipeline.schedule.nfl import fetch_nfl_schedule, parse_nflverse_games

__all__ = [
    "fetch_cfb_schedule",
    "fetch_espn_scoreboard",
    "fetch_nfl_schedule",
    "parse_cfbd_games",
    "parse_espn_scoreboard",
    "parse_nflverse_games",
]
