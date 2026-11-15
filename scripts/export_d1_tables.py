"""Export D1 tables to ``{table}.json`` for ``pipeline.backtest.load_export_dir``.

    python scripts/export_d1_tables.py --out data/d1_export

One ``wrangler d1 execute --json`` ``SELECT *`` per table. Since 2026-11-04 D1
rejects a query result over 50 MB (``D1 query result exceeded the maximum
response size limit of 50 MB``). ``odds_history`` is past that limit; the failed
query is re-read in primary-key pages and concatenated into one ``[{results}]``
document. A missing table is a warning and leaves no file, matching the previous
workflow loop. A page that fails after the first page does not leave a partial
file: an incomplete odds history would invent the wrong opener.

The API token stays in the environment. Warnings are scrubbed before printing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.backtest import D1_TABLES  # noqa: E402
from pipeline.state import _export_rows  # noqa: E402

# Primary keys from site/worker/migrations. Column order is the page cursor.
TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "games": ("game_id",),
    "odds_history": ("game_id", "book", "market", "side", "scraped_at"),
    "closings": ("game_id", "book", "market", "side"),
    "alerts": ("alert_key",),
    "stadiums": ("stadium_id",),
    "teams": ("team_id",),
    "weather_history": ("game_id", "source", "fetched_at"),
    "runs": ("run_id",),
}
PAGE_SIZE = 5000
MIN_PAGE_SIZE = 200
MAX_PAGES = 400
QUERY_TIMEOUT_S = 120
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DB_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SIZE_RE = re.compile(r"maximum response size|50\s*MB", re.I)
_SECRET_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+|((?:api[_-]?token|password)[\"']?\s*[:=]\s*)\S+")

Execute = Callable[[str, str], tuple[int, str, str]]


class ConfigError(ValueError):
    """Database name or table list cannot be passed to wrangler safely."""


def _scrub(text: str) -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN") or ""
    if token:
        text = text.replace(token, "***")
    text = _SECRET_RE.sub(lambda m: (m.group(1) or "") + (m.group(2) or "") + "***", text)
    text = " ".join(text.split())
    if len(text) > 400:
        text = text[-400:]
    return text


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _check_ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ConfigError(f"refusing table name {name!r}")
    return name


def _page_sql(table: str, columns: Sequence[str], last: Mapping[str, object] | None, limit: int) -> str:
    table = _check_ident(table)
    cols = ", ".join(_check_ident(c) for c in columns)
    statement = f"SELECT * FROM {table} ORDER BY {cols} LIMIT {int(limit)}"
    if last is None:
        return statement
    literals = ", ".join(_sql_literal(last.get(c)) for c in columns)
    return f"SELECT * FROM {table} WHERE ({cols}) > ({literals}) ORDER BY {cols} LIMIT {int(limit)}"


def _rows_from_output(text: str) -> list[dict[str, object]]:
    return _export_rows(json.loads(text))


def _too_large(stdout: str, stderr: str) -> bool:
    return _SIZE_RE.search(stdout) is not None or _SIZE_RE.search(stderr) is not None


def _warn(table: str, detail: str) -> None:
    reason = _scrub(detail).strip()
    suffix = f": {reason}" if reason else ""
    print(f"::warning::d1 export {table} failed (fresh database?){suffix}", file=sys.stderr)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _drop(path: Path) -> None:
    if path.is_file():
        path.unlink()


def execute_sql(database: str, sql: str, *, timeout: float = QUERY_TIMEOUT_S) -> tuple[int, str, str]:
    """``wrangler d1 execute --json``. Token stays in the environment, never on argv."""
    cmd = [
        "npx", "--yes", "wrangler@4", "d1", "execute", database,
        "--remote", "--json", "--command", sql,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return 124, out, f"{err}\ntimeout"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _fetch_page(
    database: str, table: str, columns: Sequence[str], last: Mapping[str, object] | None,
    page_size: int, execute: Execute, *, min_page_size: int,
) -> tuple[list[dict[str, object]], int]:
    """Rows and the LIMIT that produced them. A 50 MB page is retried smaller."""
    size = page_size
    floor = max(1, min(min_page_size, page_size))
    while True:
        code, out, err = execute(database, _page_sql(table, columns, last, size))
        if code == 0:
            try:
                return _rows_from_output(out), size
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"page was not JSON ({exc})") from exc
        nxt = size // 2
        if _too_large(out, err) and nxt >= floor:
            size = nxt
            continue
        detail = err or out or f"exit {code}"
        raise RuntimeError(detail)


def _export_pages(
    database: str, table: str, columns: Sequence[str], execute: Execute, *,
    page_size: int, min_page_size: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    last: Mapping[str, object] | None = None
    pages = 0
    while pages < MAX_PAGES:
        batch, used = _fetch_page(
            database, table, columns, last, page_size, execute, min_page_size=min_page_size,
        )
        pages += 1
        rows.extend(batch)
        # ``used`` may be smaller than ``page_size`` after a 50 MB shrink.
        # A shortfall against that LIMIT is the end; a full shrunk page is not.
        if len(batch) < used:
            break
        cursor = batch[-1]
        if last is not None and all(cursor.get(c) == last.get(c) for c in columns):
            raise RuntimeError("page cursor did not advance")
        last = cursor
    else:
        raise RuntimeError(f"stopped after {MAX_PAGES} pages")
    return rows


def export_table(
    database: str, table: str, dest: Path, execute: Execute, *,
    page_size: int = PAGE_SIZE, min_page_size: int = MIN_PAGE_SIZE,
) -> str:
    """Write ``dest`` and return ``single`` or ``paged``. Warn and leave no file on failure."""
    table = _check_ident(table)
    code, out, err = execute(database, f"SELECT * FROM {table}")
    if code == 0:
        try:
            json.loads(out)
        except json.JSONDecodeError:
            _drop(dest)
            _warn(table, err or "response was not JSON")
            return "failed"
        _write(dest, out if out.endswith("\n") else out + "\n")
        print(f"{table}.json exported")
        return "single"
    if not _too_large(out, err):
        _drop(dest)
        _warn(table, err or out or f"exit {code}")
        return "failed"
    columns = TABLE_KEYS.get(table)
    if not columns:
        _drop(dest)
        _warn(table, "result exceeded 50 MB and this table has no page key")
        return "failed"
    try:
        rows = _export_pages(
            database, table, columns, execute, page_size=max(1, page_size), min_page_size=min_page_size,
        )
    except (RuntimeError, ConfigError) as exc:
        _drop(dest)
        _warn(table, str(exc))
        return "failed"
    if not rows:
        _drop(dest)
        _warn(table, "result exceeded 50 MB but paging returned no rows")
        return "failed"
    _write(dest, json.dumps([{"results": rows}], separators=(",", ":")) + "\n")
    print(f"{table}.json exported ({len(rows)} rows, paged)")
    return "paged"


def _tables_from_env() -> list[str]:
    raw = os.environ.get("D1_TABLES", "").split()
    return raw or list(D1_TABLES)


def export_tables(
    database: str, out_dir: Path, tables: Sequence[str], execute: Execute, *,
    page_size: int = PAGE_SIZE, min_page_size: int = MIN_PAGE_SIZE,
) -> dict[str, str]:
    if not _DB_NAME.fullmatch(database or ""):
        raise ConfigError("D1_DATABASE is missing or not a plain name")
    if not tables:
        raise ConfigError("no D1 tables to export")
    out_dir.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for table in tables:
        status[table] = export_table(
            database, table, out_dir / f"{table}.json", execute,
            page_size=page_size, min_page_size=min_page_size,
        )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export D1 tables for the backtest")
    parser.add_argument("--database", default=os.environ.get("D1_DATABASE") or "")
    parser.add_argument("--out", type=Path, default=Path("data/d1_export"))
    parser.add_argument("--tables", nargs="*", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        export_tables(args.database, args.out, args.tables or _tables_from_env(), execute_sql)
    except ConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
