"""Local workflow adapter for retrying existing D1 SQL in existing state.

No network or database operations. The existing archive_last.json upload carries
unacknowledged SQL; the existing D1 step replays it before newly generated SQL.
This adapter is independent of the observation log.

The queue is bounded. A statement D1 permanently rejects would otherwise make
every later run fail forever and grow the state object without limit. When the
bound is hit the OLDEST chunks are dropped (never the fresh one) with a loud
Actions warning: those deltas are lost, which is cheaper than a permanent outage.
"""

import argparse
import json
from pathlib import Path

PENDING = "pending_d1_sql"
# One chunk is one run's attempted SQL. 48 hourly runs is about two days of outage.
MAX_PENDING_CHUNKS = 48
MAX_PENDING_BYTES = 1024 * 1024
# A SQL line comment, so replaying the combined file is unaffected by it.
SEPARATOR = "\n-- d1-pending-chunk\n"


def _chunks(text):
    return [c for c in text.split(SEPARATOR) if c.strip()]


def bound(pending, fresh, *, max_chunks=MAX_PENDING_CHUNKS, max_bytes=MAX_PENDING_BYTES):
    """Return ``(combined, dropped_chunks)``; drops oldest chunks, keeps the fresh one."""
    chunks = _chunks(pending)
    newest = [fresh] if fresh.strip() else []
    dropped = 0
    while chunks and (len(chunks) + len(newest) > max_chunks
                      or sum(len(c.encode("utf-8")) for c in chunks + newest) > max_bytes):
        chunks.pop(0)
        dropped += 1
    return SEPARATOR.join(chunks + newest), dropped


def prepare(state_dir, sql_path):
    state = Path(state_dir) / "archive_last.json"
    data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    pending = data.get(PENDING, "")
    if not isinstance(pending, str):
        raise ValueError("invalid pending D1 SQL; refusing to discard it")
    sql_path = Path(sql_path)
    fresh = sql_path.read_text(encoding="utf-8") if sql_path.exists() else ""
    combined, dropped = bound(pending, fresh)
    if dropped:
        print(f"::warning::Pending D1 SQL exceeded its bound; dropped the {dropped} oldest "
              f"chunk(s) permanently (those odds/weather deltas are not archived)")
    if combined.strip():
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path.write_text(combined, encoding="utf-8")
    # Before the only upload of existing state, persist exactly the attempted SQL.
    # Success can clear it; interruption/partial D1 application remains replayable.
    if combined.strip():
        data[PENDING] = combined
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")


def acknowledge(state_dir):
    path = Path(state_dir) / "archive_last.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if PENDING in data:
        del data[PENDING]
        path.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "acknowledge"))
    parser.add_argument("--state-dir", default="data/state")
    parser.add_argument("--sql", default="data/d1_inserts.sql")
    args = parser.parse_args(argv)
    if args.action == "prepare":
        prepare(args.state_dir, args.sql)
    else:
        acknowledge(args.state_dir)


if __name__ == "__main__":
    main()
