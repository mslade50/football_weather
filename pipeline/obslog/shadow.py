"""Best-effort local shadow receipt. These helpers cannot affect pipeline values."""

from contextlib import closing
from datetime import datetime, timezone

from pipeline.scrape_outcomes import ScrapeOutcome

from .store import append_forecasts, append_odds, connect


def odds(path, ctx, sport, games, lines, books, results):
    try:
        successful = {(name, sport) for name in books if name in results
                      and games
                      and results[name].coverage_ok
                      and results[name].outcome in (ScrapeOutcome.OK, ScrapeOutcome.EMPTY)
                      and results[name].attempted
                      and not any(d.component == "odds.merge" and d.severity in ("warn", "error")
                                  for d in ctx.degradations)}
        with closing(connect(path)) as conn:
            append_odds(conn, lines, games=games, run_id=ctx.run_id, observed_at=datetime.now(timezone.utc),
                        started_at=getattr(ctx, "started_at", ctx.now_utc), attempted={(b, sport) for b in books}, successful=successful)
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("obslog.odds", f"local shadow append failed: {type(exc).__name__}", "warn")


def weather(path, ctx, games, forecasts):
    try:
        with closing(connect(path)) as conn:
            append_forecasts(conn, list(forecasts.values()), games=games, run_id=ctx.run_id,
                             fetched_at=datetime.now(timezone.utc), started_at=getattr(ctx, "started_at", ctx.now_utc))
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("obslog.weather", f"local shadow append failed: {type(exc).__name__}", "warn")
