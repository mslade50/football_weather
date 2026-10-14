"""Payload-shape fingerprint diff + per-book null-rate checks (odds pipeline).

``check_scrape_volume`` only sees row-count drops; a renamed upstream field that
still yields rows slips through. These helpers are pure (the only I/O is reading
prior ``manifest.json`` files) and the caller (``build.stage_odds``) wraps every
use in try/except so the checks are fail-open.

* Shape: each raw capture's manifest entry carries ``shape`` (sorted
  ``path:type`` strings, see ``raw_out.payload_shape``). Per book, the current
  union is diffed against the union of the last ``N`` runs that captured that
  book. Removed paths / changed types -> alert issues; added paths -> info only.
* Nulls: per-book null rate on GameLine ``odds`` (price), ``line`` (spread/total
  only) and ``side``. Alert above an absolute threshold, or on a jump vs the
  median of recent runs.

History comes from (a) on-disk manifests under the raw store base dir and, when
a book has none there, (b) a copy persisted in the R2-round-tripped
``scrape_baseline.json`` (CI's ``data/raw_runs`` is fresh every run, so (a) alone
would never have history there).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from statistics import median
from typing import Any

SHAPE_HISTORY_RUNS = 5          # default N (env PAYLOAD_SHAPE_HISTORY_RUNS)
NULL_RATE_MAX = 0.25            # absolute alert threshold (env NULL_RATE_MAX); conservative
NULL_RATE_JUMP = 0.15           # alert when rate - median(history) exceeds this (env NULL_RATE_JUMP) ...
NULL_RATE_JUMP_FLOOR = 0.05     # ... and the current rate is at least this
NULL_RATE_MIN_LINES = 20        # fewer rows than this: too small to judge
NULL_FIELDS = ("odds", "line", "side")


def _env(name: str, default: Any, cast: Any) -> Any:
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError):
        return default


def history_runs() -> int:
    return max(1, _env("PAYLOAD_SHAPE_HISTORY_RUNS", SHAPE_HISTORY_RUNS, int))


def null_rate_max() -> float:
    return _env("NULL_RATE_MAX", NULL_RATE_MAX, float)


def null_rate_jump() -> float:
    return _env("NULL_RATE_JUMP", NULL_RATE_JUMP, float)


# ---- shape ---------------------------------------------------------------------------------

def book_for_source(source: str, books: Sequence[str]) -> str | None:
    """Manifest source names look like ``pinnacle_nfl`` / ``cfb_fanduel`` /
    ``betcris_public_cfb``: attribute to the requested book appearing as a ``_`` token."""
    tokens = set(source.lower().split("_"))
    hits = [b for b in books if b.lower() in tokens]
    return max(hits, key=len) if hits else None


def shapes_by_book(manifest: dict[str, Any], books: Sequence[str]) -> dict[str, set[str]]:
    """``{book: union of shape entries}`` for capture entries that carry a shape."""
    out: dict[str, set[str]] = {}
    for source, entry in (manifest or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("shape"), list):
            continue
        book = book_for_source(str(source), books)
        if book is None:
            continue
        out.setdefault(book, set()).update(str(x) for x in entry["shape"])
    return out


def load_manifest_history(base_dir: Path, sport: str, current_run_id: str,
                          books: Sequence[str], n: int) -> dict[str, list[set[str]]]:
    """Per book, shapes of the last ``n`` prior runs (newest first) that captured it."""
    root = Path(base_dir) / sport
    runs: list[tuple[float, Path]] = []
    try:
        for d in root.iterdir():
            m = d / "manifest.json"
            if d.name != current_run_id and m.is_file():
                runs.append((m.stat().st_mtime, m))
    except OSError:
        return {}
    out: dict[str, list[set[str]]] = {}
    for _, m in sorted(runs, key=lambda t: t[0], reverse=True):
        try:
            per_book = shapes_by_book(json.loads(m.read_text(encoding="utf-8")), books)
        except (OSError, ValueError):
            continue   # unreadable / corrupt history is skipped, never fatal
        for book, shape in per_book.items():
            if len(out.setdefault(book, [])) < n:
                out[book].append(shape)
    return out


def merge_history(primary: dict[str, list[set[str]]], fallback: dict[str, list[set[str]]],
                  n: int) -> dict[str, list[set[str]]]:
    """Per book: the on-disk manifests when present, else the persisted copy."""
    out: dict[str, list[set[str]]] = {}
    for book in set(primary) | set(fallback):
        out[book] = (primary.get(book) or fallback.get(book) or [])[:n]
    return out


def _types_by_path(entries: Iterable[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in entries:
        path, _, typ = e.rpartition(":")
        out.setdefault(path, set()).add(typ)
    return out


def diff_shape(current: set[str], history: Iterable[set[str]]) -> dict[str, list[str]]:
    """``{"removed", "changed", "added"}`` human strings, sorted.

    removed: path seen in history, absent now. changed: the non-null types seen before
    and now are disjoint (or the field went all-null). Adding a type beside a known
    one, or gaining/losing ``null``, is not a break. added: new paths (log only)."""
    union: set[str] = set()
    for h in history:
        union |= h
    old, new = _types_by_path(union), _types_by_path(current)
    removed = [p for p in sorted(old) if p not in new]
    changed = []
    for p in sorted(set(old) & set(new)):
        o, n = old[p] - {"null"}, new[p] - {"null"}
        if o and (not n or o.isdisjoint(n)):
            changed.append(f"{p}: {'|'.join(sorted(o))} -> {'|'.join(sorted(n)) or 'null'}")
    added = [p for p in sorted(new) if p not in old]
    return {"removed": removed, "changed": changed, "added": added}


def _clip(items: list[str], k: int = 5) -> str:
    more = f" (+{len(items) - k} more)" if len(items) > k else ""
    return "; ".join(items[:k]) + more


def shape_issues(current: dict[str, set[str]], history: dict[str, list[set[str]]],
                 skip_books: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """``(alerts, infos)`` across books. Books with no history, or in ``skip_books``
    (returned 0 lines: already degraded, payload legitimately empty), are skipped."""
    alerts: list[str] = []
    infos: list[str] = []
    skip = set(skip_books)
    for book in sorted(current):
        hist = history.get(book) or []
        if not hist or book in skip:
            continue
        d = diff_shape(current[book], hist)
        parts = []
        if d["removed"]:
            parts.append("removed " + _clip(d["removed"]))
        if d["changed"]:
            parts.append("type changed " + _clip(d["changed"]))
        if parts:
            alerts.append(f"{book}: payload shape changed - " + " | ".join(parts))
        if d["added"]:
            infos.append(f"{book}: {len(d['added'])} new payload key-path(s), e.g. {_clip(d['added'], 3)}")
    return alerts, infos


# ---- null rates ----------------------------------------------------------------------------

def _is_null(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and not v.strip()


def null_rates(lines: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """``{book: {"n": rows, "odds": r, "line": r, "side": r}}``; ``line`` is judged on
    spread/total rows only (moneyline has no line by contract)."""
    acc: dict[str, dict[str, list[int]]] = {}
    for ln in lines:
        a = acc.setdefault(getattr(ln, "book", None) or "?",
                           {"n": [0, 0], "odds": [0, 0], "line": [0, 0], "side": [0, 0]})
        a["n"][0] += 1
        a["odds"][0] += int(_is_null(getattr(ln, "odds", None)))
        a["odds"][1] += 1
        a["side"][0] += int(_is_null(getattr(ln, "side", None)))
        a["side"][1] += 1
        if getattr(ln, "market", None) != "ml":
            a["line"][0] += int(_is_null(getattr(ln, "line", None)))
            a["line"][1] += 1
    out: dict[str, dict[str, Any]] = {}
    for book, a in acc.items():
        row: dict[str, Any] = {"n": a["n"][0]}
        for f in NULL_FIELDS:
            nulls, tot = a[f]
            row[f] = round(nulls / tot, 4) if tot else 0.0
        out[book] = row
    return out


def null_issues(rates: dict[str, dict[str, Any]], history: dict[str, list[dict[str, Any]]],
                max_rate: float | None = None, jump: float | None = None,
                min_lines: int = NULL_RATE_MIN_LINES) -> list[str]:
    max_rate = null_rate_max() if max_rate is None else max_rate
    jump = null_rate_jump() if jump is None else jump
    out: list[str] = []
    for book in sorted(rates):
        row = rates[book]
        if int(row.get("n", 0)) < min_lines:
            continue
        for f in NULL_FIELDS:
            cur = float(row.get(f, 0.0))
            past = [float(h[f]) for h in history.get(book, []) if isinstance(h, dict) and f in h]
            if cur > max_rate:
                out.append(f"{book}: {f} null rate {cur:.0%} exceeds {max_rate:.0%} ({row['n']} rows)")
            elif past and cur >= NULL_RATE_JUMP_FLOOR and cur - median(past) > jump:
                out.append(f"{book}: {f} null rate jumped to {cur:.0%} (usual ~{median(past):.0%}, {row['n']} rows)")
    return out


# ---- persisted history (lives in scrape_baseline.json; extra keys survive state.migrate) ---

def record_null_history(state: dict, sport: str, rates: dict[str, dict[str, Any]], n: int) -> None:
    hist = state.setdefault("null_rate_history", {}).setdefault(sport, {})
    for book, row in rates.items():
        if int(row.get("n", 0)) >= NULL_RATE_MIN_LINES:
            hist[book] = ([{f: row[f] for f in NULL_FIELDS}] + list(hist.get(book, [])))[:n]


def record_shape_history(state: dict, sport: str, current: dict[str, set[str]], n: int) -> None:
    hist = state.setdefault("shape_history", {}).setdefault(sport, {})
    for book, shape in current.items():
        hist[book] = ([sorted(shape)] + list(hist.get(book, [])))[:n]


def persisted_shape_history(state: dict, sport: str) -> dict[str, list[set[str]]]:
    raw = (state.get("shape_history") or {}).get(sport) or {}
    return {b: [set(map(str, s)) for s in v if isinstance(s, list)]
            for b, v in raw.items() if isinstance(v, list)}


def persisted_null_history(state: dict, sport: str) -> dict[str, list[dict[str, Any]]]:
    raw = (state.get("null_rate_history") or {}).get(sport) or {}
    return {b: [h for h in v if isinstance(h, dict)] for b, v in raw.items() if isinstance(v, list)}
