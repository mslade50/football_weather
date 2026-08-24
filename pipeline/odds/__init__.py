"""Sportsbook scrapers: each book exposes ``XScraper(BaseScraper)`` with
``async scrape(sport) -> list[GameLine]`` and a pure parser in ``parsers/``."""
