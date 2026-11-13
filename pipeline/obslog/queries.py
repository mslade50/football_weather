"""Reproducible read-only queries; no query imports or mutates legacy state.

    QuoteKey is the main side identity (line is a value). Same-capture pairs
    additionally require matching canonical line numbers. Alternate lines stay
    in the log and are available via series(main_only=False).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import MarketKey, QuoteKey, instant, key_of, utc


@dataclass(frozen=True)
class Observation:
    id: str
    key: QuoteKey
    sport: str
    line: float | None
    odds: int | None
    observed_at: datetime
    recorded_at: datetime
    capture_id: str
    kind: str
    data: dict

    @property
    def value(self):
        return self.line, self.odds


@dataclass(frozen=True)
class Pair:
    key: MarketKey
    quotes: tuple[Observation, ...]

    @property
    def complete(self) -> bool:
        sides = {q.key.side for q in self.quotes}
        expected = {"over", "under"} if self.key.market == "total" else {"home", "away"}
        return sides == expected and len({_number(q) for q in self.quotes}) == 1

    @property
    def same_capture(self) -> bool:
        return len({q.capture_id for q in self.quotes}) == 1


@dataclass(frozen=True)
class LastObserved:
    pair: Pair
    status: str


@dataclass(frozen=True)
class Baseline:
    observation: Observation
    basis: str
    target: datetime | None = None


def _observation(row):
    return Observation(row["id"], key_of(row), row["sport"], row["line"], row["odds"],
                       instant(row["observed_at"]), instant(row["recorded_at"]), row["capture_id"], row["kind"], dict(row))


def _rows(conn, as_of, main_only=True):
    stamp = utc(as_of)
    return [_observation(r) for r in conn.execute(
        "SELECT * FROM quote_observations WHERE observed_at<=? AND recorded_at<=? "
        + ("AND is_main=1 " if main_only else "")
        + "ORDER BY observed_at, recorded_at, id", (stamp, stamp))]


def series(conn, key: QuoteKey, *, as_of: datetime, main_only: bool = True,
           include_withdrawals: bool = True) -> list[Observation]:
    return [q for q in _rows(conn, as_of, main_only) if q.key == key and (include_withdrawals or q.kind == "quote")]


def collapse(points: list[Observation], *, include_withdrawals: bool = False,
             cap: int | None = None) -> list[Observation]:
    """Legacy change-only projection ignores withdrawals; opt in for status history.

    No write compaction: repeated equal values still refresh observation age.
    cap=120 reproduces history.json retention; no cap reproduces D1 deltas.
    """
    out = []
    for point in points:
        if point.kind == "withdrawn" and not include_withdrawals:
            continue
        value = (point.kind, point.value) if include_withdrawals else point.value
        prior = ((out[-1].kind, out[-1].value) if include_withdrawals else out[-1].value) if out else None
        if not out or value != prior:
            out.append(point)
    return out[-cap:] if cap is not None and cap > 0 else out


def latest(conn, *, as_of: datetime, max_age: timedelta | None = None,
           honor_expiry: bool = False, include_withdrawals: bool = False) -> dict[QuoteKey, Observation]:
    """Latest MAIN observation, then status/age filtering (never resurrect an old price).

    honor_expiry is the carry-forward product rule. Equality at max age/expiry
    is allowed, matching carry_forward_lines. A withdrawal ends availability.
    """
    found = {q.key: q for q in _rows(conn, as_of)}
    return {key: q for key, q in found.items()
            if (include_withdrawals or q.kind == "quote")
            and (max_age is None or as_of - q.observed_at <= max_age)
            and (not honor_expiry or not q.data["expires_at"] or instant(q.data["expires_at"]) >= as_of)}


def pairs(quotes, *, complete_only: bool = False) -> dict[MarketKey, Pair]:
    grouped = {}
    for q in quotes:
        grouped.setdefault(q.key.pair, []).append(q)
    result = {key: Pair(key, tuple(sorted(rows, key=lambda q: q.key.side))) for key, rows in grouped.items()}
    return {k: p for k, p in result.items() if not complete_only or p.complete}


def _number(q):
    return round(-q.line if q.key.market == "spread" and q.key.side == "away" else q.line, 2) if q.line is not None else None


def first_observations(conn, *, as_of: datetime) -> dict[MarketKey, Pair]:
    """Literal first observation per side, grouped with pair completeness exposed.

    Unlike the legacy anchored policy this does not reject mismatched numbers.
    Use first_seen(first_complete) to require actual simultaneous book pairs.
    """
    first = {}
    for point in _rows(conn, as_of):
        if point.kind == "quote":
            first.setdefault(point.key, point)
    return pairs(first.values())


def first_seen(conn, *, as_of: datetime, policy: str = "first_complete") -> dict[MarketKey, Pair]:
    """first_complete: first simultaneous compatible pair, never invented mates.

    anchored_first_seen: legacy product policy; a lone first side anchors its
    number, later missing sides must match it. A mismatched initial pair is
    ignored. Such a pair is marked same_capture=False when assembled over time.
    Single-sided markets remain explicit incomplete Pair objects.
    """
    if policy not in ("first_complete", "anchored_first_seen"):
        raise ValueError(policy)
    batches = {}
    for q in sorted(_rows(conn, as_of), key=lambda q: (q.recorded_at, q.observed_at, q.id)):
        if q.kind == "quote" and (q.odds is not None or q.key.book == "consensus"):
            batches.setdefault((q.recorded_at, q.capture_id), []).append(q)
    selected = {}
    for batch in batches.values():
        for key, pair in pairs(batch).items():
            if policy == "first_complete":
                if key not in selected and (pair.complete or key.market == "ml" or key.book == "consensus"):
                    selected[key] = {q.key.side: q for q in pair.quotes}
                continue
            known = selected.setdefault(key, {})
            incoming = {q.key.side: q for q in pair.quotes if q.key.side not in known}
            if not known:
                if len(incoming) == 1 or pair.complete or key.market == "ml" or key.book == "consensus":
                    known.update(incoming)
            else:
                for side, q in incoming.items():
                    if key.market == "ml" or _number(q) == _number(next(iter(known.values()))):
                        known[side] = q
    return {key: Pair(key, tuple(rows.values())) for key, rows in selected.items() if rows}


def first_attested(conn, *, as_of: datetime) -> dict[MarketKey, Pair]:
    """First RECEIVED valid provider attestation wins; conflicting evidence stays logged."""
    selected = {}
    for q in sorted(_rows(conn, as_of), key=lambda q: (q.recorded_at, q.observed_at, q.id)):
        d = q.data
        if (q.kind == "quote" and d["opened_at"] and d["opening_line"] is not None
                and d["opening_odds"] and (d["opening_source"] or "").strip()
                and instant(d["opened_at"]) <= q.observed_at):
            selected.setdefault(q.key, replace(q, line=d["opening_line"], odds=d["opening_odds"], observed_at=instant(d["opened_at"])))
    return pairs(selected.values())


def in_force(conn, *, at: datetime, as_of: datetime, late_fallback: bool = False,
             change_points: bool = False, historical_quotes: bool = False) -> dict[MarketKey, Pair]:
    """Value in force at at, known by as_of. Withdrawals prevent resurrection.

    late_fallback explicitly permits first collection AFTER at (legacy T-6 rule).
    change_points returns the start of an unchanged price spell, matching the
    legacy reference timestamp, rather than its latest refresh receipt.
    historical_quotes ignores withdrawal events for historical baselines only.
    """
    utc(at)
    by_key = {}
    for q in _rows(conn, as_of):
        if historical_quotes and q.kind == "withdrawn":
            continue
        by_key.setdefault(q.key, []).append(q)
    chosen = []
    for rows in by_key.values():
        if change_points:
            rows = collapse(rows, include_withdrawals=True)
        before = [q for q in rows if q.observed_at <= at]
        q = before[-1] if before else rows[0] if late_fallback else None
        if q and q.kind == "quote":
            chosen.append(q)
    return pairs(chosen)


def baseline(conn, *, as_of: datetime, cfb_t6: bool = True,
             opener_policy: str = "anchored_first_seen") -> dict[QuoteKey, Baseline]:
    """Named legacy resolution: CFB T-6, provider-attested, first collection.

    Per-book facts only. Weighted synthetic consensus is a model computation,
    not a book observation. No provider timestamp is inferred as an attestation.
    """
    out = {q.key: Baseline(q, "first_seen") for p in first_seen(conn, as_of=as_of, policy=opener_policy).values() for q in p.quotes}
    out.update({q.key: Baseline(q, "true_opener") for p in first_attested(conn, as_of=as_of).values() for q in p.quotes})
    if cfb_t6:
        for gid, game in game_facts(conn, as_of=as_of).items():
            if game["sport"] != "cfb":
                continue
            target = instant(game["kickoff"]) - timedelta(days=6)
            for pair in in_force(conn, at=target, as_of=as_of, late_fallback=True, change_points=True, historical_quotes=True).values():
                if pair.key.game_id == gid and pair.key.market == "total" and pair.key.book != "consensus":
                    for q in pair.quotes:
                        out[q.key] = Baseline(q, "t_minus_6d", target)
    return out


def pair_at(conn, key: MarketKey, *, at: datetime, as_of: datetime) -> Pair | None:
    """One book snapshot: latest capture at at known by as_of, never cross-run mates.

    An incomplete/mismatched snapshot is returned with complete=False. Alternate
    numbers are accessible through series(main_only=False), not mixed into main.
    """
    utc(at)
    points = [q for q in _rows(conn, as_of) if q.key.pair == key and q.observed_at <= at]
    if not points:
        return None
    last = max(points, key=lambda q: (q.observed_at, q.recorded_at, q.capture_id))
    quotes = [q for q in points if q.capture_id == last.capture_id and q.kind == "quote"]
    return Pair(key, tuple(quotes))


def closing(conn, *, as_of: datetime, change_points: bool = False) -> dict[QuoteKey, Observation]:
    """Last actual quote strictly BEFORE kickoff, after the game has begun.

    Historical closings ignore withdrawals/age, matching CLV. change_points
    reproduces the timestamp of freeze_from_series/D1 change-only histories.
    """
    games = game_facts(conn, as_of=as_of)
    by_key = {}
    for q in _rows(conn, as_of):
        if q.kind == "quote":
            by_key.setdefault(q.key, []).append(q)
    out = {}
    for key, rows in by_key.items():
        if key.game_id not in games:
            continue
        kick = instant(games[key.game_id]["kickoff"])
        if kick > as_of:
            continue
        for q in collapse(rows) if change_points else rows:
            if q.observed_at < kick:
                out[key] = q
    return out


def last_observed(conn, *, as_of: datetime, max_age: timedelta = timedelta(hours=1)) -> dict[MarketKey, LastObserved]:
    """Latest main value regardless of age; withdrawn retains its original quote clock.

    Status vocabulary is current/stale/withdrawn; expired counts as stale.
    The under side represents total displays, matching last_observed_totals.
    """
    result = {}
    for key, pair in pairs(latest(conn, as_of=as_of, include_withdrawals=True).values()).items():
        preferred = next((q for q in pair.quotes if q.key.side == ("under" if key.market == "total" else "home")), pair.quotes[0])
        d = preferred.data
        age_at = instant(d["quote_at"])
        stale = as_of - age_at > max_age or (d["expires_at"] and instant(d["expires_at"]) < as_of)
        status = "withdrawn" if preferred.kind == "withdrawn" else "stale" if stale else "current"
        result[key] = LastObserved(pair, status)
    return result


def game_facts(conn, *, as_of: datetime) -> dict[str, dict]:
    return {r["game_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM game_observations WHERE observed_at<=? ORDER BY observed_at, id", (utc(as_of),))}


def forecasts(conn, *, as_of: datetime, latest_only: bool = True) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM forecast_observations WHERE fetched_at<=? ORDER BY fetched_at, id", (utc(as_of),))]
    return list({(r["game_id"], r["source"], r["model"]): r for r in rows}.values()) if latest_only else rows


def weekly_total(conn, game_id: str, *, as_of: datetime) -> dict | None:
    """First main BetOnline total in the Eastern football opening window.

    Start inclusive/end exclusive, under wins a simultaneous number mismatch.
    Unknown window observations remain unknown; no outside-window fallback.
    """
    game = game_facts(conn, as_of=as_of).get(game_id)
    if game is None:
        return None
    kick = instant(game["kickoff"]).astimezone(ZoneInfo("America/New_York"))
    tuesday = kick - timedelta(days=(kick.weekday() - 1) % 7)
    sunday = (tuesday - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = sunday + timedelta(hours=12 if game["sport"] == "cfb" else 18)
    end = sunday + timedelta(days=1, hours=0 if game["sport"] == "cfb" else 12)
    points = [q for q in _rows(conn, as_of) if q.key.pair == MarketKey(game_id, "betonline", "total")
              and q.kind == "quote" and start <= q.observed_at < end and q.odds is not None]
    if not points:
        return None
    first = min(points, key=lambda q: (q.observed_at, q.key.side != "under", q.id))
    value = dict(line=first.line, under=None, over=None, observed_at=first.observed_at, book="betonline")
    for q in points:
        if q.observed_at == first.observed_at and q.line == first.line:
            value[q.key.side] = q.odds
    return value
