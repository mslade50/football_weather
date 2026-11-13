"""Typed scrape evidence; human diagnostics are never a control protocol."""

from dataclasses import dataclass
from enum import Enum


class ScrapeOutcome(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    UPSTREAM_STALE = "upstream_stale"
    FETCH_ERROR = "fetch_error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class ScrapeResult:
    book: str
    sport: str
    outcome: ScrapeOutcome
    row_count: int
    detail: str = ""
    stale_age_seconds: float | None = None
    coverage_ok: bool = True
    attempted: bool = True
    health_detail: str | None = None  # Preserve the historical health summary for outer failures.


def scraper_result(book, sport, rows, scraper, *, outcome=None, coverage_ok=True, failure_detail=None):
    """Adapt scraper diagnostics once, at the odds boundary (including partial rows)."""
    errors = getattr(scraper, "fetch_errors", None) or {}
    detail = "; ".join(f"{k}: {v}" if k != sport else str(v) for k, v in errors.items())
    stale = (book == "betcris" and getattr(scraper, "upstream_stale", False))
    code = outcome or (ScrapeOutcome.UPSTREAM_STALE if stale else
                       ScrapeOutcome.FETCH_ERROR if errors else
                       ScrapeOutcome.OK if rows else ScrapeOutcome.EMPTY)
    return ScrapeResult(book, sport, code, rows, failure_detail or detail,
                        getattr(scraper, "stale_age_seconds", None) if stale else None,
                        coverage_ok and not errors and code in (ScrapeOutcome.OK, ScrapeOutcome.EMPTY),
                        scraper is not None, detail)
