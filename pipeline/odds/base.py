"""Abstract base class for sportsbook scrapers (copied from golf_scraping/scrapers/base.py).

Golf's Matchup/ScoreLine/OutrightLine/PropLine dataclasses are replaced by the
single ``GameLine`` contract (ARCH §4.2), imported from ``pipeline.contracts``.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import pandas as pd

from pipeline.contracts import GameLine

logger = logging.getLogger(__name__)


def _line_to_dict(line: Any) -> dict:
    if hasattr(line, "as_dict"):
        return line.as_dict()
    if hasattr(line, "to_dict"):
        return line.to_dict()
    from dataclasses import asdict
    return asdict(line)


class BaseScraper(ABC):
    """Base class all sportsbook scrapers inherit from."""

    BOOK_NAME: str = ""
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 5.0  # seconds

    @abstractmethod
    async def scrape(self, sport: str, market: Optional[str] = None, **kwargs: Any) -> list[GameLine]:
        """Scrape game lines for ``sport`` (``nfl`` | ``cfb``).

        Args:
            sport: which league to scrape; threaded through every call.
            market: filter to ``ml`` / ``spread`` / ``total``, or None for all.
        """
        ...

    async def scrape_with_retry(self, sport: str, market: Optional[str] = None, **kwargs: Any) -> list[GameLine]:
        """Scrape with retry logic. Wraps scrape() with retries and error handling."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.info(f"[{self.BOOK_NAME}] {sport} attempt {attempt}/{self.MAX_RETRIES}")
                lines = await self.scrape(sport, market=market, **kwargs)
                logger.info(f"[{self.BOOK_NAME}] {sport}: got {len(lines)} lines")
                return lines
            except Exception as e:
                logger.error(f"[{self.BOOK_NAME}] {sport} attempt {attempt} failed: {e}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * attempt)
        logger.error(f"[{self.BOOK_NAME}] All {self.MAX_RETRIES} attempts failed")
        return []

    @staticmethod
    def to_dataframe(lines: list[GameLine]) -> pd.DataFrame:
        """Convert list of GameLine objects to a DataFrame."""
        if not lines:
            return pd.DataFrame()
        return pd.DataFrame([_line_to_dict(ln) for ln in lines])
