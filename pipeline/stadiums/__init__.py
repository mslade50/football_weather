"""Stadium / team reference data (curated + enriched stadiums, climatology, teams, aliases)."""

from pipeline.stadiums.loader import (
    ResolvedGame,
    StadiumBook,
    load_aliases,
    load_stadium_book,
    normalize_alias,
    slug,
)

__all__ = ["ResolvedGame", "StadiumBook", "load_aliases", "load_stadium_book", "normalize_alias", "slug"]
