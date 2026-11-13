"""Football notification formatting, policy orchestration, and weekly postmortem.

    python -m pipeline.alerts --digest postmortem [--backtest data/board/backtest.json] [--email-fallback] [--dry-run]

Delivery is retired by default. FOOTBALL_TELEGRAM_ENABLED=1 restores delivery.
The single policy in pipeline.alert_policy selects first notices, scheduled
08:00/17:00 ET reminders, and CLEAR notices for explicitly confirmed bets.
High/Very High first notices and first notices whose kickoff precedes the next
routine slot can send immediately. Waiting candidates are rebuilt from current
cards each pass; stored queue snapshots are never delivered.

Signal eligibility remains weather-based, with prices as context. Successful
delivery alone marks alerts and appends records/feed entries. Book-health
paging is independent. Sport-specific chats fall back to TELEGRAM_CHAT_ID;
routine updates share the default chat. Disabled/dry runs print candidate keys
without sending or persisting. Weekly postmortems retain SMTP fallback.
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Optional

from pipeline import state as pstate
from pipeline.alert_liquidity import enrich_liquidity
from pipeline.model import config as model_config
from pipeline.model import process as process_screen
from pipeline.model.wind_history import stadium_wind_history
from utils.env import load_repo_dotenv
from utils.telegram import football_telegram_enabled
from utils.timeutil import ET, ensure_utc, now_utc, parse_iso_utc, to_et, utc_iso

_dt = partial(parse_iso_utc, strip=True, z_suffix_only=True)

logger = logging.getLogger(__name__)

# ---- constants ---------------------------------------------------------------------

TELEGRAM_MAX_CHARS = 4000        # API limit 4096; leave headroom for the header
SIGNAL_NONE = "No Impact"        # pipeline.model.signals.NO — the only label that never alerts
SIGNAL_SLUGS = (("very high", "very_high"), ("high", "high"), ("mid", "mid"), ("low", "low"))
TIER_RANK = {"low": 1, "mid": 2, "high": 3, "very_high": 4}
DEFAULT_MIN_TIER = "low"
DEFAULT_MAX_LEAD_DAYS = 7
STABLE_PLAY_BOOK = "best"        # key identity; the record still stores the actual book
BYPASS_TIERS = ("high", "very_high")   # immediate first-notice tiers / sort first
CONSENSUS_BOOK = "consensus"
DEFAULT_BOARD_URL = "https://football-board.mckinleyslade.workers.dev"

FAMILY_PRIORITY = {"edge": 0}

BOOK_LABELS = {
    "betonline": "BetOnline", "betcris": "Betcris", "fanduel": "FD", "draftkings": "DK", "kalshi": "Kalshi",
    "novig": "Novig", "prophetx": "ProphetX", "polymarket_us": "Polymarket US",
    "pinnacle": "Pinnacle", "consensus": "Consensus",
}
PRICE_EXCHANGES = {"novig", "kalshi", "prophetx", "polymarket_us"}
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
            lines = 4 if self.family == "edge" else 1
            self.summary = _summary(self.text, lines)


@dataclass
class Plan:
    send: list[Candidate] = field(default_factory=list)          # individual messages
    digest: list[Candidate] = field(default_factory=list)        # scheduled routine message
    queued: list[Candidate] = field(default_factory=list)        # waiting for the next routine slot


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
    max_lead_days: int = DEFAULT_MAX_LEAD_DAYS
    min_tier: str = DEFAULT_MIN_TIER
    book_health: bool = False  # Direct callers opt in; production from_env defaults it on.

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
            max_lead_days = max(0, int(e.get("TELEGRAM_MAX_LEAD_DAYS") or DEFAULT_MAX_LEAD_DAYS))
        except (TypeError, ValueError):
            max_lead_days = DEFAULT_MAX_LEAD_DAYS
        return cls(
            board_url=(e.get("BOARD_URL") or DEFAULT_BOARD_URL).rstrip("/"),
            chat_default=e.get("TELEGRAM_CHAT_ID") or None,
            chat_by_sport=by_sport,
            max_lead_days=max_lead_days,
            min_tier=min_tier,
            book_health=str(e.get("BOOK_HEALTH_ALERTS") or "1").strip().lower() in ("1", "true", "yes", "on"),
        )

    def chat_for(self, sport: Optional[str]) -> Optional[str]:
        return self.chat_by_sport.get(sport or "", self.chat_default)


@dataclass(frozen=True)
class EmailConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: tuple[str, ...] = ()
    use_ssl: bool = False
    starttls: bool = True

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> EmailConfig:
        e = os.environ if env is None else env
        try:
            port = int(e.get("SMTP_PORT") or 587)
        except (TypeError, ValueError):
            port = 587
        truthy = {"1", "true", "yes", "on"}
        ssl = str(e.get("SMTP_USE_SSL") or "").strip().lower() in truthy
        starttls = str(e.get("SMTP_STARTTLS") or "1").strip().lower() in truthy
        recipients = tuple(x.strip() for x in str(e.get("POSTMORTEM_EMAIL_TO") or "").split(",") if x.strip())
        username = str(e.get("SMTP_USERNAME") or "")
        return cls(
            host=str(e.get("SMTP_HOST") or ""), port=port, username=username,
            password=str(e.get("SMTP_PASSWORD") or ""),
            from_addr=str(e.get("SMTP_FROM") or username), to_addrs=recipients,
            use_ssl=ssl, starttls=starttls,
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_addr and self.to_addrs)


# ---- small helpers --------------------------------------------------------------------

def edge_key(season: Any, week: Any, game_id: str, market: str, side: str, book: str, model_version: str = "v1") -> str:
    return f"edge|{season}|{week}|{game_id}|{market}|{side}|{book}|{model_version}"


def parse_edge_key(key: str) -> Optional[dict[str, str]]:
    parts = key.split("|")
    if len(parts) != 8 or parts[0] != "edge":
        return None
    return dict(zip(("season", "week", "game_id", "market", "side", "book", "model_version"), parts[1:], strict=True))


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "x"


# Degradations that are expected or already reported elsewhere: the off-season game window,
# optional API keys, books disabled via BOOK_*_ENABLED, and a book returning 0 lines (the
# scoped volume-health incident handles a true dark book while peers report). They
# stay on the Status page and never page Telegram.


def _strict_number(v: Any) -> Optional[float]:
    """Published numeric fields only; strings and native bools are not evidence."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _fmt_odds(o: Any) -> str:
    n = _strict_number(o)
    if n is None:
        return "?"
    n = int(round(n))
    return f"+{n}" if n > 0 else f"{MINUS}{abs(n)}"


