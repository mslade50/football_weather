"""Telegram alert engine (ARCHITECTURE §10).

    python -m pipeline.alerts --digest clv [--state-dir data/state] [--backtest data/board/backtest.json] [--dry-run]
                                                                            # weekly CLV digest (backtest.yml, Monday)
    python -m pipeline.alerts --flush  [--state-dir data/state] [--dry-run]   # flush the quiet-hours queue

Telegram is an action channel, not a mirror of the board:

* PLAY: one stable ``edge|...|total|under|best|model`` identity per game (book
  churn and model promotion do not mint another notification). The
  default gate is Mid+ signal, a real posted book price, and at least 1 point
  above fair. Low/no-value/unpriced games remain on the board.
* UPDATE: at most one per game/run, prioritised CLOSED → tier change → line
  move → forecast move. Best-book changes update the same parent and are labeled
  as best-price changes. Betting notifications stop at kickoff.
* CLOSED: the signal/value/price no longer meets the actionable gate.
* SYSTEM: grouped operational issues. Scrape-volume incidents enter only this
  unified path, so quiet hours, dedupe, and grouping all apply.

Defaults can be tuned with ``TELEGRAM_MIN_TIER``, ``TELEGRAM_MIN_EDGE_PTS``,
``TELEGRAM_MAX_PER_RUN`` and ``TELEGRAM_INCLUDE_OPENERS``.

Pipeline: ``collect_candidates`` (pure: GameCards in → ``Candidate`` list out) →
``plan`` (dedup, current-data quiet-hours queue, three individual messages then a
bounded SUMMARY; four messages/run by default) → ``dispatch`` (send, mark ONLY after a successful send — the golf
``_alert_once`` closure — record + feed). ``--no-alerts`` / ``--dry-run`` print
the candidates with their keys instead of sending.

Chat routing: ``TELEGRAM_CHAT_ID_NFL`` / ``TELEGRAM_CHAT_ID_CFB`` fall back to
``TELEGRAM_CHAT_ID``; OPS alerts always go to the default chat.

The compact message body shows action, matchup/time, price, and short reason
bullets. Signal-only drivers such as CFB altitude plus warmth are named
explicitly. Full forecasts, model details, and the price ladder stay behind the
board link.
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
from utils.env import load_repo_dotenv
from utils.timeutil import ET, ensure_utc, now_utc, parse_iso, to_et, utc_iso

logger = logging.getLogger(__name__)

# ---- constants ---------------------------------------------------------------------

MAX_PER_RUN = 4                  # at most 3 individual alerts + one summary by default
MAX_INDIVIDUAL_PER_RUN = 3
QUIET_START_H = 23               # 23:00 ET ..
QUIET_END_H = 7                  # .. 07:00 ET
BYPASS_KICKOFF_H = 3.0           # kickoff < 3 h bypasses quiet hours
MOVE_STEP = {"total": 1.5, "spread": 1.0}
MOVE_COOLDOWN_H = 4.0
WX_STEP = 2.0
OPENER_GS_MAX = -2.0             # digest only games with gs_fg ≤ -2 ...
OPENER_WIND_MIN = 12.0           # ... or wind_fg ≥ 12
STALE_HOURS = 20.0
TELEGRAM_MAX_CHARS = 4000        # API limit 4096; leave headroom for the header
DIGEST_ITEM_MAX_CHARS = 600      # keep a single SUMMARY bounded and scan-friendly
LADDER_WRAP_CHARS = 180          # "Books:" market ladder wraps to a second line past this
SIGNAL_NONE = "No Impact"        # pipeline.model.signals.NO — the only label that never alerts
SIGNAL_SLUGS = (("very high", "very_high"), ("high", "high"), ("mid", "mid"), ("low", "low"))
TIER_RANK = {"low": 1, "mid": 2, "high": 3, "very_high": 4}
DEFAULT_MIN_TIER = "mid"
DEFAULT_MIN_EDGE_PTS = 1.0
DEFAULT_INCLUDE_OPENERS = False
STABLE_PLAY_BOOK = "best"        # key identity; the record still stores the actual book
BYPASS_TIERS = ("high", "very_high")   # signal tiers that bypass quiet hours / sort first
CONSENSUS_BOOK = "consensus"
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
            lines = 4 if self.family == "edge" else 2 if self.family == "ops" else 1
            self.summary = _summary(self.text, lines)

    @property
    def bypass_quiet(self) -> bool:
        return (
            self.tier in BYPASS_TIERS
            or self.family == "gone"
            or self.record.get("bypass_quiet") is True
        )


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
    min_tier: str = DEFAULT_MIN_TIER
    min_edge_pts: float = DEFAULT_MIN_EDGE_PTS
    include_openers: bool = DEFAULT_INCLUDE_OPENERS

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> Config:
        e = os.environ if env is None else env
        by_sport = {}
        for sport in ("nfl", "cfb"):
            v = e.get(f"TELEGRAM_CHAT_ID_{sport.upper()}")
            if v:
                by_sport[sport] = v
        min_tier = str(e.get("TELEGRAM_MIN_TIER") or DEFAULT_MIN_TIER).strip().lower().replace("-", "_")
        if min_tier not in TIER_RANK:
            logger.warning("invalid TELEGRAM_MIN_TIER=%r; using %s", min_tier, DEFAULT_MIN_TIER)
            min_tier = DEFAULT_MIN_TIER
        try:
            max_per_run = max(2, min(20, int(e.get("TELEGRAM_MAX_PER_RUN") or MAX_PER_RUN)))
        except (TypeError, ValueError):
            max_per_run = MAX_PER_RUN
        try:
            min_edge_pts = max(0.0, float(e.get("TELEGRAM_MIN_EDGE_PTS") or DEFAULT_MIN_EDGE_PTS))
        except (TypeError, ValueError):
            min_edge_pts = DEFAULT_MIN_EDGE_PTS
        include_openers = str(e.get("TELEGRAM_INCLUDE_OPENERS") or "0").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            board_url=(e.get("BOARD_URL") or DEFAULT_BOARD_URL).rstrip("/"),
            chat_default=e.get("TELEGRAM_CHAT_ID") or None,
            chat_by_sport=by_sport,
            max_per_run=max_per_run,
            min_tier=min_tier,
            min_edge_pts=min_edge_pts,
            include_openers=include_openers,
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
# scoped volume-health incident handles a true dark book while peers report). They
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


def _details_link(board_url: str, card: dict[str, Any], label: str = "Details & all prices") -> str:
    href = f"{board_url}/#sport={card.get('sport')}&week={card.get('week')}&game={card.get('game_id')}"
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def _alert_heading(kind: str, card: dict[str, Any], emoji: str, *, tier: Optional[str] = None) -> list[str]:
    sport = SPORT_LABEL.get(card.get("sport") or "", str(card.get("sport") or "").upper())
    tier_s = (tier or signal_slug(_signal_label(card)) or "").replace("_", " ").upper()
    bits = [kind]
    if tier_s:
        bits.append(tier_s)
    bits.append(f"{sport} W{card.get('week')}")
    return [
        f"{emoji} <b>{html.escape(' · '.join(bits))}</b>",
        f"<b>{html.escape(_matchup(card))}</b> · {_kick_label(card)}",
    ]


def _brief_bet(card: dict[str, Any], edge: dict[str, Any], *, label: str = "") -> str:
    market = edge.get("market")
    raw_side = _side_label(edge, card)
    side = raw_side.title() if raw_side.lower() in ("over", "under") else raw_side
    line = _num(edge.get("line"))
    if line is None:
        value = f"{side} · no line available"
    else:
        line_s = _fmt_line(line, signed=market == "spread")
        book = str(edge.get("book") or "")
        cents = _cents(edge.get("odds")) if book in CENTS_BOOKS else None
        price = f"{cents}¢" if cents is not None else _fmt_odds(edge.get("odds"))
        value = f"{side} {line_s} ({price}) · {_book_label(book)}"
    prefix = f"{label}: " if label else ""
    return f"<b>{html.escape(prefix + value)}</b>"


def _altitude_phrase(card: dict[str, Any], *, warmth: bool = False) -> str:
    alt_m = _num(card.get("travel_alt"))
    temp = _num((card.get("weather") or {}).get("temp_fg"))
    climb = f"+{round(alt_m * 3.28084):,} ft climb" if alt_m is not None else "altitude climb"
    if warmth:
        temp_s = f"{round(temp)}°F" if temp is not None else "warm conditions"
        return f"Altitude + warmth: {climb} · {temp_s}"
    return f"Altitude: {climb}"


def _driver_phrase(card: dict[str, Any]) -> str:
    wx = card.get("weather") or {}
    drivers = {str(d) for d in ((card.get("signal") or {}).get("drivers") or []) if d}
    if "altitude_warmth" in drivers:
        return _altitude_phrase(card, warmth=True)
    comps = _components(card)
    top = max(comps, key=comps.get) if comps else ""
    if top == "wind":
        return f"Wind: {_fmt_line(wx.get('wind_fg'))} mph"
    if top == "rain":
        return f"Rain: {_fmt_line(wx.get('rain_fg'))} mm"
    if top in ("cold", "cold_away", "heat", "heat_away"):
        t = _num(wx.get("temp_fg"))
        return f"Temperature: {round(t) if t is not None else '?'}°F"
    if top == "alt":
        return _altitude_phrase(card)
    flags = [str(f) for f in ((card.get("signal") or {}).get("flags") or []) if f]
    return f"Signal: {flags[0]}" if flags else f"Weather: {_wx_numbers(card)}"


def _why_lines(card: dict[str, Any], edge: dict[str, Any]) -> list[str]:
    return [
        "Why:",
        f"• Value: {_fmt_signed(edge.get('edge_pts'))} pts above fair {_fmt_line(edge.get('fair_line'))}",
        f"• {html.escape(_driver_phrase(card))}",
    ]


def _special_driver_lines(card: dict[str, Any]) -> list[str]:
    """Persistent context worth repeating on UPDATE messages."""
    drivers = {str(d) for d in ((card.get("signal") or {}).get("drivers") or []) if d}
    if "altitude_warmth" in drivers:
        return [f"• {html.escape(_altitude_phrase(card, warmth=True))}"]
    return []


def _play_summary(card: dict[str, Any], edge: dict[str, Any]) -> str:
    tier = (signal_slug(_signal_label(card)) or "?").replace("_", " ").upper()
    bet = re.sub(r"</?b>", "", _brief_bet(card, edge))
    return f"🎯 {tier} · {html.escape(_matchup(card))} · {bet} · {_kick_label(card)}"


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


def _signal_label(card: dict[str, Any]) -> Optional[str]:
    lbl = (card.get("signal") or {}).get("label")
    return str(lbl).strip() if lbl else None


def _in_signal(card: dict[str, Any]) -> bool:
    """The legacy bet rule: the map dot is any tier but 'No Impact'."""
    lbl = _signal_label(card)
    return bool(lbl) and lbl.lower() != SIGNAL_NONE.lower()


def signal_slug(label: Optional[str]) -> Optional[str]:
    """'Very High Impact' → 'very_high', 'Low (Rain)' → 'low', 'No Impact' / None → None."""
    s = (label or "").strip().lower()
    if not s or s == SIGNAL_NONE.lower():
        return None
    for prefix, slug in SIGNAL_SLUGS:
        if s.startswith(prefix):
            return slug
    return _slug(s, 20)


def _wx_numbers(card: dict[str, Any]) -> str:
    """'wind 18 mph · 41°F · rain 0.8 mm' — the numbers the signal rules read."""
    wx = card.get("weather") or {}
    t = _num(wx.get("temp_fg"))
    return (f"wind {_fmt_line(wx.get('wind_fg'))} mph · {round(t) if t is not None else '?'}°F · "
            f"rain {_fmt_line(wx.get('rain_fg'))} mm")


def _signal_line(card: dict[str, Any]) -> str:
    flags = [str(f) for f in ((card.get("signal") or {}).get("flags") or []) if f]
    line = f"<b>{html.escape(_signal_label(card) or '?')}</b> · {_wx_numbers(card)}"
    return f"{line} · {html.escape(', '.join(flags))}" if flags else line


def _is_consensus(edge: dict[str, Any]) -> bool:
    return edge.get("book") == CONSENSUS_BOOK


def _market_edge(edge: dict[str, Any], *, prefix: str = "market edge") -> str:
    """'market edge +3.4 pts / +4.1%' · '… −0.6 pts (market already there)'; the market
    edge is a note on a signal alert, never a gate, so ≤ 0 is shown honestly."""
    pts = _num(edge.get("edge_pts"))
    if pts is None:
        return f"{prefix} ?"
    prob = _num(edge.get("edge_prob"))
    s = f"{prefix} {_fmt_signed(pts)} pts"
    if prob is not None:
        s += f" / {_fmt_pct(prob * 100)}"
    if _is_consensus(edge) and _num(edge.get("fair_line")) is not None:
        s += f" vs fair {_fmt_line(edge.get('fair_line'), signed=edge.get('market') == 'spread')}"
    return s + (" (market already there)" if pts <= 0 else "")


def _bet_line(card: dict[str, Any], edge: dict[str, Any], *, opener: bool = True) -> str:
    """'<b>UNDER 38 −110 @ BetOnline</b> · market edge +3.4 pts / +4.1% · open 38'
    '<b>UNDER 37.5 (consensus)</b> · market edge −0.6 pts vs fair 38.6 (market already there)'
    '<b>UNDER</b> · no line posted yet'"""
    market = edge.get("market")
    signed = market == "spread"
    side = _side_label(edge, card)
    line = _num(edge.get("line"))
    if line is None:
        return f"<b>{side}</b> · no line posted yet"
    if _is_consensus(edge):
        return f"<b>{side} {_fmt_line(line, signed)} (consensus)</b> · {_market_edge(edge)}"
    s = f"<b>{side} {_fmt_line(line, signed)} {_fmt_odds(edge.get('odds'))} @ {_book_label(edge.get('book'))}</b> · {_market_edge(edge)}"
    op = _opener_for(card, edge) if opener else None
    return s + (f" · open {_fmt_line(op, signed)}" if op is not None else "")


def _cents(odds: Any) -> Optional[int]:
    """American odds → implied price in cents (Kalshi contracts): −108 → 52¢, +120 → 45¢."""
    n = _num(odds)
    if n is None or n == 0:
        return None
    return int(round(100 * (abs(n) / (100 + abs(n)) if n < 0 else 100 / (100 + n))))


def _ladder_rows(card: dict[str, Any], market: str, side: str) -> list[tuple[float, Optional[float], str, str]]:
    """``(line, odds, book, rendered)`` for every book in ``card['odds']`` pricing ``market``;
    the line is expressed from the bettor's side (spread away = −home_line)."""
    rows: list[tuple[float, Optional[float], str, str]] = []
    for book, blk in (card.get("odds") or {}).items():
        if book == CONSENSUS_BOOK or not isinstance(blk, dict):
            continue
        m = blk.get(market) or {}
        if market == "total":
            line, odds = _num(m.get("line")), _num(m.get(side))
        elif market == "spread":
            home = _num(m.get("home_line"))
            line = home if side == "home" else (-home if home is not None else None)
            odds = _num(m.get(f"{side}_odds"))
        else:
            continue
        if line is None:
            continue
        tag = _fmt_line(line, signed=True) if market == "spread" else f"{'u' if side == 'under' else 'o'}{line:.1f}"
        cents = _cents(odds) if book in CENTS_BOOKS else None
        price = f"({cents}¢)" if cents is not None else _fmt_odds(odds)
        rows.append((line, odds, book, f"{_book_label(book)} {tag} {price}"))
    # best first for the bettor: more favourable line (higher for under / spread side, lower for over), then better odds
    rows.sort(key=lambda r: ((r[0] if side == "over" else -r[0]), -(r[1] if r[1] is not None else -1e9), r[2]))
    return rows


