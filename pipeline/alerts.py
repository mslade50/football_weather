"""Telegram alert engine (ARCHITECTURE §10).

    python -m pipeline.alerts --digest clv [--state-dir data/state] [--backtest data/board/backtest.json] [--dry-run]
                                                                            # weekly CLV digest (backtest.yml, Monday)
    python -m pipeline.alerts --flush  [--state-dir data/state] [--dry-run]   # flush the quiet-hours queue

Families (dedup key → one send per key, markers in ``alerts.json`` round-tripped
through R2 and mirrored to D1 ``alerts``):

    EDGE     edge|{season}|{week}|{game_id}|{market}|{side}|{book}|{model_version}
    MOVE     move|{edge_key}|{bucket}      bucket = floor(|line_now − first_alerted_line| / step),
                                           step 1.0 total / 0.5 spread, ≤1 per edge key per 2 h
                                           (bucket vs the FIRST line so consecutive 1-pt moves get
                                           distinct keys; direction vs the last line we messaged)
    GONE     gone|{edge_key}               edge_pts < 0.5 → record status=closed
    WX       wx|{edge_key}|{bucket}        fair line moved ≥1.0 pt (weather), bucket = floor(|Δfair|)
    OPENERS  openers|{sport}|{season}|{week}|{ET-day}
    OPS      degr|{component}|{reason}|{ET-day}, heartbeat|{ET-day}, stadium|{game_id},
             names|{book}|{ET-day}, noref|{sport}|{ET-day}

MOVE / GONE / WX are evaluated ONLY for games that carry an open EDGE record
(``state.open_edge_records``), never for the whole board.

Pipeline: ``collect_candidates`` (pure: GameCards in → ``Candidate`` list out) →
``plan`` (dedup, quiet hours → ``telegram_state.json`` queue, 25-per-run cap →
digest) → ``dispatch`` (send, mark ONLY after a successful send — the golf
``_alert_once`` closure — record + feed). ``--no-alerts`` / ``--dry-run`` print
the candidates with their keys instead of sending.

Chat routing: ``TELEGRAM_CHAT_ID_NFL`` / ``TELEGRAM_CHAT_ID_CFB`` fall back to
``TELEGRAM_CHAT_ID``; OPS alerts always go to the default chat.

Impact numbers / components / emoji in every message come from the active alert
model's block — ``card["impact"][alert_model()]`` (``pipeline.model.config``), falling
back to v1 when the card has no such block — and the impact line names the version
it shows (``(wind 6.5 · v1)``).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from pipeline import state as pstate
from pipeline.model import config as model_config
from utils.timeutil import ET, ensure_utc, now_utc, parse_iso, to_et, utc_iso

logger = logging.getLogger(__name__)

# ---- constants ---------------------------------------------------------------------

MAX_PER_RUN = 25                 # messages per run; overflow → one digest (counted in the 25)
QUIET_START_H = 23               # 23:00 ET ..
QUIET_END_H = 7                  # .. 07:00 ET
BYPASS_KICKOFF_H = 3.0           # kickoff < 3 h bypasses quiet hours
MOVE_STEP = {"total": 1.0, "spread": 0.5}
MOVE_COOLDOWN_H = 2.0
GONE_EDGE_PTS = 0.5
WX_STEP = 1.0
OPENER_GS_MAX = -2.0             # digest only games with gs_fg ≤ -2 ...
OPENER_WIND_MIN = 12.0           # ... or wind_fg ≥ 12
STALE_HOURS = 20.0
TELEGRAM_MAX_CHARS = 4000        # API limit 4096; leave headroom for the header
ALERTED_TIERS = ("edge", "strong")
DEFAULT_BOARD_URL = "https://football-board.mckinleyslade.workers.dev"

FAMILY_PRIORITY = {"edge": 0, "wx": 1, "move": 2, "gone": 3, "openers": 4, "ops": 5}

BOOK_LABELS = {
    "betonline": "BetOnline", "betcris": "Betcris", "fanduel": "FD", "draftkings": "DK", "kalshi": "Kalshi",
    "novig": "Novig", "prophetx": "ProphetX", "pinnacle": "Pinnacle", "consensus": "Consensus",
}
CENTS_BOOKS = {"kalshi"}   # contract prices in cents
SPORT_LABEL = {"nfl": "NFL", "cfb": "CFB"}
MINUS = "−"


# ---- data ----------------------------------------------------------------------------

@dataclass
class Candidate:
    key: str
    family: str
    sport: str
    text: str
    game_id: Optional[str] = None
    tier: Optional[str] = None
    kickoff_utc: Optional[datetime] = None
    record: dict[str, Any] = field(default_factory=dict)   # fields for the alerts record / D1 row
    status: str = "open"
    summary: str = ""                                       # one-liner used inside digests

    def __post_init__(self) -> None:
        if not self.summary:
            self.summary = _summary(self.text, 3 if self.family == "edge" else 1)

    @property
    def bypass_quiet(self) -> bool:
        return self.tier == "strong" or self.family == "gone"


@dataclass
class Plan:
    send: list[Candidate] = field(default_factory=list)          # individual messages
    digest: list[Candidate] = field(default_factory=list)        # overflow → one message
    ops: list[Candidate] = field(default_factory=list)           # OPS notices → one grouped message
    flush: list[dict[str, Any]] = field(default_factory=list)    # queued items released this run
    queued: list[Candidate] = field(default_factory=list)        # parked for quiet hours
    skipped: list[str] = field(default_factory=list)             # already sent


@dataclass
class Outcome:
    sent: list[Candidate] = field(default_factory=list)
    failed: list[Candidate] = field(default_factory=list)
    n_messages: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)  # records touched this run (→ D1 upsert)

    @property
    def n_sent(self) -> int:
        return len(self.sent)


@dataclass
class Config:
    board_url: str = DEFAULT_BOARD_URL
    chat_default: Optional[str] = None
    chat_by_sport: dict[str, str] = field(default_factory=dict)
    max_per_run: int = MAX_PER_RUN

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> Config:
        e = os.environ if env is None else env
        by_sport = {}
        for sport in ("nfl", "cfb"):
            v = e.get(f"TELEGRAM_CHAT_ID_{sport.upper()}")
            if v:
                by_sport[sport] = v
        return cls(
            board_url=(e.get("BOARD_URL") or DEFAULT_BOARD_URL).rstrip("/"),
            chat_default=e.get("TELEGRAM_CHAT_ID") or None,
            chat_by_sport=by_sport,
        )

    def chat_for(self, sport: Optional[str]) -> Optional[str]:
        return self.chat_by_sport.get(sport or "", self.chat_default)


# ---- small helpers --------------------------------------------------------------------

def edge_key(season: Any, week: Any, game_id: str, market: str, side: str, book: str, model_version: str = "v1") -> str:
    return f"edge|{season}|{week}|{game_id}|{market}|{side}|{book}|{model_version}"


def parse_edge_key(key: str) -> Optional[dict[str, str]]:
    parts = key.split("|")
    if len(parts) != 8 or parts[0] != "edge":
        return None
    return dict(zip(("season", "week", "game_id", "market", "side", "book", "model_version"), parts[1:], strict=True))


def et_day(now: datetime) -> str:
    return to_et(now).strftime("%Y-%m-%d")


def in_quiet_hours(now: datetime) -> bool:
    h = to_et(now).hour
    return h >= QUIET_START_H or h < QUIET_END_H


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "x"


# Degradations that are expected or already reported elsewhere: the off-season game window,
# optional API keys, books disabled via BOOK_*_ENABLED, and a book returning 0 lines (the
# volume check pages once, edge-triggered, when a book goes dark while peers report). They
# stay on the Status page and never page Telegram.
OPS_EXPECTED_SUBSTRINGS = ("games within window", "api key missing", "api_key missing", "disabled via",
                           "returned 0 lines")


def _ops_expected(reason: str) -> bool:
    r = (reason or "").lower()
    return any(s in r for s in OPS_EXPECTED_SUBSTRINGS)


def _stable(reason: str) -> str:
    """Drop numbers so a count that changes run to run does not mint a new alert key."""
    return re.sub(r"\d+", "", reason or "")


def _dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return ensure_utc(v)
    if isinstance(v, str) and v:
        try:
            return ensure_utc(parse_iso(v))
        except ValueError:
            return None
    return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _fmt_odds(o: Any) -> str:
    n = _num(o)
    if n is None:
        return "?"
    n = int(round(n))
    return f"+{n}" if n > 0 else f"{MINUS}{abs(n)}"


def _fmt_line(x: Any, signed: bool = False) -> str:
    n = _num(x)
    if n is None:
        return "?"
    s = f"{abs(n):.1f}".rstrip("0").rstrip(".") if n != int(n) else f"{int(abs(n))}"
    if n < 0:
        return f"{MINUS}{s}"
    return f"+{s}" if signed else s


def _fmt_pct(x: Any, digits: int = 1) -> str:
    n = _num(x)
    if n is None:
        return "?"
    return f"{'+' if n > 0 else (MINUS if n < 0 else '')}{abs(n):.{digits}f}%"


def _fmt_signed(x: Any, digits: int = 1) -> str:
    n = _num(x)
    if n is None:
        return "?"
    return f"{'+' if n > 0 else (MINUS if n < 0 else '')}{abs(n):.{digits}f}"


def _summary(text: str, idx: int) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("<a ")]
    if not lines:
        return text
    pick = [lines[0]] + ([lines[idx]] if 0 < idx < len(lines) else lines[1:2])
    return " · ".join(pick)


def _book_label(book: str) -> str:
    return BOOK_LABELS.get(book, book.title() if book else "?")


def _kick_label(card: dict[str, Any]) -> str:
    """'Sun 1:00p ET' from the card's kickoff_utc."""
    k = _dt(card.get("kickoff_utc"))
    if k is None:
        return "?"
    et = k.astimezone(ET)
    h12 = et.hour % 12 or 12
    return f"{et.strftime('%a')} {h12}:{et.minute:02d}{'a' if et.hour < 12 else 'p'} ET"


