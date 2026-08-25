"""CLI entry point for on-demand football odds scraping (adapted from golf_scraping/main.py).

    python main.py --book betcris --sport cfb
    python main.py --book all --sport nfl --market total --output csv

Scrapers register themselves in ``SCRAPERS`` as they are implemented (Phase 2).
Books whose module is missing or whose optional dependency (Playwright,
curl_cffi) is not installed are skipped with a warning, so ``--help`` and the
remaining books work in any environment.
"""

import argparse
import asyncio
import importlib
import importlib.util
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from pipeline.odds.base import BaseScraper, GameLine
from utils.env import load_repo_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SPORTS = ("nfl", "cfb")
MARKETS = ("ml", "spread", "total")

# book -> (module, class). Order = display order.
BOOK_REGISTRY: dict[str, tuple[str, str]] = {
    "betcris": ("pipeline.odds.betcris", "BetcrisScraper"),
    "fanduel": ("pipeline.odds.fanduel", "FanDuelScraper"),
    "kalshi": ("pipeline.odds.kalshi", "KalshiScraper"),
    "novig": ("pipeline.odds.novig", "NovigScraper"),
    "pinnacle": ("pipeline.odds.pinnacle", "PinnacleScraper"),
    "prophetx": ("pipeline.odds.prophetx", "ProphetXScraper"),
    "betonline": ("pipeline.odds.betonline", "BetOnlineScraper"),
    "draftkings": ("pipeline.odds.draftkings", "DraftKingsScraper"),
}

OUTPUT_DIR = Path("output")


def load_scraper(name: str) -> Optional[type]:
    module_name, cls_name = BOOK_REGISTRY[name]
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        logger.warning(f"[{name}] unavailable: {e}")
        return None
    cls = getattr(module, cls_name, None)
    if cls is None:
        logger.warning(f"[{name}] {module_name} has no {cls_name}")
    return cls


async def run_scraper(name: str, scraper_cls: type, sport: str, market: Optional[str], headless: bool) -> list[GameLine]:
    """Run a single scraper with retry logic."""
    try:
        scraper: BaseScraper = scraper_cls(headless=headless)
    except TypeError:
        scraper = scraper_cls()
    return await scraper.scrape_with_retry(sport, market=market)


def lines_to_df(lines: list[GameLine]) -> pd.DataFrame:
    return BaseScraper.to_dataframe(lines)


def pivot_books(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, market, side) with each book's line/odds side by side."""
    if df.empty:
        return df
    main = df[df["is_main"]] if "is_main" in df.columns else df
    wide = main.pivot_table(
        index=["sport", "game_id", "market", "side"],
        columns="book",
        values=["line", "odds"],
        aggfunc="first",
    )
    wide.columns = [f"{book}_{val}" for val, book in wide.columns]
    return wide.reset_index().sort_values(["game_id", "market", "side"])


def print_lines(df: pd.DataFrame) -> None:
    if df.empty:
        print("(no lines)")
        return
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 200):
        print(df.to_string(index=False))


def save_csv(df: pd.DataFrame, sport: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"{sport}_lines_{stamp}.csv"
    df.to_csv(path, index=False)
    return path


async def main(books: list[str], sport: str, market: Optional[str], output: str, headless: bool) -> None:
    """Run selected scrapers in parallel and output results."""
    tasks = []
    names = []
    for book in books:
        if book not in BOOK_REGISTRY:
            logger.warning(f"Unknown book: {book}, skipping")
            continue
        cls = load_scraper(book)
        if cls is None:
            continue
        names.append(book)
        tasks.append(run_scraper(book, cls, sport, market, headless))

    if not tasks:
        logger.warning("No scrapers available for the requested books.")
        sys.exit(0)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_lines: list[GameLine] = []
    for book, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            logger.error(f"[{book}] Failed: {result}")
        else:
            logger.info(f"[{book}] {len(result)} lines")
            all_lines.extend(result)

    if not all_lines:
        logger.warning("No lines found from any book.")
        sys.exit(0)

    logger.info(f"Total lines collected: {len(all_lines)} across {len(set(ln.book for ln in all_lines))} book(s)")

    df = lines_to_df(all_lines)
    view = pivot_books(df) if df["book"].nunique() > 1 else df

    if output in ("console", "both"):
        print_lines(view)
    if output in ("csv", "both"):
        path = save_csv(df, sport)
        logger.info(f"Saved to {path}")


def cli() -> None:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="Football (NFL/CFB) odds scraper")
    parser.add_argument(
        "--book",
        choices=list(BOOK_REGISTRY.keys()) + ["all"],
        default="all",
        help="Which sportsbook to scrape (default: all)",
    )
    parser.add_argument(
        "--sport",
        choices=list(SPORTS),
        required=True,
        help="League to scrape: nfl or cfb",
    )
    parser.add_argument(
        "--market",
        choices=list(MARKETS),
        default=None,
        help="Filter to a market (default: all)",
    )
    parser.add_argument(
        "--output",
        choices=["console", "csv", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (visible) for debugging Playwright books",
    )
    args = parser.parse_args()

    if args.book == "all":
        # Optional books (draftkings) whose module is not implemented are skipped silently.
        books = [b for b, (mod, _) in BOOK_REGISTRY.items() if importlib.util.find_spec(mod) is not None]
    else:
        books = [args.book]
    asyncio.run(main(books, args.sport, args.market, args.output, not args.headed))


if __name__ == "__main__":
    cli()