def _wrap_items(prefix: str, items: Sequence[str], limit: int = LADDER_WRAP_CHARS) -> list[str]:
    lines: list[str] = []
    cur = ""
    for it in items:
        piece = it if not cur else f"{cur} · {it}"
        if cur and len(f"{prefix} {piece}") > limit:
            lines.append(f"{prefix} {cur}")
            cur = it
        else:
            cur = piece
    if cur:
        lines.append(f"{prefix} {cur}")
    return lines


def book_ladder(card: dict[str, Any], edge: dict[str, Any]) -> list[str]:
    """'Books: <b>FD u38.5 −108</b> · BetOnline u38.5 −110 · Kalshi u38.0 (47¢) · ref u37.5' —
    every book pricing the alerted market/side, best price first (bold), the consensus
    reference last; one line, wrapped past ``LADDER_WRAP_CHARS``."""
    market, side = str(edge.get("market") or "total"), str(edge.get("side") or "under")
    rows = _ladder_rows(card, market, side)
    items = [f"<b>{r[3]}</b>" if i == 0 else r[3] for i, r in enumerate(rows)]
    cons = card.get("consensus") or {}
    ref = _num(cons.get("total_now") if market == "total" else cons.get("spread_now"))
    if ref is not None:
        if market == "spread":
            ref_line = _fmt_line(ref if side == "home" else -ref, signed=True)
        else:
            ref_line = f"{'u' if side == 'under' else 'o'}{ref:.1f}"
        items.append(f"ref {ref_line}")
    if not items:
        return ["Books: no lines posted"]
    return _wrap_items("Books:", items)


