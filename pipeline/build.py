"""Pipeline orchestrator (Phase 2): schedule -> stadiums -> weather -> impact v1 -> signals -> odds -> legacy files.

    python -m pipeline.build --sport all --scope full --print [--dry-run] [--legacy-dir .]
    python -m pipeline.build --sport nfl --scope odds --books betonline

Scopes:
    weather  schedule/stadiums/weather/impact only (Phase 1 behaviour; odds columns NaN)
    light    weather + every httpx book (no Playwright)
    full     light + Playwright books (BetOnline)
    odds     weather + only the books given by ``--books`` (default: all books);
             used by the Playwright CI job to re-write the legacy files with BetOnline lines

Odds stage: scrapers run concurrently with ``asyncio.gather(return_exceptions=True)``
(one failing/hanging book never blocks the others), per-book/market counts are
recorded on the RunContext, ``check_scrape_volume`` (adapted from
golf_scraping/board/build.py ``_check_scrape_volume``) pages Telegram when a book
goes dark or a (book|market) count collapses, provisional book game ids are mapped
onto schedule ``game_id``s (``pipeline.odds.merge`` when present, else the built-in
matcher), a weighted-median consensus is computed (``pipeline.model.fair`` when
present, else built-in) and openers/archive_last/scrape_baseline are persisted under
``data/state/``. Legacy odds columns are filled from the sport's reference book
(NFL: BetOnline, CFB: FanDuel) with consensus fallback; openers come from
``openers.json`` so ``*_open`` stays fixed while ``*_now`` moves.

Only games kicking off within [now-6h, now+10d] are processed. A failing stage is
recorded as a ``Degradation`` and the run continues with what it has.

Phase 3 outputs (always written locally unless ``--dry-run``): board JSON under
``data/board/`` (``games_{sport}.json``, ``board.json``, ``history.json``,
``wx_history.json``, ``meta.json``), per-run snapshots under ``data/snapshots/``,
``data/d1_inserts.sql`` for ``wrangler d1 execute --file`` and
``data/publish_manifest.json`` (``{r2 key: local path}``, meta last). ``--publish``
pushes the manifest to R2 through boto3 when ``CF_ACCOUNT_ID`` /
``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY`` are set and runs the self-check;
``--merge-into-r2`` (Playwright job) additionally pulls the live R2 state first.
Without the R2 env the workflow's wrangler loops do the upload.

Phase 4 alerts (``pipeline/alerts.py``, any odds scope): after every sport is built,
EDGE / MOVE / GONE / FORECAST-MOVE / OPENERS / OPS candidates are collected from the
GameCards, deduped against ``alerts.json`` (rehydrated from ``alerts_export.json``
when missing), queued in ``telegram_state.json`` during quiet hours, capped at 25
messages per run and sent; markers are written ONLY after a successful send.
``--no-alerts`` / ``--dry-run`` print the candidates with their keys instead;
``--alerts-stdout`` runs the stage for real (markers written) but prints the messages. Sent
alerts are mirrored to D1 ``alerts`` (``d1_inserts.sql``), ``board/alerts_feed.json``
and ``board/status.json``.

Exit code is 1 when any ``severity='error'`` degradation was recorded (so CI
fails loudly and the Telegram step fires); warnings are printed and tolerated.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import json
import logging
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline import alerts as alerts_mod
from pipeline import state as pstate
from pipeline.contracts import Game, GameLine, Stadium, Team, WeatherForecast
from pipeline.model import clv as clv_mod
from pipeline.model import config as model_config
from pipeline.model import signals
from pipeline.model.impact import ImpactV1, ImpactV2, compute_impact_v1, compute_impact_v2
from pipeline.outputs import d1_out, json_out
from pipeline.outputs import r2 as r2_out
from pipeline.outputs.legacy import CFB_FILENAME, NFL_FILENAME, LegacyRecord, write_legacy
from pipeline.outputs.raw_out import DEFAULT_BASE, NullRawStore, RawStore
from pipeline.run_context import REPO_ROOT, RunContext
from utils.timeutil import et_weekday, naive_et_iso, now_et, to_tz, utc_iso

logger = logging.getLogger(__name__)

SPORTS_ALL = ("nfl", "cfb")
SCOPES = ("weather", "light", "full", "odds")
DEFAULT_OUT_DIR = REPO_ROOT / "data"
DEFAULT_STATE_DIR = REPO_ROOT / "data" / "state"
DEFAULT_BOARD_DIR = REPO_ROOT / "data" / "board"
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"
DEFAULT_D1_SQL = REPO_ROOT / "data" / d1_out.D1_SQL_FILENAME
PUBLISH_MANIFEST = "publish_manifest.json"
PREV_META_FILE = "prev_meta.json"

# Games outside this window are dropped before the weather stage (matches gate_check).
WINDOW_BEFORE_H = 6.0
WINDOW_AFTER_D = 10.0

# ---- books ------------------------------------------------------------------------
# book -> (module, class); order = display / alert order.
BOOK_REGISTRY: dict[str, tuple[str, str]] = {
    "pinnacle": ("pipeline.odds.pinnacle", "PinnacleScraper"),
    "betcris": ("pipeline.odds.betcris", "BetcrisScraper"),
    "fanduel": ("pipeline.odds.fanduel", "FanDuelScraper"),
    "kalshi": ("pipeline.odds.kalshi", "KalshiScraper"),
    "novig": ("pipeline.odds.novig", "NovigScraper"),
    "prophetx": ("pipeline.odds.prophetx", "ProphetXScraper"),
    "betonline": ("pipeline.odds.betonline", "BetOnlineScraper"),
    "draftkings": ("pipeline.odds.draftkings", "DraftKingsScraper"),
}
PLAYWRIGHT_BOOKS = ("betonline",)
HTTPX_BOOKS = ("pinnacle", "betcris", "fanduel", "kalshi", "novig", "prophetx")
BOOK_ORDER = HTTPX_BOOKS + PLAYWRIGHT_BOOKS
# Golf's SIM_BOOKS: a book at 0 rows while >=2 of these report is "dark", not "no market".
CRITICAL_BOOKS = frozenset({"pinnacle", "betonline", "betcris", "fanduel"})
# ARCH §7.3 consensus weights.
BOOK_WEIGHTS: dict[str, float] = {
    "pinnacle": 3.0, "betonline": 2.0, "betcris": 1.5, "fanduel": 1.0, "draftkings": 1.0,
    "kalshi": 1.0, "novig": 1.0, "prophetx": 0.75,
}
CONSENSUS_BOOK = "consensus"
# Legacy "now" columns: CFB = FanDuel (as before), NFL = BetOnline; fallback = consensus.
LEGACY_NOW_BOOK = {"nfl": "betonline", "cfb": "fanduel"}

SCRAPE_TIMEOUT_S = 300.0     # per book incl. retries (BetOnline: Chromium + CF pass)
MATCH_WINDOW_H = 36.0        # provisional-id kickoff must be within ±36 h of the schedule
VOLUME_DROP_FRAC = 0.5       # alert when current < 50% of the peak
VOLUME_MIN_PEAK = 8          # ignore small markets (noise floor)
VOLUME_DARK_MIN_ROWS = 10    # a peer counts as healthy at >= this many rows


def _import(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def default_season(now: datetime) -> int:
    """NFL/CFB season year: Jan/Feb belong to the previous season."""
    return now.year - 1 if now.month <= 2 else now.year


def in_window(kickoff_utc: datetime, now: datetime) -> bool:
    delta_h = (kickoff_utc - now).total_seconds() / 3600.0
    return -WINDOW_BEFORE_H <= delta_h <= WINDOW_AFTER_D * 24.0


def books_for_scope(scope: str, books: Sequence[str] | None = None) -> list[str]:
    """Which books a scope scrapes. ``--books`` restricts any scope; ``odds`` defaults to all."""
    if scope == "weather":
        return []
    if scope == "light":
        pool: tuple[str, ...] = HTTPX_BOOKS
    else:
        pool = BOOK_ORDER
    if books:
        wanted = [b.strip().lower() for b in books if b.strip()]
        unknown = [b for b in wanted if b not in BOOK_REGISTRY]
        if unknown:
            raise ValueError(f"unknown book(s): {', '.join(unknown)}")
        return wanted if scope == "odds" else [b for b in wanted if b in pool]
    return list(pool)


# ---- stages ---------------------------------------------------------------------

def stage_stadiums(ctx: RunContext, sport: str) -> Any | None:
    mod = _import("pipeline.stadiums.loader")
    if mod is None:
        ctx.degrade("stadiums", "pipeline.stadiums.loader not importable", "error")
        return None
    try:
        return mod.load_stadium_book(sports=(sport,))
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("stadiums", f"{type(exc).__name__}: {exc}", "error")
        return None


def stage_schedule(ctx: RunContext, sport: str, raw: RawStore, season: int | None, book: Any) -> list[Game]:
    mod = _import(f"pipeline.schedule.{sport}")
    if mod is None:
        ctx.degrade("schedule", f"pipeline.schedule.{sport} not importable", "error")
        return []
    season = season or default_season(ctx.started_et)
    raw_dir = None if isinstance(raw, NullRawStore) else raw.run_dir
    try:
        if sport == "nfl":
            games = mod.fetch_nfl_schedule(season, book=book, raw_dir=raw_dir)
        else:
            games = mod.fetch_cfb_schedule(season, book=book, raw_dir=raw_dir, ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - any stage failure becomes a Degradation
        ctx.degrade("schedule", f"{sport}: {type(exc).__name__}: {exc}", "error")
        return []
    games = [g for g in (games or []) if isinstance(g, Game)]
    now = ctx.now_utc
    upcoming = [g for g in games if in_window(g.kickoff_utc, now)]
    ctx.count("schedule", sport, len(upcoming))
    if not upcoming:
        ctx.degrade("schedule", f"{sport}: 0 games within window ({len(games)} in season {season})", "warn")
    return upcoming


def _is_conus(st: Stadium) -> bool:
    if st.country and st.country.strip().upper() not in ("US", "USA", "UNITED STATES", ""):
        return False
    return 24.0 <= st.lat <= 50.0 and -125.0 <= st.lon <= -66.0


def stage_weather(
    ctx: RunContext, sport: str, games: list[Game], stadiums: dict[str, Stadium], raw: RawStore, roof_states: dict[str, str | None],
    extras: dict[str, dict[str, Any]] | None = None,
) -> dict[str, WeatherForecast]:
    """Deterministic forecast + ensemble (Phase 5) + NWS -> merged WeatherForecast per game.
    ``extras`` (if given) collects per-game merge side-outputs: ``precip_prob_ens``,
    ``roof_heuristic``, ``ensemble`` (bool)."""
    if not games:
        return {}
    om_mod = _import("pipeline.weather.openmeteo")
    nws_mod = _import("pipeline.weather.nws")
    merge_mod = _import("pipeline.weather.merge")
    if om_mod is None or merge_mod is None or nws_mod is None:
        ctx.degrade("weather", "pipeline.weather modules not importable", "error")
        return {}
    now = ctx.now_utc
    games = [g for g in games if g.game_id in stadiums]
    if not games:
        return {}

    def capture(source: str, payload: Any, url: str | None = None) -> None:
        raw.put(f"{sport}_{source}", payload, url)

    # distinct points, split CONUS / international (different model sets)
    by_point: dict[tuple[float, float], list[Game]] = {}
    for g in games:
        st = stadiums[g.game_id]
        by_point.setdefault((round(st.lat, 4), round(st.lon, 4)), []).append(g)
    conus_pts = [pt for pt in by_point if _is_conus(stadiums[by_point[pt][0].game_id])]
    intl_pts = [pt for pt in by_point if pt not in conus_pts]
    kickoffs = [g.kickoff_utc for g in games]
    start, end = om_mod.window_for(kickoffs)
    om_by_point: dict[tuple[float, float], Any] = {}
    for pts, models, prefix in ((conus_pts, om_mod.CONUS_MODELS, "openmeteo_conus"), (intl_pts, om_mod.INTL_MODELS, "openmeteo_intl")):
        if not pts:
            continue
        try:
            parsed = om_mod.fetch_forecast(pts, start=start, end=end, models=models, capture=capture, source_prefix=prefix)
            om_by_point.update(zip(pts, parsed, strict=False))
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("weather", f"{sport}: open-meteo ({prefix}) failed: {exc}", "error")

    # ensemble members (wind_vol_fc / P10-P90 / precip_prob_ens); failure -> static wind_vol fallback
    ens_by_point: dict[tuple[float, float], Any] = {}
    all_pts = conus_pts + intl_pts
    ens_ok = False
    fetch_ens = getattr(om_mod, "fetch_ensemble", None)
    if callable(fetch_ens) and all_pts:
        try:
            parsed_ens = fetch_ens(all_pts, start=start, end=end, capture=capture, source_prefix="openmeteo_ensemble")
            ens_by_point.update(zip(all_pts, parsed_ens, strict=False))
            ens_ok = True
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("weather", f"{sport}: open-meteo ensemble failed ({exc}); wind_vol falls back to static", "warn")
    else:
        ctx.degrade("weather", f"{sport}: ensemble client unavailable; wind_vol falls back to static", "warn")

    nws_by_point: dict[tuple[float, float], Any] = {}
    cache = nws_mod.PointsCache()
    nws_h = merge_mod.NWS_HORIZON_H
    for pt in conus_pts:
        if not any((g.kickoff_utc - now).total_seconds() / 3600.0 <= nws_h for g in by_point[pt]):
            continue
        try:
            nws_by_point[pt] = nws_mod.fetch_hourly(pt[0], pt[1], cache=cache, capture=capture)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("weather", f"{sport}: nws {pt} failed: {exc}", "warn")
    try:
        cache.save()
    except OSError:
        pass

    fc: dict[str, WeatherForecast] = {}
    for pt, pt_games in by_point.items():
        for g in pt_games:
            st = stadiums[g.game_id]
            try:
                res = merge_mod.build_forecast(
                    g.game_id, g.kickoff_utc, now, om_by_point.get(pt), nws_by_point.get(pt),
                    orientation_deg=st.orientation_deg, roof_state=roof_states.get(g.game_id), run_id=ctx.run_id,
                    ens=ens_by_point.get(pt), roof_type=st.roof_type, expect_ensemble=ens_ok,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.degrade("weather", f"{g.game_id}: merge failed: {exc}", "warn")
                continue
            for d in res.degradations:
                ctx.degradations.append(d)
            fc[g.game_id] = res.forecast
            if extras is not None:
                extras[g.game_id] = {
                    "precip_prob_ens": getattr(res, "precip_prob_ens", None),
                    "roof_heuristic": bool(getattr(res, "roof_heuristic", False)),
                    "ensemble": getattr(res, "ensemble", None) is not None,
                }
    ctx.count("weather", sport, len(fc))
    missing = [g.game_id for g in games if g.game_id not in fc]
    if missing:
        ctx.degrade("weather", f"{sport}: {len(missing)} games without forecast", "warn")
    return fc


# ---- odds: scrapers ---------------------------------------------------------------

def load_scraper_class(name: str) -> type | None:
    module_name, cls_name = BOOK_REGISTRY[name]
    mod = _import(module_name)
    if mod is None:
        return None
    return getattr(mod, cls_name, None)


def make_scraper(cls: type, raw: RawStore | None, run_id: str | None) -> Any:
    """Books differ in ctor kwargs (Kalshi/Novig take no raw_store): degrade gracefully."""
    try:
        return cls(headless=True, raw_store=raw, run_id=run_id)
    except TypeError:
        try:
            return cls(headless=True)
        except TypeError:
            return cls()


async def _run_book(name: str, scraper: Any, sport: str, raw: RawStore | None, run_id: str | None) -> list[GameLine]:
    kwargs: dict[str, Any] = {}
    if raw is not None and getattr(scraper, "raw_store", "missing") == "missing":
        # scrapers without a raw_store attribute take a capture callback instead
        def capture(source: str, payload: Any, url: str | None = None) -> None:
            raw.put(source, payload, url)

        kwargs["capture"] = capture
        kwargs["run_id"] = run_id
    try:
        return await asyncio.wait_for(scraper.scrape_with_retry(sport, market=None, **kwargs), SCRAPE_TIMEOUT_S)
    except TypeError:
        # scrape() rejected the extra kwargs (BaseScraper forwards **kwargs verbatim)
        return await asyncio.wait_for(scraper.scrape_with_retry(sport, market=None), SCRAPE_TIMEOUT_S)


async def scrape_books(
    sport: str, books: Sequence[str], raw: RawStore | None, run_id: str | None, degrade: Callable[[str, str, str], Any]
) -> tuple[dict[str, list[GameLine]], dict[str, Any]]:
    """Run every requested book concurrently. Returns ``({book: lines}, {book: scraper})``;
    a book that fails / times out maps to ``[]`` (and a warn Degradation) — never raises."""
    names: list[str] = []
    scrapers: dict[str, Any] = {}
    tasks = []
    for name in books:
        cls = load_scraper_class(name)
        if cls is None:
            degrade("odds", f"{sport}: {name} unavailable (module/dependency missing)", "warn")
            continue
        try:
            scraper = make_scraper(cls, raw, run_id)
        except Exception as exc:  # noqa: BLE001
            degrade("odds", f"{sport}: {name} init failed: {type(exc).__name__}: {exc}", "warn")
            continue
        names.append(name)
        scrapers[name] = scraper
        tasks.append(_run_book(name, scraper, sport, raw, run_id))
    if not tasks:
        return {}, {}
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, list[GameLine]] = {}
    for name, res in zip(names, results, strict=True):
        if isinstance(res, asyncio.TimeoutError):
            degrade("odds", f"{sport}: {name} timed out after {SCRAPE_TIMEOUT_S:.0f}s", "warn")
            out[name] = []
        elif isinstance(res, BaseException):
            degrade("odds", f"{sport}: {name} failed: {type(res).__name__}: {res}", "warn")
            out[name] = []
        else:
            out[name] = [ln for ln in (res or []) if isinstance(ln, GameLine)]
    return out, scrapers


def _run_async(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except RuntimeError:  # already inside a loop (tests / notebooks)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def scrape_counts(lines: Sequence[Any]) -> dict[str, int]:
    """``book|market`` -> row count (main lines only; alternates inflate ladders)."""
    counts: dict[str, int] = {}
    for ln in lines:
        if not getattr(ln, "is_main", True):
            continue
        key = f"{getattr(ln, 'book', '')}|{getattr(ln, 'market', '')}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---- odds: scrape-volume alert (golf build.py L2255-2357, keys book|market) ---------

def check_scrape_volume(
    counts: dict[str, int],
    baseline: dict,
    books: Sequence[str],
    critical: frozenset[str] | set[str] = CRITICAL_BOOKS,
    drop_frac: float = VOLUME_DROP_FRAC,
    min_peak: int = VOLUME_MIN_PEAK,
) -> list[tuple[str, int, int | None]]:
    """Edge-triggered volume-drop detector. Mutates ``baseline`` (peaks/alerted/seen_books)
    and returns ``[(key, current, peak)]`` to alert; ``peak=None`` marks a fully-dark book.

    * peaks only ratchet up; a (book|market) below ``drop_frac``·peak (peak ≥ ``min_peak``)
      alerts once and re-arms on recovery
    * DARK: 0 rows for a critical book while ≥2 critical peers report ≥10 rows, or any
      book that ever reported ≥10 rows in ``seen_books`` and is now at 0
    * a dark book's per-market drops are subsumed by its DARK line
    * only ``books`` requested this run are judged (a book not scraped is not dark)
    """
    books = list(books)
    book_total: dict[str, int] = {}
    for key, n in counts.items():
        b = key.split("|", 1)[0]
        book_total[b] = book_total.get(b, 0) + n

    peaks: dict[str, int] = baseline.setdefault("peaks", {})
    alerted = set(baseline.get("alerted") or [])
    seen_books: dict[str, int] = baseline.setdefault("seen_books", {})
    for b in books:
        seen_books[b] = max(int(seen_books.get(b, 0)), book_total.get(b, 0))

    # Cold start with nothing ever seen: no baseline to judge against.
    if sum(counts.values()) < 20 and not peaks and not any(seen_books.values()):
        baseline["alerted"] = sorted(alerted)
        return []

    dark: set[str] = set()
    for b in books:
        if b not in critical:
            continue
        peers = sum(1 for o in critical if o != b and book_total.get(o, 0) >= VOLUME_DARK_MIN_ROWS)
        if book_total.get(b, 0) == 0 and peers >= 2:
            dark.add(b)
    for b in books:
        if int(seen_books.get(b, 0)) >= VOLUME_DARK_MIN_ROWS and book_total.get(b, 0) == 0:
            dark.add(b)

    drops: list[tuple[str, int, int | None]] = []
    for key in set(peaks) | set(counts):
        book = key.split("|", 1)[0]
        cur = counts.get(key, 0)
        peaks[key] = max(int(peaks.get(key, 0)), cur)  # peaks only ratchet up
        if book not in books:
            continue  # not scraped this run: neither drop nor recovery
        if book in dark:
            continue  # covered by the DARK line
        peak = peaks[key]
        if peak >= min_peak and cur < drop_frac * peak:
            if key not in alerted:
                drops.append((key, cur, peak))
                alerted.add(key)
        else:
            alerted.discard(key)  # recovered (or never dropped) -> re-arm

    for b in books:
        dkey = f"DARK|{b}"
        if b in dark:
            if dkey not in alerted:
                drops.append((dkey, 0, None))
                alerted.add(dkey)
        else:
            alerted.discard(dkey)

    baseline["alerted"] = sorted(alerted)
    return drops


def format_volume_alert(drops: Sequence[tuple[str, int, int | None]], label: str) -> str:
    lines = []
    for k, c, p in sorted(drops, key=lambda x: x[0]):
        if p is None:
            lines.append(f"{k.split('|', 1)[1]}: 0 rows - DARK while peers report")
        else:
            lines.append(f"{k.replace('|', ' ')}: {c} (usual ~{p})")
    return f"⚠️ Scrape volume alert - {label}\n" + "\n".join(lines)


def volume_scope(sport: str, games: Sequence[Game], season: int | None) -> str:
    """Baseline scope ``sport:season:week`` from the earliest upcoming game."""
    if games:
        g = min(games, key=lambda x: x.kickoff_utc)
        return f"{sport}:{g.season}:{g.week}"
    return f"{sport}:{season or '?'}:?"


def _send_alert(text: str) -> bool:
    tg = _import("utils.telegram")
    if tg is None:
        return False
    try:
        return bool(_run_async(tg.send_message(text)))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"telegram send failed (non-fatal): {exc}")
        return False


# ---- odds: provisional id -> schedule game_id (fallback when odds.merge is absent) -----

_PROVISIONAL = re.compile(r"^(?P<sport>nfl|cfb):raw:(?P<stamp>.*?):(?P<away>[^:]+)@(?P<home>[^:]+)$")


def parse_provisional(game_id: str) -> tuple[str, str, str] | None:
    """``{sport}:raw:{stamp}:{away}@{home}`` -> (stamp, away, home); None for canonical ids."""
    m = _PROVISIONAL.match(game_id or "")
    if not m:
        return None
    return m.group("stamp"), m.group("away"), m.group("home")


def _stamp_to_utc(stamp: str) -> datetime | None:
    from datetime import timezone

    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _team_id(book: Any, sport: str, raw: str) -> str | None:
    if book is None:
        return None
    try:
        return book.resolve_team(sport, raw.replace("-", " "))
    except Exception:  # noqa: BLE001
        return None


def schedule_span(games: Sequence[Game]) -> tuple[datetime, datetime] | None:
    """[earliest kickoff − 36 h, latest kickoff + 36 h]: a book game outside this span
    (next week's card, a preseason game the schedule does not carry) is not an
    "unresolved" match — it simply is not on the board."""
    if not games:
        return None
    win = timedelta(hours=MATCH_WINDOW_H)
    kicks = [g.kickoff_utc for g in games]
    return min(kicks) - win, max(kicks) + win


def in_span(when: datetime | None, span: tuple[datetime, datetime] | None) -> bool:
    if when is None or span is None:
        return True  # unknown kickoff: judge it
    return span[0] <= when <= span[1]


def fallback_match(
    sport: str, games: Sequence[Game], lines: Sequence[GameLine], book: Any
) -> tuple[list[GameLine], list[str]]:
    """Map provisional book ids onto schedule ``game_id``s: team aliases via the stadium
    book + kickoff within ±36 h; neutral-site flips handled by trying swapped sides.
    Lines already carrying a canonical id pass through. Returns (lines, unresolved)
    where unresolved lists only book games inside the schedule span."""
    by_pair: dict[tuple[str, str], list[Game]] = {}
    known_ids = {g.game_id for g in games}
    for g in games:
        by_pair.setdefault((g.away_id, g.home_id), []).append(g)
    win = timedelta(hours=MATCH_WINDOW_H)
    span = schedule_span(games)
    # provisional id -> (schedule game_id, flipped) ; None when unmatched
    cache: dict[str, tuple[str, bool] | None] = {}
    unresolved: list[str] = []
    out: list[GameLine] = []
    for ln in lines:
        if ln.game_id in known_ids:
            out.append(ln)
            continue
        parts = parse_provisional(ln.game_id)
        if parts is None:
            continue
        stamp, away_raw, home_raw = parts
        if ln.game_id not in cache:
            hit: tuple[str, bool] | None = None
            away_id = _team_id(book, sport, away_raw)
            home_id = _team_id(book, sport, home_raw)
            when = _stamp_to_utc(stamp)
            if away_id and home_id:
                cands = [(g, False) for g in by_pair.get((away_id, home_id), [])]
                # swapped sides: neutral-site games or a book listing the home side first
                cands += [(g, True) for g in by_pair.get((home_id, away_id), [])]
                if when is not None:
                    cands = [(g, f) for g, f in cands if abs(g.kickoff_utc - when) <= win]
                if cands:
                    cands.sort(key=lambda gf: (gf[1], abs(gf[0].kickoff_utc - when) if when else timedelta(0)))
                    hit = (cands[0][0].game_id, cands[0][1])
            cache[ln.game_id] = hit
            if hit is None and in_span(when, span):
                unresolved.append(f"{ln.book}:{away_raw}@{home_raw}")
        hit = cache[ln.game_id]
        if hit is None:
            continue
        target, flipped = hit
        side = ln.side
        line = ln.line
        if flipped and ln.market in ("ml", "spread"):
            side = {"home": "away", "away": "home"}.get(side, side)
        out.append(dataclasses.replace(ln, game_id=target, side=side, line=line))
    return out, unresolved


def _external_merge(
    mod: Any,
    sport: str,
    games: Sequence[Game],
    lines: Sequence[GameLine],
    scrapers: dict[str, Any],
    openers: dict,
    ctx: RunContext,
) -> tuple[list[GameLine], list[str]] | None:
    """``pipeline.odds.merge.merge_odds`` (aliases + rapidfuzz + ±36 h + neutral flip).
    Openers are recorded into the passed dict (never overwritten) and saved by the
    caller. Returns ``(canonical lines, unresolved)`` where unresolved = team names
    the resolver could not map plus in-span book games without a schedule match;
    ``None`` on any failure so the built-in matcher takes over."""
    fn = getattr(mod, "merge_odds", None)
    if not callable(fn):
        return None
    try:
        raw_games: dict[str, Any] = {}
        rg_fn = getattr(mod, "raw_games_from_scraper", None)
        if callable(rg_fn):
            for name, scraper in scrapers.items():
                raw_games.update(rg_fn(scraper, name, sport))
        res = fn(sport, list(games), list(lines), raw_games or None, openers=openers, now=ctx.now_utc, save=False)
        matched = [ln for ln in getattr(res, "lines", []) if isinstance(ln, GameLine)]
        unresolved = [str(u) for u in (getattr(res, "unresolved", None) or [])]
        span = schedule_span(games)
        for row in getattr(res, "unmatched", None) or []:
            parts = str(row).split("|", 2)
            if len(parts) != 3:
                continue
            book, pid, reason = parts
            when = None
            rg = raw_games.get(pid)
            if rg is not None:
                when = getattr(rg, "kickoff_utc", None)
            elif (pp := parse_provisional(pid)) is not None:
                when = _stamp_to_utc(pp[0])
            if in_span(when, span):
                unresolved.append(f"{book}:{pid.split(':', 3)[-1]}:{reason}")
        return matched, unresolved
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("odds.merge", f"{sport}: merge_odds failed ({type(exc).__name__}: {exc}); using built-in matcher", "warn")
        return None


# ---- odds: consensus (fallback when model.fair is absent) ----------------------------

@dataclass(frozen=True)
class ConsensusLine:
    line: float
    odds: int | None
    n_books: int
    ref_book: str | None
    side: str  # the side whose line/odds this is (home for spread, under for total)


def weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    """Weighted median of ``[(value, weight)]`` (lower-middle on ties)."""
    pts = sorted((float(v), float(w)) for v, w in values if v is not None and w > 0)
    if not pts:
        return None
    half = sum(w for _, w in pts) / 2.0
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= half:
            return v
    return pts[-1][0]


def consensus_lines(sport: str, lines: Sequence[GameLine], weights: dict[str, float] | None = None) -> dict[tuple[str, str], ConsensusLine]:
    """``(game_id, market)`` -> weighted-median main line across books (spread: home side,
    total: under side). ``ref_book`` = heaviest book sitting on the consensus number."""
    w = weights or BOOK_WEIGHTS
    side_for = {"spread": "home", "total": "under"}
    by_key: dict[tuple[str, str], dict[str, GameLine]] = {}
    for ln in lines:
        if ln.sport != sport or not ln.is_main or ln.market not in side_for or ln.side != side_for[ln.market]:
            continue
        if ln.line is None or ln.book == CONSENSUS_BOOK:
            continue
        by_key.setdefault((ln.game_id, ln.market), {})[ln.book] = ln
    out: dict[tuple[str, str], ConsensusLine] = {}
    for key, per_book in by_key.items():
        med = weighted_median([(ln.line, w.get(b, 0.5)) for b, ln in per_book.items()])
        if med is None:
            continue
        on_line = [(w.get(b, 0.5), b, ln) for b, ln in per_book.items() if ln.line == med]
        on_line.sort(key=lambda t: -t[0])
        ref = on_line[0] if on_line else None
        out[key] = ConsensusLine(
            line=med, odds=ref[2].odds if ref else None, n_books=len(per_book),
            ref_book=ref[1] if ref else None, side=side_for[key[1]],
        )
    return out


def _external_consensus(mod: Any, sport: str, lines: Sequence[GameLine], ctx: RunContext) -> dict[tuple[str, str], ConsensusLine] | None:
    """``pipeline.model.fair.consensus(sport, game_lines, market)`` per game/market
    (Pinnacle-weighted median, devigged prob moved to the consensus line). The
    opener/legacy odds for the consensus row are the ref book's price on our side
    (home / under), else the devigged consensus prob converted to American."""
    fn = getattr(mod, "consensus", None)
    if not callable(fn):
        return None
    to_american = getattr(mod, "prob_to_american", None)
    side_for = {"spread": "home", "total": "under"}
    by_game: dict[str, list[GameLine]] = {}
    for ln in lines:
        if ln.sport == sport and ln.book != CONSENSUS_BOOK:
            by_game.setdefault(ln.game_id, []).append(ln)
    out: dict[tuple[str, str], ConsensusLine] = {}
    try:
        for game_id, glines in by_game.items():
            for market, side in side_for.items():
                c = fn(sport, glines, market)
                line = getattr(c, "line", None)
                if line is None:
                    continue
                ref_book = getattr(c, "ref_book", None)
                odds = next((ln.odds for ln in glines if ln.book == ref_book and ln.market == market
                             and ln.side == side and ln.is_main and ln.line == line), None)
                prob = getattr(c, "prob", None)
                if odds is None and prob is not None and callable(to_american):
                    p = prob if side == "home" else 1.0 - prob
                    odds = int(to_american(min(0.99, max(0.01, p))))
                out[(game_id, market)] = ConsensusLine(
                    line=float(line), odds=odds, n_books=int(getattr(c, "n_books", 0) or 0),
                    ref_book=ref_book, side=side,
                )
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("model.fair", f"{sport}: consensus failed ({type(exc).__name__}: {exc}); using built-in", "warn")
        return None
    return out


def consensus_pseudo_lines(consensus: dict[tuple[str, str], ConsensusLine], sport: str) -> list[dict[str, Any]]:
    """Consensus as ``book='consensus'`` rows so openers can track it like a book."""
    rows = []
    for (game_id, market), c in consensus.items():
        if c.odds is None:
            continue
        rows.append({"sport": sport, "game_id": game_id, "market": market, "side": c.side,
                     "book": CONSENSUS_BOOK, "line": c.line, "odds": c.odds})
    return rows


# ---- odds: legacy columns ------------------------------------------------------------

def _pick(lines_by_game: dict[str, list[GameLine]], game_id: str, book: str, market: str, side: str) -> GameLine | None:
    for ln in lines_by_game.get(game_id, []):
        if ln.book == book and ln.market == market and ln.side == side and ln.is_main:
            return ln
    return None


def _opener(openers: dict, game_id: str, market: str, side: str, book: str) -> dict | None:
    return pstate.get_opener(openers, pstate.odds_key(game_id, market, side, book))


def legacy_odds(
    sport: str,
    game_id: str,
    lines_by_game: dict[str, list[GameLine]],
    consensus: dict[tuple[str, str], ConsensusLine],
    openers: dict,
) -> dict[str, Any]:
    """Legacy odds dict for ``LegacyRecord.odds`` (keys read by ``outputs.legacy``).

    NFL: ``spread_now/odds_now`` = BetOnline home spread, ``total_now/under_now`` =
    BetOnline total + under price; ``*_open`` from the openers store for the same
    book. CFB: ``fd_now/odds_n`` = FanDuel total + under, ``current`` = FanDuel home
    spread, ``fd_open/odds_o/open`` from openers; ``spread``/``total_proj`` =
    consensus. When the reference book has no line the consensus (book
    ``'consensus'``) stands in and ``ref_book`` records which was used.
    """
    ref = LEGACY_NOW_BOOK[sport]
    out: dict[str, Any] = {}

    def now_and_open(market: str, side: str) -> tuple[GameLine | None, ConsensusLine | None, dict | None, str | None]:
        ln = _pick(lines_by_game, game_id, ref, market, side)
        if ln is not None:
            return ln, None, _opener(openers, game_id, market, side, ref), ref
        c = consensus.get((game_id, market))
        if c is not None:
            return None, c, _opener(openers, game_id, market, side, CONSENSUS_BOOK), CONSENSUS_BOOK
        return None, None, None, None

    sp_ln, sp_c, sp_open, sp_book = now_and_open("spread", "home")
    to_ln, to_c, to_open, to_book = now_and_open("total", "under")
    sp_line = sp_ln.line if sp_ln else (sp_c.line if sp_c else None)
    sp_odds = sp_ln.odds if sp_ln else (sp_c.odds if sp_c else None)
    to_line = to_ln.line if to_ln else (to_c.line if to_c else None)
    to_odds = to_ln.odds if to_ln else (to_c.odds if to_c else None)
    cons_sp = consensus.get((game_id, "spread"))
    cons_to = consensus.get((game_id, "total"))

    if sport == "nfl":
        out.update({
            "spread_now": sp_line, "odds_now": sp_odds, "total_now": to_line, "under_now": to_odds,
            "spread_open": sp_open.get("line") if sp_open else None,
            "odds_open": sp_open.get("odds") if sp_open else None,
            "total_open": to_open.get("line") if to_open else None,
            "under_open": to_open.get("odds") if to_open else None,
        })
    else:
        out.update({
            "fd_now": to_line, "odds_n": to_odds,
            "fd_open": to_open.get("line") if to_open else None,
            "odds_o": to_open.get("odds") if to_open else None,
            "current": sp_line,
            "open": sp_open.get("line") if sp_open else None,
        })
    out["spread"] = cons_sp.line if cons_sp else sp_line
    out["total_proj"] = cons_to.line if cons_to else to_line
    out["ref_book"] = sp_book or to_book
    out["spread_ref_book"] = cons_sp.ref_book if cons_sp else None
    out["total_ref_book"] = cons_to.ref_book if cons_to else None
    out["n_books"] = max(cons_sp.n_books if cons_sp else 0, cons_to.n_books if cons_to else 0)
    return out


@dataclass
class OddsResult:
    lines: list[GameLine]
    consensus: dict[tuple[str, str], ConsensusLine]
    openers: dict
    by_game: dict[str, list[GameLine]]
    unresolved: list[str]
    per_book: dict[str, int]
    scraped: list[GameLine] = dataclasses.field(default_factory=list)   # this run's lines (no carry-forward)
    deltas: list[GameLine] = dataclasses.field(default_factory=list)    # main lines whose (line, odds) moved -> D1
    new_opener_keys: list[str] = dataclasses.field(default_factory=list)


def stage_odds(
    ctx: RunContext,
    sport: str,
    games: list[Game],
    stadium_book: Any,
    raw: RawStore,
    books: Sequence[str],
    state_dir: Path,
    season: int | None,
    dry_run: bool = False,
    alerts: bool = True,
) -> OddsResult:
    if not books:
        return OddsResult([], {}, pstate.load_openers(state_dir), {}, [], {})
    raw_arg: RawStore | None = None if isinstance(raw, NullRawStore) else raw
    with ctx.stage(f"{sport}.scrape"):
        per_book, scrapers = _run_async(scrape_books(sport, books, raw_arg, ctx.run_id, ctx.degrade))
    all_lines: list[GameLine] = []
    per_book_n: dict[str, int] = {}
    for name in books:
        lines = per_book.get(name, [])
        per_book_n[name] = len(lines)
        all_lines.extend(lines)
        markets: dict[str, int] = {}
        for ln in lines:
            markets[ln.market] = markets.get(ln.market, 0) + 1
        for m, n in markets.items():
            ctx.count(name, f"{sport}.{m}", n)
        ctx.count(name, sport, len(lines))
        n_games = len({ln.game_id for ln in lines})
        detail = " ".join(f"{m}={markets[m]}" for m in ("spread", "total", "ml") if m in markets)
        print(f"  odds {name:<10} {sport}: {len(lines):>4} lines / {n_games:>3} games {detail}")
        if not lines:
            ctx.degrade("odds", f"{sport}: {name} returned 0 lines", "warn")

    # volume alert (edge-triggered; state persisted so a sustained drop pings once)
    with ctx.stage(f"{sport}.volume"):
        try:
            scope = volume_scope(sport, games, season)
            baseline = pstate.load_baseline(state_dir, scope)
            drops = check_scrape_volume(scrape_counts(all_lines), baseline, books)
            if not dry_run:
                pstate.save_baseline(state_dir, baseline)
            if drops:
                text = format_volume_alert(drops, scope)
                ctx.degrade("odds.volume", "; ".join(text.splitlines()[1:]), "warn")
                if alerts and not dry_run:
                    _send_alert(text)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("odds.volume", f"{sport}: volume check failed (non-fatal): {exc}", "warn")

    # provisional ids -> schedule game_id
    openers = pstate.load_openers(state_dir)
    with ctx.stage(f"{sport}.merge"):
        merge_mod = _import("pipeline.odds.merge")
        matched: tuple[list[GameLine], list[str]] | None = None
        if merge_mod is None:
            ctx.degrade("odds.merge", "pipeline.odds.merge not importable; using built-in matcher", "info")
        else:
            matched = _external_merge(merge_mod, sport, games, all_lines, scrapers, openers, ctx)
        if matched is None:
            matched = fallback_match(sport, games, all_lines, stadium_book)
        lines, unresolved = matched
        if not games:
            unresolved = []  # nothing on the schedule to match against: not a resolver problem
        if unresolved:
            uniq = sorted(set(unresolved))
            ctx.unresolved_names.extend(uniq)
            n_book_games = len({ln.game_id for ln in all_lines})
            ctx.degrade("odds.merge", f"{sport}: {len(uniq)} unresolved book games/names (of {n_book_games} book games)", "warn")
        ctx.count("merge", sport, len(lines))

    # Books not scraped this run (the Playwright job runs --books betonline only)
    # keep their last snapshot from archive_last so consensus / FD_now do not
    # collapse to a one-book view between the light and playwright commits.
    active_ids = {g.game_id for g in games}
    archive = pstate.load_archive_last(state_dir)
    carried = carry_forward_lines(archive, sport, active_ids, books)
    if carried:
        n_books = len({ln.book for ln in carried})
        print(f"  carried {sport}: {len(carried)} lines from {n_books} unscraped book(s) (archive_last)")
    board_lines = list(lines) + carried

    # consensus / fair
    with ctx.stage(f"{sport}.fair"):
        fair_mod = _import("pipeline.model.fair")
        consensus: dict[tuple[str, str], ConsensusLine] | None = None
        if fair_mod is None:
            ctx.degrade("model.fair", "pipeline.model.fair not importable; using built-in consensus", "info")
        else:
            consensus = _external_consensus(fair_mod, sport, board_lines, ctx)
        if consensus is None:
            consensus = consensus_lines(sport, board_lines)

    # state: openers (never overwritten), archive_last (last seen per key)
    with ctx.stage(f"{sport}.state"):
        now = utc_iso(ctx.now_utc)
        main_lines = [ln for ln in lines if ln.is_main]
        before_keys = set(openers.get("openers") or {})
        added = pstate.record_openers(openers, main_lines, now)
        added += pstate.record_openers(openers, consensus_pseudo_lines(consensus, sport), now)
        new_keys = sorted(set(openers.get("openers") or {}) - before_keys)
        pruned = pstate.prune_openers(openers, _active_for(openers.get("openers") or {}, sport, active_ids))
        last = archive.setdefault("last", {})
        deltas = d1_out.odds_deltas(main_lines, last)  # change-only set BEFORE last is advanced
        for ln in main_lines:
            last[ln.key] = {"line": ln.line, "odds": ln.odds, "ts": now}
        pstate.prune_archive_last(archive, _active_for(last, sport, active_ids))
        if not dry_run:
            pstate.save_openers(state_dir, openers)
            pstate.save_archive_last(state_dir, archive)
        print(f"  openers {sport}: +{added} new, {pruned} pruned, {len(openers.get('openers') or {})} total; "
              f"{len(deltas)} moved line(s) of {len(main_lines)}")

    by_game: dict[str, list[GameLine]] = {}
    for ln in board_lines:
        by_game.setdefault(ln.game_id, []).append(ln)
    ctx.count("odds", sport, len(lines))
    return OddsResult(board_lines, consensus, openers, by_game, unresolved, per_book_n,
                      scraped=list(lines), deltas=deltas, new_opener_keys=new_keys)


def _active_for(store: dict, sport: str, active_ids: set[str]) -> set[str]:
    """Prune only this sport's keys: the other sport's entries stay as they are."""
    return active_ids | {k.split("|", 1)[0] for k in store if not k.startswith(f"{sport}:")}


def carry_forward_lines(archive: dict, sport: str, active_ids: set[str], scraped_books: Sequence[str]) -> list[GameLine]:
    """Rebuild main lines for active games from ``archive_last`` for books NOT scraped
    this run. Never carries a book that was scraped (its fresh result, even empty, wins)."""
    scraped = set(scraped_books)
    out: list[GameLine] = []
    for key, val in (archive.get("last") or {}).items():
        parts = key.split("|")
        if len(parts) != 4:
            continue
        game_id, market, side, book = parts
        if book in scraped or book == CONSENSUS_BOOK or game_id not in active_ids or not game_id.startswith(f"{sport}:"):
            continue
        odds = val.get("odds") if isinstance(val, dict) else None
        if odds is None:
            continue
        try:
            out.append(GameLine(sport=sport, game_id=game_id, book=book, market=market, side=side,
                                odds=int(odds), line=val.get("line"), is_main=True))
        except (TypeError, ValueError):
            continue
    return out


# ---- record assembly ---------------------------------------------------------------

def _name_for(team: Team | None, team_id: str) -> str:
    if team is None:
        return team_id
    return team.name or team_id


def _month_key(month: int) -> str:
    return ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")[month - 1]


def build_record(
    sport: str,
    game: Game,
    stadium: Stadium | None,
    home_team: Team | None,
    away_team: Team | None,
    fc: WeatherForecast | None,
    travel_alt: float | None = None,
    home_temp: float | None = None,
    away_temp: float | None = None,
    roof_state: str | None = None,
    wind_avg: float | None = None,
    is_fbs: bool = True,
    odds: dict[str, Any] | None = None,
) -> tuple[LegacyRecord, ImpactV1, signals.Signal, list[str]]:
    """Pure assembly of one legacy row + impact + signal from resolved inputs."""
    tz = game.tz or (stadium.timezone if stadium and stadium.timezone else "America/New_York")
    kickoff_local = game.kickoff_local if game.kickoff_local.tzinfo is not None else to_tz(game.kickoff_utc, tz)

    if roof_state is None:
        roof_state = game.roof_state or (fc.roof_state if fc else None)
    if roof_state is None and stadium is not None and stadium.roof_type == "dome":
        roof_state = "dome"
    is_dome = roof_state in ("dome", "closed") or (stadium is not None and stadium.is_dome)

    temp_fg = fc.temp_fg if fc else None
    wind_fg = fc.wind_fg if fc else None
    rain_fg = fc.rain_fg_mm if fc else None

    impact = compute_impact_v1(
        sport=sport,
        month=now_et().month,  # rain suppression keys on the RUN month (legacy generator clock)
        temp_fg=temp_fg,
        wind_fg=wind_fg,
        rain_fg_mm=rain_fg,
        travel_alt_m=travel_alt,
        away_temp=away_temp,
        home_temp=home_temp,
        roof_state=roof_state,
        home_elev_m=stadium.elevation_m if stadium is not None else None,
    )

    avg_wind = 0.0 if is_dome else (stadium.avg_wind_static if stadium else None)
    avg_wind_month = wind_avg
    if avg_wind_month is None and stadium is not None and stadium.avg_wind_by_month:
        avg_wind_month = stadium.avg_wind_by_month.get(_month_key(kickoff_local.month))

    odds = dict(odds or {})
    spread_now = odds.get("current") if sport == "cfb" else odds.get("spread_now")
    if sport == "nfl":
        sig = signals.nfl_signal(wind_fg, temp_fg, rain_fg)
    else:
        sig = signals.cfb_signal(wind_fg, temp_fg, rain_fg, odds.get("open", spread_now), travel_alt, home_temp, away_temp, et_weekday())
    flags = signals.combined_flags(sport, wind_fg, temp_fg, odds.get("open", spread_now), travel_alt, home_temp, away_temp)

    rec = LegacyRecord(
        sport=sport,
        away_name=_name_for(away_team, game.away_id),
        home_name=_name_for(home_team, game.home_id),
        kickoff_local=kickoff_local,
        stadium_name=stadium.name if stadium else None,
        lat=stadium.lat if stadium else None,
        lon=stadium.lon if stadium else None,
        avg_wind=avg_wind,
        avg_wind_month=avg_wind_month,
        wind_vol=stadium.wind_vol_static if stadium else None,
        orient=stadium.orientation_bucket if stadium else None,
        wind_impact=stadium.wind_impact_static if stadium else None,
        weakest_wind_effect=stadium.weakest_wind_effect if stadium else None,
        travel_alt=travel_alt,
        home_temp=home_temp,
        away_temp=away_temp,
        year_built=stadium.year_built if stadium else None,
        wind_dir_1h=fc.wind_dir_1h if fc else None,
        wind_dir_2h=fc.wind_dir_2h if fc else None,
        temp_fg=temp_fg,
        wind_fg=wind_fg,
        wind_dir_fg=fc.wind_dir_fg if fc else None,
        rain_fg=rain_fg,
        gs_fg_pct=impact.gs_fg_pct,
        away_fg_pct=impact.away_fg_pct,
        odds=odds,
        is_fbs=is_fbs,
        game_id=game.game_id,
    )
    return rec, impact, sig, flags


def _is_fbs(book: Any, sport: str, home_id: str, away_id: str) -> bool:
    """CFB ``Other`` sheet = any side outside FBS (unknown classification counts as FBS)."""
    if sport != "cfb" or book is None:
        return True
    cls = getattr(book, "classification", {})
    for tid in (home_id, away_id):
        c = (cls.get((sport, tid)) or "").strip().lower()
        if c and c != "fbs":
            return False
    return True


@dataclass
class SportResult:
    """Everything one sport's run produced, for the legacy / JSON / D1 writers."""

    sport: str
    records: list[LegacyRecord]
    rows: list[dict[str, Any]]                       # slim print rows
    cards: list[dict[str, Any]]                      # GameCards (json_out.build_card)
    games: list[Game]
    stadiums: dict[str, Stadium]                     # game_id -> Stadium
    teams: dict[str, Team]                           # team_id -> Team
    forecasts: dict[str, WeatherForecast]
    impacts: dict[str, ImpactV1]
    odds: OddsResult
    fairs: dict[str, Any] = dataclasses.field(default_factory=dict)   # game_id -> model.fair.GameFair (ALERT_MODEL)
    wx_changed: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # weather_history rows to insert
    impacts_v2: dict[str, ImpactV2] = dataclasses.field(default_factory=dict)   # game_id -> v2 impact (always computed)
    fairs_v2: dict[str, Any] = dataclasses.field(default_factory=dict)          # game_id -> v2 GameFair (side by side)
    wx_extras: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)  # game_id -> merge side-outputs

    @property
    def season_week(self) -> tuple[int | None, int | None]:
        if not self.games:
            return None, None
        g = min(self.games, key=lambda x: x.kickoff_utc)
        return g.season, g.week


