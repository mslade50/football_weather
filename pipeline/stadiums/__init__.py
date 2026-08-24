"""Stadium / team reference data (data/stadiums.csv, data/teams.csv, overrides, aliases)."""

from pipeline.stadiums.loader import (
    ResolvedGame,
    StadiumBook,
    load_aliases,
    load_stadium_book,
    normalize_alias,
    slug,
)

__all__ = ["ResolvedGame", "StadiumBook", "load_aliases", "load_stadium_book", "normalize_alias", "slug"]