# ---- formatters --------------------------------------------------------------------------

def format_edge(card: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL) -> str:
    """A scan-first PLAY: action, matchup/time, price, reason bullets, then details."""
    lines = [
        *_alert_heading("PLAY", card, "🎯"),
        _brief_bet(card, edge),
        *_why_lines(card, edge),
        _details_link(board_url, card),
    ]
    return "\n".join(lines)


def format_move(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], direction: str,
                board_url: str = DEFAULT_BOARD_URL) -> str:
    market = edge.get("market")
    signed = market == "spread"
    old_fair, new_fair = _num(rec.get("last_fair")), _num(edge.get("fair_line"))
    fair_change = ""
    if old_fair is not None and new_fair is not None and abs(new_fair - old_fair) >= 0.05:
        fair_change = f" · fair {_fmt_line(old_fair, signed)} → {_fmt_line(new_fair, signed)}"
    old_book = str(rec.get("last_book") or rec.get("book") or "")
    new_book = str(edge.get("book") or "")
    if old_book and new_book and old_book != new_book:
        old_edge = {
            "market": market,
            "side": edge.get("side") or rec.get("side"),
            "line": rec.get("last_line"),
            "odds": rec.get("last_odds"),
            "book": old_book,
        }
        old_bet = re.sub(r"</?b>", "", _brief_bet(card, old_edge))
        new_bet = re.sub(r"</?b>", "", _brief_bet(card, edge))
        change = f"Best price: {old_bet} → {new_bet}"
    else:
        change = (f"Line: {_side_label(edge, card).title()} {_fmt_line(rec.get('last_line'), signed)} → "
                  f"{_fmt_line(edge.get('line'), signed)} · {_book_label(edge.get('book'))} "
                  f"{_fmt_odds(edge.get('odds'))}")
    lines = [
        *_alert_heading("UPDATE", card, "🔄"),
        change,
        (f"Value: {_fmt_signed(rec.get('last_edge'))} → {_fmt_signed(edge.get('edge_pts'))} pts{fair_change}"),
        *_special_driver_lines(card),
        _details_link(board_url, card),
    ]
    return "\n".join(lines)


