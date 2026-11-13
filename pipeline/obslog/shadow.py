"""Best-effort local shadow receipt. These helpers cannot affect pipeline values."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scrape_outcomes import ScrapeOutcome

from .maintain import recover_connect
from .store import append_forecasts, append_odds


def connect(path):
    return recover_connect(path)


def recovery_degradation(path, ctx):
    notice = Path(path).with_suffix(".recovered")
    if notice.exists():
        ctx.degrade("obslog.cache", "corrupt cache moved aside; starting an empty log", "warn")
        notice.unlink()


def odds(path, ctx, sport, games, lines, books, results):
    try:
        receipts = [r for name, r in results.items() if name in books and r.book == name and r.sport == sport]
        successful = {(r.book, r.sport) for r in receipts if games and r.coverage_ok and r.attempted
                      and r.outcome in (ScrapeOutcome.OK, ScrapeOutcome.EMPTY)}
        attempted = {(r.book, r.sport) for r in receipts if r.attempted
                     and r.outcome not in (ScrapeOutcome.SKIPPED, ScrapeOutcome.NOT_REQUESTED)}
        with closing(connect(path)) as conn:
            recovery_degradation(path, ctx)
            append_odds(conn, lines, games=games, run_id=ctx.run_id, observed_at=datetime.now(timezone.utc),
                        started_at=getattr(ctx, "started_at", ctx.now_utc), attempted=attempted, successful=successful)
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("obslog.odds", f"local shadow append failed: {type(exc).__name__}", "warn")


def weather(path, ctx, games, forecasts):
    try:
        with closing(connect(path)) as conn:
            recovery_degradation(path, ctx)
            append_forecasts(conn, list(forecasts.values()), games=games, run_id=ctx.run_id,
                             fetched_at=datetime.now(timezone.utc), started_at=getattr(ctx, "started_at", ctx.now_utc))
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("obslog.weather", f"local shadow append failed: {type(exc).__name__}", "warn")
