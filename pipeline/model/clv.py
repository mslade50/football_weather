"""Closing-line freeze + CLV (ARCHITECTURE §7.3 "CLV", §4.3 ``closings``).

* **Freeze**: the first run after kickoff freezes, per odds key
  ``game_id|market|side|book``, the last ``odds_history`` row scraped *before*
  ``kickoff_utc``. Sources: D1 ``odds_history`` rows (dicts) or the R2
  ``history.json`` change-only series (``series[key] = [[ts, line, odds], ...]``).
  Frozen closings never change (``INSERT OR IGNORE`` in D1, first-write-wins in
  ``closings.json``).
* **clv_pts**: signed points in the alerted side's favour. Lines are stored
  side-relative (``GameLine.line`` / ``Edge.line`` / ``alerts.first_line``: the
  away spread is the away team's number, ``+3`` when home is ``-3``). Under, home
  and away all want a *bigger* number, over wants a *smaller* one::

      under/home/away: clv = first_line - closing_line
      over:            clv = closing_line - first_line

  ``edge_pts`` in ``model/fair.py`` is the same idea with ``fair`` in place of the
  closing number, but on home-relative spreads; ``clv_pts(..., home_relative=True)``
  accepts that convention too.
* **Legacy CLV status** (backtest grid): ``'Positive'`` when the total moved down
  from open to now (``open > now``), else ``'Negative'`` — verbatim from
  ``pages/cfb_weather.py`` (ties are Negative; only the under side was ever graded).
* **alerts update**: every EDGE record whose game has a frozen closing gets
  ``closing_line`` / ``clv_pts`` and ``status='settled'`` — the D1 ``alerts`` mirror
  and ``alerts_feed.json`` pick those up through the existing upsert / feed join.

State file ``closings.json`` (R2 ``board/closings.json`` — already in
``r2.STATE_FILES``)::

    {"schema_version": 1, "run_id": ..., "closings": {key: {line, odds, scraped_at, kickoff_utc, frozen_at}}}
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from pipeline import state as pstate
from utils.timeutil import ensure_utc, parse_iso, utc_iso

PathLike = Union[str, Path]

CLOSINGS_FILE = "closings.json"
CLOSING_COLS = ("game_id", "book", "market", "side", "line", "odds", "scraped_at", "kickoff_utc", "frozen_at")
SETTLED = "settled"
POSITIVE = "Positive"
NEGATIVE = "Negative"


@dataclass(frozen=True)
class Closing:
    game_id: str
    book: str
    market: str
    side: str
    line: Optional[float]
    odds: Optional[int]
    scraped_at: str
    kickoff_utc: str
    frozen_at: str

    @property
    def key(self) -> str:
        return pstate.odds_key(self.game_id, self.market, self.side, self.book)

    def to_row(self) -> dict[str, Any]:
        return {c: getattr(self, c) for c in CLOSING_COLS}


# ---- helpers ------------------------------------------------------------------------

def _dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return ensure_utc(v)
    if isinstance(v, str) and v.strip():
        try:
            return ensure_utc(parse_iso(v))
        except ValueError:
            return None
    return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def kickoffs_from_cards(cards: Iterable[Mapping[str, Any]]) -> dict[str, datetime]:
    """``{game_id: kickoff_utc}`` from GameCards / Game dicts / D1 ``games`` rows."""
    out: dict[str, datetime] = {}
    for c in cards:
        gid = c.get("game_id")
        k = _dt(c.get("kickoff_utc"))
        if gid and k is not None:
            out[str(gid)] = k
    return out


# ---- sign conventions ------------------------------------------------------------------

def clv_pts(market: str, side: str, first_line: Any, closing_line: Any, *, home_relative: bool = False) -> Optional[float]:
    """Signed CLV in ``side``'s favour (positive = the alerted number beat the close).

    ``home_relative=True`` means both spread numbers are the home team's number
    regardless of ``side`` (the ``fair.py`` convention); default is side-relative.
    Moneylines have no line → None."""
    a, b = _num(first_line), _num(closing_line)
    if a is None or b is None or market not in ("total", "spread"):
        return None
    if market == "total":
        return (b - a) if side == "over" else (a - b)
    if side == "home":
        return a - b
    if side == "away":
        return (b - a) if home_relative else (a - b)
    return None


def clv_status(open_total: Any, now_total: Any) -> Optional[str]:
    """Legacy 'CLV from Open' label for the under: Positive when the total came
    down (``open > now``), Negative otherwise (ties included)."""
    o, n = _num(open_total), _num(now_total)
    if o is None or n is None:
        return None
    return POSITIVE if o > n else NEGATIVE


# ---- freeze ---------------------------------------------------------------------------

def freeze_from_rows(
    rows: Iterable[Mapping[str, Any]],
    kickoffs: Mapping[str, datetime],
    now: datetime,
) -> dict[str, Closing]:
    """Closing per key from D1 ``odds_history``-shaped rows (``scraped_at, game_id,
    book, market, side, line, odds``): the LAST row scraped strictly before the
    game's kickoff, only for games that have kicked off by ``now``."""
    now = ensure_utc(now)
    best: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    for r in rows:
        gid = str(r.get("game_id") or "")
        kick = kickoffs.get(gid)
        ts = _dt(r.get("scraped_at"))
        if kick is None or ts is None or kick > now or ts >= kick:
            continue
        key = pstate.odds_key(gid, str(r.get("market") or ""), str(r.get("side") or ""), str(r.get("book") or ""))
        cur = best.get(key)
        if cur is None or ts >= cur[0]:
            best[key] = (ts, r)
    frozen = utc_iso(now)
    out: dict[str, Closing] = {}
    for key, (ts, r) in best.items():
        gid, market, side, book = key.split("|")
        odds = r.get("odds")
        out[key] = Closing(gid, book, market, side, _num(r.get("line")),
                           int(odds) if isinstance(odds, (int, float)) and odds == odds else None,
                           utc_iso(ts), utc_iso(kickoffs[gid]), frozen)
    return out


