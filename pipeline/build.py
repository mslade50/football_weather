"""Pipeline orchestrator (Phase 2): schedule -> stadiums -> weather -> impact v1 -> signals -> odds -> board JSON + D1 SQL.

    python -m pipeline.build --sport all --scope full --print [--dry-run]
    python -m pipeline.build --sport nfl --scope odds --books betonline

Scopes:
    weather  schedule/stadiums/weather/impact only (Phase 1 behaviour; odds columns NaN)
    light    weather + every httpx book (no Playwright)
    exchanges weather + enabled exchange books only
    full     light + Playwright books (BetOnline)
    odds     weather + only the books given by ``--books`` (default: all books);
             used by the Playwright CI job to refresh the board with BetOnline lines

Odds stage: scrapers run concurrently with ``asyncio.gather(return_exceptions=True)``
(one failing/hanging book never blocks the others), per-book/market counts are
recorded on the RunContext, ``check_scrape_volume`` (adapted from
golf_scraping/board/build.py ``_check_scrape_volume``) records a data issue when a
book goes dark or a (book|market) count collapses; the unified alert stage decides
whether and how to notify. Provisional book game ids are mapped
onto schedule ``game_id``s (``pipeline.odds.merge``), a consensus is computed
(``pipeline.model.fair``) and openers/archive_last/scrape_baseline are persisted under
``data/state/``. CFB total openers use the line in force at kickoff minus six days;
other opener fields remain first-seen.

Two horizons (decoupled): weather / impact / cards / alerts cover
games kicking off within [now-6h, now+10d] (``WINDOW_AFTER_D``); the odds stage
matches scraped lines against every schedule game within [now-6h, now+45d]
(``ODDS_WINDOW_AFTER_D``, same number as ``gate_check.HORIZON_DAYS``) so openers,
line history, archive_last and D1 ``odds_history`` / ``games`` rows start the day a
book first posts a line, weeks before the game has a card. A failing stage is
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

Alerts (``pipeline/alerts.py``, any odds scope): first notices, scheduled
reminders, and confirmed-bet CLEAR candidates use ``pipeline/alert_policy.py``.
Delivery is retired by default behind ``FOOTBALL_TELEGRAM_ENABLED``. Successful
delivery alone records dedup markers and feed entries in ``alerts.json``
(rehydrated from ``alerts_export.json`` when missing).
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
import os
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline import alerts as alerts_mod
from pipeline import state as pstate
from pipeline.contracts import Game, GameLine, Stadium, Team, WeatherForecast, actionable_lines
from pipeline.model import clv as clv_mod
from pipeline.model import config as model_config
from pipeline.model import fair as fair_mod
from pipeline.model import signals
from pipeline.model.config import BOOK_WEIGHTS, DEFAULT_BOOK_WEIGHT
from pipeline.model.impact import ImpactV1, ImpactV2, compute_impact_v1, compute_impact_v2
from pipeline.odds import handoff
from pipeline.odds import merge as odds_merge
from pipeline.odds.main_select import select_main_lines
from pipeline.odds.weekly_openers import record_weekly_totals
from pipeline.outputs import d1_out, json_out
from pipeline.outputs import r2 as r2_out
from pipeline.outputs.raw_out import DEFAULT_BASE, NullRawStore, RawStore
from pipeline.run_context import REPO_ROOT, RunContext
from pipeline.schedule import cfb as cfb_schedule
from pipeline.schedule import nfl as nfl_schedule
from pipeline.scrape_outcomes import ScrapeOutcome, ScrapeResult, scraper_result
from pipeline.stadiums import loader as stadium_loader
from pipeline.stadiums.roofs import resolve_roof_state, weather_exposed
from pipeline.weather import ecmwf_fallback as fallback_mod
from pipeline.weather import first_pass, point_handoff, screening, selective
from pipeline.weather import merge as merge_mod
from pipeline.weather import nws as nws_mod
from pipeline.weather import openmeteo as om_mod
from utils.env import load_repo_dotenv
from utils.timeutil import now_et, to_tz, utc_iso

logger = logging.getLogger(__name__)

SPORTS_ALL = ("nfl", "cfb")
SCOPES = ("weather", "light", "full", "odds", "exchanges")
DEFAULT_STATE_DIR = REPO_ROOT / "data" / "state"
DEFAULT_BOARD_DIR = REPO_ROOT / "data" / "board"
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"
DEFAULT_D1_SQL = REPO_ROOT / "data" / d1_out.D1_SQL_FILENAME
PUBLISH_MANIFEST = "publish_manifest.json"
PREV_META_FILE = "prev_meta.json"

# Weather window: games outside it get no forecast / impact / card / alert.
WINDOW_BEFORE_H = 6.0
WINDOW_AFTER_D = 10.0
# Odds horizon: books post NFL week 1+ and CFB weeks ahead long before the weather
# window opens, so lines are matched (openers / history / archive_last / D1) against
# every schedule game within [now-6h, now+ODDS_WINDOW_AFTER_D]. gate_check.HORIZON_DAYS
# must carry the same number (pinned by tests/test_gate_check.py).
ODDS_WINDOW_AFTER_D = 45.0
CFB_TOTAL_BASELINE_DAYS = 6
# Carry quotes between the light and BetOnline jobs, not between hours-old runs.
ODDS_CARRY_MAX_AGE = timedelta(hours=1)

# ---- books ------------------------------------------------------------------------
# book -> (module, class); order = display / alert order.
BOOK_REGISTRY: dict[str, tuple[str, str]] = {
    "pinnacle": ("pipeline.odds.pinnacle", "PinnacleScraper"),
    "betcris": ("pipeline.odds.betcris", "BetcrisScraper"),
    "fanduel": ("pipeline.odds.fanduel", "FanDuelScraper"),
    "kalshi": ("pipeline.odds.kalshi", "KalshiScraper"),
    "novig": ("pipeline.odds.novig", "NovigScraper"),
    "prophetx": ("pipeline.odds.prophetx", "ProphetXScraper"),
    "polymarket_us": ("pipeline.odds.polymarket_us", "PolymarketUSScraper"),
    "betonline": ("pipeline.odds.betonline", "BetOnlineScraper"),
}
PLAYWRIGHT_BOOKS = ("betonline",)
HTTPX_BOOKS = ("pinnacle", "betcris", "fanduel", "kalshi", "novig", "prophetx", "polymarket_us")
EXCHANGE_BOOKS = ("kalshi", "novig", "prophetx", "polymarket_us")
BOOK_ORDER = HTTPX_BOOKS + PLAYWRIGHT_BOOKS
# Golf's SIM_BOOKS: a book at 0 rows while >=2 of these report is "dark", not "no market".
CRITICAL_BOOKS = frozenset({"pinnacle", "betonline", "betcris", "fanduel"})
# ARCH §7.3 consensus weights.
CONSENSUS_BOOK = "consensus"

SCRAPE_TIMEOUT_S = 300.0     # per book incl. retries (BetOnline: Chromium + CF pass)
VOLUME_DROP_FRAC = 0.5       # alert when current < 50% of the peak
VOLUME_MIN_PEAK = 8          # ignore small markets (noise floor)
VOLUME_DARK_MIN_ROWS = 10    # a peer counts as healthy at >= this many rows


def default_season(now: datetime) -> int:
    """NFL/CFB season year: Jan/Feb belong to the previous season."""
    return now.year - 1 if now.month <= 2 else now.year


def in_window(kickoff_utc: datetime, now: datetime, after_d: float = WINDOW_AFTER_D) -> bool:
    delta_h = (kickoff_utc - now).total_seconds() / 3600.0
    return -WINDOW_BEFORE_H <= delta_h <= after_d * 24.0


def in_odds_horizon(kickoff_utc: datetime, now: datetime) -> bool:
    return in_window(kickoff_utc, now, ODDS_WINDOW_AFTER_D)


def split_schedule(games: Sequence[Game], now: datetime) -> tuple[list[Game], list[Game]]:
    """``(window games, odds-horizon games)`` — the first is a subset of the second."""
    horizon = [g for g in games if in_odds_horizon(g.kickoff_utc, now)]
    window = [g for g in horizon if in_window(g.kickoff_utc, now)]
    return window, horizon


def entered_window_ids(games: Sequence[Game], prev_finished: datetime | None, now: datetime) -> set[str]:
    """Window games whose window entry (kickoff − WINDOW_AFTER_D) fell after the previous
    run: their openers were recorded weeks ago (odds horizon) but are new *to the board*,
    which is what the OPENERS digest is about. No previous run: every window game."""
    if prev_finished is None:
        return {g.game_id for g in games}
    lead = timedelta(days=WINDOW_AFTER_D)
    return {g.game_id for g in games if prev_finished < g.kickoff_utc - lead <= now}


def books_for_scope(scope: str, books: Sequence[str] | None = None) -> list[str]:
    """Which books a scope scrapes. ``--books`` restricts any scope; ``odds`` defaults to all."""
    if scope == "weather":
        return []
    if scope == "light":
        pool: tuple[str, ...] = HTTPX_BOOKS
    elif scope == "exchanges":
        pool = EXCHANGE_BOOKS
    else:
        pool = BOOK_ORDER
    if books:
        wanted = [b.strip().lower() for b in books if b.strip()]
        unknown = [b for b in wanted if b not in BOOK_REGISTRY]
        if unknown:
            raise ValueError(f"unknown book(s): {', '.join(unknown)}")
        chosen = wanted if scope == "odds" else [b for b in wanted if b in pool]
    else:
        chosen = list(pool)
    # BOOK_<NAME>_ENABLED=0 (repo variable / .env) drops a book from the run entirely:
    # no scrape, no "returned 0 lines" degradation, not judged by the volume check.
    return [b for b in chosen if book_enabled(b)]


def book_enabled(name: str) -> bool:
    return os.environ.get(f"BOOK_{name.upper()}_ENABLED", "1").strip() != "0"


# ---- stages ---------------------------------------------------------------------

def stage_stadiums(ctx: RunContext, sport: str) -> Any | None:
    try:
        return stadium_loader.load_stadium_book(sports=(sport,))
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("stadiums", f"{type(exc).__name__}: {exc}", "error")
        return None


def fetch_schedule(ctx: RunContext, sport: str, raw: RawStore, season: int | None, book: Any) -> list[Game]:
    """Every non-final game of the season the source knows about (nflverse games.csv
    and CFBD ``/games?year`` are full-season; the ESPN scoreboard fallback is only
    the current slate, so the odds horizon simply sees fewer games)."""
    season = season or default_season(ctx.started_et)
    raw_dir = None if isinstance(raw, NullRawStore) else raw.run_dir
    try:
        if sport == "nfl":
            games = nfl_schedule.fetch_nfl_schedule(season, book=book, raw_dir=raw_dir)
        else:
            games = cfb_schedule.fetch_cfb_schedule(season, book=book, raw_dir=raw_dir, ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - any stage failure becomes a Degradation
        ctx.degrade("schedule", f"{sport}: {type(exc).__name__}: {exc}", "error")
        return []
    return [g for g in (games or []) if isinstance(g, Game)]


def stage_schedule(ctx: RunContext, sport: str, raw: RawStore, season: int | None, book: Any) -> tuple[list[Game], list[Game]]:
    """``(window games, odds-horizon games)``. An empty window with games on the odds
    horizon (NFL preseason, CFB August) is expected — lines-only runs — so it is an
    ``info`` degradation; ``warn`` only when nothing is upcoming at all."""
    games = fetch_schedule(ctx, sport, raw, season, book)
    window, horizon = split_schedule(games, ctx.now_utc)
    ctx.count("schedule", sport, len(window))
    ctx.count("schedule", f"{sport}.odds", len(horizon))
    if not window:
        ctx.degrade(
            "schedule",
            f"{sport}: 0 games within the {WINDOW_AFTER_D:.0f}-day window ({len(horizon)} within the "
            f"{ODDS_WINDOW_AFTER_D:.0f}-day odds horizon, {len(games)} in season {season or default_season(ctx.started_et)})",
            "info" if horizon else "warn",
        )
    return window, horizon


def _is_conus(st: Stadium) -> bool:
    if st.country and st.country.strip().upper() not in ("US", "USA", "UNITED STATES", ""):
        return False
    return 24.0 <= st.lat <= 50.0 and -125.0 <= st.lon <= -66.0


def weather_workers() -> int:
    """Concurrent Open-Meteo batches; FOOTBALL_WEATHER_WORKERS=1 restores sequential."""
    try:
        return max(1, min(8, int(os.environ.get("FOOTBALL_WEATHER_WORKERS", "3"))))
    except ValueError:
        return 3


def _fetch_point_batches(
    points: Sequence[tuple[float, float]],
    fetcher: Callable[..., Sequence[Any]],
    *,
    batch_size: int | Callable[[list[tuple[float, float]]], int],
    source_prefix: str,
    point_windows: Mapping[tuple[float, float], tuple[datetime, datetime]] | None = None,
    received: Callable[[list[tuple[float, float]]], None] | None = None,
    **kwargs: Any,
) -> tuple[dict[tuple[float, float], Any], list[tuple[int, int, Exception]]]:
    """Fetch independent point batches and retain every successful batch.

    The provider client also enforces its own maximum size, but batching here is
    deliberate: one HTTP 429 must not discard forecasts already returned by an
    earlier request in the same run.
    """
    fetched: dict[tuple[float, float], Any] = {}
    failures: list[tuple[int, int, Exception]] = []
    workers = weather_workers()
    # A callable batch_size packs against live budget headroom, so planning must
    # stay sequential; fixed sizes can be planned up front and fetched in a pool.
    if workers > 1 and not callable(batch_size) and len(points) > batch_size >= 1:
        plan = [list(points[i:i + batch_size]) for i in range(0, len(points), batch_size)]
        capture = kwargs.get("capture")
        if capture is not None:
            lock = threading.Lock()

            def locked_capture(*args: Any, **kw: Any) -> Any:
                with lock:  # RawStore.put mutates a shared manifest
                    return capture(*args, **kw)

            kwargs = {**kwargs, "capture": locked_capture}

        def run(index: int, batch: list[tuple[float, float]]) -> list[Any]:
            batch_kwargs = dict(kwargs)
            if point_windows is not None:
                batch_kwargs["start"] = min(point_windows[point][0] for point in batch)
                batch_kwargs["end"] = max(point_windows[point][1] for point in batch)
            parsed = list(fetcher(batch, source_prefix=f"{source_prefix}_batch{index:02d}", **batch_kwargs))
            if len(parsed) != len(batch):
                raise RuntimeError(f"provider returned {len(parsed)} locations for {len(batch)} points")
            return parsed

        with ThreadPoolExecutor(max_workers=min(workers, len(plan))) as pool:
            futures = [pool.submit(run, index, batch) for index, batch in enumerate(plan)]
        for index, (batch, future) in enumerate(zip(plan, futures, strict=True)):
            try:
                fetched.update(zip(batch, future.result(), strict=True))
                if received is not None:
                    received(batch)
            except Exception as exc:  # noqa: BLE001 - caller records one aggregate degradation
                failures.append((index, len(batch), exc))
        return fetched, failures
    offset, batch_index = 0, 0
    while offset < len(points):
        remaining = list(points[offset:])
        size = batch_size(remaining) if callable(batch_size) else batch_size
        if size < 1:
            raise ValueError("batch size must be positive")
        batch = remaining[:size]
        offset += len(batch)
        batch_kwargs = dict(kwargs)
        if point_windows is not None:
            batch_kwargs["start"] = min(point_windows[point][0] for point in batch)
            batch_kwargs["end"] = max(point_windows[point][1] for point in batch)
        try:
            parsed = list(
                fetcher(
                    batch,
                    source_prefix=f"{source_prefix}_batch{batch_index:02d}",
                    **batch_kwargs,
                )
            )
            if len(parsed) != len(batch):
                raise RuntimeError(
                    f"provider returned {len(parsed)} locations for {len(batch)} points"
                )
            fetched.update(zip(batch, parsed, strict=True))
            if received is not None:
                received(batch)
        except Exception as exc:  # noqa: BLE001 - caller records one aggregate degradation
            failures.append((batch_index, len(batch), exc))
        batch_index += 1
    return fetched, failures


def stage_weather(
    ctx: RunContext, sport: str, games: list[Game], stadiums: dict[str, Stadium], raw: RawStore, roof_states: dict[str, str | None],
    extras: dict[str, dict[str, Any]] | None = None,
    state_dir: Path | None = None,
    openers: dict[str, Any] | None = None,
) -> dict[str, WeatherForecast]:
    """Deterministic forecast + ensemble (Phase 5) + NWS -> merged WeatherForecast per game.
    ``extras`` (if given) collects per-game merge side-outputs: ``precip_prob_ens``,
    ``roof_heuristic``, ``ensemble`` (bool)."""
    if not games:
        return {}
    fallback_mod.prepare(ctx)
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
    kickoffs = [g.kickoff_utc for g in games]
    start, end = om_mod.window_for(kickoffs)
    point_hours = {point: {merge_mod.hour_floor(g.kickoff_utc) + timedelta(hours=i) for g in group for i in range(-1, 5)}
                   for point, group in by_point.items()}
    reused = point_handoff.read(ctx.weather_state.get("reuse_point_dir"), ctx, sport, om_mod, by_point, stadiums, roof_states, point_hours)
    budget_kwargs: dict[str, Any] = {}
    ctx.request_budgets.setdefault("openmeteo", om_mod.RequestBudget())
    budget_kwargs["budget"] = ctx.request_budgets["openmeteo"]
    om_by_point, nws_by_point, point_stamps, point_meta = first_pass.collect(
        ctx, sport, om_mod, nws_mod, by_point, conus_pts, point_hours, reused, start, end,
        capture, _fetch_point_batches, state_dir, budget_kwargs,
    )

    # First merge fresh point data only. Screening never inspects members or
    # ensemble-dependent confidence. Existing open signals remain eligible.
    active = set()
    previous = {}
    if state_dir is not None:
        for record in pstate.load_alerts(state_dir).get("records", {}).values():
            if record.get("family") == "edge" and record.get("notification_active"):
                active.add(record.get("game_id"))
        previous = d1_out.load_wx_last(state_dir).get("last", {})
    signal_openers = openers if openers is not None else pstate.load_openers(state_dir) if state_dir is not None else {}
    opening_spreads = {g.game_id: json_out.consensus_spread_opener(g.game_id, signal_openers)[0] for g in games} if sport == "cfb" else {}
    decisions = {}
    screen_clock = ctx.now_utc
    def merged(game, point, clock, ensemble=None):
        stadium = stadiums[game.game_id]
        use_nws = (point_meta.get(point, {}).get("stage") not in ("refined_multimodel", "refined_split_fields")
                   and not point_meta.get(point, {}).get("nws_aged", True)
                   and first_pass.complete(nws_by_point.get(point), {merge_mod.hour_floor(game.kickoff_utc) + timedelta(hours=i) for i in range(-1, 5)}))
        return merge_mod.build_forecast(
            game.game_id, game.kickoff_utc, clock, None if use_nws else om_by_point.get(point),
            nws_by_point.get(point) if not point_meta.get(point, {}).get("nws_aged", True) else None,
            orientation_deg=stadium.orientation_deg, roof_state=roof_states.get(game.game_id), run_id=ctx.run_id,
            stadium_id=stadium.stadium_id, ens=ensemble, roof_type=stadium.roof_type, expect_ensemble=ensemble is not None,
            report_source_degradations=False,
        )
    for point, point_games in by_point.items():
        for game in point_games:
            try:
                provisional = merged(game, point, screen_clock)
            except Exception as exc:  # noqa: BLE001
                ctx.degrade("weather", f"{game.game_id}: point merge failed: {exc}", "warn")
                continue
            decisions[game.game_id] = screening.screen(
                sport, dataclasses.replace(provisional.forecast, run_time=point_stamps[point]), om_by_point.get(point),
                game.kickoff_utc, roof_state=roof_states.get(game.game_id), active=game.game_id in active, open_spread=opening_spreads.get(game.game_id),
                previous=previous.get(game.game_id), now=screen_clock,
                blend_cfg=merge_mod._default_blend_cfg(), nws_rows=nws_by_point.get(point) if not point_meta.get(point, {}).get("nws_aged", True) else None, for_refinement=True,
            )
    refine_points = [point for point, group in by_point.items() if (point not in reused or
                     point_meta.get(point, {}).get("stage") not in ("refined_multimodel", "refined_split_fields")) and
                     any(first_pass.needs_refinement(decisions[g.game_id]) for g in group if g.game_id in decisions)]
    first_pass.refine(ctx, sport, om_mod, refine_points, conus_pts, om_by_point, point_stamps, point_meta, start, end,
                      capture, _fetch_point_batches, budget_kwargs, point_hours)
    screen_clock = ctx.now_utc
    for point, group in by_point.items():
        for game in group:
            if game.game_id not in decisions:
                continue
            provisional = merged(game, point, screen_clock)
            decisions[game.game_id] = screening.screen(
                sport, dataclasses.replace(provisional.forecast, run_time=point_stamps[point]), om_by_point.get(point),
                game.kickoff_utc, roof_state=roof_states.get(game.game_id), active=game.game_id in active, open_spread=opening_spreads.get(game.game_id),
                previous=previous.get(game.game_id), now=screen_clock,
                blend_cfg=merge_mod._default_blend_cfg(), nws_rows=nws_by_point.get(point) if not point_meta.get(point, {}).get("nws_aged", True) else None,
            )
    point_handoff.write(ctx.weather_state.get("write_point_dir"), ctx, sport, om_mod, by_point, stadiums, roof_states,
                        point_hours, om_by_point, nws_by_point, point_stamps)
    ctx.degrade("weather", f"{sport}: cheap first pass refined {len(refine_points)}/{len(by_point)} locations", "info")
    selected_points = [point for point in by_point if any(decisions[g.game_id].eligible for g in by_point[point] if g.game_id in decisions)]
    selected_games = {point: [g for g in by_point[point] if g.game_id in decisions and decisions[g.game_id].eligible] for point in selected_points}
    windows = {point: om_mod.window_for([g.kickoff_utc for g in group]) for point, group in selected_games.items()}
    hours = {point: {merge_mod.hour_floor(g.kickoff_utc) + timedelta(hours=i) for g in group for i in range(-1, 5)}
             for point, group in selected_games.items()}
    # Cheap points remain fresh each ordinary run. Only distant, inactive games
    # may reuse covered original members from an older verified source version.
    max_age = ctx.weather_state.setdefault("member_max_age", {})
    for point, group in selected_games.items():
        allowed = 12 if all(decisions[g.game_id].priority != 0 and
                            (g.kickoff_utc - screen_clock).total_seconds() > 72 * 3600 for g in group) else 0
        max_age[point] = min(max_age.get(point, allowed), allowed)
    selected_points.sort(key=lambda point: min((decisions[g.game_id].priority, g.kickoff_utc) for g in selected_games[point]))
    ensembles, coverage = selective.members(
        ctx, om_mod, selected_points, windows, hours, capture, _fetch_point_batches, state_dir=state_dir, sport=sport,
    )
    ctx.degrade("weather", f"{sport}: full-member screen selected {sum(d.eligible for d in decisions.values())}/{len(games)} games at {len(selected_points)} locations", "info")
    fc: dict[str, WeatherForecast] = {}
    clock = ctx.now_utc  # recompute lead/climatology weights after source retrieval
    for point, point_games in by_point.items():
        for game in point_games:
            if game.game_id not in decisions:
                continue
            decision = decisions[game.game_id]
            try:
                result = merged(game, point, clock, ensembles.get(point) if decision.eligible else None)
                stats = result.ensemble
                trace = coverage.get(point, {}) if decision.eligible else {}
                status = ("not_sampled_closed_roof" if not decision.eligible and "confirmed_closed_roof" in decision.reasons else
                          "not_sampled_ineligible_signal" if not decision.eligible and "cfb_opening_spread_ineligible" in decision.reasons else
                          "not_sampled_point_unavailable" if not decision.eligible and decision.priority == 2 else
                          "not_sampled_below_signal_buffer" if not decision.eligible else
                          "unavailable_degraded" if stats is None else
                          "retained_members_degraded" if trace.get("unverified_sources") else
                          "partial_members_degraded" if trace.get("errors") or stats.n_members < 82
                          or not selective.complete(ensembles.get(point), {merge_mod.hour_floor(game.kickoff_utc) + timedelta(hours=i) for i in range(-1, 5)})
                          else "aged_members" if trace.get("aged_sources") else "full_members")
                ecmwf_source, source_updated = fallback_mod.forecast_markers(point_meta.get(point), trace)
                if not source_updated and point_meta.get(point, {}).get("updated_at"):
                    source_updated = {"nws": point_meta[point]["updated_at"]}
                fc[game.game_id] = dataclasses.replace(
                    result.forecast, run_time=point_stamps[point],
                    point_stage=point_meta.get(point, {}).get("stage", "unavailable"),
                    point_source_updated_at=source_updated,
                    ecmwf_source=ecmwf_source,
                    point_aged=point_meta.get(point, {}).get("aged", True),
                    ensemble_status=status, ensemble_eligible=decision.eligible, ensemble_screen_reasons=list(decision.reasons),
                    ensemble_models=stats.models if stats else [], ensemble_members=stats.n_members if stats else 0,
                    ensemble_fetched_at=trace.get("fetched_at", {}), ensemble_source_versions=trace.get("source_versions", {}),
                    ensemble_cached_sources=trace.get("cached_sources", []),
                    ensemble_aged_sources=trace.get("aged_sources", []),
                    ensemble_unverified_sources=trace.get("unverified_sources", []),
                    ensemble_verification_errors=trace.get("errors", {}),
                )
            except Exception as exc:  # noqa: BLE001
                ctx.degrade("weather", f"{game.game_id}: merge failed: {exc}", "warn")
                continue
            ctx.degradations.extend(result.degradations)
            missing = [name for name, value in (("temperature", fc[game.game_id].temp_fg),
                       ("wind", fc[game.game_id].wind_fg), ("rainfall", fc[game.game_id].rain_fg_mm))
                       if not first_pass.finite(value)]
            if missing:
                ctx.degrade("weather", f"{game.game_id}: final point forecast incomplete; missing {', '.join(missing)}", "warn")
            if decision.eligible and status not in ("full_members", "aged_members"):
                ctx.degrade("weather", f"{game.game_id}: selected ensemble {status}; source/member coverage remains explicit", "warn")
            if extras is not None:
                extras[game.game_id] = {"precip_prob_ens": result.precip_prob_ens, "roof_heuristic": result.roof_heuristic,
                    "ensemble": stats is not None, "ensemble_source": stats.method if stats else None}
    ctx.count("weather", sport, len(fc))
    nws_only = [
        forecast
        for forecast in fc.values()
        if forecast.source == merge_mod.NWS
        and any(value is not None for value in (forecast.temp_fg, forecast.wind_fg, forecast.rain_fg_mm))
    ]
    no_source = [
        forecast
        for forecast in fc.values()
        if all(value is None for value in (forecast.temp_fg, forecast.wind_fg, forecast.rain_fg_mm))
    ]
    if nws_only:
        ctx.degrade(
            "weather",
            f"{sport}: {len(nws_only)} games use NWS first-pass forecasts",
            "info",
        )
    if no_source:
        nearest = min((forecast.lead_hours for forecast in no_source), default=0.0)
        ctx.degrade(
            "weather",
            f"{sport}: {len(no_source)} games have no usable forecast (nearest kickoff {nearest:.0f}h away)",
            "warn",
        )
    missing = [g.game_id for g in games if g.game_id not in fc]
    if missing:
        ctx.degrade("weather", f"{sport}: {len(missing)} games without forecast", "warn")
    if os.environ.get("OBSLOG_SHADOW_PATH"):
        try:
            from pipeline.obslog.shadow import weather as shadow_weather
            shadow_weather(os.environ["OBSLOG_SHADOW_PATH"], ctx, games, fc)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("obslog.weather", f"shadow unavailable: {type(exc).__name__}", "warn")
    return fc


# ---- odds: scrapers ---------------------------------------------------------------

def load_scraper_class(name: str) -> type | None:
    module_name, cls_name = BOOK_REGISTRY[name]
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(mod, cls_name, None)


def make_scraper(cls: type, raw: RawStore | None, run_id: str | None) -> Any:
    return cls(headless=True, raw_store=raw, run_id=run_id)


async def _run_book(scraper: Any, sport: str) -> list[GameLine]:
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
            degrade("odds", f"{sport}: {name} unavailable (module/dependency missing)", "warn", outcome_code=ScrapeOutcome.SKIPPED)
            continue
        try:
            scraper = make_scraper(cls, raw, run_id)
        except Exception as exc:  # noqa: BLE001
            degrade("odds", f"{sport}: {name} init failed: {type(exc).__name__}: {exc}", "warn", outcome_code=ScrapeOutcome.SKIPPED)
            continue
        names.append(name)
        scrapers[name] = scraper
        tasks.append(_run_book(scraper, sport))
    if not tasks:
        return {}, {}
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, list[GameLine]] = {}
    for name, res in zip(names, results, strict=True):
        outcome = None
        if isinstance(res, asyncio.TimeoutError):
            outcome = ScrapeOutcome.TIMEOUT
            degrade("odds", f"{sport}: {name} timed out after {SCRAPE_TIMEOUT_S:.0f}s", "warn", outcome_code=outcome)
            out[name] = []
        elif isinstance(res, BaseException):
            outcome = ScrapeOutcome.FETCH_ERROR
            degrade("odds", f"{sport}: {name} failed: {type(res).__name__}: {res}", "warn", outcome_code=outcome)
            out[name] = []
        else:
            out[name] = [ln for ln in (res or []) if isinstance(ln, GameLine)]
        # Page-based books can return partial/empty data after handling a network
        # error internally. Surface the cause on the board, not only in CI logs.
        scrapers[name].scrape_result = scraper_result(
            name, sport, len(out[name]), scrapers[name], outcome=outcome,
            failure_detail=f"{type(res).__name__}: {res}" if isinstance(res, BaseException) else None)
        errors = getattr(scrapers[name], "fetch_errors", {})
        # A stale Betcris file is one warning from note_empty_book, not a fetch failure.
        if errors and upstream_stale_detail(scrapers[name]) is None:
            detail = "; ".join(f"{page}: {reason}" for page, reason in errors.items())
            degrade(f"odds.{name}", f"{sport}: public lines fetch failed ({detail})", "warn",
                    outcome_code=scrapers[name].scrape_result.outcome)
    return out, scrapers


def ingest_book_lines(ctx: RunContext, sport: str, name: str, ingest_dir: Path) -> tuple[list[GameLine], Any]:
    """Load one Playwright book's handoff. A missing file reads as a failed scrape
    (0 lines, fetch error for book health); stale/expired lines are dropped, never
    re-stamped, so ``scraped_at`` stays the time the book was actually seen."""
    ing = handoff.load_lines(ingest_dir, sport, name, now=ctx.now_utc, max_age=ODDS_CARRY_MAX_AGE)
    for problem in ing.problems:
        ctx.degrade("odds.ingest", f"{sport}: {name} {problem}", "warn", outcome_code=ScrapeOutcome.FETCH_ERROR)
    dropped = ing.rejected_stale + ing.rejected_expired + ing.rejected_invalid
    previous = getattr(ing.scraper, 'scrape_result', None)
    ing.scraper.scrape_result = scraper_result(
        name, sport, len(ing.lines), ing.scraper,
        outcome=previous.outcome if previous else None,
        coverage_ok=not ing.missing and not dropped and (previous.coverage_ok if previous else True))
    if dropped:
        ctx.degrade("odds.ingest", f"{sport}: {name} handoff dropped {dropped} line(s) "
                    f"(older than {ODDS_CARRY_MAX_AGE}: {ing.rejected_stale}, expired: {ing.rejected_expired}, "
                    f"invalid: {ing.rejected_invalid})", "warn", outcome_code=ing.scraper.scrape_result.outcome)
    errors = getattr(ing.scraper, "fetch_errors", {})
    if errors and not ing.missing and upstream_stale_detail(ing.scraper, name) is None:
        detail = "; ".join(f"{page}: {reason}" for page, reason in errors.items())
        ctx.degrade(f"odds.{name}", f"{sport}: public lines fetch failed ({detail})", "warn",
                    outcome_code=ing.scraper.scrape_result.outcome)
    oldest = min((ln.scraped_at for ln in ing.lines if ln.scraped_at is not None), default=None)
    age = f", oldest {int((ctx.now_utc - oldest).total_seconds())}s old" if oldest is not None else ""
    print(f"  ingest {name:<10} {sport}: {len(ing.lines)} lines from run {ing.run_id or '-'}{age}")
    return ing.lines, ing.scraper


def scrape_only(sports: Sequence[str], out_dir: Path, *, books: Sequence[str] | None = None,
                raw_dir: Path = DEFAULT_BASE, run_id: str | None = None) -> int:
    """Parallel CI job: scrape the Playwright books and write raw captures plus a
    ``pipeline.odds.handoff`` file per sport/book. Reads no state and writes nothing
    to R2; the publish job ingests the handoff after the light job. A book that fails
    still gets a (0-line) handoff carrying its fetch errors."""
    ctx = RunContext(sport="all" if len(sports) > 1 else sports[0], scope="scrape-only", run_id=run_id or "")
    try:
        book_list = books_for_scope("odds", books or list(PLAYWRIGHT_BOOKS))
    except ValueError as exc:
        ctx.degrade("scope", str(exc), "error")
        book_list = []
    for sport in sports:
        raw = RawStore(sport, ctx.run_id, raw_dir)
        with ctx.stage(f"{sport}.scrape"):
            per_book, scrapers = _run_async(scrape_books(sport, book_list, raw, ctx.run_id, ctx.degrade))
        raw.finalize()
        for name in book_list:
            scraper = scrapers.get(name)
            lines = per_book.get(name, [])
            raw_games = list(odds_merge.raw_games_from_scraper(scraper, name, sport).values()) if scraper is not None else []
            errors = dict(getattr(scraper, "fetch_errors", None) or {})
            if scraper is None:
                errors.setdefault(sport, "scraper unavailable")
            path = handoff.dump_lines(out_dir, sport, name, lines, run_id=ctx.run_id, raw_games=raw_games,
                                      fetch_errors=errors, scrape_result=getattr(scraper, 'scrape_result', None))
            print(f"  scrape-only {name:<10} {sport}: {len(lines):>4} lines / {len(raw_games):>3} games -> {path}")
    for line in ctx.summary_lines():
        print(line)
    return 1 if any(d.severity == "error" for d in ctx.degradations) else 0


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

def upstream_stale_detail(scraper: Any, book: str | None = None) -> str | None:
    """Betcris public feed only. Other books are never excused by this marker."""
    if book is not None and book != "betcris":
        return None
    if book is None and getattr(scraper, "BOOK_NAME", None) != "betcris":
        return None
    errors = getattr(scraper, "fetch_errors", None)
    if not isinstance(errors, dict) or not errors:
        return None
    texts = [str(value) for value in errors.values() if str(value).strip()]
    if not texts or not getattr(scraper, "upstream_stale", False):
        return None
    return "; ".join(texts)


def check_scrape_volume(
    counts: dict[str, int],
    baseline: dict,
    books: Sequence[str],
    critical: frozenset[str] | set[str] = CRITICAL_BOOKS,
    drop_frac: float = VOLUME_DROP_FRAC,
    min_peak: int = VOLUME_MIN_PEAK,
    excused: frozenset[str] | set[str] = frozenset(),
) -> list[tuple[str, int, int | None]]:
    """Active volume-drop detector. Mutates ``baseline`` (peaks/alerted/seen_books)
    and returns current ``[(key, current, peak)]`` incidents; ``peak=None`` marks
    a fully-dark book. ``alerts.json`` owns delivery deduplication, so an active
    incident is returned again after a failed Telegram send.

    * peaks only ratchet up; a (book|market) below ``drop_frac``·peak (peak ≥ ``min_peak``)
      remains active until recovery
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

    excused_books = set(excused)
    dark: set[str] = set()
    for b in books:
        if b in excused_books or b not in critical:
            continue
        peers = sum(1 for o in critical if o != b and book_total.get(o, 0) >= VOLUME_DARK_MIN_ROWS)
        if book_total.get(b, 0) == 0 and peers >= 2:
            dark.add(b)
    for b in books:
        if b in excused_books:
            continue
        if int(seen_books.get(b, 0)) >= VOLUME_DARK_MIN_ROWS and book_total.get(b, 0) == 0:
            dark.add(b)

    drops: list[tuple[str, int, int | None]] = []
    for key in set(peaks) | set(counts):
        book = key.split("|", 1)[0]
        cur = counts.get(key, 0)
        peaks[key] = max(int(peaks.get(key, 0)), cur)  # peaks only ratchet up
        if book not in books:
            continue  # not scraped this run: neither drop nor recovery
        if book in dark or book in excused_books:
            continue  # DARK covers a real outage; a stale Betcris feed is not one
        peak = peaks[key]
        if peak >= min_peak and cur < drop_frac * peak:
            drops.append((key, cur, peak))
            alerted.add(key)
        else:
            alerted.discard(key)  # recovered (or never dropped) -> re-arm

    for b in books:
        dkey = f"DARK|{b}"
        if b in dark:
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