def format_gone(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL,
                reason: Optional[str] = None) -> str:
    """The alerted play is no longer actionable."""
    market = edge.get("market")
    signed = market == "spread"
    was = str(rec.get("last_signal") or "?")
    reason = reason or f"Signal {was} → {_signal_label(card) or SIGNAL_NONE}"
    lines = [
        *_alert_heading("CLOSED", card, "⛔", tier=""),
        f"Reason: {html.escape(reason)}",
        (f"Was: {_side_label(edge, card).title()} {_fmt_line(rec.get('first_line'), signed)} · "
         f"Now: {_fmt_line(edge.get('line'), signed)} ({_fmt_signed(edge.get('edge_pts'))} pts vs fair)"),
        _details_link(board_url, card),
    ]
    return "\n".join(lines)


def format_signal_change(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any],
                         board_url: str = DEFAULT_BOARD_URL) -> str:
    """The actionable signal tier changed; this replaces any same-run market/weather updates."""
    old, new = str(rec.get("last_signal") or "?"), _signal_label(card) or "?"
    lines = [
        *_alert_heading("UPDATE", card, "🔄"),
        f"Signal: <b>{html.escape(old)} → {html.escape(new)}</b>",
        _brief_bet(card, edge, label="Play"),
        *_why_lines(card, edge),
        _details_link(board_url, card),
    ]
    return "\n".join(lines)


