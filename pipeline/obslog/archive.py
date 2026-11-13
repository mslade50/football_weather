"""Local workflow adapter for retrying existing D1 SQL in existing state.

No network or database operations. The existing archive_last.json upload carries
unacknowledged SQL; the existing D1 step replays it before newly generated SQL.
This adapter is independent of whether observation logging is enabled.
"""

import argparse
import json
from pathlib import Path

PENDING = "pending_d1_sql"


def prepare(state_dir, sql_path):
    state = Path(state_dir) / "archive_last.json"
    data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    pending = data.get(PENDING, "")
    if not isinstance(pending, str):
        raise ValueError("invalid pending D1 SQL; refusing to discard it")
    sql_path = Path(sql_path)
    fresh = sql_path.read_text(encoding="utf-8") if sql_path.exists() else ""
    combined = pending + ("\n" if pending else "") + fresh
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