def baseline_scope(sport: str, games: Sequence[Game], odds_games: Sequence[Game] = (), season: int | None = None) -> str:
    """Volume-baseline scope keyed on the WINDOW games (resets week to week as before).
    Empty window (preseason lines-only runs): week stays ``?`` so the peaks ratcheted
    on a preseason-shaped board do not judge week 1; season comes from the horizon."""
    if games:
        return volume_scope(sport, games, season)
    if season is None and odds_games:
        season = min(odds_games, key=lambda x: x.kickoff_utc).season
    return f"{sport}:{season or '?'}:?"


# ---- odds: canonical merge ---------------------------------------------------------

def _merge_lines(
    sport: str,
    games: Sequence[Game],
    lines: Sequence[GameLine],
    scrapers: dict[str, Any],
    openers: dict,
    ctx: RunContext,
) -> tuple[list[GameLine], list[str]]:
    """``pipeline.odds.merge.merge_odds`` (aliases + rapidfuzz + ±36 h + neutral flip).
    Openers are recorded into the passed dict (never overwritten) and saved by the
    caller. Returns ``(canonical lines, unresolved)`` where unresolved = team names
    the resolver could not map plus in-span book games without a schedule match;
    Empty results on failure; other books and sports can continue."""
    try:
        raw_games: dict[str, Any] = {}
        for name, scraper in scrapers.items():
            raw_games.update(odds_merge.raw_games_from_scraper(scraper, name, sport))
        res = odds_merge.merge_odds(sport, list(games), list(lines), raw_games or None, openers=openers, now=ctx.now_utc, save=False)
        matched = [ln for ln in res.lines if isinstance(ln, GameLine)]
        unresolved = [str(u) for u in res.unresolved]
        span = (min(g.kickoff_utc for g in games) - timedelta(hours=odds_merge.WINDOW_H),
                max(g.kickoff_utc for g in games) + timedelta(hours=odds_merge.WINDOW_H)) if games else None
        for row in res.unmatched:
            parts = str(row).split("|", 2)
            if len(parts) != 3:
                continue
            book, pid, reason = parts
            when = None
            rg = raw_games.get(pid)
            if rg is not None:
                when = rg.kickoff_utc
            else:
                try:
                    parsed = odds_merge.parse_provisional(pid)
                except ValueError:
                    parsed = None
                if parsed is not None:
                    when = parsed.kickoff_utc
                    # Preserve the reporting span: date-only stamps were midnight UTC.
                    if when is not None and parsed.date_only:
                        when = when.replace(hour=0)
            if when is None or span is None or span[0] <= when <= span[1]:
                unresolved.append(f"{book}:{pid.split(':', 3)[-1]}:{reason}")
        return matched, unresolved
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("odds.merge", f"{sport}: merge_odds failed ({type(exc).__name__}: {exc}); using built-in matcher", "warn")
        return [], []


