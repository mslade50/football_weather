"""R2-persisted board state (openers, archive_last, scrape_baseline, alerts, history).

Adapted from golf_scraping/board/state.py (L40-50, 55-116, 213-256, 258-290,
294-391). Golf keyed every file by ``event_id`` and reset on event change;
football keys odds by ``game_id|market|side|book`` where ``game_id`` already
embeds sport/season/week, so state is pruned by *active game ids* instead of
reset wholesale. Every file carries ``schema_version`` and is passed through
``migrate()`` on load (ARCH §13: older versions upgrade, newer versions fail).

Everything fails open: missing / corrupt state behaves as fresh state, so the
board can never blank from a state problem — except an unknown *newer*
schema_version, which raises so a downgraded pipeline never clobbers state it
does not understand.
"""
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

OPENERS_FILE = "openers.json"
ARCHIVE_LAST_FILE = "archive_last.json"
BASELINE_FILE = "scrape_baseline.json"
ALERTS_FILE = "alerts.json"
ALERTS_EXPORT_FILE = "alerts_export.json"   # optional `wrangler d1 export --table alerts` / /api/alerts dump
TELEGRAM_STATE_FILE = "telegram_state.json"
HISTORY_FILE = "history.json"

ALERTS_CAP = 500   # dedup markers kept (oldest pruned)
FEED_CAP = 200     # sent alerts kept for board/alerts_feed.json
HISTORY_CAP = 120  # max change-points kept per (game, market, side, book) series

_KEY_SEP = "|"

# Default shape per state kind; ``migrate()`` fills these in for any version.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "openers": {"openers": {}},
    "archive_last": {"last": {}},
    "baseline": {"scope": None, "peaks": {}, "alerted": [], "seen_books": {}, "scopes": {}},
    "alerts": {"sent": {}},   # + "records" / "feed" added lazily by the alert helpers below
    "telegram_state": {"queue": []},
    "history": {"series": {}, "fair_series": {}},
}

PathLike = Union[str, Path]


class StateSchemaError(RuntimeError):
    """Raised when a state file was written by a newer pipeline."""


def odds_key(game_id: str, market: str, side: str, book: str) -> str:
    """``game_id|market|side|book`` — the canonical odds identity (ARCH §4.1)."""
    return _KEY_SEP.join([game_id or "", market or "", side or "", book or ""])


def _key_game_id(key: str) -> str:
    return key.split(_KEY_SEP, 1)[0]


# ── io ─────────────────────────────────────────────────────────────────────────

def _load(path: PathLike) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: PathLike, data: dict) -> None:
    data["schema_version"] = SCHEMA_VERSION
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, allow_nan=False)


# ── schema migration ───────────────────────────────────────────────────────────