def _matchup(card: dict[str, Any]) -> str:
    away = (card.get("away") or {}).get("short") or (card.get("away") or {}).get("team_id") or "?"
    home = (card.get("home") or {}).get("short") or (card.get("home") or {}).get("team_id") or "?"
    return f"{away} @ {home}"


def _header(card: dict[str, Any], emoji: str = "") -> str:
    sport = SPORT_LABEL.get(card.get("sport") or "", str(card.get("sport") or "").upper())
    lead = f"{emoji} " if emoji else ""
    return f"<b>{lead}{sport} Wk {card.get('week')} · {html.escape(_matchup(card))} · {_kick_label(card)}</b>"


def board_link(board_url: str, card: dict[str, Any]) -> str:
    href = f"{board_url}/#sport={card.get('sport')}&week={card.get('week')}&game={card.get('game_id')}"
    return f'<a href="{html.escape(href, quote=True)}">board</a>'


def _impact_block(card: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(model_version, impact block)`` for the active alert model (``ALERT_MODEL``,
    ``pipeline.model.config.alert_model``); a card without that block falls back to v1."""
    blocks = card.get("impact") or {}
    want = model_config.alert_model()
    blk = blocks.get(want)
    if blk:
        return want, blk
    return model_config.MODEL_VERSION_V1, (blocks.get(model_config.MODEL_VERSION_V1) or {})


def _impact(card: dict[str, Any]) -> dict[str, Any]:
    return _impact_block(card)[1]


def _components(card: dict[str, Any]) -> dict[str, float]:
    comps = _impact(card).get("components") or {}
    return {k: v for k, v in comps.items() if _num(v) is not None and v > 0}


def _emoji_for(card: dict[str, Any]) -> str:
    comps = _components(card)
    if not comps:
        return "📈"
    top = max(comps, key=lambda k: comps[k])
    return {"wind": "🌬", "rain": "🌧", "cold": "🥶", "cold_away": "🥶", "heat": "🔥", "heat_away": "🔥", "alt": "⛰"}.get(top, "📈")


def _wx_line(card: dict[str, Any]) -> str:
    wx = card.get("weather") or {}
    st = card.get("stadium") or {}
    name = html.escape(str(st.get("name") or "?"))
    if (card.get("stadium") or {}).get("roof_state") in ("closed", "dome"):
        return f"{name} · roof closed"
    bits = [f"wind {_fmt_line(wx.get('wind_fg'))} mph {wx.get('wind_dir_fg') or ''}".strip()]
    inner = []
    if _num(wx.get("gust_fg")) is not None:
        inner.append(f"gust {_fmt_line(wx.get('gust_fg'))}")
    if _num(wx.get("wind_vol_fc")) is not None:
        inner.append(f"vol {_fmt_line(wx.get('wind_vol_fc'))}")
    if _num(wx.get("cross_mph")) is not None:
        inner.append(f"cross {_fmt_line(wx.get('cross_mph'))}")
    if inner:
        bits[0] += f" ({' · '.join(inner)})"
    if _num(wx.get("temp_fg")) is not None:
        bits.append(f"{round(wx['temp_fg'])}°F")
    pop = _num(wx.get("precip_prob"))
    mm = _num(wx.get("rain_fg"))
    if pop is not None or mm is not None:
        pop_s = f"{round(pop * 100)}%" if pop is not None else "?"
        bits.append(f"rain {pop_s} / {mm if mm is None else round(mm, 1)} mm")
    return f"{name} · " + " · ".join(bits)


def _impact_line(card: dict[str, Any], edge: dict[str, Any]) -> str:
    """'Impact −6.5% (wind 6.5 · v1) · conf 0.72 · fair total 34.6 (ref pinnacle, 6 books)' — the
    impact block of the active alert model, labelled with the version actually shown."""
    version, imp = _impact_block(card)
    comps = _components(card)
    comp_s = " ".join(f"{k} {v:.1f}" for k, v in sorted(comps.items(), key=lambda kv: -kv[1])[:3])
    label = f"{comp_s} · {version}" if comp_s else version
    gs = _fmt_pct(imp.get("gs_fg_pct"))
    fair = card.get("fair") or {}
    market = edge.get("market")
    fair_val = edge.get("fair_line")
    if fair_val is None:
        # v2 fair lines live beside v1's in the card (fair_total_v2 / fair_spread_v2)
        fair_val = fair.get(f"fair_{market}_v2") if version == model_config.MODEL_VERSION_V2 else None
        fair_val = fair_val if fair_val is not None else fair.get(f"fair_{market}")
    conf = _num(edge.get("confidence"))
    conf_s = f"{conf:.2f}" if conf is not None else "?"
    ref = edge.get("ref_book") or (card.get("consensus") or {}).get("ref_book") or "?"
    n_books = edge.get("n_books") or (card.get("consensus") or {}).get("n_books") or 0
    return (f"Impact {gs} ({label}) · conf {conf_s} · fair {market} {_fmt_line(fair_val, signed=market == 'spread')} "
            f"(ref {ref}, {n_books} books)")


def _side_label(edge: dict[str, Any], card: dict[str, Any]) -> str:
    side = edge.get("side")
    if side in ("over", "under"):
        return str(side).upper()
    team = (card.get(side) or {}).get("short") or str(side).upper()
    return f"{team}"


def _opener_for(card: dict[str, Any], edge: dict[str, Any]) -> Optional[float]:
    book = (card.get("odds") or {}).get(edge.get("book")) or {}
    m = book.get(edge.get("market")) or {}
    return _num(m.get("open_line"))


def _other_books(card: dict[str, Any], edge: dict[str, Any]) -> list[str]:
    """'Best: Under 38.5 −108 Betcris · FD 38.0 · Kalshi 37.5 (52¢)' — same market/side at the other books."""
    market, side, book = edge.get("market"), edge.get("side"), edge.get("book")
    peers = [e for e in ((card.get("fair") or {}).get("edges") or [])
             if e.get("market") == market and e.get("side") == side and e.get("book") != book and _num(e.get("line")) is not None]
    if not peers:
        return []
    peers.sort(key=lambda e: -(_num(e.get("edge_pts")) or -99))
    out = []
    for i, e in enumerate(peers):
        lbl = _book_label(e.get("book"))
        if i == 0:
            out.append(f"{str(side).title()} {_fmt_line(e.get('line'), signed=market == 'spread')} {_fmt_odds(e.get('odds'))} {lbl}")
        elif e.get("book") in CENTS_BOOKS and _num(e.get("vigfree_prob")) is not None:
            out.append(f"{lbl} {_fmt_line(e.get('line'), signed=market == 'spread')} ({round(e['vigfree_prob'] * 100)}¢)")
        else:
            out.append(f"{lbl} {_fmt_line(e.get('line'), signed=market == 'spread')}")
    return out


# ---- formatters --------------------------------------------------------------------------

def format_edge(card: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL) -> str:
    market = edge.get("market")
    bet = (f"<b>{_side_label(edge, card)} {_fmt_line(edge.get('line'), signed=market == 'spread')} "
           f"{_fmt_odds(edge.get('odds'))} @ {_book_label(edge.get('book'))}</b>")
    tier = " · <b>STRONG</b>" if edge.get("tier") == "strong" else ""
    edge_s = f"edge {_fmt_line(edge.get('edge_pts'))} pts / {_fmt_pct((_num(edge.get('edge_prob')) or 0.0) * 100)}"
    opener = _opener_for(card, edge)
    open_s = f" · open {_fmt_line(opener, signed=market == 'spread')}" if opener is not None else ""
    lines = [
        _header(card, _emoji_for(card)),
        _wx_line(card),
        _impact_line(card, edge),
        f"{bet} · {edge_s}{open_s}{tier}",
    ]
    others = _other_books(card, edge)
    if others:
        lines.append("Best: " + " · ".join(others))
    lines.append(board_link(board_url, card))
    return "\n".join(lines)


def format_move(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], direction: str,
                board_url: str = DEFAULT_BOARD_URL) -> str:
    market = edge.get("market")
    signed = market == "spread"
    lines = [
        _header(card, "↕️"),
        (f"<b>{_side_label(edge, card)} @ {_book_label(edge.get('book'))}</b> moved "
         f"{_fmt_line(rec.get('last_line'), signed)} → {_fmt_line(edge.get('line'), signed)} ({direction})"),
        (f"fair {_fmt_line(edge.get('fair_line'), signed)} · edge now {_fmt_line(edge.get('edge_pts'))} pts "
         f"(was {_fmt_line(rec.get('last_edge'))}) · {_fmt_odds(edge.get('odds'))}"),
        board_link(board_url, card),
    ]
    return "\n".join(lines)


def format_gone(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL) -> str:
    market = edge.get("market")
    signed = market == "spread"
    lines = [
        _header(card, "🚫"),
        (f"EDGE GONE: <b>{_side_label(edge, card)} {_fmt_line(edge.get('line'), signed)} @ {_book_label(edge.get('book'))}</b> "
         f"· edge {_fmt_line(edge.get('edge_pts'))} pts (alerted at {_fmt_line(rec.get('first_line'), signed)}, "
         f"{_fmt_line(rec.get('first_edge'))} pts)"),
        board_link(board_url, card),
    ]
    return "\n".join(lines)


def format_wx_move(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL) -> str:
    wx = card.get("weather") or {}
    market = edge.get("market")
    signed = market == "spread"
    old_w, new_w = _fmt_line(rec.get("last_wind")), _fmt_line(wx.get("wind_fg"))
    old_r, new_r = _fmt_line(rec.get("last_rain")), _fmt_line(wx.get("rain_fg"))
    lines = [
        _header(card, "🌦"),
        f"FORECAST MOVE: wind {old_w} → {new_w} mph · rain {old_r} → {new_r} mm",
        (f"fair {market} {_fmt_line(rec.get('last_fair'), signed)} → {_fmt_line(edge.get('fair_line'), signed)} · "
         f"<b>{_side_label(edge, card)} {_fmt_line(edge.get('line'), signed)} @ {_book_label(edge.get('book'))}</b> "
         f"edge {_fmt_line(edge.get('edge_pts'))} pts"),
        board_link(board_url, card),
    ]
    return "\n".join(lines)


def format_openers(sport: str, season: Any, week: Any, items: Sequence[tuple[dict[str, Any], list[str]]],
                   board_url: str = DEFAULT_BOARD_URL) -> str:
    head = f"<b>📋 {SPORT_LABEL.get(sport, sport.upper())} Wk {week} openers · {len(items)} weather game(s)</b>"
    rows = []
    for card, keys in items:
        wx = card.get("weather") or {}
        imp = _impact(card)
        books = sorted({k.split("|")[3] for k in keys if len(k.split("|")) == 4})
        cons = card.get("consensus") or {}
        rows.append(
            f"{html.escape(_matchup(card))} {_kick_label(card)} · wind {_fmt_line(wx.get('wind_fg'))} · "
            f"{_fmt_pct(imp.get('gs_fg_pct'))} · tot {_fmt_line(cons.get('total_now'))} sp {_fmt_line(cons.get('spread_now'), True)} · "
            f"{', '.join(_book_label(b) for b in books)}"
        )
    link = f'<a href="{html.escape(board_url + "/#sport=" + sport + "&week=" + str(week), quote=True)}">board</a>'
    return "\n".join([head, *rows, link])


def format_ops(title: str, body: str = "") -> str:
    return f"⚠️ <b>{html.escape(title)}</b>" + (f"\n{html.escape(body)}" if body else "")


def format_digest(title: str, items: Sequence[str]) -> list[str]:
    """One or more messages (Telegram 4096-char limit) with ``title`` + numbered items."""
    msgs: list[str] = []
    cur = f"<b>{html.escape(title)} ({len(items)})</b>"
    for i, it in enumerate(items, 1):
        piece = f"\n\n{i}. {it}"
        if len(cur) + len(piece) > TELEGRAM_MAX_CHARS:
            msgs.append(cur)
            cur = f"<b>{html.escape(title)} (cont.)</b>{piece}"
        else:
            cur += piece
    msgs.append(cur)
    return msgs


# ---- candidate collection (pure) ---------------------------------------------------------------

def _alertable_edges(card: dict[str, Any]) -> list[dict[str, Any]]:
    fair = card.get("fair") or {}
    if not fair.get("weather_driven"):
        return []
    if (card.get("consensus") or {}).get("thin"):
        return []
    out = []
    for e in fair.get("edges") or []:
        if e.get("tier") in ALERTED_TIERS and e.get("market") in ("total", "spread") and _num(e.get("edge_pts")) is not None:
            out.append(e)
    return out


def _edge_record(card: dict[str, Any], e: dict[str, Any], run_id: Optional[str]) -> dict[str, Any]:
    wx = card.get("weather") or {}
    return {
        "family": "edge", "game_id": card.get("game_id"), "sport": card.get("sport"), "season": card.get("season"),
        "week": card.get("week"), "market": e.get("market"), "side": e.get("side"), "book": e.get("book"),
        "tier": e.get("tier"), "model_version": e.get("model_version") or "v1",
        "last_line": _num(e.get("line")), "last_odds": e.get("odds"), "last_fair": _num(e.get("fair_line")),
        "last_edge": _num(e.get("edge_pts")), "status": "open", "run_id": run_id,
        "last_wind": _num(wx.get("wind_fg")), "last_rain": _num(wx.get("rain_fg")),
        "kickoff_utc": card.get("kickoff_utc"),
    }


def edge_candidates(card: dict[str, Any], alerts: dict, cfg: Config, run_id: Optional[str] = None) -> list[Candidate]:
    out = []
    for e in _alertable_edges(card):
        key = edge_key(card.get("season"), card.get("week"), card.get("game_id"), e["market"], e["side"], e["book"],
                       e.get("model_version") or "v1")
        if pstate.alert_sent(alerts, key):
            continue
        out.append(Candidate(key, "edge", card.get("sport") or "", format_edge(card, e, cfg.board_url),
                             game_id=card.get("game_id"), tier=e.get("tier"), kickoff_utc=_dt(card.get("kickoff_utc")),
                             record=_edge_record(card, e, run_id)))
    return out


def _edge_for_record(card: dict[str, Any], rec: dict[str, Any]) -> Optional[dict[str, Any]]:
    for e in ((card.get("fair") or {}).get("edges") or []):
        if e.get("market") == rec.get("market") and e.get("side") == rec.get("side") and e.get("book") == rec.get("book"):
            return e
    return None


def move_bucket(market: str, line_now: float, last_line: float) -> int:
    step = MOVE_STEP.get(market, 1.0)
    return int(math.floor(abs(line_now - last_line) / step + 1e-9))


def move_direction(market: str, side: str, line_now: float, last_line: float, fair: float) -> str:
    return "toward fair" if abs(line_now - fair) < abs(last_line - fair) else "away from fair"


def followup_candidates(card: dict[str, Any], alerts: dict, cfg: Config, now: datetime,
                        run_id: Optional[str] = None) -> list[Candidate]:
    """MOVE / GONE / WX for the open EDGE records of this game only."""
    out: list[Candidate] = []
    game_id = card.get("game_id")
    kick = _dt(card.get("kickoff_utc"))
    wx = card.get("weather") or {}
    for rec in pstate.open_edge_records(alerts, game_id):
        ekey = rec.get("alert_key") or ""
        e = _edge_for_record(card, rec)
        if e is None:
            continue
        line_now, fair_now, pts_now = _num(e.get("line")), _num(e.get("fair_line")), _num(e.get("edge_pts"))
        if line_now is None or pts_now is None:
            continue
        base = {"game_id": game_id, "sport": card.get("sport"), "season": card.get("season"), "week": card.get("week"),
                "market": rec.get("market"), "side": rec.get("side"), "book": rec.get("book"), "tier": rec.get("tier"),
                "model_version": rec.get("model_version") or "v1", "last_line": line_now, "last_odds": e.get("odds"),
                "last_fair": fair_now, "last_edge": pts_now, "run_id": run_id, "edge_key": ekey}
        # EDGE GONE
        if pts_now < GONE_EDGE_PTS:
            key = f"gone|{ekey}"
            if not pstate.alert_sent(alerts, key):
                out.append(Candidate(key, "gone", card.get("sport") or "", format_gone(card, rec, e, cfg.board_url),
                                     game_id=game_id, tier=rec.get("tier"), kickoff_utc=kick,
                                     record={**base, "family": "gone", "status": "closed"}, status="closed"))
            continue
        # FORECAST MOVE (fair moved ≥ 1.0 vs the last fair we messaged about)
        last_fair = _num(rec.get("last_fair"))
        if fair_now is not None and last_fair is not None:
            wb = int(math.floor(abs(fair_now - last_fair) / WX_STEP + 1e-9))
            if wb >= 1:
                key = f"wx|{ekey}|{wb}"
                if not pstate.alert_sent(alerts, key):
                    out.append(Candidate(key, "wx", card.get("sport") or "", format_wx_move(card, rec, e, cfg.board_url),
                                         game_id=game_id, tier=rec.get("tier"), kickoff_utc=kick,
                                         record={**base, "family": "wx", "status": "open",
                                                 "last_wind": _num(wx.get("wind_fg")), "last_rain": _num(wx.get("rain_fg"))}))
        # LINE MOVE (≤ 1 per edge key per 2 h)
        last_line = _num(rec.get("last_line"))
        first_line = _num(rec.get("first_line"))
        if last_line is not None and first_line is not None:
            mb = move_bucket(rec.get("market") or "total", line_now, first_line)
            last_move = _dt(rec.get("last_move_at"))
            cooled = last_move is None or (now - last_move) >= timedelta(hours=MOVE_COOLDOWN_H)
            if mb >= 1 and cooled:
                key = f"move|{ekey}|{mb}"
                if not pstate.alert_sent(alerts, key):
                    direction = move_direction(rec.get("market") or "total", rec.get("side") or "", line_now, last_line,
                                               fair_now if fair_now is not None else last_line)
                    out.append(Candidate(key, "move", card.get("sport") or "",
                                         format_move(card, rec, e, direction, cfg.board_url),
                                         game_id=game_id, tier=rec.get("tier"), kickoff_utc=kick,
                                         record={**base, "family": "move", "status": "open", "direction": direction}))
    return out


def opener_candidates(sport: str, cards: Sequence[dict[str, Any]], new_keys: Iterable[str], alerts: dict, cfg: Config,
                      now: datetime, run_id: Optional[str] = None) -> list[Candidate]:
    """One digest per run when new ``game_id|market|side|book`` keys appeared,
    restricted to weather games (gs_fg ≤ −2 or wind_fg ≥ 12)."""
    by_game: dict[str, list[str]] = {}
    for k in new_keys:
        parts = k.split("|")
        if len(parts) != 4 or parts[3] == "consensus":
            continue
        by_game.setdefault(parts[0], []).append(k)
    if not by_game:
        return []
    items: list[tuple[dict[str, Any], list[str]]] = []
    for card in cards:
        keys = by_game.get(card.get("game_id") or "")
        if not keys:
            continue
        gs = _num(_impact(card).get("gs_fg_pct"))
        wind = _num((card.get("weather") or {}).get("wind_fg"))
        if (gs is not None and gs <= OPENER_GS_MAX) or (wind is not None and wind >= OPENER_WIND_MIN):
            items.append((card, keys))
    if not items:
        return []
    season, week = items[0][0].get("season"), items[0][0].get("week")
    key = f"openers|{sport}|{season}|{week}|{et_day(now)}"
    if pstate.alert_sent(alerts, key):
        return []
    return [Candidate(key, "openers", sport, format_openers(sport, season, week, items, cfg.board_url),
                      record={"family": "openers", "sport": sport, "season": season, "week": week, "run_id": run_id})]


def ops_candidates(
    ctx: Any,
    cards_by_sport: dict[str, Sequence[dict[str, Any]]],
    alerts: dict,
    now: datetime,
    *,
    heartbeat_ts: Optional[datetime] = None,
    prev_meta_ts: Optional[datetime] = None,
) -> list[Candidate]:
    day = et_day(now)
    out: list[Candidate] = []
    run_id = getattr(ctx, "run_id", None)

    def add(key: str, sport: str, text: str, game_id: Optional[str] = None) -> None:
        if not pstate.alert_sent(alerts, key):
            out.append(Candidate(key, "ops", sport, text, game_id=game_id,
                                 record={"family": "ops", "sport": sport, "game_id": game_id, "run_id": run_id}))

    for d in getattr(ctx, "degradations", []) or []:
        if d.severity not in ("warn", "error") or _ops_expected(d.reason):
            continue
        add(f"degr|{d.component}|{_slug(_stable(d.reason))}|{day}", "",
            format_ops(f"Degradation [{d.severity}] {d.component}", d.reason))
    for ts, what in ((heartbeat_ts, "CF cron heartbeat"), (prev_meta_ts, "board meta")):
        if ts is not None and (now - ts) > timedelta(hours=STALE_HOURS):
            add(f"heartbeat|{_slug(what)}|{day}", "", format_ops(f"{what} stale", f"last seen {utc_iso(ts)} (> {STALE_HOURS:.0f} h)"))
    names_by_book: dict[str, list[str]] = {}
    for u in getattr(ctx, "unresolved_names", []) or []:
        s = str(u)
        if s.endswith(":no-schedule-match"):
            continue   # a book listing a game outside the FBS/NFL schedule (FCS etc.) is expected
        book = s.split(":", 1)[0] if ":" in s and not s.startswith(("nfl:", "cfb:")) else "schedule"
        names_by_book.setdefault(book, []).append(s)
    for book, names in sorted(names_by_book.items()):
        if book == "schedule":
            continue
        add(f"names|{book}|{day}", "", format_ops(f"Unresolved team names · {book} ({len(names)})", "\n".join(sorted(set(names))[:25])))
    for sport, cards in cards_by_sport.items():
        for card in cards:
            if card.get("stadium") is None:
                add(f"stadium|{card.get('game_id')}", sport, format_ops(f"No stadium resolved · {_matchup(card)}", str(card.get("game_id"))),
                    game_id=card.get("game_id"))
        if cards and all((c.get("consensus") or {}).get("total_now") is None for c in cards) and getattr(ctx, "scope", "") != "weather":
            add(f"noref|{sport}|{day}", sport, format_ops(f"No reference line · {SPORT_LABEL.get(sport, sport)}",
                                                          f"{len(cards)} games, no consensus total"))
    return out


def collect_candidates(
    ctx: Any,
    cards_by_sport: dict[str, Sequence[dict[str, Any]]],
    alerts: dict,
    cfg: Config,
    now: datetime,
    *,
    new_keys_by_sport: Optional[dict[str, Iterable[str]]] = None,
    heartbeat_ts: Optional[datetime] = None,
    prev_meta_ts: Optional[datetime] = None,
    include_ops: bool = True,
) -> list[Candidate]:
    run_id = getattr(ctx, "run_id", None)
    out: list[Candidate] = []
    for sport, cards in cards_by_sport.items():
        for card in cards:
            out += followup_candidates(card, alerts, cfg, now, run_id)   # before new EDGEs: a key gone this run never MOVEs
            out += edge_candidates(card, alerts, cfg, run_id)
        out += opener_candidates(sport, cards, (new_keys_by_sport or {}).get(sport) or [], alerts, cfg, now, run_id)
    if include_ops:
        out += ops_candidates(ctx, cards_by_sport, alerts, now, heartbeat_ts=heartbeat_ts, prev_meta_ts=prev_meta_ts)
    return out


# ---- planning: dedup, quiet hours, cap ---------------------------------------------------------

def _priority(c: Candidate, now: datetime) -> tuple:
    hrs = ((c.kickoff_utc - now) / timedelta(hours=1)) if c.kickoff_utc else 999.0
    return (0 if c.tier == "strong" else 1, FAMILY_PRIORITY.get(c.family, 9), hrs, c.key)


def _to_queue_item(c: Candidate, now: datetime) -> dict[str, Any]:
    return {"key": c.key, "family": c.family, "sport": c.sport, "game_id": c.game_id, "tier": c.tier,
            "text": c.text, "ts": utc_iso(now), "status": c.status,
            "kickoff_utc": utc_iso(c.kickoff_utc) if c.kickoff_utc else None, "record": c.record}


def _from_queue_item(q: dict[str, Any]) -> Candidate:
    return Candidate(q.get("key") or "", q.get("family") or "ops", q.get("sport") or "", q.get("text") or "",
                     game_id=q.get("game_id"), tier=q.get("tier"), kickoff_utc=_dt(q.get("kickoff_utc")),
                     record=q.get("record") or {}, status=q.get("status") or "open")


def plan(candidates: Sequence[Candidate], alerts: dict, tg: dict, now: datetime, cfg: Config) -> Plan:
    """Dedup against markers, park non-bypass alerts during quiet hours (23:00–07:00
    ET) in ``tg['queue']``, release the queue outside quiet hours as one digest,
    and cap individual sends at ``cfg.max_per_run − 1`` with the overflow folded
    into one digest message (so a run never exceeds ``max_per_run`` messages)."""
    p = Plan()
    quiet = in_quiet_hours(now)
    fresh: list[Candidate] = []
    seen: set[str] = set()
    for c in candidates:
        if c.key in seen:
            continue
        seen.add(c.key)
        if pstate.alert_sent(alerts, c.key):
            p.skipped.append(c.key)
            continue
        fresh.append(c)
    if not quiet:
        p.flush = [q for q in pstate.drain_queue(tg) if not pstate.alert_sent(alerts, q.get("key") or "")]
    fresh.sort(key=lambda c: _priority(c, now))
    for c in fresh:
        hrs = ((c.kickoff_utc - now) / timedelta(hours=1)) if c.kickoff_utc else None
        soon = hrs is not None and 0.0 <= hrs < BYPASS_KICKOFF_H
        if quiet and not (c.bypass_quiet or soon):
            pstate.queue_alert(tg, _to_queue_item(c, now))   # same key → latest text wins
            p.queued.append(c)
            continue
        p.send.append(c)
    # OPS notices are health chatter, not bets: one grouped message per run.
    p.ops = [c for c in p.send if c.family == "ops"]
    p.send = [c for c in p.send if c.family != "ops"]
    budget = max(1, cfg.max_per_run - (1 if p.flush else 0) - (1 if p.ops else 0))
    if len(p.send) > budget:
        p.digest = p.send[budget - 1:]
        p.send = p.send[:budget - 1]
    return p


# ---- dispatch ----------------------------------------------------------------------

Sender = Callable[[str, Optional[str]], bool]


def default_sender() -> Sender:
    """``utils.telegram.send_message`` wrapped for sync callers."""
    from utils import telegram as tg_mod

    def _send(text: str, chat: Optional[str]) -> bool:
        try:
            return bool(asyncio.run(tg_mod.send_message(text, chat_id=chat)))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"telegram send failed: {exc}")
            return False
    return _send


def print_sender(prefix: str = "  [alert]") -> Sender:
    def _send(text: str, chat: Optional[str]) -> bool:
        out = f"{prefix} chat={chat or '-'}\n" + "\n".join(f"    {ln}" for ln in text.splitlines())
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(out.encode(enc, errors="replace").decode(enc))  # cp1252 consoles on Windows can't print emoji
        return True
    return _send


def _feed_item(c: Candidate, now: str) -> dict[str, Any]:
    return {"alert_key": c.key, "family": c.family, "tier": c.tier, "game_id": c.game_id, "sport": c.sport,
            "text_html": c.text, "sent_at": now, "clv_pts": None}


def _mark(c: Candidate, alerts: dict, now: datetime, outcome: Outcome) -> None:
    """The golf ``_alert_once`` tail: mark + record + feed, only after success."""
    ts = utc_iso(now)
    pstate.mark_alert(alerts, c.key, ts)
    fields = dict(c.record)
    fields.setdefault("family", c.family)
    fields["status"] = c.status if c.family != "edge" else "open"
    fields["last_sent_at"] = ts
    rec = pstate.upsert_alert_record(alerts, c.key, fields, ts)
    rec["sends"] = int(rec.get("sends") or 0) + 1
    outcome.records.append(rec)
    # MOVE/GONE/WX update the parent EDGE record (last_* / status / cooldown)
    parent_key = c.record.get("edge_key")
    parent = pstate.get_alert_record(alerts, parent_key) if parent_key else None
    if parent is not None:
        for k in ("last_line", "last_odds", "last_fair", "last_edge"):
            if c.record.get(k) is not None:
                parent[k] = c.record[k]
        parent["last_sent_at"] = ts
        if c.family == "move":
            parent["last_move_at"] = ts
        if c.family == "wx":
            parent["last_wind"], parent["last_rain"] = c.record.get("last_wind"), c.record.get("last_rain")
        if c.family == "gone":
            parent["status"] = "closed"
        outcome.records.append(parent)
    pstate.append_feed(alerts, _feed_item(c, ts))
    outcome.sent.append(c)


def _alert_once(sender: Sender, alerts: dict, now: datetime, outcome: Outcome, cfg: Config) -> Callable[[Candidate], bool]:
    """Closure from golf build.py L2494-2502: check marker → send → mark only on success."""
    def _once(c: Candidate) -> bool:
        if pstate.alert_sent(alerts, c.key):
            return False
        ok = False
        try:
            ok = bool(sender(c.text, cfg.chat_for(c.sport) if c.family != "ops" else cfg.chat_default))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"alert send failed [{c.key}]: {exc}")
        if ok:
            outcome.n_messages += 1
            _mark(c, alerts, now, outcome)
            logger.warning(f"alerted [{c.key}]")
        else:
            outcome.failed.append(c)
        return ok
    return _once


def _send_group(title: str, members: Sequence[Candidate], sender: Sender, alerts: dict, now: datetime,
                outcome: Outcome, cfg: Config) -> None:
    """One digest per chat; members marked only when their digest message went out."""
    by_chat: dict[Optional[str], list[Candidate]] = {}
    for c in members:
        by_chat.setdefault(cfg.chat_for(c.sport) if c.family != "ops" else cfg.chat_default, []).append(c)
    for chat, group in by_chat.items():
        pending = [c for c in group if not pstate.alert_sent(alerts, c.key)]
        if not pending:
            continue
        ok_all = True
        for msg in format_digest(title, [c.summary for c in pending]):
            try:
                ok = bool(sender(msg, chat))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"digest send failed: {exc}")
                ok = False
            ok_all = ok_all and ok
            if ok:
                outcome.n_messages += 1
        if ok_all:
            for c in pending:
                _mark(c, alerts, now, outcome)
        else:
            outcome.failed.extend(pending)


def dispatch(p: Plan, alerts: dict, sender: Sender, now: datetime, cfg: Config) -> Outcome:
    outcome = Outcome()
    once = _alert_once(sender, alerts, now, outcome, cfg)
    if p.flush:
        _send_group("Overnight alerts", [_from_queue_item(q) for q in p.flush], sender, alerts, now, outcome, cfg)
    for c in p.send:
        once(c)
    if p.digest:
        _send_group("Alert digest (run cap)", p.digest, sender, alerts, now, outcome, cfg)
    if p.ops:
        _send_group("Ops notices", p.ops, sender, alerts, now, outcome, cfg)
    pstate.prune_alert_records(alerts)
    return outcome


# ---- entry point used by pipeline.build ----------------------------------------------------

@dataclass
class AlertsRun:
    candidates: list[Candidate]
    plan: Plan
    outcome: Outcome
    alerts: dict
    telegram_state: dict
    source: str = "r2"

    @property
    def n_alerts(self) -> int:
        return self.outcome.n_sent

    def keys_for(self, game_id: str) -> list[str]:
        return sorted(k for k, r in (self.alerts.get("records") or {}).items()
                      if isinstance(r, dict) and r.get("game_id") == game_id and r.get("status", "open") == "open")


def _heartbeat_ts(state_dir: Path) -> Optional[datetime]:
    d = pstate._load(state_dir / "cf_heartbeat.json")
    ts = d.get("ts") or d.get("last") or d.get("at")
    if isinstance(ts, str):
        try:
            return ensure_utc(parse_iso(ts.replace(" UTC", "Z").replace(" ", "T")))
        except ValueError:
            return None
    return None


def _prev_meta_ts(state_dir: Path) -> Optional[datetime]:
    d = pstate._load(state_dir / "prev_meta.json")
    return _dt(d.get("last_updated"))


def run_alerts(
    ctx: Any,
    cards_by_sport: dict[str, Sequence[dict[str, Any]]],
    state_dir: Path,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    new_keys_by_sport: Optional[dict[str, Iterable[str]]] = None,
    sender: Optional[Sender] = None,
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
    fetch_rows: Optional[Callable[[], Any]] = None,
) -> AlertsRun:
    """Collect → plan → dispatch → persist. With ``enabled=False`` or ``dry_run``
    the candidates are printed with their keys and nothing is sent or marked."""
    cfg = cfg or Config.from_env()
    now = ensure_utc(now) if now else now_utc()
    state_dir = Path(state_dir)
    alerts, source = pstate.load_alerts_rehydrated(state_dir, fetch_rows)
    tg = pstate.load_telegram_state(state_dir)
    cands = collect_candidates(ctx, cards_by_sport, alerts, cfg, now, new_keys_by_sport=new_keys_by_sport,
                               heartbeat_ts=_heartbeat_ts(state_dir), prev_meta_ts=_prev_meta_ts(state_dir))
    live = enabled and not dry_run
    if not live:
        n_q = len(tg.get("queue") or [])
        print(f"  alerts ({'dry-run' if dry_run else 'disabled'}): {len(cands)} candidate(s), {n_q} queued, quiet={in_quiet_hours(now)}, state={source}")
        for c in sorted(cands, key=lambda c: _priority(c, now)):
            print(f"    [{c.family}{'/' + c.tier if c.tier else ''}] {c.key}")
        return AlertsRun(cands, Plan(), Outcome(), alerts, tg, source)
    p = plan(cands, alerts, tg, now, cfg)
    outcome = dispatch(p, alerts, sender or default_sender(), now, cfg)
    pstate.save_alerts(state_dir, alerts)
    pstate.save_telegram_state(state_dir, tg)
    print(f"  alerts: {outcome.n_sent} sent in {outcome.n_messages} message(s), {len(p.queued)} queued, "
          f"{len(p.digest)} digested, {len(p.flush)} flushed, {len(p.skipped)} dedup, {len(outcome.failed)} failed")
    return AlertsRun(cands, p, outcome, alerts, tg, source)


# ---- weekly CLV digest (backtest.yml) ------------------------------------------------------------

DIGEST_KINDS = ("clv",)


def _backtest_section(backtest: Optional[dict[str, Any]]) -> list[str]:
    """v1 vs v2 CLV + the promotion gate from ``board/backtest.json`` (``alerts_clv.by_model``,
    normalised by ``pipeline.calibrate.clv_block``)."""
    from pipeline.calibrate import clv_block

    blk = clv_block(backtest)
    by_model = blk["by_model"]
    if not by_model:
        return []
    weeks = blk.get("weeks")
    lines = [f"<b>v1 vs v2 (backtest{f', {int(weeks)} wk' if weeks else ''})</b>"]
    for model in sorted(by_model):
        m = by_model[model]
        pos = m.get("pos_frac")
        pos_s = f" · +CLV {round(pos * 100)}%" if pos is not None else ""
        lines.append(f"  {html.escape(model)}: n={m.get('n') if m.get('n') is not None else '?'} avg {_fmt_signed(m.get('avg_clv'), 2)}{pos_s}")
    a1, a2 = (by_model.get("v1") or {}).get("avg_clv"), (by_model.get("v2") or {}).get("avg_clv")
    if a1 is not None and a2 is not None:
        enough = bool(weeks) and weeks >= 4
        verdict = "v2 >= v1 over >= 4 wk → promotion eligible (set ALERT_MODEL=v2 in model/config.py)" \
            if (a2 >= a1 and enough) else ("v2 >= v1 but < 4 wk — keep v1" if a2 >= a1 else "v2 < v1 — keep v1")
        lines.append(f"  gate: {verdict}")
    return lines


def clv_digest(alerts: dict, *, sport: Optional[str] = None, top_n: int = 5,
               backtest: Optional[dict[str, Any]] = None) -> str:
    recs = [r for r in (alerts.get("records") or {}).values()
            if isinstance(r, dict) and r.get("family") == "edge" and _num(r.get("clv_pts")) is not None
            and (sport is None or r.get("sport") == sport)]
    if not recs:
        return "\n".join(["<b>📊 Weekly CLV digest</b>", "no settled EDGE alerts with a closing line yet",
                          *_backtest_section(backtest)])

    def group(keyfn: Callable[[dict], str]) -> list[str]:
        acc: dict[str, list[float]] = {}
        for r in recs:
            acc.setdefault(keyfn(r), []).append(float(r["clv_pts"]))
        rows = []
        for k, xs in sorted(acc.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            pos = sum(1 for x in xs if x > 0)
            rows.append(f"  {html.escape(k)}: n={len(xs)} avg {_fmt_signed(sum(xs) / len(xs), 2)} · +CLV {pos}/{len(xs)}")
        return rows

    lines = [f"<b>📊 Weekly CLV digest · {len(recs)} alerts</b>", "<b>by tier</b>", *group(lambda r: str(r.get("tier"))),
             "<b>by league</b>", *group(lambda r: str(r.get("sport")).upper()), "<b>by book</b>", *group(lambda r: _book_label(str(r.get("book")))),
             "<b>by model</b>", *group(lambda r: str(r.get("model_version") or "v1"))]
    ordered = sorted(recs, key=lambda r: float(r["clv_pts"]), reverse=True)

    def row(r: dict) -> str:
        return (f"  {html.escape(str(r.get('game_id')))} {str(r.get('side')).upper()} {_fmt_line(r.get('first_line'))} "
                f"@ {_book_label(str(r.get('book')))} → close {_fmt_line(r.get('closing_line'))} · CLV {_fmt_signed(r.get('clv_pts'))}")
    lines += [f"<b>top {top_n}</b>", *[row(r) for r in ordered[:top_n]],
              f"<b>bottom {top_n}</b>", *[row(r) for r in ordered[-top_n:][::-1]]]
    lines += _backtest_section(backtest)
    return "\n".join(lines)


def _load_backtest(path: Optional[Path]) -> Optional[dict[str, Any]]:
    if path is None or not Path(path).is_file():
        return None
    try:
        data = pstate._load_any(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"backtest.json unreadable: {exc}")
        return None
    return data if isinstance(data, dict) else None


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pipeline.alerts", description=__doc__.split("\n\n")[0])
    p.add_argument("--digest", nargs="?", const="clv", choices=DIGEST_KINDS, default=None,
                   help="send a weekly digest: 'clv' (default) = CLV by tier/league/book/model from alerts.json "
                        "records + v1 vs v2 from --backtest")
    p.add_argument("--flush", action="store_true", help="release the quiet-hours queue now (ignores the clock)")
    p.add_argument("--sport", choices=("nfl", "cfb"), default=None)
    p.add_argument("--state-dir", type=Path, default=Path("data/state"))
    p.add_argument("--backtest", type=Path, default=None, help="board/backtest.json (v1 vs v2 CLV section of the digest)")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    cfg = Config.from_env()
    sender = print_sender() if args.dry_run else default_sender()
    now = now_utc()
    if args.digest == "clv":
        alerts, _ = pstate.load_alerts_rehydrated(args.state_dir)
        text = clv_digest(alerts, sport=args.sport, backtest=_load_backtest(args.backtest))
        ok = sender(text, cfg.chat_for(args.sport))
        print(f"  clv digest: {'sent' if ok else 'FAILED'}")
        return 0 if ok else 1
    if args.flush:
        alerts, _ = pstate.load_alerts_rehydrated(args.state_dir)
        tg = pstate.load_telegram_state(args.state_dir)
        queued = [q for q in pstate.drain_queue(tg) if not pstate.alert_sent(alerts, q.get("key") or "")]
        outcome = Outcome()
        if queued:
            _send_group("Overnight alerts", [_from_queue_item(q) for q in queued], sender, alerts, now, outcome, cfg)
        if not args.dry_run:
            pstate.save_alerts(args.state_dir, alerts)
            pstate.save_telegram_state(args.state_dir, tg)
        print(f"  flush: {outcome.n_sent} alert(s) in {outcome.n_messages} message(s), {len(outcome.failed)} failed")
        return 0 if not outcome.failed else 1
    print("nothing to do: pass --digest or --flush")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