# ---- odds: consensus adapter ----------------------------

@dataclass(frozen=True)
class ConsensusLine:
    line: float
    odds: int | None
    n_books: int
    ref_book: str | None
    side: str  # the side whose line/odds this is (home for spread, under for total)
    src: str | None = None  # spread: averaged members ("bol+pin" subset) or "fallback"


def _build_consensus(sport: str, lines: Sequence[GameLine], ctx: RunContext,
                     *, now: datetime | None = None) -> dict[tuple[str, str], ConsensusLine]:
    """``pipeline.model.fair.consensus(sport, game_lines, market)`` per game/market
    (Pinnacle-weighted median, devigged prob moved to the consensus line). The
    opener/legacy odds for the consensus row are the ref book's price on our side
    (home / under), else the devigged consensus prob converted to American."""
    side_for = {"spread": "home", "total": "under"}
    by_game: dict[str, list[GameLine]] = {}
    for ln in lines:
        if ln.sport == sport and ln.book != CONSENSUS_BOOK:
            by_game.setdefault(ln.game_id, []).append(ln)
    out: dict[tuple[str, str], ConsensusLine] = {}
    for game_id, glines in by_game.items():
        game_consensus: dict[tuple[str, str], ConsensusLine] = {}
        try:
            for market, side in side_for.items():
                c = fair_mod.consensus(sport, glines, market, now=now or ctx.now_utc)
                line = c.line
                if line is None:
                    continue
                ref_book = c.ref_book
                odds = next((ln.odds for ln in glines if ln.book == ref_book and ln.market == market
                             and ln.side == side and ln.is_main and ln.line == line), None)
                prob = c.prob
                if odds is None and prob is not None:
                    p = prob if side == "home" else 1.0 - prob
                    odds = int(fair_mod.prob_to_american(min(0.99, max(0.01, p))))
                game_consensus[(game_id, market)] = ConsensusLine(
                    line=float(line), odds=odds, n_books=int(c.n_books or 0),
                    ref_book=ref_book, side=side, src=c.src,
                )
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("model.fair", f"{sport}: consensus failed ({type(exc).__name__}: {exc}); using built-in", "warn")
            continue
        out.update(game_consensus)
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