def migrate(data: Optional[dict], kind: str) -> dict:
    """Bring a loaded state dict up to ``SCHEMA_VERSION``.

    * missing / empty / corrupt → fresh default for ``kind``
    * ``schema_version`` absent → treated as version 0 (pre-versioned golf-style
      file) and upgraded in place
    * version < SCHEMA_VERSION → stepwise upgrade
    * version > SCHEMA_VERSION → ``StateSchemaError`` (never silently downgrade)
    """
    if kind not in _DEFAULTS:
        raise ValueError(f"unknown state kind: {kind!r}")
    if not isinstance(data, dict) or not data:
        return _fresh(kind)

    out = dict(data)
    try:
        version = int(out.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version > SCHEMA_VERSION:
        raise StateSchemaError(
            f"{kind} state has schema_version {version} > supported {SCHEMA_VERSION}"
        )

    while version < SCHEMA_VERSION:
        out = _MIGRATIONS[version](out, kind)
        version += 1

    for k, v in _DEFAULTS[kind].items():
        if isinstance(v, (dict, list)):
            if not isinstance(out.get(k), type(v)):
                out[k] = json.loads(json.dumps(v))  # fresh copy of the default
        else:
            out.setdefault(k, v)
    out["schema_version"] = SCHEMA_VERSION
    return out


def _fresh(kind: str) -> dict:
    d = json.loads(json.dumps(_DEFAULTS[kind]))
    d["schema_version"] = SCHEMA_VERSION
    return d


def _migrate_0_to_1(d: dict, kind: str) -> dict:
    """v0 → v1: golf-era files were keyed by ``event_id``; football state has no
    event scope, so that key is dropped. Everything else is shape-compatible."""
    d.pop("event_id", None)
    d["schema_version"] = 1
    return d


_MIGRATIONS = {0: _migrate_0_to_1}


def _load_kind(data_dir: PathLike, filename: str, kind: str) -> dict:
    return migrate(_load(Path(data_dir) / filename), kind)


# ── openers (first-seen book line per game/market/side/book) ───────────────────

def load_openers(data_dir: PathLike) -> dict:
    return _load_kind(data_dir, OPENERS_FILE, "openers")


def record_openers(openers: dict, lines: Iterable[Any], now: str) -> int:
    """Add any (game, market, side, book) not seen before with the current
    line/odds + timestamp. Existing entries are preserved (that's the opener).
    Returns the number of new openers recorded."""
    store = openers.setdefault("openers", {})
    added = 0
    for ln in lines:
        game_id, market, side, book, line, odds = _line_tuple(ln)
        if not game_id or not book or odds is None:
            continue
        key = odds_key(game_id, market, side, book)
        if key not in store:
            store[key] = {"line": line, "odds": odds, "ts": now}
            added += 1
    return added


def get_opener(openers: dict, key: str) -> Optional[dict]:
    return (openers.get("openers") or {}).get(key)


def prune_openers(openers: dict, active_game_ids: Iterable[str]) -> int:
    """Drop openers for games no longer on the schedule (past weeks). Returns
    the count removed."""
    active = set(active_game_ids)
    store = openers.setdefault("openers", {})
    stale = [k for k in store if _key_game_id(k) not in active]
    for k in stale:
        del store[k]
    return len(stale)


def save_openers(data_dir: PathLike, openers: dict) -> None:
    _save(Path(data_dir) / OPENERS_FILE, openers)


# ── archive last-values (change-only D1 inserts) ───────────────────────────────

def load_archive_last(data_dir: PathLike) -> dict:
    """Last archived (line, odds) per key, for change-only D1 inserts."""
    return _load_kind(data_dir, ARCHIVE_LAST_FILE, "archive_last")


def prune_archive_last(d: dict, active_game_ids: Iterable[str]) -> int:
    active = set(active_game_ids)
    last = d.setdefault("last", {})
    stale = [k for k in last if _key_game_id(k) not in active]
    for k in stale:
        del last[k]
    return len(stale)


def save_archive_last(data_dir: PathLike, d: dict) -> None:
    _save(Path(data_dir) / ARCHIVE_LAST_FILE, d)


# ── scrape-volume baseline (per book|market peaks) ─────────────────────────────

def load_baseline(data_dir: PathLike, scope: str) -> dict:
    """Per-(book|market) peaks for one ``sport:season:week`` scope.

    All scopes share one R2 file but retain independent ``peaks`` / ``alerted``
    blocks. Previously, alternating NFL and CFB runs reset each other's active
    scope and re-armed sustained DARK alerts on every pass.
    """
    d = _load_kind(data_dir, BASELINE_FILE, "baseline")
    scopes = d.setdefault("scopes", {})
    # Upgrade the active legacy top-level block lazily without a schema bump.
    legacy_scope = d.get("scope")
    if legacy_scope and str(legacy_scope) not in scopes:
        scopes[str(legacy_scope)] = {
            "peaks": d.get("peaks") or {},
            "alerted": d.get("alerted") or [],
        }
    active = scopes.get(str(scope))
    if not isinstance(active, dict):
        active = {"peaks": {}, "alerted": []}
        scopes[str(scope)] = active
    d["scope"] = scope
    d["peaks"] = active.get("peaks") if isinstance(active.get("peaks"), dict) else {}
    d["alerted"] = active.get("alerted") if isinstance(active.get("alerted"), list) else []
    return d


def save_baseline(data_dir: PathLike, d: dict) -> None:
    scope = str(d.get("scope") or "")
    if scope:
        d.setdefault("scopes", {})[scope] = {
            "peaks": d.get("peaks") or {},
            "alerted": d.get("alerted") or [],
        }
    _save(Path(data_dir) / BASELINE_FILE, d)


# ── alert dedup markers (R2-round-tripped; repo data/ is fresh every CI run) ───

def load_alerts(data_dir: PathLike) -> dict:
    """Dedup markers for one-shot Telegram alerts. Keys embed their own scope
    (game_id / ET-day) so they survive week changes. Must ride the R2 state
    fetch/push loops (a repo-data/ marker is fresh on every CI checkout)."""
    return _load_kind(data_dir, ALERTS_FILE, "alerts")


def alert_sent(alerts: dict, key: str) -> bool:
    return key in (alerts.get("sent") or {})


def mark_alert(alerts: dict, key: str, now: str, cap: int = ALERTS_CAP) -> None:
    """Record AFTER a successful send (check/mark split: marking before a failed
    send permanently eats the alert). Oldest entries pruned at the cap."""
    sent = alerts.setdefault("sent", {})
    sent[key] = now
    if len(sent) > cap:
        for k in sorted(sent, key=lambda k: sent[k])[:len(sent) - cap]:
            del sent[k]


def save_alerts(data_dir: PathLike, alerts: dict) -> None:
    _save(Path(data_dir) / ALERTS_FILE, alerts)


# ---- alert records / feed (ARCH §10: alerts.json mirrors D1 ``alerts``) ----------

# D1 ``alerts`` columns (ARCH §4.3 0002_alerts.sql). Extra per-record keys used
# only by pipeline/alerts.py (last_move_at, last_wind, last_rain, kickoff_utc,
# label) stay in the JSON and are dropped by d1_out.alert_rows.
ALERT_RECORD_COLS = (
    "alert_key", "family", "game_id", "sport", "season", "week", "market", "side", "book", "tier",
    "model_version", "first_sent_at", "last_sent_at", "sends", "first_line", "first_odds", "first_fair",
    "first_edge", "last_line", "last_odds", "last_fair", "last_edge", "closing_line", "clv_pts", "status", "run_id",
)


def get_alert_record(alerts: dict, key: str) -> Optional[dict]:
    rec = (alerts.get("records") or {}).get(key)
    return rec if isinstance(rec, dict) else None


def upsert_alert_record(alerts: dict, key: str, fields: dict[str, Any], now: str) -> dict:
    """Create (first send) or update (re-send / status change) the record for
    ``key``. ``first_*`` fields are frozen on creation; ``last_*`` follow every
    call; ``sends`` counts successful sends."""
    records = alerts.setdefault("records", {})
    rec = records.get(key)
    if not isinstance(rec, dict):
        rec = {"alert_key": key, "first_sent_at": now, "sends": 0, "status": "open"}
        for k in ("line", "odds", "fair", "edge"):
            rec[f"first_{k}"] = fields.get(f"last_{k}", fields.get(f"first_{k}"))
        records[key] = rec
    for k, v in fields.items():
        if k.startswith("first_") and rec.get(k) is not None:
            continue
        rec[k] = v
    rec["last_sent_at"] = fields.get("last_sent_at", now)
    return rec


def open_edge_records(alerts: dict, game_id: Optional[str] = None) -> list[dict]:
    """EDGE records with ``status == 'open'`` (line-move / gone / forecast-move
    detection runs only on these)."""
    out = []
    for rec in (alerts.get("records") or {}).values():
        if not isinstance(rec, dict) or rec.get("family") != "edge" or rec.get("status", "open") != "open":
            continue
        if game_id is not None and rec.get("game_id") != game_id:
            continue
        out.append(rec)
    return out


def append_feed(alerts: dict, item: dict[str, Any], cap: int = FEED_CAP) -> None:
    feed = alerts.setdefault("feed", [])
    feed.append(item)
    if len(feed) > cap:
        del feed[0:len(feed) - cap]


def prune_alert_records(alerts: dict, cap: int = ALERTS_CAP) -> int:
    """Keep at most ``cap`` records: closed/settled ones go first, then oldest."""
    records = alerts.setdefault("records", {})
    if len(records) <= cap:
        return 0
    order = sorted(records, key=lambda k: (records[k].get("status", "open") == "open", records[k].get("last_sent_at") or ""))
    stale = order[:len(records) - cap]
    for k in stale:
        del records[k]
    return len(stale)


def rehydrate_alerts(alerts: dict, rows: Iterable[dict[str, Any]]) -> int:
    """Rebuild dedup markers + records from D1 ``alerts`` rows (``wrangler d1
    export --table alerts`` JSON or ``/api/alerts`` ``rows``). Existing markers win.
    Returns the number of keys added."""
    sent = alerts.setdefault("sent", {})
    records = alerts.setdefault("records", {})
    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("alert_key")
        if not key:
            continue
        ts = row.get("last_sent_at") or row.get("first_sent_at") or ""
        if key not in sent:
            sent[key] = ts
            added += 1
        if key not in records:
            records[key] = {c: row.get(c) for c in ALERT_RECORD_COLS}
            if not records[key].get("status"):
                records[key]["status"] = "open"
    return added


def _export_rows(data: Any) -> list[dict[str, Any]]:
    """Rows from any of: ``[row,...]``, ``{"rows": [...]}`` (/api/alerts),
    ``[{"results": [...]}]`` (wrangler d1 execute --json)."""
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return [r for r in data["rows"] if isinstance(r, dict)]
        if isinstance(data.get("results"), list):
            return [r for r in data["results"] if isinstance(r, dict)]
        return []
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and isinstance(data[0].get("results"), list):
            return [r for chunk in data for r in (chunk.get("results") or []) if isinstance(r, dict)]
        return [r for r in data if isinstance(r, dict)]
    return []


def load_alerts_rehydrated(data_dir: PathLike, fetch_rows: Optional[Any] = None) -> tuple[dict, str]:
    """``load_alerts`` plus the ARCH §10 fallback: when ``alerts.json`` is missing
    from the state dir (R2 NoSuchKey on a fresh bucket / lost state), rebuild the
    markers from ``alerts_export.json`` in the same dir, else from
    ``fetch_rows()`` (a callable returning D1 rows, e.g. an ``/api/alerts`` GET).
    Returns ``(alerts, source)`` with source in ``r2 | export | api | fresh``."""
    path = Path(data_dir) / ALERTS_FILE
    if path.is_file():
        return load_alerts(data_dir), "r2"
    alerts = _fresh("alerts")
    export = Path(data_dir) / ALERTS_EXPORT_FILE
    if export.is_file():
        rows = _export_rows(_load_any(export))
        if rows:
            n = rehydrate_alerts(alerts, rows)
            logger.warning(f"alerts.json missing: rehydrated {n} keys from {export.name}")
            return alerts, "export"
    if callable(fetch_rows):
        try:
            rows = _export_rows(fetch_rows())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"alerts rehydrate fetch failed: {exc}")
            rows = []
        if rows:
            n = rehydrate_alerts(alerts, rows)
            logger.warning(f"alerts.json missing: rehydrated {n} keys from API")
            return alerts, "api"
    return alerts, "fresh"


