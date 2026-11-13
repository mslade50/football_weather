"""Best-effort local shadow receipt. These helpers cannot affect pipeline values."""

from contextlib import closing
from datetime import datetime, timezone

from .store import append_forecasts, append_odds, connect


def odds(path, ctx, sport, games, lines, books, scrapers):
    try:
        # gather() failures can leave a scraper without fetch_errors. Degradation
        # evidence must also veto coverage. Unknown/missing scraper is not success.
        successful = {(name, sport) for name in books if name in scrapers
                      and games
                      and not getattr(scrapers[name], "fetch_errors", None)
                      and not any((d.component == "odds.merge" and d.severity in ("warn", "error"))
                                  or (name in d.reason and ("failed" in d.reason or "timed out" in d.reason
                                                          or d.component == "odds.ingest"))
                                  for d in ctx.degradations if d.component.startswith("odds"))}
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
