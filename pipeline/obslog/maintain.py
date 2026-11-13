"""Best-effort upkeep of disposable Actions-cache evidence, never cloud state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import UnsupportedSchema, connect, instant, utc

KEEP_DAYS = 2
MAX_MIB = 192


def warning(message):
    print(f"::warning::Observation log: {message}")


def recovery_notice(path, message):
    # Local, uncached notice lets prepare communicate recovery to the build's
    # warn degradation without introducing another remote persistence path.
    Path(path).with_suffix(".recovered").write_text(message, encoding="utf-8")
    warning(message)


def quarantine(path):
    """Keep corrupt bytes beside the DB, outside the cache's exact-file path."""
    path = Path(path)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = path.with_name(f"{path.stem}.corrupt-{suffix}.sqlite")
    for extra in ("", "-journal", "-wal", "-shm"):
        source = Path(str(path) + extra)
        if source.exists():
            source.rename(Path(str(target) + extra))
    return target


def recover_connect(path, warn=None, *, check=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = connect(path)
        if check and conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("integrity check failed")
        return conn
    except (sqlite3.DatabaseError, UnsupportedSchema):
        # A corrupt file and a newer/unknown schema are both unusable here; the
        # cache is disposable, so keep the bytes beside it and start empty.
        if conn is not None:
            conn.close()
        quarantine(path)
        message = "corrupt cache moved aside; starting an empty log"
        if warn is None:
            recovery_notice(path, message)
        else:
            warn(message)
        return connect(path)


def maintain(path, *, keep_days=KEEP_DAYS, as_of=None, max_mib=MAX_MIB, prepare=False):
    if keep_days < 1 or max_mib <= 0:
        raise ValueError("positive retention and size budget required")
    now = as_of or datetime.now(timezone.utc)
    cutoff = utc(now - timedelta(days=keep_days))
    counts = {}
    with closing(recover_connect(path, check=True)) as conn:
        if prepare:
            return {"prepared": True}
        # Latest schedule fact wins: an old postponed kickoff must not prune a live game.
        conn.execute("CREATE TEMP TABLE retired (game_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO retired SELECT game_id FROM (SELECT game_id, kickoff, "
                     "row_number() OVER (PARTITION BY game_id ORDER BY observed_at DESC, id DESC) AS n "
                     "FROM game_observations) WHERE n=1 AND kickoff<?", (cutoff,))
        with conn:
            for table, clock in (("quote_observations", "recorded_at"), ("forecast_observations", "fetched_at"),
                                 ("game_observations", "observed_at")):
                counts[table] = conn.execute(
                    f"DELETE FROM {table} WHERE game_id IN (SELECT game_id FROM retired) AND {clock}<?"
                    + (" AND observed_at<?" if table == "quote_observations" else ""),
                    (cutoff, cutoff) if table == "quote_observations" else (cutoff,)).rowcount
            counts["runs"] = conn.execute(
                "DELETE FROM runs WHERE finished_at<? AND run_id NOT IN ("
                "SELECT run_id FROM quote_observations UNION SELECT run_id FROM forecast_observations "
                "UNION SELECT run_id FROM game_observations)", (cutoff,)).rowcount
        conn.execute("VACUUM")
    size = Path(path).stat().st_size
    counts["bytes_before_budget"] = size
    # Odds can be posted weeks ahead: time retention alone cannot bound live games.
    # Cache eviction is permitted. Rotate the whole disposable cache rather than
    # silently truncate active-game history and pretend it is complete evidence.
    if size > max_mib * 1024 * 1024:
        warning(f"cache exceeded {max_mib} MiB; evicting evidence after parity reporting")
        Path(path).unlink()
        with closing(connect(path)):
            pass
        counts["budget_evicted"] = True
    counts["bytes"] = Path(path).stat().st_size
    return counts


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--path", required=True)
        parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
        parser.add_argument("--max-mib", type=float, default=MAX_MIB)
        parser.add_argument("--as-of")
        parser.add_argument("--prepare", action="store_true")
        args = parser.parse_args(argv)
        print(json.dumps(maintain(args.path, keep_days=args.keep_days, max_mib=args.max_mib,
                                  as_of=instant(args.as_of) if args.as_of else None, prepare=args.prepare)))
    except (Exception, SystemExit) as exc:
        warning(f"maintenance unavailable ({type(exc).__name__}); production continues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