def _load_any(path: PathLike) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---- telegram quiet-hours queue -------------------------------------------------------

def load_telegram_state(data_dir: PathLike) -> dict:
    return _load_kind(data_dir, TELEGRAM_STATE_FILE, "telegram_state")


def queue_alert(tg: dict, item: dict[str, Any]) -> None:
    """Park an alert during quiet hours. ``item`` = ``{key, text, chat, family,
    sport, game_id, tier, ts, record}``; duplicates (same key) are replaced so a
    line that keeps moving overnight yields one message at flush."""
    queue = tg.setdefault("queue", [])
    for i, q in enumerate(queue):
        if isinstance(q, dict) and q.get("key") == item.get("key"):
            queue[i] = item
            return
    queue.append(item)


def drain_queue(tg: dict) -> list[dict[str, Any]]:
    queue = [q for q in (tg.get("queue") or []) if isinstance(q, dict)]
    tg["queue"] = []
    return queue


def save_telegram_state(data_dir: PathLike, tg: dict) -> None:
    _save(Path(data_dir) / TELEGRAM_STATE_FILE, tg)


# ── odds + model-fair history (change-only series for hover cards) ─────────────

def load_history(data_dir: PathLike) -> dict:
    return _load_kind(data_dir, HISTORY_FILE, "history")