def retarget_cfb_total_openers(
    openers: dict,
    history: dict,
    lines: Sequence[GameLine],
    games: Sequence[Game],
    now: datetime,
) -> list[str]:
    """Set CFB total baselines to the line in force at kickoff minus six days.

    Each book is selected independently from its change-only history. The
    consensus baseline is then rebuilt with the normal book weights, using only
    books observed by the target when any exist. This prevents a book that first
    appeared after T-6 from leaking into the historical consensus.
    """
    targets: dict[str, datetime] = {}
    for game in games:
        target = pstate.parse_utc(game.kickoff_utc - timedelta(days=CFB_TOTAL_BASELINE_DAYS))
        if target is not None:
            targets[game.game_id] = target
    now_text = utc_iso(now)
    changed = set(pstate.retarget_openers(
        openers,
        history,
        lines,
        targets,
        now_text,
        market="total",
        excluded_books=(CONSENSUS_BOOK,),
    ))
    store = openers.setdefault('references', {}).setdefault('t_minus_6d', {})

    for game_id, target in targets.items():
        side_rows: dict[str, dict[str, tuple[dict[str, Any], datetime]]] = {}
        for key, val in store.items():
            parts = key.split("|")
            if len(parts) != 4 or not isinstance(val, dict):
                continue
            gid, market, side, book = parts
            if (
                gid != game_id
                or market != "total"
                or book == CONSENSUS_BOOK
                or side not in ("under", "over")
            ):
                continue
            line = val.get("line")
            ts = pstate.parse_utc(val.get("ts"))
            if line is None or ts is None:
                continue
            side_rows.setdefault(book, {})[side] = (val, ts)

        per_book: dict[str, tuple[str, dict[str, Any], datetime]] = {}
        for book, sides in side_rows.items():
            pre_target = {side: row for side, row in sides.items() if row[1] <= target}
            available = pre_target or sides
            side = "under" if "under" in available else "over"
            val, ts = available[side]
            per_book[book] = (side, val, ts)

        rows = list(per_book.items())
        before = [row for row in rows if row[1][2] <= target]
        if before:
            rows = before
        weighted = [(float(row[1][1]["line"]), BOOK_WEIGHTS.get(row[0], DEFAULT_BOOK_WEIGHT)) for row in rows]
        line = odds_merge._weighted_median([(v, w) for v, w in weighted if w > 0])
        if line is None:
            continue
        refs = [row for row in rows if float(row[1][1]["line"]) == line]
        ref_book, (ref_side, ref_val, _ref_ts) = max(
            refs,
            key=lambda row: BOOK_WEIGHTS.get(row[0], DEFAULT_BOOK_WEIGHT),
        )
        source_ts = max(row[1][2] for row in rows)
        key = pstate.odds_key(game_id, "total", "under", CONSENSUS_BOOK)
        desired = {
            "line": line,
            "odds": ref_val.get("odds") if ref_side == "under" else None,
            "ts": utc_iso(source_ts),
            "basis": "t_minus_6d",
            "target_ts": utc_iso(target),
            "ref_book": ref_book,
            "n_books": len(rows),
        }
        if store.get(key) != desired:
            store[key] = desired
            changed.add(key)

    return sorted(changed)