def _fmt_line(x: Any, signed: bool = False) -> str:
    n = _strict_number(x)
    if n is None:
        return "?"
    s = f"{abs(n):.1f}".rstrip("0").rstrip(".") if n != int(n) else f"{int(abs(n))}"
    if n < 0:
        return f"{MINUS}{s}"
    return f"+{s}" if signed else s


def _fmt_signed(x: Any, digits: int = 1) -> str:
    n = _strict_number(x)
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


def _details_link(board_url: str, card: dict[str, Any], label: str = "Details & all prices") -> str:
    href = f"{board_url}/#sport={card.get('sport')}&week={card.get('week')}&game={card.get('game_id')}"
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def _alert_heading(kind: str, card: dict[str, Any], emoji: str, *, tier: Optional[str] = None,
                   proc: Optional[dict[str, Any]] = None) -> list[str]:
    sport = SPORT_LABEL.get(card.get("sport") or "", str(card.get("sport") or "").upper())
    tier_s = (tier or signal_slug(_signal_label(card, proc)) or "").replace("_", " ").upper()
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
    line = _strict_number(edge.get("line"))
    if line is None or _strict_number(edge.get("odds")) is None or edge.get("book") == CONSENSUS_BOOK:
        value = f"{side} · no posted price available"
    else:
        line_s = _fmt_line(line, signed=market == "spread")
        book = str(edge.get("book") or "")
        cents = _cents(edge.get("odds")) if book in CENTS_BOOKS else None
        price = f"{cents}¢" if cents is not None else _fmt_odds(edge.get("odds"))
        value = f"{side} {line_s} ({price}) · {_book_label(book)}"
    prefix = f"{label}: " if label else ""
    return f"<b>{html.escape(prefix + value)}</b>"


def _altitude_phrase(card: dict[str, Any], *, warmth: bool = False) -> str:
    alt_m = _strict_number(card.get("travel_alt"))
    temp = _strict_number((card.get("weather") or {}).get("temp_fg"))
    climb = f"+{round(alt_m * 3.28084):,} ft climb" if alt_m is not None else "altitude climb"
    if warmth:
        temp_s = f"{round(temp)}°F" if temp is not None else "warm conditions"
        return f"Altitude + warmth: {climb} · {temp_s}"
    return f"Altitude: {climb}"