def update_history(history: dict, lines: Iterable[Any], now: str, cap: int = HISTORY_CAP) -> int:
    """Append changed book lines: ``series[key] = [[ts, line, odds], ...]``,
    appended only when line OR odds moved. Capped to the last ``cap`` points.
    Returns the number of points appended."""
    store = history.setdefault("series", {})
    appended = 0
    for ln in lines:
        game_id, market, side, book, line, odds = _line_tuple(ln)
        if not game_id or not book or odds is None:
            continue
        key = odds_key(game_id, market, side, book)
        seq = store.setdefault(key, [])
        if not seq or seq[-1][1:] != [line, odds]:
            seq.append([now, line, odds])
            appended += 1
            if len(seq) > cap:
                del seq[0:len(seq) - cap]
    return appended


def update_fair_history(history: dict, fairs: dict[str, float], now: str, cap: int = HISTORY_CAP) -> int:
    """Model-fair counterpart: ``fair_series[game_id|market|side] = [[ts, value]]``.
    Only finite values are retained (consensus fallbacks are market prices, not
    "our fair" — the caller must pass model output only)."""
    store = history.setdefault("fair_series", {})
    appended = 0
    for key, val in fairs.items():
        if not isinstance(val, (int, float)) or val != val:
            continue
        seq = store.setdefault(key, [])
        if not seq or seq[-1][1] != val:
            seq.append([now, val])
            appended += 1
            if len(seq) > cap:
                del seq[0:len(seq) - cap]
    return appended


def prune_history(history: dict, active_game_ids: Iterable[str]) -> int:
    active = set(active_game_ids)
    removed = 0
    for tree in ("series", "fair_series"):
        store = history.setdefault(tree, {})
        stale = [k for k in store if _key_game_id(k) not in active]
        for k in stale:
            del store[k]
        removed += len(stale)
    return removed


def save_history(data_dir: PathLike, history: dict) -> None:
    _save(Path(data_dir) / HISTORY_FILE, history)


# ── helpers ────────────────────────────────────────────────────────────────────

def _line_tuple(ln: Any) -> tuple[str, str, str, str, Optional[float], Optional[int]]:
    """(game_id, market, side, book, line, odds) from a GameLine or dict."""
    if isinstance(ln, dict):
        get = ln.get
    else:
        def get(name: str, default: Any = None) -> Any:
            return getattr(ln, name, default)
    return (
        str(get("game_id") or ""),
        str(get("market") or ""),
        str(get("side") or ""),
        str(get("book") or ""),
        get("line"),
        get("odds"),
    )