def consensus_spread_lines(consensus: dict[tuple[str, str], ConsensusLine], sport: str, now: datetime,
                           run_id: str | None = None) -> list[GameLine]:
    """The consensus SPREAD as ``book='consensus'`` home GameLines so history.json,
    D1 ``odds_history`` (change-only) and the CLV closings track it like a book."""
    out: list[GameLine] = []
    for (game_id, market), c in consensus.items():
        if market != "spread" or c.line is None:
            continue
        out.append(GameLine(sport=sport, game_id=game_id, book=CONSENSUS_BOOK, market="spread", side="home",
                            odds=None, line=float(c.line), is_main=True,
                            source_id=c.src, scraped_at=now, run_id=run_id))
    return out


# ---- odds: board fair inputs ---------------------------------------------------------

def card_odds(
    sport: str,
    game_id: str,
    lines_by_game: dict[str, list[GameLine]],
    consensus: dict[tuple[str, str], ConsensusLine],
) -> dict[str, Any]:
    """Reference prices needed by published fair values and the console summary.

    Preserve the designated book (NFL BetOnline, CFB FanDuel) with a
    per-market consensus fallback. Openers and book prices live in JSON's
    odds/consensus blocks and do not need a second export representation.
    """
    ref = "betonline" if sport == "nfl" else "fanduel"

    def current(market: str, side: str) -> tuple[float | None, str | None]:
        for line in lines_by_game.get(game_id, []):
            if line.book == ref and line.market == market and line.side == side and line.is_main:
                return line.line, ref
        value = consensus.get((game_id, market))
        return (value.line, CONSENSUS_BOOK) if value is not None else (None, None)

    spread, spread_book = current("spread", "home")
    total, total_book = current("total", "under")
    cons_sp = consensus.get((game_id, "spread"))
    cons_to = consensus.get((game_id, "total"))
    return {
        "current_total": total,
        "spread": cons_sp.line if cons_sp else spread,
        "total_proj": cons_to.line if cons_to else total,
        "spread_src": cons_sp.src if cons_sp else None,
        "ref_book": spread_book or total_book,
    }


def note_empty_book(
    ctx: RunContext, sport: str, name: str, games: Sequence[Game], n_lines: int, scraper: Any,
    result: ScrapeResult | None = None,
) -> None:
    """Warn, without failing the run, when an enabled book is dark and the league has games.

    A league with nothing in the odds horizon is not a dark book. The GitHub
    annotation keeps a successful job from looking clean, and the run status
    becomes ``warn`` so the Status tab does not stay green.
    """
    if n_lines or not games:
        return
    stale = upstream_stale_detail(scraper, name)
    if stale is not None:
        reason = f"{sport}: {name} upstream stale ({stale})"
    else:
        detail = ""
        errors = getattr(scraper, "fetch_errors", None)
        if isinstance(errors, dict) and errors:
            detail = "; ".join(f"{key}: {value}" if key != sport else str(value) for key, value in errors.items())
        reason = f"{sport}: {name} returned 0 lines"
        if detail:
            reason = f"{reason} ({detail})"
    result = result or scraper_result(name, sport, n_lines, scraper)
    ctx.degrade("odds", reason, "warn", outcome_code=result.outcome, empty_book=True)
    print(f"::warning::{reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"- Warning: {reason}\n")
    except OSError:
        return


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
    pseudo: list[GameLine] = dataclasses.field(default_factory=list)    # consensus spread as book='consensus' rows
    opener_update_keys: list[str] = dataclasses.field(default_factory=list)  # rebased existing rows -> D1 upsert
    last_observed: dict[str, dict] = dataclasses.field(default_factory=dict)  # descriptive only; never current lines
    priced_at: datetime | None = None


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
    ingest_dir: Path | None = None,
) -> OddsResult:
    """``games`` is the odds-horizon schedule (``split_schedule``): every game in it
    is matched and gets openers / archive_last / deltas / consensus, card or not."""
    if not books:
        return OddsResult([], {}, pstate.load_openers(state_dir), {}, [], {})
    raw_arg: RawStore | None = None if isinstance(raw, NullRawStore) else raw
    # --ingest-lines: Playwright books were scraped by the parallel CI job; read its
    # handoff (scraped_at preserved, stale/expired lines rejected) instead of scraping.
    ingest_books = [b for b in books if ingest_dir is not None and b in PLAYWRIGHT_BOOKS]
    live_books = [b for b in books if b not in ingest_books]
    with ctx.stage(f"{sport}.scrape"):
        per_book, scrapers = (_run_async(scrape_books(sport, live_books, raw_arg, ctx.run_id, ctx.degrade))
                              if live_books else ({}, {}))
    if ingest_books and ingest_dir is not None:
        with ctx.stage(f"{sport}.ingest"):
            for name in ingest_books:
                per_book[name], scrapers[name] = ingest_book_lines(ctx, sport, name, ingest_dir)
    all_lines: list[GameLine] = []
    per_book_n: dict[str, int] = {}
    outcomes: dict[str, ScrapeResult] = {}
    for name in books:
        lines = per_book.get(name, [])
        per_book_n[name] = len(lines)
        scraper = scrapers.get(name)
        outcomes[name] = getattr(scraper, 'scrape_result', None) or scraper_result(
            name, sport, len(lines), scraper,
            outcome=ScrapeOutcome.SKIPPED if scraper is None else None)
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
        note_empty_book(ctx, sport, name, games, len(lines), scraper, outcomes[name])

    # Cross-run per-book health (only books scraped this run; carried books are untouched).
    if not dry_run:
        try:
            health = pstate.load_book_health(state_dir)
            pstate.update_book_health(health, sport, {n: per_book_n[n] for n in books if n in scrapers},
                                      now=utc_iso(ctx.now_utc), horizon_games=len(games), results=outcomes)
            pstate.save_book_health(state_dir, health)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("odds.health", f"{sport}: book health update failed (non-fatal): {exc}", "warn")

    # Active volume health incident; alerts.json owns successful-delivery dedup
    # so transport failures retry without NFL/CFB scope switches re-arming it.
    with ctx.stage(f"{sport}.volume"):
        try:
            scope = baseline_scope(sport, [g for g in games if in_window(g.kickoff_utc, ctx.now_utc)], games, season)
            baseline = pstate.load_baseline(state_dir, scope)
            excused = frozenset(
                name for name in books if outcomes[name].outcome == ScrapeOutcome.UPSTREAM_STALE
            )
            drops = check_scrape_volume(scrape_counts(all_lines), baseline, books, excused=excused)
            if not dry_run:
                pstate.save_baseline(state_dir, baseline)
            if drops:
                text = format_volume_alert(drops, scope)
                detail = "; ".join(text.splitlines()[1:])
                ctx.degrade("odds.volume", f"{scope}: {detail}", "warn")
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("odds.volume", f"{sport}: volume check failed (non-fatal): {exc}", "warn")

    # provisional ids -> schedule game_id
    openers = pstate.load_openers(state_dir)
    with ctx.stage(f"{sport}.merge"):
        matched = _merge_lines(sport, games, all_lines, scrapers, openers, ctx)
        lines, unresolved = matched
        if not games:
            unresolved = []  # nothing on the schedule to match against: not a resolver problem
        if unresolved:
            uniq = sorted(set(unresolved))
            ctx.unresolved_names.extend(uniq)
            n_book_games = len({ln.game_id for ln in all_lines})
            # Games a book lists that are not on the FBS/NFL schedule (FCS etc.) are expected;
            # only genuinely unmapped team names are a resolver problem worth paging.
            names = [u for u in uniq if not u.endswith(":no-schedule-match")]
            n_off = len(uniq) - len(names)
            if names:
                ctx.degrade("odds.merge", f"{sport}: {len(names)} unresolved book team names (of {n_book_games} book games)", "warn")
            if n_off:
                ctx.degrade("odds.merge", f"{sport}: {n_off} book games outside the schedule (of {n_book_games} book games)", "info")
        ctx.count("merge", sport, len(lines))

    if os.environ.get("OBSLOG_SHADOW_PATH"):
        try:
            from pipeline.obslog.shadow import odds as shadow_odds
            shadow_odds(os.environ["OBSLOG_SHADOW_PATH"], ctx, sport, games, lines, books, outcomes)
        except Exception as exc:  # noqa: BLE001
            ctx.degrade("obslog.odds", f"shadow unavailable: {type(exc).__name__}", "warn")
    # Books not scraped this run (the Playwright job runs --books betonline only)
    # keep their last snapshot from archive_last so consensus / FD_now do not
    # collapse to a one-book view between the light and playwright commits.
    active_ids = {g.game_id for g in games}
    archive = pstate.load_archive_last(state_dir)
    carried = carry_forward_lines(archive, sport, active_ids, books, now=ctx.now_utc)
    if carried:
        n_books = len({ln.book for ln in carried})
        print(f"  carried {sport}: {len(carried)} lines from {n_books} unscraped book(s) (archive_last)")
    # One main pair per (game, book, market): every consumer below (consensus, openers,
    # history/D1, per-game fair + odds_block) sees the same normalized rows.
    lines = select_main_lines(lines)
    carried = select_main_lines(carried)
    board_lines = list(lines) + carried

    # consensus / fair
    priced_at = ctx.now_utc
    with ctx.stage(f"{sport}.fair"):
        consensus = _build_consensus(sport, board_lines, ctx, now=priced_at)

    # state: first-seen openers except CFB totals, which use the T-6-day snapshot;
    # archive_last remains the most recently seen value per key
    with ctx.stage(f"{sport}.state"):
        now = utc_iso(ctx.now_utc)
        main_lines = [ln for ln in lines if ln.is_main]
        # A BetCris print past the provider TTL stays on the board, but it is not
        # an opener, a history point, or a closing used for CLV.
        priced_lines = actionable_lines(main_lines, ctx.now_utc)
        before_keys = set(openers.get("openers") or {})
        added = pstate.record_openers(openers, priced_lines, now)
        added += pstate.record_openers(openers, consensus_pseudo_lines(consensus, sport), now)
        record_weekly_totals(openers, pstate.load_history(state_dir), priced_lines, games, ctx.now_utc)
        json_out.freeze_consensus_spread_openers(openers, sorted(active_ids), now)
        opener_update_keys = (
            retarget_cfb_total_openers(openers, pstate.load_history(state_dir), priced_lines, games, ctx.now_utc)
            if sport == "cfb" else []
        )
        new_keys = sorted(set(openers.get("openers") or {}) - before_keys)
        opener_active = (_active_for(openers.get("openers") or {}, sport, active_ids)
                         | _active_for(openers.get("weekly_totals") or {}, sport, active_ids))
        pruned = pstate.prune_openers(openers, opener_active)
        last = archive.setdefault("last", {})
        # consensus spread rides along as book='consensus' (history / D1 / closings), never carried forward
        pseudo = consensus_spread_lines(consensus, sport, ctx.now_utc, ctx.run_id)
        deltas = d1_out.odds_deltas(priced_lines + pseudo, last)  # change-only set BEFORE last is advanced
        # Keep historical observations, but an empty/partial scrape withdraws its
        # missing quotes from later jobs. Do not resurrect them from archive_last.
        for key, value in last.items():
            if key.startswith(f"{sport}:") and key.rsplit("|", 1)[-1] in books and isinstance(value, dict):
                value["available"] = False
        for ln in main_lines + pseudo:
            last[ln.key] = {"line": ln.line, "odds": ln.odds, "ts": utc_iso(ln.scraped_at) if ln.scraped_at else now,
                            "available": True, "source_id": ln.source_id, "outcome_ids": ln.outcome_ids}
            if ln.source_updated_at is not None:
                last[ln.key]["source_updated_at"] = utc_iso(ln.source_updated_at)
            if ln.expires_at is not None:
                last[ln.key]["expires_at"] = utc_iso(ln.expires_at)
            if ln.actionable_until is not None:
                last[ln.key]["actionable_until"] = utc_iso(ln.actionable_until)
        if _prune_allowed(archive, sport, len(active_ids), ctx, "archive_last"):
            pstate.prune_archive_last(archive, _active_for(last, sport, active_ids))
        if not dry_run:
            pstate.save_openers(state_dir, openers)
            pstate.save_archive_last(state_dir, archive)
        print(f"  openers {sport}: +{added} new, {len(opener_update_keys)} T-6 updated, {pruned} pruned, "
              f"{len(openers.get('openers') or {})} total; "
              f"{len(deltas)} moved line(s) of {len(main_lines)}")

    by_game: dict[str, list[GameLine]] = {}
    for ln in board_lines:
        by_game.setdefault(ln.game_id, []).append(ln)
    ctx.count("odds", sport, len(lines))
    return OddsResult(board_lines, consensus, openers, by_game, unresolved, per_book_n,
                      scraped=list(lines), deltas=deltas, new_opener_keys=new_keys, pseudo=pseudo,
                      opener_update_keys=opener_update_keys,
                      last_observed=last_observed_totals(archive, sport, active_ids, ctx.now_utc), priced_at=priced_at)