def _driver_phrase(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> str:
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
        t = _strict_number(wx.get("temp_fg"))
        return f"Temperature: {round(t) if t is not None else '?'}°F"
    if top == "alt":
        return _altitude_phrase(card)
    flags = [str(f) for f in _decision(card, proc)["flags"] if f]
    return f"Signal: {flags[0]}" if flags else f"Weather: {_wx_numbers(card)}"


def _why_lines(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> list[str]:
    return [f"Weather: {html.escape(_driver_phrase(card, proc))}", f"Forecast: {_wx_numbers(card)}"]


def _play_summary(card: dict[str, Any], edge: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> str:
    proc = _decision(card, proc)
    tier = (signal_slug(_signal_label(card, proc)) or "?").replace("_", " ").upper()
    bet = re.sub(r"</?b>", "", _brief_bet(card, edge))
    size = _quote_liquidity(next((q for q in _total_quotes(card, "under")
                                 if q["book"] == edge.get("book") and q["line"] == edge.get("line")), edge))
    return (f"{_emoji_for(card, proc)} {tier} · {html.escape(_matchup(card))} · {bet}{size} · {_kick_label(card)}"
            f"\n{_price_context(card, edge)[0]}\nForecast: {_wx_numbers(card)}"
            + "".join(f"\n{line}" for line in _opener_line_note(card, proc))
            + "".join(f"\n{line}" for line in _liquidity_context(card)))


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
    return {k: v for k, v in comps.items() if _strict_number(v) is not None and v > 0}


def _emoji_for(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> str:
    icons = {"wind": "🌬️", "rain": "🌧️", "cold": "🥶", "cold_away": "🥶", "heat": "🔥", "heat_away": "🔥",
             "temperature": "🌡️", "alt": "⛰️", "altitude_warmth": "⛰️ 🔥"}
    # Use what triggered the signal, not a larger but non-triggering model component.
    drivers = (card.get("signal") or {}).get("drivers") or []
    selected = list(dict.fromkeys(icons[d] for d in drivers if d in icons))
    if selected:
        return " ".join(selected)
    label = (_signal_label(card, proc) or "").lower()
    for word, driver in (("rain", "rain"), ("wind", "wind"), ("temp", "temperature")):
        if word in label:
            return icons[driver]
    comps = _components(card)
    if not comps:
        return "📈"
    top = max(comps, key=lambda k: comps[k])
    return icons.get(top, "📈")


def _side_label(edge: dict[str, Any], card: dict[str, Any]) -> str:
    side = edge.get("side")
    if side in ("over", "under"):
        return str(side).upper()
    team = (card.get(side) or {}).get("short") or str(side).upper()
    return f"{team}"


def _decision(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The card's one decision record. Pass ``proc`` to reuse it within a card.

    Stored snapshots without a record fall back through ``decision_for`` at the
    alert clock stamped on the card, not the wall clock.
    """
    return proc if proc is not None else process_screen.decision_for(card, now=_dt(card.get("_alert_at")))


def _signal_label(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Read the build decision, with the shared fallback for stored snapshots."""
    return _decision(card, proc)["alert_label"]


def _in_signal(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> bool:
    """The legacy bet rule: the map dot is any tier but 'No Impact'."""
    lbl = _signal_label(card, proc)
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
    """The game-window forecast values used by the signal rules, including zeros."""
    wx = card.get("weather") or {}
    if wx.get("signal_source"):
        shown = {"wind_fg": wx.get("signal_wind_mph"), "temp_fg": wx.get("signal_temp_f"),
                 "rain_fg": wx.get("signal_rain_mm")}
    else:
        shown = wx
    def value(key: str, unit: str) -> str:
        return f"{_fmt_line(shown[key])} {unit}" if _strict_number(shown.get(key)) is not None else "unavailable"
    line = f"wind {value('wind_fg', 'mph')} · temp {value('temp_fg', '°F')} · rain {value('rain_fg', 'mm')}"
    source = str(wx.get("signal_source") or "")
    if source.startswith("ecmwf"):
        line += " · ECMWF"
    return line


def _cents(odds: Any) -> Optional[int]:
    """American odds → implied price in cents (Kalshi contracts): −108 → 52¢, +120 → 45¢."""
    n = _strict_number(odds)
    if n is None or n == 0:
        return None
    return int(round(100 * (abs(n) / (100 + abs(n)) if n < 0 else 100 / (100 + n))))


# ---- formatters --------------------------------------------------------------------------

def _fresh_price(updated_at: Any, now: datetime) -> bool:
    updated = _dt(updated_at)
    return updated is not None and timedelta(0) <= now - updated <= timedelta(hours=1)


def _total_quotes(card: dict[str, Any], side: str) -> list[dict[str, Any]]:
    now = _dt(card.get("_alert_at")) or now_utc()
    kickoff = _dt(card.get("kickoff_utc"))
    if not kickoff or kickoff <= now:
        return []
    return sorted((q for q in (card.get("total_prices") or {}).get("quotes", [])
                   if q.get("book") in PRICE_EXCHANGES and q.get("side") == side and q.get("liquidity_status") != "empty" and _strict_number(q.get("ev_roi")) is not None
                   and _strict_number(q.get("line")) is not None and _strict_number(q.get("odds")) is not None
                   and _fresh_price(q.get("updated_at"), now)),
                  key=lambda q: (-q["ev_roi"], q["book"]))


def _quote_liquidity(quote: dict[str, Any]) -> str:
    if quote.get("book") not in PRICE_EXCHANGES:
        return ""
    if quote.get("liquidity_status") == "verified":
        return f" · ${quote['liquidity_dollars']:,.2f} available"
    return " · size unverified"


def _liquidity_context(card: dict[str, Any]) -> list[str]:
    snapshot = card.get("alert_liquidity")
    if not snapshot:
        if any(q.get("book") in PRICE_EXCHANGES for q in (card.get("total_prices") or {}).get("quotes", [])):
            return ["Liquidity: unverified"]
        return []
    stamp = _dt(snapshot.get("checked_at"))
    rows = ["Cash stake capacity · taker fees separate" + (f" · {to_et(stamp):%I:%M %p %Z}" if stamp else "")]
    for i, fill in enumerate(snapshot["allocations"], 1):
        manual = " · manual" if fill["book"] == "novig" else ""
        p = fill["all_in_price"]
        odds = round(-100 * p / (1 - p) if p >= .5 else 100 * (1 - p) / p)
        rows.append(f"{i}) {_book_label(fill['book'])} U{_fmt_line(fill['line'])} ({_fmt_odds(odds)}): "
                    f"${fill['available_dollars']:,.2f} cash stake available · fees ${fill.get('fees', 0):,.2f} "
                    f"· debit ${fill['spend']:,.2f}{manual}")
    capacity = snapshot.get("cash_stake_capacity")
    if capacity is None:
        rows.append("Weather watch · cash stake capacity unverified")
    else:
        verified = snapshot.get("cash_liquidity_verified") is True and capacity >= 500
        rows.append(f"${capacity:,.2f} cash stake capacity · " + ("$500 minimum verified" if verified else "$500 minimum not met"))
        rows.append(f"Fees ${snapshot.get('fee_capacity', 0):,.2f} · total debit ${snapshot.get('debit_capacity', 0):,.2f} "
                    f"· winning payout ${snapshot.get('payout_capacity', 0):,.2f}")
        rows.append("Weather watch · probability estimate empirically unvalidated")
    return rows


def _comparison_context(card: dict[str, Any], edge: dict[str, Any]) -> list[str]:
    rows = []
    if edge.get("market") == "total":
        quotes = _total_quotes(card, str(edge.get("side")))
        for label, choices in (("Best price", quotes),
                               ("Best exchange", [q for q in quotes if q["book"] in PRICE_EXCHANGES])):
            if not choices:
                rows.append(f"{label}: unavailable (no fresh comparison)")
                continue
            q = choices[0]
            name = "NoVig" if q["book"] == "novig" else _book_label(q["book"])
            rows.append(f"{label}: {html.escape(name)} · {str(q['side']).title()} {_fmt_line(q['line'])} "
                        f"({_fmt_odds(q['odds'])}) · est. EV {_fmt_signed(q['ev_roi'] * 100)}%"
                        + (" · no +EV" if q["ev_roi"] <= 0 else "") + _quote_liquidity(q))
        rows.extend(_liquidity_context(card))
    history = card.get("stadium_wind_history") or {}
    stamp = _dt(history.get("as_of"))
    as_of = f" · as of {to_et(stamp):%m/%d/%Y}" if stamp else ""
    if history.get("status") != "ok":
        rows.append("Stadium wind unders: history unavailable")
    elif not history.get("n"):
        rows.append(f"Stadium wind unders: no graded {html.escape(history['signal'])} games in available history{as_of}")
    else:
        seasons = history["seasons"]
        period = str(seasons[0]) if len(seasons) == 1 else f"{seasons[0]}–{seasons[-1]}"
        rows.append(f"Stadium wind unders: {history['wins']}-{history['losses']}-{history['pushes']} W-L-P "
                    f"· n={history['n']} · {period}")
        venue = html.escape(str((card.get("stadium") or {}).get("name") or "this stadium"))
        rows.append(f"Basis: {html.escape(history['signal'])} · closing forecasts/totals{as_of} · {venue}")
    if history.get("status") == "ok" and not history.get("archive_available"):
        rows.append("Historical archive unavailable; forecast record covers current feed only")
    actual = history.get("actual") or {}
    if actual.get("status") == "ok":
        if actual.get("n"):
            seasons = actual["seasons"]
            period = str(seasons[0]) if len(seasons) == 1 else f"{seasons[0]}–{seasons[-1]}"
            rows.append(f"Actual-wind unders: {actual['wins']}-{actual['losses']}-{actual['pushes']} W-L-P "
                        f"· n={actual['n']} · {period}")
        else:
            rows.append("Actual-wind unders: no graded games in available history")
        rows.append("Basis: ERA5 wind ≥15 mph · closing totals · descriptive, not forecast signals")
    return rows


def _price_context(card: dict[str, Any], edge: dict[str, Any]) -> list[str]:
    """Stored weekly baseline and current exchange quotes for the alerted side."""
    market, side = edge.get("market"), edge.get("side")
    if market not in ("total", "spread", "ml"):
        return []
    books = card.get("odds") or {}
    book = "betonline" if market == "total" else str(edge.get("book") or CONSENSUS_BOOK)
    if market == "total":
        weekly = card.get("weekly_total_open") or {}
        opening = {"open_line": weekly.get("line"), "open_under": weekly.get("under"),
                   "open_over": weekly.get("over")}
    else:
        opening = (books.get(book) or {}).get(market) or {}

    def quote(data: dict[str, Any], venue: str, *, opener: bool = False) -> str:
        if market == "total":
            line = _strict_number(data.get("open_line" if opener else "line"))
            odds = data.get(f"open_{side}" if opener else side)
        elif market == "spread":
            line = _strict_number(data.get("open_line" if opener else "home_line"))
            if side == "away" and line is not None:
                line = -line
            # The board stores only the home-side spread opening juice.
            odds = (data.get("open_odds") if side == "home" else None) if opener else data.get(f"{side}_odds")
        else:
            line = None
            odds = data.get(f"open_{side}" if opener else side)
        if (market != "ml" and line is None) or (not opener and _strict_number(odds) is None):
            return "unavailable"
        if market == "ml" and _strict_number(odds) is None:
            return "unavailable"
        label = html.escape(_side_label(edge, card).title())
        if market != "ml":
            label += f" {_fmt_line(line, signed=market == 'spread')}"
        cents = _cents(odds) if venue in CENTS_BOOKS else None
        price = f"{cents}¢" if cents is not None else _fmt_odds(odds)
        return f"{label} ({price})"

    baseline = quote(opening, book, opener=True)
    source = _book_label(book)
    if baseline == "unavailable" and market == "spread":
        opening_line = (card.get("consensus") or {}).get(f"{market}_open")
        baseline = quote({"open_line": opening_line}, CONSENSUS_BOOK, opener=True)
        source = "reference"
    rows = [f"Week open: {baseline}" + (f" · {source}" if baseline != "unavailable" else "")]
    for venue, name in (("kalshi", "Kalshi"), ("novig", "NoVig")):
        data = (books.get(venue) or {}).get(market) or {}
        now = _dt(card.get("_alert_at")) or now_utc()
        value = quote(data, venue)
        if value != "unavailable" and ("total_prices" in card or "updated_at" in data):
            if not _fresh_price(data.get("updated_at"), now):
                value = "unavailable (quote stale or timestamp missing)"
        rows.append(f"{name} now: {value}")
    return rows


def _opener_line_note(card: dict[str, Any], proc: Optional[dict[str, Any]] = None) -> list[str]:
    """Surface the opener evidence already evaluated at publication."""
    if _decision(card, proc).get("line_open_missing"):
        return ["Opener line unknown"]
    return []


def format_edge(card: dict[str, Any], edge: dict[str, Any], board_url: str = DEFAULT_BOARD_URL,
                proc: Optional[dict[str, Any]] = None) -> str:
    """Weather notification with optional price/model context, not an instruction to bet."""
    proc = _decision(card, proc)
    lines = [
        *_alert_heading("SIGNAL", card, _emoji_for(card, proc), proc=proc),
        _brief_bet(card, edge),
        *_why_lines(card, proc),
        *_comparison_context(card, edge),
        *_price_context(card, edge),
        *_opener_line_note(card, proc),
        _details_link(board_url, card),
    ]
    return "\n".join(lines)


def format_ops(title: str, body: str = "") -> str:
    return f"⚠️ <b>{html.escape(title)}</b>" + (f"\n{html.escape(body)}" if body else "")


def telegram_pages(text: str, continuation: str = "<b>Signal details · continued</b>") -> list[str]:
    """Split on complete lines so price ladders are retained and HTML stays intact."""
    pages, page = [], ""
    for line in text.splitlines():
        if len(page) + len(line) + 1 > TELEGRAM_MAX_CHARS:
            pages.append(page)
            page = continuation
        page += ("\n" if page else "") + line
    if page:
        pages.append(page)
    return pages


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
    line, fair_t = _strict_number(cons.get("total_now")), _strict_number(fair.get("fair_total"))
    return {
        "game_id": card.get("game_id"), "market": "total", "side": "under", "book": CONSENSUS_BOOK,
        "line": line, "odds": None, "fair_line": fair_t,
        "edge_pts": round(line - fair_t, 2) if line is not None and fair_t is not None else None,
        "edge_prob": None, "tier": None, "model_version": _model_version(card),
        "confidence": fair.get("confidence"), "ref_book": cons.get("ref_book"), "n_books": cons.get("n_books"),
    }


def _play_edge(card: dict[str, Any]) -> dict[str, Any]:
    """Use the same fresh ROI-ranked under as Best price, independent of signal eligibility.

    Without a model comparison, show a fresh posted under if available. Legacy
    cards without total_prices retain their fair.edges price selection.
    """
    entry = consensus_entry(card)
    if "total_prices" in card:
        quotes = _total_quotes(card, "under")
        if not quotes:
            now = _dt(card.get("_alert_at")) or now_utc()
            for book, markets in (card.get("odds") or {}).items():
                if book not in PRICE_EXCHANGES:
                    continue
                total = markets.get("total") or {}
                if any(q.get("book") == book and q.get("side") == "under" and q.get("line") == total.get("line")
                       and q.get("liquidity_status") == "empty" for q in (card.get("total_prices") or {}).get("quotes", [])):
                    continue
                if (_strict_number(total.get("line")) is not None and _strict_number(total.get("under")) is not None
                        and _fresh_price(total.get("updated_at"), now)):
                    quotes.append({"book": book, "line": total["line"], "odds": total["under"]})
            quotes.sort(key=lambda q: (-q["line"], -q["odds"], q["book"]))
        if quotes:
            q = quotes[0]
            fair = _strict_number(entry.get("fair_line"))
            return dict(entry, book=q["book"], line=q["line"], odds=q["odds"],
                        edge_pts=round(q["line"] - fair, 2) if fair is not None else None)
        return dict(entry, line=None, odds=None, edge_pts=None)
    unders = [e for e in ((card.get("fair") or {}).get("edges") or [])
              if e.get("market") == "total" and e.get("side") == "under"
              and _strict_number(e.get("line")) is not None and _strict_number(e.get("odds")) is not None
              and e.get("book") != "betcris"]
    if unders:   # ties on edge_pts → the best price for an under bettor (higher line, then better odds)
        return max(unders, key=lambda e: (_strict_number(e.get("edge_pts")) or 0.0, _strict_number(e.get("line")) or -1e9,
                                          _strict_number(e.get("odds")) or -1e9))
    # A reference consensus is not an obtainable betting entry.
    return dict(entry, line=None, odds=None, edge_pts=None)


def _tier_at_least(label: Optional[str], minimum: str) -> bool:
    return TIER_RANK.get(signal_slug(label) or "", 0) >= TIER_RANK.get(minimum, TIER_RANK[DEFAULT_MIN_TIER])


def _qualifying_signal(card: dict[str, Any], cfg: Config, proc: Optional[dict[str, Any]] = None) -> bool:
    """Process screen plus the tier floor. v1 impact is not this gate.

    Covered roofs, the 12–15 mph wind watch, wind below 12 mph, light rain,
    heavy rain without ECMWF agreement, and a consensus under-price move past
    the configured opener limit do not qualify. Temperature does not block
    unless that league's optional gate is on. ``TELEGRAM_MIN_TIER`` can still
    require a higher published tier.
    """
    proc = _decision(card, proc)
    if not proc.get("alert"):
        return False
    return _tier_at_least(_signal_label(card, proc), cfg.min_tier)


def _within_alert_window(kickoff: Optional[datetime], now: datetime, cfg: Config) -> bool:
    """Require a valid future kickoff no farther away than the betting horizon."""
    if kickoff is None:
        return False
    delta = kickoff - now
    return timedelta(0) < delta <= timedelta(days=cfg.max_lead_days)


def _card_within_alert_window(card: dict[str, Any], now: datetime, cfg: Config) -> bool:
    return _within_alert_window(_dt(card.get("kickoff_utc")), now, cfg)


def _alertable_edges(card: dict[str, Any], cfg: Optional[Config] = None,
                     proc: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """At most one weather notification per game, even without a posted total."""
    cfg = cfg or Config()
    proc = _decision(card, proc)
    if not _in_signal(card, proc):
        return []
    play = _play_edge(card)
    return [play] if _qualifying_signal(card, cfg, proc) else []


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


def _edge_record(card: dict[str, Any], e: dict[str, Any], run_id: Optional[str],
                 proc: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    wx = card.get("weather") or {}
    label = _signal_label(card, proc)
    return {
        "family": "edge", "game_id": card.get("game_id"), "sport": card.get("sport"), "season": card.get("season"),
        "week": card.get("week"), "market": e.get("market"), "side": e.get("side"), "book": e.get("book"),
        "tier": signal_slug(label), "model_version": e.get("model_version") or "v1",
        "last_line": _strict_number(e.get("line")), "last_odds": e.get("odds"), "last_fair": _strict_number(e.get("fair_line")),
        "last_edge": _strict_number(e.get("edge_pts")), "status": "open", "run_id": run_id,
        "last_wind": _strict_number(wx.get("wind_fg")), "last_rain": _strict_number(wx.get("rain_fg")), "last_signal": label,
        "last_book": e.get("book"), "notification_active": True, "kickoff_utc": card.get("kickoff_utc"),
    }


def edge_candidates(card: dict[str, Any], alerts: dict, cfg: Config, run_id: Optional[str] = None,
                    now: Optional[datetime] = None) -> list[Candidate]:
    """One stable weather notification per game when the signal qualifies.

    ``Candidate.tier`` is the signal slug (low | mid | high | very_high); the
    default includes Low+ and never depends on price or fair-value availability.

    When supplied, ``now`` prevents a new betting notification at or after
    kickoff. ``collect_candidates`` always passes the run clock; direct callers
    may omit it for deterministic candidate construction."""
    if now is not None:
        card = dict(card, _alert_at=utc_iso(now))
        kickoff = _dt(card.get("kickoff_utc"))
        if not _within_alert_window(kickoff, now, cfg):
            return []
    out = []
    proc = _decision(card)
    for e in _alertable_edges(card, cfg, proc):
        key = edge_key(card.get("season"), card.get("week"), card.get("game_id"), e["market"], e["side"], STABLE_PLAY_BOOK,
                       e.get("model_version") or "v1")
        if pstate.alert_sent(alerts, key) or _play_already_alerted(alerts, card, e):
            continue
        out.append(Candidate(key, "edge", card.get("sport") or "", format_edge(card, e, cfg.board_url, proc),
                             game_id=card.get("game_id"), tier=signal_slug(_signal_label(card, proc)),
                             kickoff_utc=_dt(card.get("kickoff_utc")), record=_edge_record(card, e, run_id, proc),
                             summary=_play_summary(card, e, proc)))
    return out


def collect_candidates(
    ctx: Any,
    cards_by_sport: dict[str, Sequence[dict[str, Any]]],
    alerts: dict,
    cfg: Config,
    now: datetime,
) -> list[Candidate]:
    from pipeline import alert_policy
    return alert_policy.collect(ctx, cards_by_sport, alerts, cfg, now, alerts.get('_confirmations') or {})


# ---- book health (independent of TELEGRAM_SYSTEM_ALERTS) ------------------------------------

BOOK_HEALTH_MIN_EMPTY = 3
BOOK_HEALTH_MARKERS = "book_health"   # alerts["book_health"]["{sport}|{book}"] = ISO ts of the down message


def book_health_messages(health: dict, alerts: dict) -> list[tuple[str, str, str, str]]:
    """``[(kind, sport, book, text)]`` for books newly down (>= 3 consecutive empty scrapes while the
    odds horizon has games) and for previously-alerted books that returned rows. Pure: markers are
    recorded by the caller only after a successful send."""
    markers = alerts.get(BOOK_HEALTH_MARKERS) or {}
    out: list[tuple[str, str, str, str]] = []
    for book, sports in sorted((health.get("books") or {}).items()):
        for sport, e in sorted((sports or {}).items()):
            mk = f"{sport}|{book}"
            empty = int(e.get("consecutive_empty") or 0)
            from pipeline.scrape_outcomes import ScrapeOutcome
            code = e.get('outcome_code')
            if (code not in (ScrapeOutcome.UPSTREAM_STALE, ScrapeOutcome.SKIPPED, ScrapeOutcome.NOT_REQUESTED)
                    and empty >= BOOK_HEALTH_MIN_EMPTY and int((health.get("horizon") or {}).get(sport) or 0) > 0 and mk not in markers):
                last_ok = e.get("last_ok_utc") or "never"
                err = e.get("last_error") or "returned 0 lines"
                out.append(("down", sport, book, format_ops(
                    f"{book} {sport.upper()} down",
                    f"{empty} empty scrapes in a row; last rows {last_ok}. Last error: {err}")))
            elif int(e.get("rows") or 0) > 0 and mk in markers:
                out.append(("up", sport, book, format_ops(f"{book} {sport.upper()} recovered", f"Back with {int(e['rows'])} rows.")))
    return out


def send_book_health(health: dict, alerts: dict, sender: Sender, cfg: Config, now: datetime) -> int:
    """Send book-health messages; record/clear dedup markers only after a successful send."""
    sent = 0
    for kind, sport, book, text in book_health_messages(health, alerts):
        try:
            ok = bool(sender(text, cfg.chat_default))
        except Exception as exc:  # noqa: BLE001
            logger.warning("book-health alert failed: %s", exc)
            ok = False
        if not ok:
            continue
        markers = alerts.setdefault(BOOK_HEALTH_MARKERS, {})
        if kind == "down":
            markers[f"{sport}|{book}"] = utc_iso(now)
        else:
            markers.pop(f"{sport}|{book}", None)
        sent += 1
    return sent


# ---- planning: first notices, scheduled reminders, explicit CLEAR ---------------------------------------------------------

def _priority(c: Candidate, now: datetime) -> tuple:
    hrs = ((c.kickoff_utc - now) / timedelta(hours=1)) if c.kickoff_utc else 999.0
    return (0 if c.tier in BYPASS_TIERS else 1, FAMILY_PRIORITY.get(c.family, 9), hrs, c.key)


def plan(candidates: Sequence[Candidate], alerts: dict, tg: dict, now: datetime, cfg: Config) -> Plan:
    from pipeline import alert_policy
    return alert_policy.plan(candidates, alerts, tg, now, cfg)


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
    if c.family == 'clear':
        alerts.setdefault('cleared_bets', {})[c.record['bet_id']] = ts
    fields = dict(c.record)
    fields.setdefault("family", c.family)
    fields["status"] = c.status if c.family != "edge" else "open"
    fields["last_sent_at"] = ts
    rec = pstate.upsert_alert_record(alerts, c.key, fields, ts)
    rec["sends"] = int(rec.get("sends") or 0) + 1
    outcome.records.append(rec)
    if c.family == "edge":
        (alerts.get("first_signals") or {}).pop(str(c.game_id), None)
    # MOVE/GONE/WX update the parent EDGE record (last_* / status / cooldown)
    parent_key = c.record.get("edge_key")
    parent = pstate.get_alert_record(alerts, parent_key) if parent_key else None
    if parent is not None:
        for k in ("last_line", "last_odds", "last_fair", "last_edge", "last_signal", "last_book", "tier",
                  "notification_active", "signal_revision"):
            if k in c.record:
                parent[k] = c.record[k]
        parent["last_sent_at"] = ts
        if c.family == "move":
            parent["last_move_at"] = ts
        if c.record.get("last_wind") is not None or c.record.get("last_rain") is not None:
            parent["last_wind"], parent["last_rain"] = c.record.get("last_wind"), c.record.get("last_rain")
        parent["status"] = c.status
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


def _alert_once(sender: Sender, alerts: dict, now: datetime, outcome: Outcome, cfg: Config,
                checkpoint: Optional[Callable[[], None]] = None) -> Callable[[Candidate], bool]:
    """Closure from golf build.py L2494-2502: check marker → send → mark only on success."""
    def _once(c: Candidate) -> bool:
        if c.family == "gone":
            return False
        if pstate.alert_sent(alerts, c.key):
            return False
        ok = False
        try:
            ok = True
            for page in telegram_pages(c.text):
                if not sender(page, cfg.chat_for(c.sport) if c.family != "ops" else cfg.chat_default):
                    ok = False
                    break
                outcome.n_messages += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"alert send failed [{c.key}]: {exc}")
            ok = False
        if ok:
            _mark(c, alerts, now, outcome)
            if checkpoint is not None:
                checkpoint()  # Stop delivery if the durable receipt cannot be saved.
            logger.warning(f"alerted [{c.key}]")
        else:
            outcome.failed.append(c)
        return ok
    return _once


def dispatch(p: Plan, alerts: dict, sender: Sender, now: datetime, cfg: Config,
             checkpoint: Optional[Callable[[], None]] = None,
             confirmation_reader: Optional[Callable[[], dict]] = None) -> Outcome:
    from pipeline import alert_policy
    return alert_policy.dispatch(p, alerts, sender, now, cfg, checkpoint, confirmation_reader)


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


def run_alerts(
    ctx: Any,
    cards_by_sport: dict[str, Sequence[dict[str, Any]]],
    state_dir: Path,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    sender: Optional[Sender] = None,
    cfg: Optional[Config] = None,
    now: Optional[datetime] = None,
    fetch_rows: Optional[Callable[[], Any]] = None,
    receipt_checkpoint: Optional[Callable[[dict, dict], None]] = None,
    confirmation_reader: Optional[Callable[[], dict]] = None,
) -> AlertsRun:
    """Collect → plan → dispatch → persist. With ``enabled=False`` or ``dry_run``
    the candidates are printed with their keys and nothing is sent or marked."""
    enabled = enabled and football_telegram_enabled()
    cfg = cfg or Config.from_env()
    supplied_now = now is not None
    now = ensure_utc(now) if now else now_utc()
    state_dir = Path(state_dir)
    alerts, source = pstate.load_alerts_rehydrated(state_dir, fetch_rows)
    from pipeline.bet_confirmations import load_confirmations
    alerts['_confirmations'] = load_confirmations(state_dir)
    tg = pstate.load_telegram_state(state_dir)
    history = _load_backtest(state_dir / "backtest.json")
    archive = _load_backtest(state_dir / "wind-history-v1.json")
    cards_by_sport = {sport: [dict(card, stadium_wind_history=stadium_wind_history(card, history, now=now, archive=archive))
                             for card in cards] for sport, cards in cards_by_sport.items()}
    if enabled and not dry_run:
        enrich_liquidity([card for cards in cards_by_sport.values() for card in cards
                          if _qualifying_signal(card, cfg) and _card_within_alert_window(card, now, cfg)])
        if not supplied_now:
            now = now_utc()
        if confirmation_reader:
            alerts['_confirmations'] = confirmation_reader()
    cands = collect_candidates(ctx, cards_by_sport, alerts, cfg, now)
    live = enabled and not dry_run
    if not live:
        n_q = len(tg.get("queue") or [])
        print(f"  alerts ({'dry-run' if dry_run else 'disabled'}): {len(cands)} candidate(s), {n_q} queued, state={source}")
        for c in sorted(cands, key=lambda c: _priority(c, now)):
            print(f"    [{c.family}{'/' + c.tier if c.tier else ''}] {c.key}")
        return AlertsRun(cands, Plan(), Outcome(), alerts, tg, source)
    p = plan(cands, alerts, tg, now, cfg)
    send = sender or default_sender()
    checkpoint = (lambda: receipt_checkpoint(alerts, tg)) if receipt_checkpoint is not None else None
    outcome = dispatch(p, alerts, send, now, cfg, checkpoint, confirmation_reader)
    if cfg.book_health:
        try:
            outcome.n_messages += send_book_health(pstate.load_book_health(state_dir), alerts, send, cfg, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("book-health alerts skipped: %s", exc)
    pstate.save_alerts(state_dir, alerts)
    pstate.save_telegram_state(state_dir, tg)
    if checkpoint is not None:
        checkpoint()
    print(f"  alerts: {outcome.n_sent} sent in {outcome.n_messages} message(s), {len(p.queued)} queued, "
          f"{len(p.digest)} digested, {len(outcome.failed)} failed")
    return AlertsRun(cands, p, outcome, alerts, tg, source)


# ---- weekly postmortem digest (backtest.yml) --------------------------------------------------

DIGEST_KINDS = ("postmortem",)
POSTMORTEM_MORE_MARKER = "more on dashboard"


def postmortem_digest(backtest: Optional[dict[str, Any]], *, sport: Optional[str] = None,
                      board_url: str = DEFAULT_BOARD_URL, max_chars: Optional[int] = TELEGRAM_MAX_CHARS) -> str:
    """One scan-friendly weekly scorecard; the dashboard retains every detail."""
    pm = (backtest or {}).get("postmortem") or {}
    latest = pm.get("latest") or {}
    bets = [b for b in (latest.get("bets") or []) if isinstance(b, dict)
            and (sport is None or b.get("sport") == sport)]
    if not bets:
        return "\n".join([
            "<b>🧾 WEEKLY POST-MORTEM</b>",
            f"No completed {SPORT_LABEL.get(sport, sport or '').strip()} plays in the last 7 days.".replace("  ", " "),
            f'<a href="{html.escape(board_url + "/#view=backtest", quote=True)}">Season report</a>',
        ])

    graded = [b for b in bets if b.get("result") in {"W", "L", "P"}]
    wins = sum(b.get("result") == "W" for b in graded)
    losses = sum(b.get("result") == "L" for b in graded)
    pushes = sum(b.get("result") == "P" for b in graded)
    units = [_strict_number(b.get("unit_profit")) for b in graded]
    units = [x for x in units if x is not None]
    clvs = [_strict_number(b.get("clv_pts")) for b in bets]
    clvs = [x for x in clvs if x is not None]
    thesis = [b for b in bets if b.get("wind_thesis")]
    thesis_checked = [b for b in thesis if b.get("wind_materialized") is not None]
    held = sum(b.get("wind_materialized") is True for b in thesis_checked)
    first_wind_errors = [abs(float(first) - float(actual)) for b in bets
                         if (first := _strict_number((b.get("first_weather") or {}).get("wind_mph"))) is not None
                         and (actual := _strict_number((b.get("actual_weather") or {}).get("wind_mph"))) is not None]
    final_wind_errors = [abs(float(final) - float(actual)) for b in bets
                         if (final := _strict_number((b.get("final_forecast") or {}).get("wind_mph"))) is not None
                         and (actual := _strict_number((b.get("actual_weather") or {}).get("wind_mph"))) is not None]

    start, end = _dt(latest.get("window_start")), _dt(latest.get("window_end"))
    def day(dt: Optional[datetime]) -> str:
        return f"{to_et(dt).strftime('%b')} {to_et(dt).day}" if dt is not None else "?"

    league = SPORT_LABEL.get(sport or "", "FOOTBALL")
    title = f"<b>🧾 {league} WEEKLY POST-MORTEM · {day(start)}–{day(end)}</b>"
    units_text = f" · {_fmt_signed(sum(units), 2)}u" if units else ""
    roi_text = f" · ROI {_fmt_signed(100 * sum(units) / len(units), 1)}%" if units else ""
    close_text = (f"Closing line: beat {sum(x > 0 for x in clvs)}/{len(clvs)} · "
                  f"avg {_fmt_signed(sum(clvs) / len(clvs), 2)} pts"
                  if clvs else "Closing line: pending")
    pending = len(bets) - len(graded)
    result_text = f"Results: {wins}-{losses}-{pushes} W-L-P{units_text}{roi_text}" + (f" · {pending} pending" if pending else "")
    if thesis_checked:
        wind_text = f"Wind thesis: {held}/{len(thesis_checked)} materialized"
        if len(thesis) > len(thesis_checked):
            wind_text += f" · {len(thesis) - len(thesis_checked)} pending"
    elif thesis:
        wind_text = f"Wind thesis: {len(thesis)} awaiting actual weather"
    else:
        wind_text = "Wind thesis: no 12+ mph forecasts"
    accuracy_text = (f"Wind MAE: {sum(first_wind_errors) / len(first_wind_errors):.1f} at alert"
                     + (f" → {sum(final_wind_errors) / len(final_wind_errors):.1f} final" if final_wind_errors else "")
                     + " mph") if first_wind_errors else ""
    lines = [title, result_text, close_text, wind_text, *([accuracy_text] if accuracy_text else []), "<b>Games</b>"]

    def game_line(b: dict[str, Any]) -> str:
        result_icon = {"W": "✅", "L": "❌", "P": "➖"}.get(b.get("result"), "⏳")
        away, home = html.escape(str(b.get("away") or "?")), html.escape(str(b.get("home") or "?"))
        market, side = str(b.get("market") or ""), str(b.get("side") or "")
        if market == "total":
            bet = f"{'U' if side == 'under' else 'O'}{_fmt_line(b.get('first_line'))}"
        else:
            team = home if side == "home" else away
            bet = f"{team} {_fmt_line(b.get('first_line'), True)}"
        lead = _strict_number(b.get("hours_before_kickoff"))
        first = (f"Bet: {bet} {_fmt_odds(b.get('first_odds'))} · {_book_label(str(b.get('book') or ''))}"
                 f" · edge {_fmt_signed(b.get('first_edge'))} pts")
        if lead is not None:
            first += f" · {round(lead):g}h early"
        close = (f"Close: {_fmt_line(b.get('closing_line'), market == 'spread')} · "
                 f"CLV {_fmt_signed(b.get('clv_pts'))} pts" if b.get("closing_line") is not None
                 else "Close: unavailable")
        final = f"game total {_fmt_line(b.get('actual_total'))}" if b.get("actual_total") is not None else "result pending"
        fw = _strict_number((b.get("first_weather") or {}).get("wind_mph"))
        lw = _strict_number((b.get("final_forecast") or {}).get("wind_mph"))
        aw = _strict_number((b.get("actual_weather") or {}).get("wind_mph"))
        wx = f"Wind: {_fmt_line(fw)} alert → {_fmt_line(lw)} final fcst → {_fmt_line(aw)} actual mph"
        if b.get("wind_thesis"):
            if b.get("wind_materialized") is None:
                wx += " — actual pending"
            else:
                wx += " — materialized" if b.get("wind_materialized") else " — missed"
        elif aw is not None:
            wx += " — emerged (not thesis)" if b.get("wind_materialized") else " — not wind-driven"
        return f"• {result_icon} <b>{away} @ {home}</b>\n  {first}\n  {close} · {final}\n  {wx}"

    link = f'<a href="{html.escape(board_url + "/#view=backtest", quote=True)}">Full season report</a>'
    rendered = "\n".join(lines)
    for idx, bet in enumerate(bets):
        item = game_line(bet)
        remaining = len(bets) - idx - 1
        suffix = f"\n… +{remaining} more on dashboard" if remaining else ""
        if max_chars is not None and len(rendered) + len(item) + len(suffix) + len(link) + 4 > max_chars:
            rendered += f"\n… +{remaining + 1} {POSTMORTEM_MORE_MARKER}"
            break
        rendered += "\n" + item
    return f"{rendered}\n{link}"


def send_postmortem_email(report_html: str, *, sport: Optional[str], board_url: str,
                          cfg: Optional[EmailConfig] = None, smtp_factory: Optional[Callable[..., Any]] = None) -> bool:
    """Send the complete report through ordinary SMTP without logging credentials."""
    import smtplib
    from email.message import EmailMessage

    cfg = cfg or EmailConfig.from_env()
    if not cfg.configured:
        logger.warning("post-mortem email fallback is not configured")
        return False
    plain = html.unescape(re.sub(r"<[^>]*>", "", report_html)).strip()
    plain += f"\n\nDashboard: {board_url}/#view=backtest"
    msg = EmailMessage()
    league = SPORT_LABEL.get(sport or "", "Football")
    msg["Subject"] = f"{league} weekly betting post-mortem"
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    msg.set_content(plain)
    factory = smtp_factory or (smtplib.SMTP_SSL if cfg.use_ssl else smtplib.SMTP)
    try:
        with factory(cfg.host, cfg.port, timeout=20) as client:
            if not cfg.use_ssl and cfg.starttls:
                client.starttls()
            if cfg.username:
                client.login(cfg.username, cfg.password)
            client.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-mortem email fallback failed: %s", exc)
        return False


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
    p.add_argument("--digest", choices=DIGEST_KINDS, default=None,
                   help="send the weekly postmortem: results, close, and weather")
    p.add_argument("--sport", choices=("nfl", "cfb"), default=None)
    p.add_argument("--state-dir", type=Path, default=Path("data/state"))
    p.add_argument("--backtest", type=Path, default=None, help="board/backtest.json for the postmortem")
    p.add_argument("--email-fallback", action="store_true",
                   help="email the complete post-mortem if Telegram fails or must omit games")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_repo_dotenv()
    args = parse_args(argv)
    # Retirement must not trigger failure/SMTP fallback.
    # Explicit dry-run rendering remains available.
    if not args.dry_run and args.digest and not football_telegram_enabled():
        print("football Telegram retired: digest delivery skipped")
        return 0
    cfg = Config.from_env()
    sender = print_sender() if args.dry_run else default_sender()
    if args.digest == "postmortem":
        backtest = _load_backtest(args.backtest)
        text = postmortem_digest(backtest, sport=args.sport, board_url=cfg.board_url)
        ok = sender(text, cfg.chat_for(args.sport))
        print(f"  postmortem digest: {'sent' if ok else 'FAILED'}")
        incomplete = POSTMORTEM_MORE_MARKER in text
        if args.email_fallback and (not ok or incomplete):
            complete = postmortem_digest(backtest, sport=args.sport, board_url=cfg.board_url, max_chars=None)
            if args.dry_run:
                print("\n--- EMAIL FALLBACK PREVIEW ---\n")
                plain = html.unescape(re.sub(r"<[^>]*>", "", complete))
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(plain.encode(enc, errors="replace").decode(enc))
                print(f"  email fallback: dry-run ({'telegram failed' if not ok else 'telegram was truncated'})")
                return 0
            email_ok = send_postmortem_email(complete, sport=args.sport, board_url=cfg.board_url)
            print(f"  email fallback: {'sent' if email_ok else 'FAILED'}"
                  f" ({'telegram failed' if not ok else 'telegram was truncated'})")
            return 0 if email_ok else 1
        return 0 if ok else 1
    print("nothing to do: pass --digest postmortem")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