def _evaluate_fair(fair_mod: Any, ctx: RunContext, sport: str, game: Game, lines: Sequence[GameLine],
                   impact: Any, fc: WeatherForecast | None, stadium: Stadium | None,
                   model_version: str = "v1") -> Any | None:
    """``impact`` is an ImpactV1 or ImpactV2; edges are stamped with ``model_version``."""
    fn = getattr(fair_mod, "evaluate_game", None) if fair_mod is not None else None
    if not callable(fn) or not lines or impact is None:
        return None
    try:
        return fn(
            sport, game.game_id, list(lines), impact.gs_fg_pct, impact.away_fg_pct,
            rain_c=impact.rain_c,
            wind_vol_fc=fc.wind_vol_fc if fc else None,
            wind_vol_static=stadium.wind_vol_static if stadium else None,
            model_disagreement=fc.model_disagreement if fc else None,
            lead_hours=fc.lead_hours if fc else None,
            model_version=model_version,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("model.fair", f"{game.game_id}: evaluate_game[{model_version}] failed ({type(exc).__name__}: {exc})", "warn")
        return None


def _compute_v2(ctx: RunContext, sport: str, game: Game, rg: Any, fc: WeatherForecast | None,
                impact_v1: ImpactV1, extras: dict[str, Any] | None) -> ImpactV2 | None:
    """v2 impact (ARCH §7.5) from the merged forecast + stadium orientation / weak-wind set.
    Computed for every game regardless of ALERT_MODEL; failures degrade to None."""
    st: Stadium | None = rg.stadium if rg is not None else None
    extras = extras or {}
    try:
        fair_mod = _import("pipeline.model.fair")
        conf = None
        if fair_mod is not None and fc is not None:
            conf = fair_mod.confidence(fc.wind_vol_fc, fc.model_disagreement, fc.lead_hours,
                                       st.wind_vol_static if st else None)
        return compute_impact_v2(
            sport,
            fc.temp_fg if fc else None,
            fc.wind_fg if fc else None,
            fc.gust_fg if fc else None,
            fc.rain_fg_mm if fc else None,
            fc.precip_prob if fc else None,
            rg.travel_alt if rg is not None else None,
            rg.home_temp if rg is not None else None,
            rg.away_temp if rg is not None else None,
            wind_dir_deg=fc.wind_dir_deg if fc else None,
            wind_dir_fg=fc.wind_dir_fg if fc else None,
            orientation_deg=st.orientation_deg if st else None,
            weakest_wind_effect=st.weakest_wind_effect if st else None,
            precip_prob_ens=extras.get("precip_prob_ens"),
            roof_state=(rg.roof_state if rg is not None else None) or game.roof_state or (fc.roof_state if fc else None)
            or ("dome" if impact_v1.roof_closed else None),
            conf=conf,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("model.v2", f"{game.game_id}: compute_impact_v2 failed ({type(exc).__name__}: {exc})", "warn")
        return None


def _legacy_derived(fair_mod: Any, sport: str, game_odds: dict[str, Any], impact: ImpactV1) -> dict[str, Any] | None:
    fn = getattr(fair_mod, "legacy_derived", None) if fair_mod is not None else None
    if not callable(fn) or not game_odds:
        return None
    now_total = game_odds.get("fd_now") if sport == "cfb" else game_odds.get("total_now")
    try:
        return fn(game_odds.get("total_proj"), game_odds.get("spread"), now_total, impact.gs_fg_pct, impact.away_fg_pct)
    except Exception:  # noqa: BLE001
        return None


def update_histories(ctx: RunContext, sport: str, res: SportResult, state_dir: Path, dry_run: bool) -> None:
    """history.json (odds change-points + model fair) and wx_history.json /
    wx_last.json (weather change-points; ``res.wx_changed`` = D1 rows)."""
    now = utc_iso(ctx.now_utc)
    active = {g.game_id for g in res.games}
    with ctx.stage(f"{sport}.history"):
        hist = pstate.load_history(state_dir)
        n_pts = pstate.update_history(hist, [ln for ln in res.odds.scraped if ln.is_main], now)
        fairs: dict[str, float] = {}
        for gid, gf in res.fairs.items():
            for market, side in (("total", "over"), ("spread", "home")):
                val = getattr(gf, f"fair_{market}", None)
                if isinstance(val, (int, float)):
                    fairs[f"{gid}|{market}|{side}"] = float(val)
        n_fair = pstate.update_fair_history(hist, fairs, now)
        pstate.prune_history(hist, _active_for(hist.get("series") or {}, sport, active) | _active_for(hist.get("fair_series") or {}, sport, active))
        wx_hist = json_out.load_wx_history(state_dir)
        wx_last = d1_out.load_wx_last(state_dir)
        points = {gid: pt for gid, fc in res.forecasts.items() if (pt := json_out.wx_point(fc, res.impacts.get(gid))) is not None}
        changed = json_out.update_wx_history(wx_hist, points, now)
        json_out.prune_wx_history(wx_hist, _active_for(wx_hist.get("series") or {}, sport, active))
        rows = [d1_out.weather_row(fc, res.impacts.get(gid), now, ctx.run_id, impact_v2=res.impacts_v2.get(gid))
                for gid, fc in res.forecasts.items()]
        res.wx_changed = d1_out.weather_deltas(rows, wx_last)
        if not dry_run:
            pstate.save_history(state_dir, hist)
            json_out.save_wx_history(state_dir, wx_hist)
            d1_out.save_wx_last(state_dir, wx_last)
        print(f"  history {sport}: +{n_pts} odds points, +{n_fair} fair points, {len(changed)} weather change(s)")


def run_sport(
    ctx: RunContext,
    sport: str,
    raw: RawStore,
    season: int | None,
    books: Sequence[str] = (),
    state_dir: Path = DEFAULT_STATE_DIR,
    alerts: bool = True,
) -> SportResult:
    with ctx.stage(f"{sport}.stadiums"):
        book = stage_stadiums(ctx, sport)

    with ctx.stage(f"{sport}.schedule"):
        games = stage_schedule(ctx, sport, raw, season, book)

    with ctx.stage(f"{sport}.resolve"):
        stadiums: dict[str, Stadium] = {}
        roof_states: dict[str, str | None] = {}
        resolved: dict[str, Any] = {}
        unresolved: list[str] = []
        for g in games:
            rg = book.resolve(g, ctx) if book is not None else None
            resolved[g.game_id] = rg
            if rg is None or rg.stadium is None:
                unresolved.append(f"{g.game_id}:{g.stadium_id or g.home_id}")
            else:
                stadiums[g.game_id] = rg.stadium
                roof_states[g.game_id] = rg.roof_state
        if unresolved:
            ctx.unresolved_names.extend(unresolved)
            ctx.degrade("stadiums", f"{sport}: {len(unresolved)} games without stadium", "warn")

    wx_extras: dict[str, dict[str, Any]] = {}
    with ctx.stage(f"{sport}.weather"):
        forecasts = stage_weather(ctx, sport, games, stadiums, raw, roof_states, extras=wx_extras)

    odds = stage_odds(ctx, sport, games, book, raw, books, state_dir, season, dry_run=ctx.dry_run, alerts=alerts)

    records: list[LegacyRecord] = []
    rows: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    impacts: dict[str, ImpactV1] = {}
    impacts_v2: dict[str, ImpactV2] = {}
    fairs: dict[str, Any] = {}
    fairs_v2: dict[str, Any] = {}
    teams: dict[str, Team] = {}
    fair_mod = _import("pipeline.model.fair") if books else None
    alert_model = model_config.alert_model()
    with ctx.stage(f"{sport}.impact"):
        for g in games:
            rg = resolved[g.game_id]
            fc = forecasts.get(g.game_id)
            game_odds = legacy_odds(sport, g.game_id, odds.by_game, odds.consensus, odds.openers) if books else {}
            if rg is None:
                rec, impact, sig, flags = build_record(sport, g, None, None, None, fc, odds=game_odds)
                card_kwargs: dict[str, Any] = {}
            else:
                rec, impact, sig, flags = build_record(
                    sport, g, rg.stadium, rg.home_team, rg.away_team, fc,
                    travel_alt=rg.travel_alt, home_temp=rg.home_temp, away_temp=rg.away_temp,
                    roof_state=rg.roof_state, wind_avg=rg.wind_avg,
                    is_fbs=_is_fbs(book, sport, g.home_id, g.away_id), odds=game_odds,
                )
                for t in (rg.home_team, rg.away_team):
                    if t is not None:
                        teams[t.team_id] = t
                card_kwargs = {"travel_alt": rg.travel_alt, "home_temp": rg.home_temp, "away_temp": rg.away_temp,
                               "roof_state": rg.roof_state, "avg_wind_month": rec.avg_wind_month}
            records.append(rec)
            impacts[g.game_id] = impact
            game_lines = odds.by_game.get(g.game_id, [])
            impact2 = _compute_v2(ctx, sport, g, rg, fc, impact, wx_extras.get(g.game_id))
            impacts_v2[g.game_id] = impact2
            gf = _evaluate_fair(fair_mod, ctx, sport, g, game_lines, impact, fc, rg.stadium if rg else None)
            gf2 = _evaluate_fair(fair_mod, ctx, sport, g, game_lines, impact2, fc, rg.stadium if rg else None,
                                 model_version="v2")
            if gf2 is not None:
                fairs_v2[g.game_id] = gf2
            # ALERT_MODEL selects which fair/edges feed histories, D1 odds rows and alerts
            gf_alert = gf2 if alert_model == "v2" else gf
            if gf_alert is not None:
                fairs[g.game_id] = gf_alert
            cards.append(json_out.build_card(
                sport, g, rg.stadium if rg else None, rg.home_team if rg else None, rg.away_team if rg else None,
                fc, impact, sig, flags, lines=game_lines, openers=odds.openers, consensus=odds.consensus, fair=gf_alert,
                legacy_derived=_legacy_derived(fair_mod, sport, game_odds, impact), avg_wind=rec.avg_wind,
                run_id=ctx.run_id, impact_v2=impact2, fair_v2=gf2, model_version=alert_model, **card_kwargs,
            ))
            rows.append({
                "game_id": g.game_id,
                "game": f"{rec.away_name} @ {rec.home_name}",
                "kickoff": rec.kickoff_local.isoformat(),
                "temp_fg": rec.temp_fg,
                "wind_fg": rec.wind_fg,
                "rain_fg": rec.rain_fg,
                "gs_fg_pct": impact.gs_fg_pct,
                "away_fg_pct": impact.away_fg_pct,
                "gs_fg_v2": impact2.gs_fg_pct if impact2 else None,
                "away_fg_v2": impact2.away_fg_pct if impact2 else None,
                "signal": sig.label,
                "flags": flags,
                "spread": game_odds.get("spread"),
                "total": game_odds.get("total_proj"),
                "ref_book": game_odds.get("ref_book"),
            })
    if books:
        priced = sum(1 for r in records if r.odds.get("total_proj") is not None)
        ctx.count("priced", sport, priced)
        if records and priced == 0:
            ctx.degrade("odds", f"{sport}: no game received a consensus total", "warn")
    ctx.count("legacy", sport, len(records))
    res = SportResult(sport, records, rows, cards, games, stadiums, teams, forecasts, impacts, odds, fairs,
                      impacts_v2={k: v for k, v in impacts_v2.items() if v is not None}, fairs_v2=fairs_v2,
                      wx_extras=wx_extras)
    ctx.count("impact_v2", sport, len(res.impacts_v2))
    n_ens = sum(1 for e in wx_extras.values() if e.get("ensemble"))
    if forecasts and n_ens == 0:
        ctx.degrade("weather", f"{sport}: no game has ensemble spread; wind_vol_fc static for all", "info")
    update_histories(ctx, sport, res, state_dir, ctx.dry_run)
    return res


# ---- CLI ------------------------------------------------------------------------

def run_alert_stage(ctx: RunContext, results: Sequence[SportResult], state_dir: Path, *, enabled: bool, dry_run: bool,
                    stdout: bool = False, now: datetime | None = None) -> alerts_mod.AlertsRun | None:
    """EDGE / MOVE / GONE / WX / OPENERS / OPS alerts (pipeline/alerts.py) over this
    run's GameCards; stamps ``card["alerts"]`` with the open keys per game. Never
    fatal: a failure is a warn Degradation and the board still publishes."""
    with ctx.stage("alerts"):
        try:
            run = alerts_mod.run_alerts(
                ctx, {r.sport: r.cards for r in results}, state_dir, enabled=enabled, dry_run=dry_run,
                new_keys_by_sport={r.sport: r.odds.new_opener_keys for r in results},
                sender=alerts_mod.print_sender() if stdout else None, now=now,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("alerts", f"alert stage failed (non-fatal): {type(exc).__name__}: {exc}", "warn")
            return None
        for r in results:
            for card in r.cards:
                card["alerts"] = run.keys_for(card.get("game_id") or "")
        return run


def run_clv_stage(ctx: RunContext, results: Sequence[SportResult], state_dir: Path, *, dry_run: bool,
                  alerts_run: alerts_mod.AlertsRun | None = None) -> clv_mod.ClvResult | None:
    """Freeze closing lines for games that kicked off (state history.json -> closings.json)
    and settle EDGE alerts with their CLV (pipeline/model/clv.py). Never fatal."""
    cards = [c for r in results for c in r.cards]
    if not cards:
        return None
    with ctx.stage("clv"):
        try:
            res = clv_mod.run_clv_stage(state_dir, cards, ctx.now_utc, run_id=ctx.run_id, dry_run=dry_run,
                                        alerts=alerts_run.alerts if alerts_run is not None else None)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("clv", f"clv stage failed (non-fatal): {type(exc).__name__}: {exc}", "warn")
            return None
        if res.settled and alerts_run is not None and not dry_run:
            pstate.save_alerts(state_dir, alerts_run.alerts)   # settle stamps live on the alert-stage dict
        if res.new or res.settled:
            print(f"  clv: {len(res.new)} closing(s) frozen, {len(res.settled)} alert(s) settled")
        return res


def _print_cards(cards: list[dict[str, Any]]) -> None:
    for c in cards:
        odds = ""
        if c.get("total") is not None or c.get("spread") is not None:
            odds = f" sp={c.get('spread')!s:>5} tot={c.get('total')!s:>5} [{c.get('ref_book') or '-'}]"
        print(
            f"  {c['game']:<45} {c['kickoff']:<25} temp={c['temp_fg']!s:>6} wind={c['wind_fg']!s:>6} "
            f"rain={c['rain_fg']!s:>5} gs={c['gs_fg_pct']:+.2f} away={c['away_fg_pct']:+.2f} {c['signal']} {' '.join(c['flags'])}{odds}"
        )


def _load_prev_meta(state_dir: Path) -> dict[str, Any] | None:
    p = Path(state_dir) / PREV_META_FILE
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def d1_statements(ctx: RunContext, results: Sequence[SportResult], finished_at: datetime,
                  alert_records: Sequence[dict[str, Any]] = (), n_alerts: int = 0,
                  closings: Sequence[dict[str, Any]] = ()) -> list[str]:
    """Upserts for games/stadiums/teams, change-only odds/weather history, new openers,
    alerts touched this run (first_* frozen), this run."""
    now = utc_iso(finished_at)
    games: list[dict[str, Any]] = []
    stadiums: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    odds_rows: list[dict[str, Any]] = []
    wx_rows: list[dict[str, Any]] = []
    opener_rows: list[dict[str, Any]] = []
    n_games = n_lines = 0
    seasons: list[tuple[int, int]] = []
    for res in results:
        games += d1_out.game_rows(res.games, now, impacts=res.impacts, impacts_v2=res.impacts_v2)
        stadiums += d1_out.stadium_rows(res.stadiums.values(), now)
        teams += d1_out.team_rows(res.teams.values(), now)
        edges = [e for gf in res.fairs.values() for e in (getattr(gf, "edges", None) or [])]
        odds_rows += d1_out.odds_rows(res.odds.deltas, now, ctx.run_id, edges)
        wx_rows += res.wx_changed
        opener_rows += d1_out.opener_rows(res.odds.openers, res.odds.new_opener_keys, ctx.run_id)
        n_games += len(res.games)
        n_lines += len(res.odds.scraped)
        s, w = res.season_week
        if s is not None and w is not None:
            seasons.append((s, w))
    season, week = min(seasons) if seasons else (None, None)
    run = d1_out.run_row(ctx, season=season, week=week, finished_at=finished_at, n_games=n_games, n_lines=n_lines,
                         n_alerts=n_alerts)
    return d1_out.build_statements(games=games, stadiums=stadiums, teams=teams, odds=odds_rows,
                                   weather=wx_rows, openers=opener_rows, runs=[run],
                                   alerts=d1_out.alert_rows(alert_records), closings=closings)


def write_outputs(
    ctx: RunContext,
    results: Sequence[SportResult],
    book_list: Sequence[str],
    *,
    board_dir: Path,
    snapshot_dir: Path,
    state_dir: Path,
    d1_sql: Path,
    raw_files: dict[str, Path],
    finished_at: datetime | None = None,
    alerts_run: alerts_mod.AlertsRun | None = None,
    clv_run: clv_mod.ClvResult | None = None,
) -> dict[str, Path]:
    """Board JSON (meta last), snapshots, D1 SQL and the publish manifest
    ``{r2 key: local path}`` (written to ``board_dir/../publish_manifest.json``)."""
    finished = finished_at or ctx.now_utc
    prev_meta = _load_prev_meta(state_dir)
    cards_by_sport = {r.sport: r.cards for r in results}
    sport_counts = {r.sport: len(r.cards) for r in results}
    baselines: dict[str, dict] = {}
    for r in results:
        try:
            baselines[r.sport] = pstate.load_baseline(state_dir, volume_scope(r.sport, r.games, None))
        except Exception:  # noqa: BLE001
            continue
    books = json_out.books_status(ctx.counts, book_list, baselines, utc_iso(finished),
                                  previous=(prev_meta or {}).get("books"))
    seasons = [r.season_week for r in results if r.season_week[0] is not None]
    season, week = min(seasons) if seasons else (None, None)
    meta = json_out.build_meta(ctx, sport_counts, books, season=season, week=week, finished_at=finished,
                               extra={"books_requested": list(book_list), "n_lines": sum(len(r.odds.scraped) for r in results)})
    hist = pstate.load_history(state_dir)
    wx_hist = json_out.load_wx_history(state_dir)
    alerts_state = alerts_run.alerts if alerts_run is not None else pstate.load_alerts(state_dir)
    alert_records = alerts_run.outcome.records if alerts_run is not None else []
    n_alerts = alerts_run.n_alerts if alerts_run is not None else 0
    seasons_ = [r.season_week for r in results if r.season_week[0] is not None]
    run = d1_out.run_row(ctx, season=min(seasons_)[0] if seasons_ else None, week=min(seasons_)[1] if seasons_ else None,
                         finished_at=finished, n_games=sum(len(r.games) for r in results),
                         n_lines=sum(len(r.odds.scraped) for r in results), n_alerts=n_alerts)
    feed = json_out.build_alerts_feed(alerts_state, meta)
    status = json_out.build_status(meta, run, previous=json_out.load_previous_status(state_dir), books=books)
    files = json_out.write_board(board_dir, cards_by_sport, meta, history=hist, wx_history=wx_hist, snapshots_dir=snapshot_dir,
                                 alerts_feed=feed, status=status)
    json_out.dump_json(Path(state_dir) / json_out.STATUS_FILE, status)  # rolling runs list for the next build

    stmts = d1_statements(ctx, results, finished, alert_records=alert_records, n_alerts=n_alerts,
                          closings=clv_run.new_rows if clv_run is not None else ())
    sql_path = d1_out.write_sql(d1_sql, stmts)
    print(f"  d1: {len(stmts)} statement(s) -> {sql_path or '(nothing to execute)'}")

    manifest: dict[str, Path] = {}
    manifest.update(raw_files)
    for k, p in files.items():
        if k != r2_out.META_KEY:
            manifest[k] = p
    for name in r2_out.STATE_FILES:
        p = Path(state_dir) / f"{name}.json"
        if p.is_file() and f"{r2_out.BOARD_PREFIX}/{name}.json" not in manifest:
            manifest[f"{r2_out.BOARD_PREFIX}/{name}.json"] = p
    manifest[r2_out.META_KEY] = files[r2_out.META_KEY]  # LAST
    json_out.dump_json(Path(board_dir).parent / PUBLISH_MANIFEST, {k: str(v) for k, v in manifest.items()}, indent=2)
    return manifest


def publish_outputs(ctx: RunContext, manifest: dict[str, Path], state_dir: Path, force: bool = False) -> bool:
    """boto3 publish (payloads -> state -> meta last) + self-check. Returns False
    (and records an error Degradation) on any failure so the job fails loudly."""
    cfg = r2_out.config_from_env()
    if cfg is None:
        print("  publish: R2 not configured (CF_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY); "
              "relying on the workflow's wrangler put loop")
        return True
    try:
        client = r2_out.make_client(cfg)
        pushed = r2_out.publish(client, cfg.bucket, manifest)
        print(f"  publish: {len(pushed)} objects -> r2://{cfg.bucket} (last={pushed[-1] if pushed else '-'})")
        problems = r2_out.self_check(ctx.run_id, prev_meta_file=Path(state_dir) / PREV_META_FILE, force=force, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("publish", f"R2 publish failed: {type(exc).__name__}: {exc}", "error")
        return False
    for pr in problems:
        ctx.degrade("publish", f"self-check: {pr}", "error")
    return not problems


def fetch_state_from_r2(ctx: RunContext, state_dir: Path) -> bool:
    """``--merge-into-r2``: pull the live state + previous meta before building so
    openers/history continue from R2, not from an empty checkout."""
    cfg = r2_out.config_from_env()
    if cfg is None:
        print("  merge-into-r2: R2 not configured; using local state dir as-is")
        return True
    try:
        client = r2_out.make_client(cfg)
        got = r2_out.get_state(client, cfg.bucket, state_dir)
        meta = r2_out.get_object(client, cfg.bucket, r2_out.META_KEY)
        if meta is not None:
            (Path(state_dir) / PREV_META_FILE).write_bytes(meta)
        print(f"  merge-into-r2: fetched {sum(1 for p in got.values() if p)} state file(s)")
        return True
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("r2.state", f"state fetch failed: {type(exc).__name__}: {exc}", "error")
        return False


def build(
    sports: Sequence[str],
    scope: str = "weather",
    out_dir: Path = DEFAULT_OUT_DIR,
    legacy_dir: Path | None = None,
    raw_dir: Path = DEFAULT_BASE,
    dry_run: bool = False,
    print_rows: bool = False,
    run_id: str | None = None,
    season: int | None = None,
    started_at: datetime | None = None,
    books: Sequence[str] | None = None,
    state_dir: Path = DEFAULT_STATE_DIR,
    alerts: bool = True,
    alerts_stdout: bool = False,
    board_dir: Path = DEFAULT_BOARD_DIR,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    d1_sql: Path = DEFAULT_D1_SQL,
    publish: bool = False,
    merge_into_r2: bool = False,
    force: bool = False,
) -> int:
    ctx = RunContext(sport="all" if len(sports) > 1 else sports[0], scope=scope, dry_run=dry_run,
                     run_id=run_id or "", **({"started_at": started_at} if started_at else {}))
    try:
        book_list = books_for_scope(scope, books)
    except ValueError as exc:
        ctx.degrade("scope", str(exc), "error")
        book_list = []
    if scope != "weather":
        print(f"scope={scope} books={','.join(book_list) or '-'} state={state_dir}")
    if merge_into_r2 and not dry_run:
        if not fetch_state_from_r2(ctx, state_dir):
            for line in ctx.summary_lines():
                print(line)
            return 1
    mirror = None
    if (publish or merge_into_r2) and not dry_run:
        cfg = r2_out.config_from_env()
        if cfg is not None:
            try:
                mirror = r2_out.raw_mirror(r2_out.make_client(cfg), cfg.bucket)
            except Exception as exc:  # noqa: BLE001
                ctx.degrade("r2.raw", f"raw mirror unavailable: {exc}", "warn")

    timestamp = naive_et_iso(ctx.started_at)
    written: list[Path] = []
    results: list[SportResult] = []
    raw_files: dict[str, Path] = {}
    for sport in sports:
        raw: RawStore = NullRawStore(sport, ctx.run_id) if dry_run else RawStore(sport, ctx.run_id, raw_dir, mirror=mirror)
        res = run_sport(ctx, sport, raw, season, books=book_list, state_dir=state_dir, alerts=alerts)
        results.append(res)
        if print_rows:
            print(f"== {sport} ({len(res.records)} games)")
            _print_cards(res.rows)
        if dry_run:
            continue
        with ctx.stage(f"{sport}.legacy"):
            path = write_legacy(sport, res.records, out_dir, timestamp)
            written.append(path)
            if legacy_dir is not None:
                dest = Path(legacy_dir) / (NFL_FILENAME if sport == "nfl" else CFB_FILENAME)
                if dest.resolve() != path.resolve():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, dest)
                    written.append(dest)
            raw.finalize()
            raw_files.update(raw.r2_files())

    alerts_run = run_alert_stage(ctx, results, state_dir, enabled=alerts, dry_run=dry_run, stdout=alerts_stdout) if book_list else None
    clv_run = run_clv_stage(ctx, results, state_dir, dry_run=dry_run, alerts_run=alerts_run) if book_list else None

    if not dry_run:
        with ctx.stage("outputs"):
            manifest = write_outputs(ctx, results, book_list, board_dir=board_dir, snapshot_dir=snapshot_dir,
                                     state_dir=state_dir, d1_sql=d1_sql, raw_files=raw_files, alerts_run=alerts_run,
                                     clv_run=clv_run)
            written.extend(p for k, p in manifest.items() if k.startswith(r2_out.BOARD_PREFIX))
        if publish or merge_into_r2:
            with ctx.stage("publish"):
                publish_outputs(ctx, manifest, state_dir, force=force)

    for line in ctx.summary_lines():
        print(line)
    for book_name, per in ctx.counts.items():
        if book_name in BOOK_REGISTRY:
            print(f"  count {book_name:<10} " + " ".join(f"{k}={v}" for k, v in per.items()))
    for p in written:
        print(f"  wrote {p}")
    if ctx.unresolved_names:
        print(f"  unresolved ({len(ctx.unresolved_names)}): {', '.join(ctx.unresolved_names[:20])}")
    errors = [d for d in ctx.degradations if d.severity == "error"]
    return 1 if errors else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.build", description=__doc__.split("\n\n")[0])
    p.add_argument("--sport", choices=("nfl", "cfb", "all"), default="all")
    p.add_argument("--scope", choices=SCOPES, default="weather")
    p.add_argument("--books", default=None, help="comma-separated books (restricts light/full; required set for odds)")
    p.add_argument("--print", dest="print_rows", action="store_true", help="print per-game rows")
    p.add_argument("--dry-run", action="store_true", help="fetch + compute but write nothing (no state either)")
    p.add_argument("--no-alerts", dest="alerts", action="store_false", help="never send Telegram; alert candidates are printed with their keys")
    p.add_argument("--alerts-stdout", action="store_true",
                   help="run the alert stage for real (dedup / alerts.json marked) but print messages instead of sending to Telegram")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where legacy files are written (default data/)")
    p.add_argument("--legacy-dir", type=Path, default=None, help="also copy legacy files here (e.g. repo root for Streamlit)")
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_BASE)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="openers/archive_last/scrape_baseline (default data/state/)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--board-dir", type=Path, default=DEFAULT_BOARD_DIR, help="board JSON payloads (R2 board/ prefix)")
    p.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR, help="per-run GameCard snapshots (R2 snapshots/)")
    p.add_argument("--d1-sql", type=Path, default=DEFAULT_D1_SQL, help="SQL file for 'wrangler d1 execute --file'")
    p.add_argument("--publish", action="store_true", help="push raw/board/state/meta to R2 via boto3 (needs R2_* env) + self-check")
    p.add_argument("--merge-into-r2", dest="merge_into_r2", action="store_true",
                   help="Playwright job: fetch R2 state first, then build + publish (meta last)")
    p.add_argument("--force", action="store_true", help="skip the publish content floor")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sports = list(SPORTS_ALL) if args.sport == "all" else [args.sport]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    return build(
        sports,
        scope=args.scope,
        out_dir=args.out_dir,
        legacy_dir=args.legacy_dir,
        raw_dir=args.raw_dir,
        dry_run=args.dry_run,
        print_rows=args.print_rows,
        run_id=args.run_id,
        season=args.season,
        books=[b for b in args.books.split(",")] if args.books else None,
        state_dir=args.state_dir,
        alerts=args.alerts,
        alerts_stdout=args.alerts_stdout,
        board_dir=args.board_dir,
        snapshot_dir=args.snapshot_dir,
        d1_sql=args.d1_sql,
        publish=args.publish,
        merge_into_r2=args.merge_into_r2,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