def last_observed_totals(archive: dict, sport: str, active_ids: set[str], now: datetime) -> dict[str, dict]:
    """Retain observed under references when partial runs cannot carry current prices.

    This display-only block never enters GameLine, consensus, model or execution.
    Preserve original book clocks, including failed-source availability evidence.
    """
    out: dict[str, dict] = {}
    for key, value in (archive.get("last") or {}).items():
        parts = key.split("|")
        if len(parts) != 4 or not isinstance(value, dict):
            continue
        gid, market, side, book = parts
        if gid not in active_ids or not gid.startswith(f"{sport}:") or market != "total" or side != "under" or book == CONSENSUS_BOOK:
            continue
        if value.get("line") is None or value.get("odds") is None:
            continue
        stamp = pstate.parse_utc(value.get("ts"))
        expiry = pstate.parse_utc(value.get("expires_at"))
        status = "unavailable" if value.get("available") is False else "unknown"
        if value.get("available") is True and stamp is not None and stamp <= now:
            status = "expired" if expiry and expiry < now else "stale" if now - stamp > ODDS_CARRY_MAX_AGE else "current"
        out.setdefault(gid, {})[book] = {"line": value.get("line"), "under": value.get("odds"),
            "updated_at": value.get("ts"), "source_updated_at": value.get("source_updated_at"),
            "expires_at": value.get("expires_at"), "status": status}
    return out


PRUNE_SHRINK_MIN = 0.7


def _prune_allowed(store: dict, sport: str, n_active: int, ctx: Any, label: str) -> bool:
    """False when the schedule is empty or shrank >30% vs the last pruned run.

    A failed/partial schedule fetch (e.g. ESPN current-week fallback) must not
    delete state for games that merely dropped out of the slate. The reference
    count resets when pruning proceeds and decays 15% per skipped run, so a
    one-off bad fetch is guarded while a genuine slate shrink (bye weeks, CFB
    championship week) resumes pruning after a few runs.
    """
    guard = store.setdefault("prune_guard", {})
    prev = guard.get(sport)
    prev_n = int(prev) if isinstance(prev, (int, float)) and not isinstance(prev, bool) else 0
    if n_active <= 0 or (prev_n and n_active < prev_n * PRUNE_SHRINK_MIN):
        ctx.degrade(f"{sport}.state", f"{sport}: {label} prune skipped (schedule {n_active} games vs {prev_n} last pruned run)", "warn")
        if n_active > 0:
            guard[sport] = max(n_active, int(prev_n * 0.85))
        return False
    guard[sport] = n_active
    return True


def _active_for(store: dict, sport: str, active_ids: set[str]) -> set[str]:
    """Prune only this sport's keys: the other sport's entries stay as they are."""
    return active_ids | {k.split("|", 1)[0] for k in store if not k.startswith(f"{sport}:")}