def freeze_from_series(history: Mapping[str, Any], kickoffs: Mapping[str, datetime], now: datetime) -> dict[str, Closing]:
    """Same freeze from the change-only ``history.json`` series (``[[ts, line, odds], ...]``).
    A series point at ``ts`` is the price from ``ts`` until the next point, so the
    last point before kickoff is the closing even when it was recorded days earlier."""
    rows: list[dict[str, Any]] = []
    for key, seq in (history.get("series") or {}).items():
        parts = str(key).split("|")
        if len(parts) != 4 or not isinstance(seq, list):
            continue
        gid, market, side, book = parts
        for pt in seq:
            if isinstance(pt, list) and len(pt) >= 3:
                rows.append({"scraped_at": pt[0], "game_id": gid, "market": market, "side": side, "book": book,
                             "line": pt[1], "odds": pt[2]})
    return freeze_from_rows(rows, kickoffs, now)


# ---- closings state ---------------------------------------------------------------------

def _closings_default() -> dict[str, Any]:
    return {"schema_version": pstate.SCHEMA_VERSION, "closings": {}}


def load_closings(state_dir: PathLike) -> dict[str, Any]:
    d = pstate._load(Path(state_dir) / CLOSINGS_FILE)
    if not d:
        return _closings_default()
    try:
        version = int(d.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version > pstate.SCHEMA_VERSION:
        raise pstate.StateSchemaError(f"closings state has schema_version {version} > supported {pstate.SCHEMA_VERSION}")
    if not isinstance(d.get("closings"), dict):
        d["closings"] = {}
    d["schema_version"] = pstate.SCHEMA_VERSION
    return d


def save_closings(state_dir: PathLike, closings: dict[str, Any], run_id: Optional[str] = None) -> None:
    if run_id:
        closings["run_id"] = run_id
    pstate._save(Path(state_dir) / CLOSINGS_FILE, closings)


def record_closings(store: dict[str, Any], frozen: Mapping[str, Closing]) -> list[Closing]:
    """First-write-wins merge into ``closings.json``. Returns the NEW closings
    (the ones that need a D1 ``INSERT OR IGNORE``)."""
    bucket = store.setdefault("closings", {})
    new: list[Closing] = []
    for key, c in frozen.items():
        if key in bucket:
            continue
        bucket[key] = {"line": c.line, "odds": c.odds, "scraped_at": c.scraped_at,
                       "kickoff_utc": c.kickoff_utc, "frozen_at": c.frozen_at}
        new.append(c)
    return new


def get_closing(store: Mapping[str, Any], key: str) -> Optional[dict[str, Any]]:
    v = (store.get("closings") or {}).get(key)
    return v if isinstance(v, dict) else None


def prune_closings(store: dict[str, Any], keep_game_ids: Iterable[str]) -> int:
    keep = set(keep_game_ids)
    bucket = store.setdefault("closings", {})
    stale = [k for k in bucket if k.split("|", 1)[0] not in keep]
    for k in stale:
        del bucket[k]
    return len(stale)


def closing_rows(closings: Iterable[Closing] | Mapping[str, Any]) -> list[dict[str, Any]]:
    """D1 ``closings`` rows from Closing objects or the ``closings.json`` store."""
    if isinstance(closings, Mapping):
        rows = []
        for key, v in (closings.get("closings") or {}).items():
            parts = str(key).split("|")
            if len(parts) != 4 or not isinstance(v, dict):
                continue
            gid, market, side, book = parts
            rows.append({"game_id": gid, "book": book, "market": market, "side": side, "line": v.get("line"),
                         "odds": v.get("odds"), "scraped_at": v.get("scraped_at"), "kickoff_utc": v.get("kickoff_utc"),
                         "frozen_at": v.get("frozen_at")})
        return rows
    return [c.to_row() for c in closings]


# ---- alerts update ----------------------------------------------------------------------

def settle_alerts(alerts: dict[str, Any], closings: Mapping[str, Any], now: Optional[str] = None) -> list[dict[str, Any]]:
    """Stamp ``closing_line`` / ``clv_pts`` / ``status='settled'`` on every EDGE
    record whose key has a frozen closing. Records already settled are skipped.
    Returns the records touched (for the D1 alerts upsert)."""
    touched: list[dict[str, Any]] = []
    store = closings.get("closings") if "closings" in closings else closings
    for rec in (alerts.get("records") or {}).values():
        if not isinstance(rec, dict) or rec.get("family") != "edge" or rec.get("status") == SETTLED:
            continue
        key = pstate.odds_key(str(rec.get("game_id") or ""), str(rec.get("market") or ""),
                              str(rec.get("side") or ""), str(rec.get("book") or ""))
        c = (store or {}).get(key)
        if isinstance(c, Closing):
            c = {"line": c.line}
        if not isinstance(c, dict):
            continue
        rec["closing_line"] = _num(c.get("line"))
        rec["clv_pts"] = clv_pts(str(rec.get("market")), str(rec.get("side")), rec.get("first_line"), rec.get("closing_line"))
        rec["status"] = SETTLED
        if now:
            rec["settled_at"] = now
        touched.append(rec)
    for it in alerts.get("feed") or []:
        if isinstance(it, dict):
            rec = (alerts.get("records") or {}).get(it.get("alert_key"))
            if isinstance(rec, dict) and rec.get("clv_pts") is not None:
                it["clv_pts"] = rec["clv_pts"]
    return touched


# ---- one-call stage (build.py / backtest.py) ----------------------------------------------

@dataclass
class ClvResult:
    frozen: dict[str, Closing]
    new: list[Closing]
    settled: list[dict[str, Any]]
    store: dict[str, Any]

    @property
    def new_rows(self) -> list[dict[str, Any]]:
        return closing_rows(self.new)


def run_clv(
    *,
    history: Optional[Mapping[str, Any]] = None,
    odds_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    kickoffs: Mapping[str, datetime],
    alerts: Optional[dict[str, Any]] = None,
    store: Optional[dict[str, Any]] = None,
    now: datetime,
) -> ClvResult:
    """Freeze closings for kicked-off games from ``history`` (history.json) and/or
    ``odds_rows`` (D1 odds_history), merge first-write-wins into ``store``
    (closings.json), then settle EDGE alerts. Pure on inputs except ``store`` /
    ``alerts`` which are mutated in place."""
    store = store if store is not None else _closings_default()
    frozen: dict[str, Closing] = {}
    if odds_rows is not None:
        frozen.update(freeze_from_rows(odds_rows, kickoffs, now))
    if history is not None:
        for k, v in freeze_from_series(history, kickoffs, now).items():
            frozen.setdefault(k, v)
    new = record_closings(store, frozen)
    settled = settle_alerts(alerts, store, utc_iso(now)) if alerts is not None else []
    return ClvResult(frozen, new, settled, store)


def run_clv_stage(state_dir: PathLike, cards: Sequence[Mapping[str, Any]], now: datetime, *, run_id: Optional[str] = None,
                  dry_run: bool = False, alerts: Optional[dict[str, Any]] = None) -> ClvResult:
    """File-backed wrapper: history.json + closings.json (+ alerts.json when not
    passed) under ``state_dir``; saves closings (and alerts when it owned them)."""
    state_dir = Path(state_dir)
    history = pstate.load_history(state_dir)
    store = load_closings(state_dir)
    own_alerts = alerts is None
    if own_alerts:
        alerts, _ = pstate.load_alerts_rehydrated(state_dir)
    res = run_clv(history=history, kickoffs=kickoffs_from_cards(cards), alerts=alerts, store=store, now=now)
    if not dry_run:
        save_closings(state_dir, store, run_id)
        if own_alerts and res.settled:
            pstate.save_alerts(state_dir, alerts)
    return res


__all__ = [
    "CLOSINGS_FILE", "CLOSING_COLS", "SETTLED", "POSITIVE", "NEGATIVE", "Closing", "ClvResult",
    "kickoffs_from_cards", "clv_pts", "clv_status", "freeze_from_rows", "freeze_from_series",
    "load_closings", "save_closings", "record_closings", "get_closing", "prune_closings", "closing_rows",
    "settle_alerts", "run_clv", "run_clv_stage",
]