def format_wx_move(card: dict[str, Any], rec: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL) -> str:
    wx = card.get("weather") or {}
    market = edge.get("market")
    signed = market == "spread"
    old_w, new_w = _fmt_line(rec.get("last_wind")), _fmt_line(wx.get("wind_fg"))
    old_r, new_r = _fmt_line(rec.get("last_rain")), _fmt_line(wx.get("rain_fg"))
    lines = [
        *_alert_heading("UPDATE", card, "🔄"),
        f"Forecast: fair {market} {_fmt_line(rec.get('last_fair'), signed)} → {_fmt_line(edge.get('fair_line'), signed)}",
        f"Weather: wind {old_w} → {new_w} mph · rain {old_r} → {new_r} mm",
        _brief_bet(card, edge, label="Play"),
        *_special_driver_lines(card),
        _details_link(board_url, card),
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
    """One bounded message with numbered items and an explicit overflow count.

    Grouping must reduce volume, so a large incident set never fans back out into
    several Telegram messages. Full detail remains on the board.
    """
    cur = f"<b>{html.escape(title)} ({len(items)})</b>"
    for i, it in enumerate(items, 1):
        item = str(it)
        if len(item) > DIGEST_ITEM_MAX_CHARS:
            # Avoid cutting an HTML tag in half when a caller supplies a long
            # formatted summary.
            plain = re.sub(r"<[^>]*>", "", html.unescape(item))
            item = html.escape(plain[:DIGEST_ITEM_MAX_CHARS - 1].rstrip()) + "…"
        piece = f"\n\n{i}. {item}"
        after = len(items) - i
        reserve = f"\n\n… +{after} more — see board" if after else ""
        if len(cur) + len(piece) + len(reserve) > TELEGRAM_MAX_CHARS:
            cur += f"\n\n… +{after + 1} more — see board"
            break
        cur += piece
    return [cur]


# ---- candidate collection (pure) ---------------------------------------------------------------

def _model_version(card: dict[str, Any]) -> str:
    fair = card.get("fair") or {}
    for e in fair.get("edges") or []:
        if e.get("model_version"):
            return str(e["model_version"])
    return str((card.get("impact") or {}).get("model_version") or fair.get("model_version") or "v1")


def consensus_entry(card: dict[str, Any]) -> dict[str, Any]:
    """A fair.edges-shaped TOTAL UNDER entry at the ``consensus`` book: line = consensus.total_now
    (may be None), fair_line = fair.fair_total, edge_pts = total_now − fair_total when both exist."""
    cons = card.get("consensus") or {}
    fair = card.get("fair") or {}
    line, fair_t = _num(cons.get("total_now")), _num(fair.get("fair_total"))
    return {
        "game_id": card.get("game_id"), "market": "total", "side": "under", "book": CONSENSUS_BOOK,
        "line": line, "odds": None, "fair_line": fair_t,
        "edge_pts": round(line - fair_t, 2) if line is not None and fair_t is not None else None,
        "edge_prob": None, "tier": None, "model_version": _model_version(card),
        "confidence": fair.get("confidence"), "ref_book": cons.get("ref_book"), "n_books": cons.get("n_books"),
    }


def _play_edge(card: dict[str, Any]) -> dict[str, Any]:
    """The play for a weather signal is the TOTAL UNDER: the fair.edges under entry with the
    largest market edge (any book), else the synthesised consensus entry."""
    unders = [e for e in ((card.get("fair") or {}).get("edges") or [])
              if e.get("market") == "total" and e.get("side") == "under" and _num(e.get("edge_pts")) is not None]
    if unders:   # ties on edge_pts → the best price for an under bettor (higher line, then better odds)
        return max(unders, key=lambda e: (_num(e.get("edge_pts")) or 0.0, _num(e.get("line")) or -1e9,
                                          _num(e.get("odds")) or -1e9))
    return consensus_entry(card)


def _tier_at_least(label: Optional[str], minimum: str) -> bool:
    return TIER_RANK.get(signal_slug(label) or "", 0) >= TIER_RANK.get(minimum, TIER_RANK[DEFAULT_MIN_TIER])


def _actionable_play(card: dict[str, Any], edge: dict[str, Any], cfg: Config) -> bool:
    """Telegram is for a bet someone can act on: Mid+ by default, a real posted
    book/price, and at least the configured point advantage. Lower-signal and
    already-priced weather remains visible on the board."""
    return (
        _tier_at_least(_signal_label(card), cfg.min_tier)
        and edge.get("book") != CONSENSUS_BOOK
        and _num(edge.get("line")) is not None
        and _num(edge.get("odds")) is not None
        and (_num(edge.get("edge_pts")) or 0.0) >= cfg.min_edge_pts
    )


def _alertable_edges(card: dict[str, Any], cfg: Optional[Config] = None) -> list[dict[str, Any]]:
    """At most one actionable TOTAL UNDER play per game."""
    cfg = cfg or Config()
    if not _in_signal(card):
        return []
    play = _play_edge(card)
    return [play] if _actionable_play(card, play, cfg) else []


def _same_play_key(parsed: dict[str, str], card: dict[str, Any], edge: dict[str, Any]) -> bool:
    return (
        parsed.get("season") == str(card.get("season"))
        and parsed.get("week") == str(card.get("week"))
        and parsed.get("game_id") == str(card.get("game_id"))
        and parsed.get("market") == str(edge.get("market"))
        and parsed.get("side") == str(edge.get("side"))
    )


def _play_already_alerted(alerts: dict, card: dict[str, Any], edge: dict[str, Any]) -> bool:
    """Compatibility dedupe for old book-keyed EDGE markers plus the new stable key."""
    for key in (alerts.get("sent") or {}):
        parsed = parse_edge_key(str(key))
        if parsed and _same_play_key(parsed, card, edge):
            return True
    return False


def _edge_record(card: dict[str, Any], e: dict[str, Any], run_id: Optional[str]) -> dict[str, Any]:
    wx = card.get("weather") or {}
    label = _signal_label(card)
    return {
        "family": "edge", "game_id": card.get("game_id"), "sport": card.get("sport"), "season": card.get("season"),
        "week": card.get("week"), "market": e.get("market"), "side": e.get("side"), "book": e.get("book"),
        "tier": signal_slug(label), "model_version": e.get("model_version") or "v1",
        "last_line": _num(e.get("line")), "last_odds": e.get("odds"), "last_fair": _num(e.get("fair_line")),
        "last_edge": _num(e.get("edge_pts")), "status": "open", "run_id": run_id,
        "last_wind": _num(wx.get("wind_fg")), "last_rain": _num(wx.get("rain_fg")), "last_signal": label,
        "last_book": e.get("book"), "notification_active": True, "kickoff_utc": card.get("kickoff_utc"),
    }


def edge_candidates(card: dict[str, Any], alerts: dict, cfg: Config, run_id: Optional[str] = None,
                    now: Optional[datetime] = None) -> list[Candidate]:
    """One stable PLAY per game when the configured action gate is met.

    ``Candidate.tier`` is the signal slug (low | mid | high | very_high); the
    default gate admits Mid+ only, with a real price and at least a one-point edge.

    When supplied, ``now`` prevents a new betting notification at or after
    kickoff. ``collect_candidates`` always passes the run clock; direct callers
    may omit it for deterministic candidate construction."""
    if now is not None:
        kickoff = _dt(card.get("kickoff_utc"))
        if kickoff is not None and now >= kickoff:
            return []
    out = []
    for e in _alertable_edges(card, cfg):
        key = edge_key(card.get("season"), card.get("week"), card.get("game_id"), e["market"], e["side"], STABLE_PLAY_BOOK,
                       e.get("model_version") or "v1")
        if pstate.alert_sent(alerts, key) or _play_already_alerted(alerts, card, e):
            continue
        out.append(Candidate(key, "edge", card.get("sport") or "", format_edge(card, e, cfg.board_url),
                             game_id=card.get("game_id"), tier=signal_slug(_signal_label(card)),
                             kickoff_utc=_dt(card.get("kickoff_utc")), record=_edge_record(card, e, run_id),
                             summary=_play_summary(card, e)))
    return out


def _edge_for_record(card: dict[str, Any], rec: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The card entry behind an open EDGE record; a ``consensus``-book TOTAL UNDER record is
    re-synthesised from the card so GONE / SIGNAL CHANGE / MOVE keep evaluating."""
    for e in ((card.get("fair") or {}).get("edges") or []):
        if e.get("market") == rec.get("market") and e.get("side") == rec.get("side") and e.get("book") == rec.get("book"):
            return e
    if rec.get("book") == CONSENSUS_BOOK and rec.get("market") == "total" and rec.get("side") == "under":
        return consensus_entry(card)
    return None


def _record_identity(rec: dict[str, Any]) -> tuple[str, str, str]:
    # Telegram tracks the recommended play, not the implementation version that
    # produced it. A model promotion must not mint a second open parent.
    return (str(rec.get("game_id") or ""), str(rec.get("market") or ""), str(rec.get("side") or ""))


def _canonical_open_edges(alerts: dict, game_id: str) -> list[dict[str, Any]]:
    """One parent per game/play, even when legacy best-book churn created several."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rec in pstate.open_edge_records(alerts, game_id):
        grouped.setdefault(_record_identity(rec), []).append(rec)
    out = []
    for recs in grouped.values():
        recs.sort(key=lambda r: (
            0 if f"|{STABLE_PLAY_BOOK}|" in str(r.get("alert_key") or "") else 1,
            str(r.get("first_sent_at") or ""),
            str(r.get("alert_key") or ""),
        ))
        chosen = dict(recs[0])
        chosen["_related_edge_keys"] = [str(r.get("alert_key") or "") for r in recs[1:] if r.get("alert_key")]
        out.append(chosen)
    return out


def _record_notification_active(rec: dict[str, Any], cfg: Config) -> bool:
    explicit = rec.get("notification_active")
    if isinstance(explicit, bool):
        return explicit
    legacy_rank = {"edge": TIER_RANK["mid"], "strong": TIER_RANK["high"]}
    tier = str(rec.get("tier") or "")
    return (
        TIER_RANK.get(tier, legacy_rank.get(tier, 0)) >= TIER_RANK.get(cfg.min_tier, 2)
        and rec.get("book") != CONSENSUS_BOOK
        and _num(rec.get("first_line")) is not None
        and (_num(rec.get("first_edge")) or 0.0) >= cfg.min_edge_pts
    )


def move_bucket(market: str, line_now: float, last_line: float) -> int:
    step = MOVE_STEP.get(market, 1.0)
    return int(math.floor(abs(line_now - last_line) / step + 1e-9))


def move_direction(market: str, side: str, line_now: float, last_line: float, fair: float) -> str:
    return "toward fair" if abs(line_now - fair) < abs(last_line - fair) else "away from fair"


def followup_candidates(card: dict[str, Any], alerts: dict, cfg: Config, now: datetime,
                        run_id: Optional[str] = None) -> list[Candidate]:
    """At most one follow-up per game/play per run.

    Priority is CLOSED → signal change/reactivation → line move → forecast
    move. Legacy duplicate best-book parents are collapsed before evaluation.
    """
    out: list[Candidate] = []
    game_id = str(card.get("game_id") or "")
    kick = _dt(card.get("kickoff_utc"))
    if kick is not None and now >= kick:
        return out
    wx = card.get("weather") or {}
    label_now = _signal_label(card)
    slug_now = signal_slug(label_now)
    for rec in _canonical_open_edges(alerts, game_id):
        ekey = rec.get("alert_key") or ""
        # The notification identity is the game-level play, while the recommended
        # book may change. Re-evaluate the current best price without minting a new EDGE.
        e = _play_edge(card) if rec.get("market") == "total" and rec.get("side") == "under" else _edge_for_record(card, rec)
        if e is None:
            continue
        line_now, fair_now, pts_now = _num(e.get("line")), _num(e.get("fair_line")), _num(e.get("edge_pts"))
        base = {"game_id": game_id, "sport": card.get("sport"), "season": card.get("season"), "week": card.get("week"),
                "market": rec.get("market"), "side": rec.get("side"), "book": e.get("book"), "tier": slug_now,
                "model_version": rec.get("model_version") or "v1", "last_line": line_now, "last_odds": e.get("odds"),
                "last_fair": fair_now, "last_edge": pts_now, "run_id": run_id, "edge_key": ekey,
                "last_signal": label_now, "last_book": e.get("book"),
                "last_wind": _num(wx.get("wind_fg")), "last_rain": _num(wx.get("rain_fg")),
                "related_edge_keys": rec.get("_related_edge_keys") or []}
        was_active = _record_notification_active(rec, cfg)
        active_now = _actionable_play(card, e, cfg)

        # Only close plays that met the new actionable policy. Legacy Low/no-value
        # records are ignored so deployment does not generate a wall of CLOSED alerts.
        if was_active and not active_now:
            if not _tier_at_least(label_now, cfg.min_tier):
                reason = f"Signal {rec.get('last_signal') or '?'} → {label_now or SIGNAL_NONE}; below {cfg.min_tier.upper()}"
            elif e.get("book") == CONSENSUS_BOOK or line_now is None or _num(e.get("odds")) is None:
                reason = "No actionable book price is available"
            else:
                reason = f"Value fell to {_fmt_signed(pts_now)} pts (minimum +{cfg.min_edge_pts:g})"
            key = f"gone|{ekey}"
            if not pstate.alert_sent(alerts, key):
                out.append(Candidate(
                    key, "gone", card.get("sport") or "", format_gone(card, rec, e, cfg.board_url, reason=reason),
                    game_id=game_id, tier=slug_now, kickoff_utc=kick,
                    record={**base, "family": "gone", "status": "closed", "notification_active": False},
                    status="closed", summary=f"⛔ CLOSED · {html.escape(_matchup(card))} · {html.escape(reason)}",
                ))
            continue

        if not active_now:
            continue

        # A legacy Low/no-value notice can become actionable later. Emit one clear
        # PLAY without creating a second EDGE parent.
        if not was_active:
            key = f"activate|{ekey}|sig-{slug_now}"
            if not pstate.alert_sent(alerts, key):
                out.append(Candidate(
                    key, "wx", card.get("sport") or "", format_edge(card, e, cfg.board_url),
                    game_id=game_id, tier=slug_now, kickoff_utc=kick,
                    record={**base, "family": "wx", "status": "open", "notification_active": True},
                    summary=_play_summary(card, e),
                ))
            continue

        # SIGNAL CHANGE: only a tier change, not Low(Rain) → Low(Wind), and it
        # consumes this game's update slot for the run.
        last_sig = rec.get("last_signal")
        if last_sig and label_now and slug_now != signal_slug(str(last_sig)):
            key = f"wx|{ekey}|sig-{slug_now}"
            if not pstate.alert_sent(alerts, key):
                out.append(Candidate(
                    key, "wx", card.get("sport") or "", format_signal_change(card, rec, e, cfg.board_url),
                    game_id=game_id, tier=slug_now, kickoff_utc=kick,
                    record={**base, "family": "wx", "status": "open", "notification_active": True},
                    summary=(f"🔄 {html.escape(_matchup(card))} · {html.escape(str(last_sig))} → "
                             f"{html.escape(label_now)} · {re.sub(r'</?b>', '', _brief_bet(card, e))}"),
                ))
                continue
        if line_now is None or pts_now is None:
            continue

        # LINE MOVE (one per four hours); if the forecast moved too, the line
        # message includes its fair-line delta and absorbs that update.
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
                    if rec.get("last_book") and rec.get("last_book") != e.get("book"):
                        previous = {
                            "market": e.get("market"), "side": e.get("side"), "line": last_line,
                            "odds": rec.get("last_odds"), "book": rec.get("last_book"),
                        }
                        old_bet = re.sub(r"</?b>", "", _brief_bet(card, previous))
                        new_bet = re.sub(r"</?b>", "", _brief_bet(card, e))
                        move_summary = (f"🔄 {html.escape(_matchup(card))} · Best price: {old_bet} → {new_bet} "
                                        f"· value {_fmt_signed(pts_now)} pts")
                    else:
                        move_summary = (f"🔄 {html.escape(_matchup(card))} · {_side_label(e, card).title()} "
                                        f"{_fmt_line(last_line)} → {_fmt_line(line_now)} · value "
                                        f"{_fmt_signed(pts_now)} pts")
                    out.append(Candidate(
                        key, "move", card.get("sport") or "", format_move(card, rec, e, direction, cfg.board_url),
                        game_id=game_id, tier=slug_now, kickoff_utc=kick,
                        record={**base, "family": "move", "status": "open", "direction": direction,
                                "notification_active": True},
                        summary=move_summary,
                    ))
                    continue

        # FORECAST MOVE, bucketed from the first alerted fair so later material
        # shifts receive distinct keys. It only fires when no higher-priority
        # update was emitted above.
        last_fair = _num(rec.get("last_fair"))
        first_fair = _num(rec.get("first_fair"))
        if fair_now is not None and last_fair is not None and first_fair is not None:
            wb = int(math.floor(abs(fair_now - first_fair) / WX_STEP + 1e-9))
            if wb >= 1:
                key = f"wx|{ekey}|{wb}"
                if not pstate.alert_sent(alerts, key):
                    out.append(Candidate(
                        key, "wx", card.get("sport") or "", format_wx_move(card, rec, e, cfg.board_url),
                        game_id=game_id, tier=slug_now, kickoff_utc=kick,
                        record={**base, "family": "wx", "status": "open", "notification_active": True},
                        summary=(f"🔄 {html.escape(_matchup(card))} · fair {_fmt_line(last_fair)} → "
                                 f"{_fmt_line(fair_now)} · value {_fmt_signed(pts_now)} pts"),
                    ))
    return out


def opener_candidates(sport: str, cards: Sequence[dict[str, Any]], new_keys: Iterable[str], alerts: dict, cfg: Config,
                      now: datetime, run_id: Optional[str] = None) -> list[Candidate]:
    """One digest per run when new ``game_id|market|side|book`` keys appeared,
    restricted to weather games (gs_fg ≤ −2 or wind_fg ≥ 12)."""
    if not cfg.include_openers:
        return []
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
        kickoff = _dt(card.get("kickoff_utc"))
        if kickoff is not None and now >= kickoff:
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

    def add(key: str, sport: str, text: str, game_id: Optional[str] = None, *, bypass_quiet: bool = False) -> None:
        if not pstate.alert_sent(alerts, key):
            record = {"family": "ops", "sport": sport, "game_id": game_id, "run_id": run_id}
            if bypass_quiet:
                record["bypass_quiet"] = True
            out.append(Candidate(key, "ops", sport, text, game_id=game_id,
                                 record=record))

    for d in getattr(ctx, "degradations", []) or []:
        # Fatal build errors are owned by the workflow's one failure notification.
        # Sending them here as well duplicated the page and, because a failed job
        # does not publish alert state, could repeat the detailed copy next run.
        if d.severity != "warn" or _ops_expected(d.reason):
            continue
        title = ("DATA ISSUE · odds coverage dropped" if d.component == "odds.volume"
                 else f"{d.component} degraded")
        add(f"degr|{d.component}|{_slug(_stable(d.reason))}|{day}", "",
            format_ops(title, d.reason), bypass_quiet=d.component == "odds.volume")
    for ts, what in ((heartbeat_ts, "CF cron heartbeat"), (prev_meta_ts, "board meta")):
        if ts is not None and (now - ts) > timedelta(hours=STALE_HOURS):
            add(f"heartbeat|{_slug(what)}|{day}", "", format_ops(f"Refresh stale · {what}",
                                                                  f"Last seen {utc_iso(ts)} (> {STALE_HOURS:.0f} h)"))
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
        add(f"names|{book}|{day}", "", format_ops(f"DATA ISSUE · unresolved teams · {_book_label(book)} ({len(names)})",
                                                    "\n".join(sorted(set(names))[:25])))
    for sport, cards in cards_by_sport.items():
        for card in cards:
            if card.get("stadium") is None:
                add(f"stadium|{card.get('game_id')}", sport, format_ops(f"DATA ISSUE · stadium missing · {_matchup(card)}",
                                                                        str(card.get("game_id"))),
                    game_id=card.get("game_id"))
        if cards and all((c.get("consensus") or {}).get("total_now") is None for c in cards) and getattr(ctx, "scope", "") != "weather":
            add(f"noref|{sport}|{day}", sport, format_ops(f"DATA ISSUE · no reference totals · {SPORT_LABEL.get(sport, sport)}",
                                                          f"{len(cards)} games have no consensus total"))
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
            out += edge_candidates(card, alerts, cfg, run_id, now)
        out += opener_candidates(sport, cards, (new_keys_by_sport or {}).get(sport) or [], alerts, cfg, now, run_id)
    if include_ops:
        out += ops_candidates(ctx, cards_by_sport, alerts, now, heartbeat_ts=heartbeat_ts, prev_meta_ts=prev_meta_ts)
    return out


# ---- planning: dedup, quiet hours, cap ---------------------------------------------------------

def _priority(c: Candidate, now: datetime) -> tuple:
    hrs = ((c.kickoff_utc - now) / timedelta(hours=1)) if c.kickoff_utc else 999.0
    return (0 if c.tier in BYPASS_TIERS else 1, FAMILY_PRIORITY.get(c.family, 9), hrs, c.key)


def _to_queue_item(c: Candidate, now: datetime) -> dict[str, Any]:
    return {"key": c.key, "family": c.family, "sport": c.sport, "game_id": c.game_id, "tier": c.tier,
            "text": c.text, "summary": c.summary, "ts": utc_iso(now), "status": c.status,
            "kickoff_utc": utc_iso(c.kickoff_utc) if c.kickoff_utc else None, "record": c.record}


def _from_queue_item(q: dict[str, Any]) -> Candidate:
    return Candidate(q.get("key") or "", q.get("family") or "ops", q.get("sport") or "", q.get("text") or "",
                     game_id=q.get("game_id"), tier=q.get("tier"), kickoff_utc=_dt(q.get("kickoff_utc")),
                     record=q.get("record") or {}, status=q.get("status") or "open", summary=q.get("summary") or "")


def plan(candidates: Sequence[Candidate], alerts: dict, tg: dict, now: datetime, cfg: Config) -> Plan:
    """Dedup against markers, park non-bypass alerts during quiet hours (23:00–07:00
    ET) in ``tg['queue']``, release the queue outside quiet hours as one digest,
    and send no more than three alerts individually. The rest becomes a SUMMARY;
    the default plan is therefore at most four messages before per-chat/size splits."""
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
        # Revalidate queued snapshots against this run. If a signal/issue cleared
        # overnight, its key is no longer a current candidate and stale text is dropped.
        current_by_key = {c.key: c for c in fresh}
        p.flush = [_to_queue_item(current_by_key[str(q.get("key") or "")], now)
                   for q in pstate.drain_queue(tg)
                   if str(q.get("key") or "") in current_by_key
                   and not pstate.alert_sent(alerts, q.get("key") or "")]
        flush_keys = {str(q.get("key") or "") for q in p.flush}
        fresh = [c for c in fresh if c.key not in flush_keys]
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
    reserved = (1 if p.flush else 0) + (1 if p.ops else 0)
    budget = max(0, cfg.max_per_run - reserved)
    if budget == 0 and p.send:
        # A deliberately tight cap can be consumed by MORNING SUMMARY + SYSTEM.
        # Preserve fresh play updates for the next run instead of exceeding it.
        for c in p.send:
            pstate.queue_alert(tg, _to_queue_item(c, now))
            p.queued.append(c)
        p.send = []
        return p
    individual_limit = min(MAX_INDIVIDUAL_PER_RUN, budget)
    if len(p.send) > budget or len(p.send) > individual_limit:
        # Reserve one of the remaining slots for the summary.
        keep = min(individual_limit, max(0, budget - 1))
        p.digest = p.send[keep:]
        p.send = p.send[:keep]
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
        for k in ("last_line", "last_odds", "last_fair", "last_edge", "last_signal", "last_book", "tier",
                  "notification_active"):
            if c.record.get(k) is not None:
                parent[k] = c.record[k]
        parent["last_sent_at"] = ts
        if c.family == "move":
            parent["last_move_at"] = ts
        if c.record.get("last_wind") is not None or c.record.get("last_rain") is not None:
            parent["last_wind"], parent["last_rain"] = c.record.get("last_wind"), c.record.get("last_rain")
        if c.family == "gone":
            parent["status"] = "closed"
        outcome.records.append(parent)
        # Close legacy best-book duplicates with the canonical parent so a later
        # run cannot emit the same CLOSED follow-up from a second record.
        if c.family == "gone":
            for related_key in c.record.get("related_edge_keys") or []:
                related = pstate.get_alert_record(alerts, str(related_key))
                if related is not None:
                    related["status"] = "closed"
                    related["notification_active"] = False
                    related["last_sent_at"] = ts
                    outcome.records.append(related)
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
        _send_group("MORNING SUMMARY", [_from_queue_item(q) for q in p.flush], sender, alerts, now, outcome, cfg)
    for c in p.send:
        once(c)
    if p.digest:
        _send_group("SUMMARY", p.digest, sender, alerts, now, outcome, cfg)
    if p.ops:
        _send_group("SYSTEM", p.ops, sender, alerts, now, outcome, cfg)
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


def clv_digest(alerts: dict, *, sport: Optional[str] = None, top_n: int = 3,
               backtest: Optional[dict[str, Any]] = None) -> str:
    recs = [r for r in (alerts.get("records") or {}).values()
            if isinstance(r, dict) and r.get("family") == "edge" and _num(r.get("clv_pts")) is not None
            and (sport is None or r.get("sport") == sport)]
    if not recs:
        return "\n".join(["<b>📊 CLV SCORECARD</b>", "No settled plays with a closing line yet.",
                          *_backtest_section(backtest)])

    def group(keyfn: Callable[[dict], str]) -> list[str]:
        acc: dict[str, list[float]] = {}
        for r in recs:
            acc.setdefault(keyfn(r), []).append(float(r["clv_pts"]))
        rows = []
        for k, xs in sorted(acc.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            pos = sum(1 for x in xs if x > 0)
            rows.append(f"  {html.escape(k.replace('_', ' ').title())}: {len(xs)} plays · avg {_fmt_signed(sum(xs) / len(xs), 2)} · positive {pos}/{len(xs)}")
        return rows

    values = [float(r["clv_pts"]) for r in recs]
    positive = sum(1 for x in values if x > 0)
    lines = [
        f"<b>📊 CLV SCORECARD · {len(recs)} settled plays</b>",
        f"Overall: avg {_fmt_signed(sum(values) / len(values), 2)} pts · positive {positive}/{len(values)}",
        "<b>By signal</b>",
        *group(lambda r: str(r.get("tier") or "?")),
    ]
    models = {str(r.get("model_version") or "v1") for r in recs}
    if len(models) > 1:
        lines += ["<b>By model</b>", *group(lambda r: str(r.get("model_version") or "v1"))]
    ordered = sorted(recs, key=lambda r: float(r["clv_pts"]), reverse=True)

    def matchup(game_id: Any) -> str:
        tail = str(game_id or "?").rsplit(":", 1)[-1]
        return " @ ".join(part.replace("-", " ").upper() for part in tail.split("@", 1))

    def row(r: dict) -> str:
        return (f"  {html.escape(matchup(r.get('game_id')))} · {str(r.get('side')).title()} {_fmt_line(r.get('first_line'))} "
                f"· {_book_label(str(r.get('book')))} → close {_fmt_line(r.get('closing_line'))} · {_fmt_signed(r.get('clv_pts'))}")
    n_show = min(top_n, len(ordered))
    lines += [f"<b>Best {n_show}</b>", *[row(r) for r in ordered[:n_show]],
              f"<b>Worst {n_show}</b>", *[row(r) for r in ordered[-n_show:][::-1]]]
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
    p.add_argument("--flush", action="store_true",
                   help="release stored queue snapshots now; started games are discarded")
    p.add_argument("--sport", choices=("nfl", "cfb"), default=None)
    p.add_argument("--state-dir", type=Path, default=Path("data/state"))
    p.add_argument("--backtest", type=Path, default=None, help="board/backtest.json (v1 vs v2 CLV section of the digest)")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_repo_dotenv()
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
        queued = []
        for q in pstate.drain_queue(tg):
            if pstate.alert_sent(alerts, q.get("key") or ""):
                continue
            kickoff = _dt(q.get("kickoff_utc"))
            if q.get("game_id") and kickoff is not None and now >= kickoff:
                continue
            queued.append(q)
        outcome = Outcome()
        if queued:
            _send_group("MANUAL QUEUE · SNAPSHOT", [_from_queue_item(q) for q in queued], sender, alerts, now,
                        outcome, cfg)
        if not args.dry_run:
            pstate.save_alerts(args.state_dir, alerts)
            pstate.save_telegram_state(args.state_dir, tg)
        print(f"  flush: {outcome.n_sent} alert(s) in {outcome.n_messages} message(s), {len(outcome.failed)} failed")
        return 0 if not outcome.failed else 1
    print("nothing to do: pass --digest or --flush")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