def carry_forward_lines(archive: dict, sport: str, active_ids: set[str], scraped_books: Sequence[str],
                       *, now: datetime) -> list[GameLine]:
    """Rebuild main lines for active games from ``archive_last`` for books NOT scraped
    this run. Require a recent quote present in that book's last scrape. Legacy
    state without availability must be scraped once before it can be carried."""
    scraped = set(scraped_books)
    out: list[GameLine] = []
    for key, val in (archive.get("last") or {}).items():
        parts = key.split("|")
        if len(parts) != 4:
            continue
        game_id, market, side, book = parts
        if book in scraped or book == CONSENSUS_BOOK or game_id not in active_ids or not game_id.startswith(f"{sport}:"):
            continue
        if not isinstance(val, dict) or val.get("available") is not True:
            continue
        quoted_at = pstate.parse_utc(val.get("ts"))
        if quoted_at is None or not timedelta(0) <= now - quoted_at <= ODDS_CARRY_MAX_AGE:
            continue
        expires_at = pstate.parse_utc(val.get("expires_at"))
        if "expires_at" in val and (expires_at is None or now > expires_at):
            continue
        odds = val.get("odds")
        if odds is None:
            continue
        try:
            out.append(GameLine(sport=sport, game_id=game_id, book=book, market=market, side=side,
                                odds=int(odds), line=val.get("line"), is_main=True, scraped_at=quoted_at,
                                source_id=val.get("source_id"), outcome_ids=val.get("outcome_ids") or {},
                                source_updated_at=pstate.parse_utc(val.get("source_updated_at")), expires_at=expires_at,
                                actionable_until=pstate.parse_utc(val.get("actionable_until"))))
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
    signal_open_spread: float | None = None,
) -> tuple[dict[str, Any], ImpactV1, signals.Signal, list[str]]:
    """Assemble board wind metadata, impact and signal from resolved inputs."""
    tz = game.tz or (stadium.timezone if stadium and stadium.timezone else "America/New_York")
    kickoff_local = game.kickoff_local if game.kickoff_local.tzinfo is not None else to_tz(game.kickoff_utc, tz)

    if roof_state is None:
        roof_state = game.roof_state or (fc.roof_state if fc else None)
    roof_type = stadium.roof_type if stadium else None
    roof_state = resolve_roof_state(roof_type, roof_state)
    exposed = weather_exposed(roof_type, roof_state)
    is_dome = roof_state in ("dome", "closed") or (stadium is not None and stadium.is_dome)

    temp_fg = fc.temp_fg if fc else None
    wind_fg = fc.wind_fg if fc else None
    rain_fg = fc.rain_fg_mm if fc else None
    # v1 impact stays on the stitch. The published tier follows the process
    # forecast (ECMWF beyond 48 h) when that forecast is present.
    if fc is not None and getattr(fc, "signal_source", None):
        drive_temp = fc.signal_temp_f
        drive_wind = fc.signal_wind_mph
        drive_rain = fc.signal_rain_mm
    else:
        drive_temp, drive_wind, drive_rain = temp_fg, wind_fg, rain_fg
    # Keep the outside forecast for display; it cannot drive covered-field signals.
    signal_temp, signal_wind, signal_rain = (temp_fg, wind_fg, rain_fg) if exposed else (None, None, None)
    if not exposed:
        drive_temp = drive_wind = drive_rain = None

    impact = compute_impact_v1(
        sport=sport,
        month=now_et().month,  # rain suppression keys on the RUN month (legacy generator clock)
        temp_fg=signal_temp,
        wind_fg=signal_wind,
        rain_fg_mm=signal_rain,
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

    if sport == "nfl":
        sig = signals.nfl_signal(drive_wind, drive_temp, drive_rain)
    else:
        sig = signals.cfb_signal(
            drive_wind, drive_temp, drive_rain, signal_open_spread, travel_alt, home_temp, away_temp
        )
    flags = signals.combined_flags(
        sport, drive_wind, drive_temp, signal_open_spread, travel_alt, home_temp, away_temp
    )

    return {"avg_wind": avg_wind, "avg_wind_month": avg_wind_month}, impact, sig, flags


@dataclass
class SportResult:
    """Everything one sport's run produced, for the JSON / D1 writers."""

    sport: str
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
    home_stadiums: dict[str, Stadium] = dataclasses.field(default_factory=dict)  # stadium_id -> home venue of every team row (D1 FK)
    odds_games: list[Game] = dataclasses.field(default_factory=list)   # odds-horizon schedule (superset of ``games``)
    board_new_opener_keys: list[str] | None = None   # opener keys new to the BOARD (alerts); None -> odds.new_opener_keys

    @property
    def season_week(self) -> tuple[int | None, int | None]:
        src = self.games or self.odds_games
        if not src:
            return None, None
        g = min(src, key=lambda x: x.kickoff_utc)
        return g.season, g.week

    @property
    def d1_games(self) -> list[Game]:
        """``games`` rows for D1: every card game plus the out-of-window games that carry
        lines (their impact columns stay NULL until they enter the weather window)."""
        carded = {g.game_id for g in self.games}
        priced = set(self.odds.by_game)
        return [*self.games, *(g for g in self.odds_games if g.game_id not in carded and g.game_id in priced)]



def _evaluate_fair(fair_mod: Any, ctx: RunContext, sport: str, game: Game, lines: Sequence[GameLine],
                   impact: Any, fc: WeatherForecast | None, stadium: Stadium | None,
                   model_version: str = "v1", *, now: datetime | None = None) -> Any | None:
    """``impact`` is an ImpactV1 or ImpactV2; edges are stamped with ``model_version``."""
    if not lines or impact is None:
        return None
    try:
        return fair_mod.evaluate_game(
            sport, game.game_id, list(lines), impact.gs_fg_pct, impact.away_fg_pct,
            rain_c=impact.rain_c,
            wind_vol_fc=fc.wind_vol_fc if fc else None,
            wind_vol_static=stadium.wind_vol_static if stadium else None,
            model_disagreement=fc.model_disagreement if fc else None,
            lead_hours=fc.lead_hours if fc else None,
            model_version=model_version,
            now=now or ctx.now_utc,
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
    roof_type = st.roof_type if st else None
    roof_state = resolve_roof_state(
        roof_type, (rg.roof_state if rg else None) or game.roof_state or (fc.roof_state if fc else None),
    )
    weather = fc if weather_exposed(roof_type, roof_state) else None
    try:
        conf = None
        if fc is not None:
            conf = fair_mod.confidence(fc.wind_vol_fc, fc.model_disagreement, fc.lead_hours,
                                       st.wind_vol_static if st else None)
        return compute_impact_v2(
            sport,
            weather.temp_fg if weather else None,
            weather.wind_fg if weather else None,
            weather.gust_fg if weather else None,
            weather.rain_fg_mm if weather else None,
            weather.precip_prob if weather else None,
            rg.travel_alt if rg is not None else None,
            rg.home_temp if rg is not None else None,
            rg.away_temp if rg is not None else None,
            wind_dir_deg=fc.wind_dir_deg if fc else None,
            wind_dir_fg=fc.wind_dir_fg if fc else None,
            orientation_deg=st.orientation_deg if st else None,
            weakest_wind_effect=st.weakest_wind_effect if st else None,
            precip_prob_ens=extras.get("precip_prob_ens") if weather else None,
            roof_state=roof_state,
            conf=conf,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.degrade("model.v2", f"{game.game_id}: compute_impact_v2 failed ({type(exc).__name__}: {exc})", "warn")
        return None


def _legacy_derived(fair_mod: Any, sport: str, game_odds: dict[str, Any], impact: ImpactV1) -> dict[str, Any] | None:
    fn = getattr(fair_mod, "legacy_derived", None) if fair_mod is not None else None
    if not callable(fn) or not game_odds:
        return None
    now_total = game_odds.get("current_total")
    try:
        return fn(game_odds.get("total_proj"), game_odds.get("spread"), now_total, impact.gs_fg_pct, impact.away_fg_pct)
    except Exception:  # noqa: BLE001
        return None


def previous_run_finished(state_dir: Path, sport: str) -> datetime | None:
    """``finished_at`` of the last run that built ``sport`` (state status.json, which
    rides R2 like the other state files); None on a fresh state."""
    prev = json_out.load_previous_status(state_dir) or {}
    for run in prev.get("runs") or []:
        if not isinstance(run, dict) or run.get("sport") not in (sport, "all"):
            continue
        ts = run.get("finished_at")
        if not isinstance(ts, str) or not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return None


def board_new_opener_keys(res: SportResult, prev_finished: datetime | None, now: datetime) -> list[str]:
    """Opener keys the OPENERS digest should see: recorded this run, or belonging to a
    game that entered the weather window since the previous run (its openers were
    captured earlier on the odds horizon, so ``new_opener_keys`` no longer carries them)."""
    entered = entered_window_ids(res.games, prev_finished, now)
    keys = set(res.odds.new_opener_keys)
    for key in res.odds.openers.get("openers") or {}:
        if key.split("|", 1)[0] in entered:
            keys.add(key)
    return sorted(keys)


def update_histories(ctx: RunContext, sport: str, res: SportResult, state_dir: Path, dry_run: bool) -> None:
    """history.json (odds change-points + model fair) and wx_history.json /
    wx_last.json (weather change-points; ``res.wx_changed`` = D1 rows)."""
    now = utc_iso(ctx.now_utc)
    active = {g.game_id for g in res.games}
    # odds series live for the whole odds horizon (bounded by ODDS_WINDOW_AFTER_D) so
    # the drawer chart starts at the opener, not at window entry
    odds_active = active | {g.game_id for g in res.odds_games}
    with ctx.stage(f"{sport}.history"):
        hist = pstate.load_history(state_dir)
        # book lines + the consensus spread series (book='consensus'), the drawer chart / CLV source
        priced = actionable_lines([ln for ln in res.odds.scraped if ln.is_main], ctx.now_utc)
        n_pts = pstate.update_history(hist, priced + list(res.odds.pseudo), now)
        fairs: dict[str, float] = {}
        for gid, gf in res.fairs.items():
            for market, side in (("total", "over"), ("spread", "home")):
                val = getattr(gf, f"fair_{market}", None)
                if isinstance(val, (int, float)):
                    fairs[f"{gid}|{market}|{side}"] = float(val)
        n_fair = pstate.update_fair_history(hist, fairs, now)
        if _prune_allowed(hist, sport, len(active), ctx, "history"):
            pstate.prune_history(hist, _active_for(hist.get("series") or {}, sport, odds_active) | _active_for(hist.get("fair_series") or {}, sport, active))
        res.board_new_opener_keys = board_new_opener_keys(res, previous_run_finished(state_dir, sport), ctx.now_utc)
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
    ingest_dir: Path | None = None,
) -> SportResult:
    with ctx.stage(f"{sport}.stadiums"):
        book = stage_stadiums(ctx, sport)

    with ctx.stage(f"{sport}.schedule"):
        games, odds_games = stage_schedule(ctx, sport, raw, season, book)

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

    # CFB selection uses the same current opening consensus as its signal model.
    # Capture newly arriving book openers before weather; unknowns stay eligible.
    odds = stage_odds(ctx, sport, odds_games, book, raw, books, state_dir, season, dry_run=ctx.dry_run,
                      ingest_dir=ingest_dir) if sport == "cfb" else None
    wx_extras: dict[str, dict[str, Any]] = {}
    with ctx.stage(f"{sport}.weather"):
        forecasts = stage_weather(ctx, sport, games, stadiums, raw, roof_states, extras=wx_extras, state_dir=state_dir,
                                  openers=odds.openers if odds is not None else None)

    if odds is None:
        odds = stage_odds(ctx, sport, odds_games, book, raw, books, state_dir, season, dry_run=ctx.dry_run,
                      ingest_dir=ingest_dir)
    if books:
        carded_ids = {g.game_id for g in games}
        n_priced = len(odds.by_game)
        n_carded = sum(1 for gid in odds.by_game if gid in carded_ids)
        ctx.count("odds_games", sport, n_priced)
        print(f"  odds games {sport}: {n_priced} with lines within {ODDS_WINDOW_AFTER_D:.0f} d "
              f"({len(odds_games)} scheduled), {n_carded} of them on the {WINDOW_AFTER_D:.0f}-day board")

    rows: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    impacts: dict[str, ImpactV1] = {}
    impacts_v2: dict[str, ImpactV2] = {}
    fairs: dict[str, Any] = {}
    fairs_v2: dict[str, Any] = {}
    teams: dict[str, Team] = {}
    home_stadiums: dict[str, Stadium] = {}
    alert_model = model_config.alert_model()
    # One pricing clock for consensus, both model versions and total comparisons.
    # Later serialization, upload and reads preserve this snapshot verbatim.
    priced_at = odds.priced_at or ctx.now_utc
    with ctx.stage(f"{sport}.impact"):
        for g in games:
            rg = resolved[g.game_id]
            fc = forecasts.get(g.game_id)
            game_lines = odds.by_game.get(g.game_id, [])
            pricing_by_game = {g.game_id: fair_mod._pricing_lines(game_lines, priced_at)}
            game_odds = card_odds(sport, g.game_id, pricing_by_game, odds.consensus) if books else {}
            signal_open_spread = (
                json_out.consensus_spread_opener(g.game_id, odds.openers)[0] if sport == "cfb" else None
            )
            if rg is None:
                wind_meta, impact, sig, flags = build_record(
                    sport, g, None, None, None, fc, signal_open_spread=signal_open_spread
                )
                card_kwargs: dict[str, Any] = {}
            else:
                wind_meta, impact, sig, flags = build_record(
                    sport, g, rg.stadium, rg.home_team, rg.away_team, fc,
                    travel_alt=rg.travel_alt, home_temp=rg.home_temp, away_temp=rg.away_temp,
                    roof_state=rg.roof_state, wind_avg=rg.wind_avg,
                    signal_open_spread=signal_open_spread,
                )
                for t in (rg.home_team, rg.away_team):
                    if t is not None:
                        teams[t.team_id] = t
                        # teams.home_stadium_id is a D1 foreign key: the away side's home venue
                        # is not a game venue this week, so carry it into the stadiums batch.
                        hs = book.stadiums.get(t.home_stadium_id) if (book is not None and t.home_stadium_id) else None
                        if hs is not None:
                            home_stadiums[hs.stadium_id] = hs
                card_kwargs = {"travel_alt": rg.travel_alt, "home_temp": rg.home_temp, "away_temp": rg.away_temp,
                               "roof_state": rg.roof_state, "avg_wind_month": wind_meta["avg_wind_month"]}
            impacts[g.game_id] = impact
            impact2 = _compute_v2(ctx, sport, g, rg, fc, impact, wx_extras.get(g.game_id))
            impacts_v2[g.game_id] = impact2
            gf = _evaluate_fair(fair_mod, ctx, sport, g, game_lines, impact, fc, rg.stadium if rg else None, now=priced_at)
            gf2 = _evaluate_fair(fair_mod, ctx, sport, g, game_lines, impact2, fc, rg.stadium if rg else None,
                                 model_version="v2", now=priced_at)
            if gf2 is not None:
                fairs_v2[g.game_id] = gf2
            # ALERT_MODEL selects which fair/edges feed histories, D1 odds rows and alerts
            gf_alert = gf2 if alert_model == "v2" else gf
            if gf_alert is not None:
                fairs[g.game_id] = gf_alert
            cards.append(json_out.build_card(
                sport, g, rg.stadium if rg else None, rg.home_team if rg else None, rg.away_team if rg else None,
                fc, impact, sig, flags, lines=game_lines, openers=odds.openers, consensus=odds.consensus, fair=gf_alert,
                legacy_derived=_legacy_derived(fair_mod, sport, game_odds, impact), avg_wind=wind_meta["avg_wind"],
                run_id=ctx.run_id, impact_v2=impact2, fair_v2=gf2, model_version=alert_model,
                last_observed=odds.last_observed.get(g.game_id), priced_at=priced_at, **card_kwargs,
            ))
            tz = g.tz or (rg.stadium.timezone if rg and rg.stadium and rg.stadium.timezone else "America/New_York")
            kickoff_local = g.kickoff_local if g.kickoff_local.tzinfo is not None else to_tz(g.kickoff_utc, tz)
            rows.append({
                "game_id": g.game_id,
                "game": f"{_name_for(rg.away_team if rg else None, g.away_id)} @ {_name_for(rg.home_team if rg else None, g.home_id)}",
                "kickoff": kickoff_local.isoformat(),
                "temp_fg": fc.temp_fg if fc else None,
                "wind_fg": fc.wind_fg if fc else None,
                "rain_fg": fc.rain_fg_mm if fc else None,
                "gs_fg_pct": impact.gs_fg_pct,
                "away_fg_pct": impact.away_fg_pct,
                "gs_fg_v2": impact2.gs_fg_pct if impact2 else None,
                "away_fg_v2": impact2.away_fg_pct if impact2 else None,
                "signal": sig.label,
                "flags": flags,
                "spread": game_odds.get("spread"),
                "spread_src": game_odds.get("spread_src"),
                "total": game_odds.get("total_proj"),
                "ref_book": game_odds.get("ref_book"),
            })
    if books:
        priced = sum(1 for row in rows if row["total"] is not None)
        ctx.count("priced", sport, priced)
        if rows and priced == 0:
            ctx.degrade("odds", f"{sport}: no game received a consensus total", "warn")
    # Retain the published count key for existing board/status consumers.
    ctx.count("legacy", sport, len(cards))
    res = SportResult(sport, rows, cards, games, stadiums, teams, forecasts, impacts, odds, fairs,
                      impacts_v2={k: v for k, v in impacts_v2.items() if v is not None}, fairs_v2=fairs_v2,
                      wx_extras=wx_extras, home_stadiums=home_stadiums, odds_games=odds_games)
    ctx.count("impact_v2", sport, len(res.impacts_v2))
    n_ens = sum(1 for e in wx_extras.values() if e.get("ensemble"))
    if forecasts and n_ens == 0:
        ctx.degrade("weather", f"{sport}: no game has ensemble spread; wind_vol_fc static for all", "info")
    update_histories(ctx, sport, res, state_dir, ctx.dry_run)
    return res


# ---- CLI ------------------------------------------------------------------------

def run_alert_stage(ctx: RunContext, results: Sequence[SportResult], state_dir: Path, *, enabled: bool, dry_run: bool,
                    stdout: bool = False, now: datetime | None = None) -> alerts_mod.AlertsRun | None:
    """First notices, scheduled reminders and confirmed-bet CLEAR notices over this
    run's GameCards; stamps ``card["alerts"]`` with the open record keys per game.
    Never fatal: a failure is a warn Degradation and the board still publishes."""
    with ctx.stage("alerts"):
        try:
            run = alerts_mod.run_alerts(
                ctx, {r.sport: r.cards for r in results}, state_dir, enabled=enabled, dry_run=dry_run,
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
            odds = f" sp={c.get('spread')!s:>6} ({c.get('spread_src') or '-'}) tot={c.get('total')!s:>5} [{c.get('ref_book') or '-'}]"
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
        # card games + out-of-window games carrying lines (impact columns NULL) so
        # odds_history / openers rows always join to a games row
        games += d1_out.game_rows(res.d1_games, now, impacts=res.impacts, impacts_v2=res.impacts_v2)
        stadiums += d1_out.stadium_rows([*res.stadiums.values(), *res.home_stadiums.values()], now)
        teams += d1_out.team_rows(res.teams.values(), now)
        edges = [e for gf in res.fairs.values() for e in (getattr(gf, "edges", None) or [])]
        odds_rows += d1_out.odds_rows(res.odds.deltas, now, ctx.run_id, edges)
        wx_rows += res.wx_changed
        opener_keys = set(res.odds.new_opener_keys) | set(res.odds.opener_update_keys)
        opener_rows += d1_out.opener_rows(res.odds.openers, sorted(opener_keys), ctx.run_id)
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
            baselines[r.sport] = pstate.load_baseline(state_dir, baseline_scope(r.sport, r.games, r.odds_games))
        except Exception:  # noqa: BLE001
            continue
    books = json_out.books_status(ctx.counts, book_list, baselines, utc_iso(finished),
                                  previous=(prev_meta or {}).get("books"))
    seasons = [r.season_week for r in results if r.season_week[0] is not None]
    season, week = min(seasons) if seasons else (None, None)
    try:
        book_health = pstate.book_health_export(pstate.load_book_health(state_dir))
    except Exception:  # noqa: BLE001
        book_health = {}
    weeks = json_out.current_weeks({r.sport: (r.games or r.odds_games) for r in results}, ctx.now_utc)
    extra = {"book_health": book_health, "weeks": weeks,
             "books_requested": list(book_list), "n_lines": sum(len(r.odds.scraped) for r in results)}
    ecmwf_status = ctx.weather_state.get("ecmwf_opendata")
    if ecmwf_status:
        extra["ecmwf_opendata"] = ecmwf_status
    meta = json_out.build_meta(ctx, sport_counts, books, season=season, week=week, finished_at=finished, extra=extra)
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
        if name == "cf_heartbeat":
            continue  # Worker-owned; builds read it but cannot overwrite newer ticks.
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
    weather_handoff_dir: Path | None = None,
    reuse_weather_dir: Path | None = None,
    ingest_dir: Path | None = None,
) -> int:
    ctx = RunContext(sport="all" if len(sports) > 1 else sports[0], scope=scope, dry_run=dry_run,
                     run_id=run_id or "", **({"started_at": started_at} if started_at else {}))
    ctx.weather_state.update(write_point_dir=weather_handoff_dir, reuse_point_dir=reuse_weather_dir)
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

    written: list[Path] = []
    results: list[SportResult] = []
    raw_files: dict[str, Path] = {}
    for sport in sports:
        raw: RawStore = NullRawStore(sport, ctx.run_id) if dry_run else RawStore(sport, ctx.run_id, raw_dir, mirror=mirror)
        # ingest_dir only when set: keeps run_sport's long-standing call shape otherwise.
        res = run_sport(ctx, sport, raw, season, books=book_list, state_dir=state_dir,
                        **({"ingest_dir": ingest_dir} if ingest_dir is not None else {}))
        results.append(res)
        if print_rows:
            print(f"== {sport} ({len(res.cards)} games)")
            _print_cards(res.rows)
        if dry_run:
            continue
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
    p.add_argument("--ecmwf-opendata", choices=("auto", "force", "off"), default=None,
                   help="ECMWF source: auto falls back to ECMWF Open Data on Open-Meteo error, 429, or stale data; force always uses it; off disables it")
    p.add_argument("--weather-handoff-dir", type=Path, help="save raw point inputs for this workflow's dependent job")
    p.add_argument("--reuse-weather-dir", type=Path, help="reuse matching same-workflow raw point inputs only")
    p.add_argument("--scrape-only", type=Path, metavar="DIR",
                   help="scrape the Playwright books only (raw captures + DIR/{sport}/{book}.json handoff); "
                        "no schedule, state, outputs or R2")
    p.add_argument("--ingest-lines", type=Path, metavar="DIR",
                   help="read Playwright-book lines from a --scrape-only handoff instead of scraping them")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_repo_dotenv()
    args = parse_args(argv)
    if args.ecmwf_opendata:
        os.environ["ECMWF_OPENDATA_FALLBACK"] = args.ecmwf_opendata
    sports = list(SPORTS_ALL) if args.sport == "all" else [args.sport]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    books = [b for b in args.books.split(",")] if args.books else None
    if args.scrape_only is not None:
        return scrape_only(sports, args.scrape_only, books=books, raw_dir=args.raw_dir, run_id=args.run_id)
    return build(
        sports,
        scope=args.scope,
        raw_dir=args.raw_dir,
        dry_run=args.dry_run,
        print_rows=args.print_rows,
        run_id=args.run_id,
        season=args.season,
        books=books,
        state_dir=args.state_dir,
        alerts=args.alerts,
        alerts_stdout=args.alerts_stdout,
        board_dir=args.board_dir,
        snapshot_dir=args.snapshot_dir,
        d1_sql=args.d1_sql,
        publish=args.publish,
        merge_into_r2=args.merge_into_r2,
        force=args.force,
        weather_handoff_dir=args.weather_handoff_dir,
        reuse_weather_dir=args.reuse_weather_dir,
        ingest_dir=args.ingest_lines,
    )


if __name__ == "__main__":
    sys.exit(main())
