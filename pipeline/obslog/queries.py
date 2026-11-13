"""Reproducible read-only queries; no query imports or mutates legacy state.

    QuoteKey is the main side identity (line is a value). Same-capture pairs
    additionally require matching canonical line numbers. Alternate lines stay
    in the log and are available via series(main_only=False).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
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
        return sides == expected and len({_canonical_line(q) for q in self.quotes}) == 1

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


def _rows(conn, as_of, main_only=True, game_id=None, key=None):
    stamp = utc(as_of)
    return [_observation(r) for r in conn.execute(
        "SELECT * FROM quote_observations WHERE observed_at<=? AND recorded_at<=? "
        + ("AND is_main=1 " if main_only else "")
        + ("AND game_id=? " if game_id else "")
        + ("AND book=? AND market=? AND side=? " if key else "")
        + "ORDER BY observed_at, recorded_at, id",
        (stamp, stamp, *((game_id,) if game_id else ()), *((key.book, key.market, key.side) if key else ())))]


@dataclass(frozen=True)
class Cover:
    id: str
    capture_id: str
    run_id: str
    observed_at: datetime
    recorded_at: datetime


class Coverage:
    """Successful full-coverage scrapes, per (book, sport, market).

    Storage is change-only, so an unchanged quote has no row for later runs.
    A quote is still live at a coverage event when it is the latest logged row
    for its key (not withdrawn) and the event was recorded after that row and
    before the key's next logged row. Failed/partial scrapes write no coverage.
    """

    def __init__(self, conn, as_of: datetime):
        stamp = utc(as_of)
        self.by_scope: dict[tuple, list[Cover]] = {}
        self.clock: dict[tuple, datetime] = {}
        for r in conn.execute("SELECT * FROM coverage WHERE observed_at<=? AND recorded_at<=? ORDER BY recorded_at, id", (stamp, stamp)):
            c = Cover(r["id"], r["capture_id"], r["run_id"], instant(r["observed_at"]), instant(r["recorded_at"]))
            self.by_scope.setdefault((r["book"], r["sport"], r["market"]), []).append(c)
            self.clock[(c.capture_id, r["book"], r["sport"], r["market"])] = c.observed_at
        self._recorded = {k: [c.recorded_at for c in v] for k, v in self.by_scope.items()}

    def between(self, scope, after: datetime, before: datetime | None) -> list[Cover]:
        covers = self.by_scope.get(scope)
        if not covers:
            return []
        recorded = self._recorded[scope]
        lo = bisect_right(recorded, after)
        hi = bisect_left(recorded, before) if before is not None else len(covers)
        return covers[lo:hi]

    @staticmethod
    def lifted(obs: Observation, c: Cover, clock: datetime | None = None) -> Observation:
        at = clock or c.observed_at
        data = {**obs.data, "observed_at": utc(at), "quote_at": utc(at), "recorded_at": utc(c.recorded_at),
                "capture_id": c.capture_id, "run_id": c.run_id}
        shift = at - obs.observed_at  # expiry offsets are part of the stored value (see store._changed)
        for field in ("expires_at", "actionable_until"):
            if data.get(field):
                data[field] = utc(instant(data[field]) + shift)
        return replace(obs, observed_at=at, recorded_at=c.recorded_at, capture_id=c.capture_id, data=data)

    def lift(self, obs: Observation, next_recorded: datetime | None, *, limit: datetime | None = None,
             strict: bool = False) -> Observation:
        """The newest confirmation of an unchanged live quote, as the repeat row it replaces."""
        if obs.kind != "quote":
            return obs
        best = None
        for c in self.between((obs.key.book, obs.sport, obs.key.market), obs.recorded_at, next_recorded):
            if limit is not None and (c.observed_at >= limit if strict else c.observed_at > limit):
                continue
            if best is None or (c.observed_at, c.recorded_at, c.id) > (best.observed_at, best.recorded_at, best.id):
                best = c
        if best is not None and (best.observed_at, best.recorded_at) > (obs.observed_at, obs.recorded_at):
            return self.lifted(obs, best)
        return obs

    def virtuals(self, obs: Observation, next_recorded: datetime | None) -> list[Observation]:
        if obs.kind != "quote":
            return []
        return [self.lifted(obs, c) for c in self.between((obs.key.book, obs.sport, obs.key.market), obs.recorded_at, next_recorded)
                if c.observed_at > obs.observed_at]


def _next_recorded(rows):
    """Per key: sorted recorded clocks of every logged row (any kind)."""
    out = {}
    for q in rows:
        out.setdefault(q.key, []).append(q.recorded_at)
    for v in out.values():
        v.sort()
    return out


def _following(recs, obs):
    i = bisect_right(recs, obs.recorded_at)
    return recs[i] if i < len(recs) else None


def series(conn, key: QuoteKey, *, as_of: datetime, main_only: bool = True,
           include_withdrawals: bool = True, expand: bool = False) -> list[Observation]:
    """Logged rows (changes, withdrawals, and every row of an unsuccessful scrape).

    expand=True also yields one derived repeat per later successful-coverage
    confirmation of an unchanged main quote, i.e. the pre-v3 one-row-per-run view.
    """
    rows = _rows(conn, as_of, main_only, key.game_id, key)
    if expand:
        cov, recs = Coverage(conn, as_of), _next_recorded(rows)
        extra = [v for q in rows if q.key == key and q.data["is_main"] for v in cov.virtuals(q, _following(recs[q.key], q))]
        rows = sorted([*rows, *extra], key=lambda q: (q.observed_at, q.recorded_at, q.id))
    return [q for q in rows if include_withdrawals or q.kind == "quote"]


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
    rows = _rows(conn, as_of)
    found = {q.key: q for q in rows}
    recs, cov = _next_recorded(rows), Coverage(conn, as_of)
    # Freshness is when the quote was last confirmed live, not when it last changed.
    found = {key: cov.lift(q, None) if q.recorded_at >= recs[key][-1] else q for key, q in found.items()}
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


def _canonical_line(q):
    """Canonical home spread/total with two-decimal rounding, not scalar coercion."""
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


def _sides(market):
    return ("over", "under") if market == "total" else ("home", "away")


def first_seen(conn, *, as_of: datetime, policy: str = "first_complete") -> dict[MarketKey, Pair]:
    """first_complete: first simultaneous compatible pair, never invented mates.

    anchored_first_seen: legacy product policy, identical to
    state.record_openers/_record_pair_openers. Per capture (one run) each side
    contributes its FIRST main line, because build.py passes the store main
    lines only. Neither side known: a lone side anchors its number, a
    mismatched pair records nothing. One side known: the other joins only at
    that number. Such a pair is marked same_capture=False when assembled over
    time.

    Storage is change-only: within a successful-coverage capture, live unchanged
    main sides are carried into the batch with the capture's confirmation clock,
    which reproduces the pre-v3 repeat rows.
    """
    if policy not in ("first_complete", "anchored_first_seen"):
        raise ValueError(policy)
    anchored = policy == "anchored_first_seen"
    stamp = utc(as_of)
    cov = Coverage(conn, as_of)
    rows = []
    for r in conn.execute("SELECT rowid AS _rid, * FROM quote_observations WHERE observed_at<=? AND recorded_at<=? "
                          + "AND is_main=1 ORDER BY recorded_at, rowid", (stamp, stamp)):
        data = dict(r)
        data.pop("_rid")
        data.pop("rowid", None)
        rows.append(_observation(data))
    grouped = {}
    for q in rows:
        grouped.setdefault((q.recorded_at, q.capture_id), []).append(q)
    head, batches = {}, {}
    for (recorded, capture), batch in grouped.items():
        quotes = [q for q in batch if q.kind == "quote" and (q.odds is not None or q.key.book == "consensus")]
        present = {q.key for q in batch if q.data["is_main"]}
        for pair, sport in {(q.key.pair, q.sport) for q in batch}:
            clock = cov.clock.get((capture, pair.book, sport, pair.market))
            if clock is None:
                continue
            for side in _sides(pair.market):
                k = QuoteKey(pair.game_id, pair.book, pair.market, side)
                h = head.get(k)
                if h and k not in present and h.kind == "quote" and (h.odds is not None or k.book == "consensus"):
                    quotes.append(Coverage.lifted(h, Cover("", capture, h.data["run_id"], max(h.observed_at, clock), recorded)))
        for q in batch:
            if q.data["is_main"]:
                head[q.key] = q
        batches[(recorded, capture)] = quotes
    selected = {}
    for batch in batches.values():
        if not anchored:
            for key, pair in pairs(batch).items():
                if key not in selected and (pair.complete or key.market == "ml" or key.book == "consensus"):
                    selected[key] = {q.key.side: q for q in pair.quotes}
            continue
        firsts = {}
        for q in batch:
            firsts.setdefault(q.key.pair, {}).setdefault(q.key.side, q)
        for key, got in firsts.items():
            known = selected.setdefault(key, {})
            incoming = {s: q for s, q in got.items() if s not in known}
            if not known:
                complete = (set(got) == set(_sides(key.market))
                            and len({_canonical_line(q) for q in got.values()}) == 1)
                if len(incoming) == 1 or complete or key.market == "ml" or key.book == "consensus":
                    known.update(incoming)
            else:
                for side, q in incoming.items():
                    if key.market == "ml" or _canonical_line(q) == _canonical_line(next(iter(known.values()))):
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
             change_points: bool = False, historical_quotes: bool = False, game_id: str | None = None) -> dict[MarketKey, Pair]:
    """Value in force at at, known by as_of. Withdrawals prevent resurrection.

    late_fallback explicitly permits first collection AFTER at (legacy T-6 rule).
    change_points returns the start of an unchanged price spell, matching the
    legacy reference timestamp, rather than its latest refresh receipt.
    historical_quotes ignores withdrawal events for historical baselines only.
    """
    utc(at)
    by_key, everything = {}, _rows(conn, as_of, game_id=game_id)
    recs, cov = _next_recorded(everything), Coverage(conn, as_of)
    for q in everything:
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
            if before and not change_points:
                q = cov.lift(q, _following(recs[q.key], q), limit=at)
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
            for pair in in_force(conn, at=target, as_of=as_of, late_fallback=True, change_points=True,
                                 historical_quotes=True, game_id=gid).values():
                if pair.key.game_id == gid and pair.key.market == "total" and pair.key.book != "consensus":
                    for q in pair.quotes:
                        out[q.key] = Baseline(q, "t_minus_6d", target)
    return out


def pair_at(conn, key: MarketKey, *, at: datetime, as_of: datetime) -> Pair | None:
    """One book snapshot: latest capture at at known by as_of, never cross-run mates.

    An incomplete/mismatched snapshot is returned with complete=False. Alternate
    numbers are accessible through series(main_only=False), not mixed into main.
    In a successful-coverage capture, live unchanged sides belong to the snapshot
    (they were re-observed in that run); in any other capture only logged rows do.
    """
    utc(at)
    rows = [q for q in _rows(conn, as_of, game_id=key.game_id) if q.key.pair == key]
    if not rows:
        return None
    sport, cov = rows[0].sport, Coverage(conn, as_of)
    scope = (key.book, sport, key.market)
    events = {(q.observed_at, q.recorded_at, q.capture_id) for q in rows if q.observed_at <= at}
    events |= {(c.observed_at, c.recorded_at, c.capture_id) for c in cov.by_scope.get(scope, []) if c.observed_at <= at}

    def heads(recorded):
        by_side = {}
        for q in sorted(rows, key=lambda q: (q.recorded_at, q.id)):
            if q.recorded_at <= recorded:
                by_side[q.key.side] = q
        return by_side

    for _observed, recorded, capture in sorted(events, reverse=True):
        in_capture = [q for q in rows if q.capture_id == capture and q.observed_at <= at]
        covered = cov.clock.get((capture, key.book, sport, key.market))
        if covered is None:
            if in_capture:
                return Pair(key, tuple(q for q in in_capture if q.kind == "quote"))
            continue
        live = {s: q for s, q in heads(recorded).items() if q.kind == "quote"}
        if not in_capture and not live:
            continue
        quotes = [q if q.capture_id == capture else Coverage.lifted(q, Cover("", capture, q.data["run_id"], max(q.observed_at, covered), recorded))
                  for q in live.values()]
        return Pair(key, tuple(quotes))
    return None


def closing(conn, *, as_of: datetime, change_points: bool = False) -> dict[QuoteKey, Observation]:
    """Last actual quote strictly BEFORE kickoff, after the game has begun.

    Historical closings ignore withdrawals/age, matching CLV. change_points
    reproduces the timestamp of freeze_from_series/D1 change-only histories.
    """
    games = game_facts(conn, as_of=as_of)
    by_key, everything = {}, _rows(conn, as_of)
    recs, cov = _next_recorded(everything), Coverage(conn, as_of)
    for q in everything:
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
        if key in out and not change_points:
            out[key] = cov.lift(out[key], _following(recs[key], out[key]), limit=kick, strict=True)
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
    mk = MarketKey(game_id, "betonline", "total")
    rows = [q for q in _rows(conn, as_of, game_id=game_id) if q.key.pair == mk]
    recs, cov = _next_recorded(rows), Coverage(conn, as_of)
    # Unchanged in-window confirmations count as the repeat receipts they replace.
    rows += [v for q in rows for v in cov.virtuals(q, _following(recs[q.key], q))]
    points = [q for q in rows if q.kind == "quote" and start <= q.observed_at < end and q.odds is not None]
    if not points:
        return None
    first = min(points, key=lambda q: (q.observed_at, q.key.side != "under", q.id))
    value = dict(line=first.line, under=None, over=None, observed_at=first.observed_at, book="betonline")
    for q in points:
        if q.observed_at == first.observed_at and q.line == first.line:
            value[q.key.side] = q.odds
    return value
