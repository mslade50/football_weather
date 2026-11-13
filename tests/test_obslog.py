"""Local-only observation log and replay parity, including documented differences.

All clocks are historical/fixed; no wall-clock acceptance or external services.
300 generated scenarios * 10 receipts exercise the legacy functions directly.
"""

import json
import random
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline import build
from pipeline import state as legacy
from pipeline.contracts import Game, GameLine, WeatherForecast, WeatherPoint
from pipeline.model import clv
from pipeline.obslog import (
    MarketKey,
    QuoteKey,
    append_forecasts,
    append_games,
    append_odds,
    connect,
    record_withdrawals,
    shadow,
)
from pipeline.obslog import queries as q
from pipeline.odds.weekly_openers import record_weekly_totals
from pipeline.outputs import d1_out
from pipeline.outputs.raw_out import NullRawStore
from pipeline.run_context import RunContext
from tests.test_selective_weather import run as weather_run
from tests.test_selective_weather import weather  # noqa: F401 - reusable offline fixture

T = datetime(2026, 9, 27, 23, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def text(t):
    return t.isoformat().replace("+00:00", "Z")


def game(sport="nfl", kickoff=None):
    kick = kickoff or T + timedelta(days=6, hours=18)
    return Game(f"{sport}:2026:4:away@home", sport, 2026, 4, kick, kick,
                "America/New_York", "home", "away", "stadium")


def line(g, book="betonline", market="total", side="under", value=45, now=T, **kw):
    return GameLine(g.sport, g.game_id, book, market, side, kw.pop("odds", -110),
                    None if market == "ml" else value, scraped_at=now, **kw)


def key(ln):
    return QuoteKey(ln.game_id, ln.book, ln.market, ln.side)


def legacy_key(k):
    return legacy.odds_key(k.game_id, k.market, k.side, k.book)


def points(rows):
    return [[text(x.observed_at), x.line, x.odds] for x in rows]


def advance_archive(archive, lines, books, sport, now):
    """The small archive advancement loop in stage_odds, without executing a stage."""
    last = archive.setdefault("last", {})
    for k, value in last.items():
        if k.startswith(f"{sport}:") and k.rsplit("|", 1)[-1] in books:
            value["available"] = False
    for ln in lines:
        last[ln.key] = dict(line=ln.line, odds=ln.odds, ts=text(ln.scraped_at or now), available=True)
        if ln.expires_at:
            last[ln.key]["expires_at"] = text(ln.expires_at)


def assert_baselines(conn, op, now, all_keys):
    actual = q.baseline(conn, as_of=now)
    for k in all_keys:
        expected = legacy.get_baseline(op, legacy_key(k))
        found = actual.get(k)
        assert (found is None) == (expected is None), (k, expected, found)
        if found:
            assert found.observation.value == (expected["line"], expected["odds"]), (k, expected, found)
            assert found.observation.observed_at == legacy.parse_utc(expected["ts"]), (k, expected, found)
            assert found.basis == expected.get("basis", "first_seen")


@pytest.mark.parametrize("seed", range(300))
def test_generated_replay_parity(seed):
    rng = random.Random(seed)
    # Half begin after T-6; the others cross it. Both sports, all three markets.
    g = game("cfb" if seed % 2 else "nfl", T + timedelta(days=6, minutes=30 if seed % 4 < 2 else -30))
    op, hist, archive = (legacy.migrate(None, kind) for kind in ("openers", "history", "archive_last"))
    all_keys, emitted = set(), {}
    with closing(connect()) as conn:
        for run in range(10):
            now = T + timedelta(minutes=run * 17)
            books = [b for b in ("betonline", "pinnacle", "fanduel") if rng.random() < .7]
            rows = []
            for book in books:
                # Empty, one-sided, mismatched pairs, moves, and stable repeats.
                for market, sides in (("total", ("over", "under")), ("spread", ("home", "away")), ("ml", ("home", "away"))):
                    value = rng.choice([45, 45, 46]) if market == "total" else -3
                    for side in sides:
                        if rng.random() < .25:
                            continue
                        number = -value if market == "spread" and side == "away" else value
                        if rng.random() < .1 and market != "ml":
                            number += .5
                        extra = {}
                        if market != "ml" and rng.random() < .1:
                            extra = dict(opening_line=number + 1, opening_odds=-108,
                                         opened_at=T - timedelta(days=2), opening_source="provider")
                        rows.append(line(g, book, market, side, number, now, odds=rng.choice([-110, -110, -105]), **extra))
            all_keys.update(key(ln) for ln in rows)
            for ln in d1_out.odds_deltas(rows, archive["last"]):
                emitted.setdefault(key(ln), []).append([text(now), ln.line, ln.odds])
            # Exactly the build order: record, retarget against prior history +
            # current lines, then append history later in update_histories.
            legacy.record_openers(op, rows, text(now))
            if g.sport == "cfb":
                build.retarget_cfb_total_openers(op, hist, rows, [g], now)
            append_odds(conn, rows, games=[g], run_id=str(run), observed_at=now,
                        attempted={(b, g.sport) for b in books}, successful={(b, g.sport) for b in books})
            assert_baselines(conn, op, now, all_keys)
            legacy.update_history(hist, rows, text(now))
            advance_archive(archive, rows, books, g.sport, now)
            # Use a future partial run so every currently present book is eligible.
            check_at = now + timedelta(minutes=5)
            carry = build.carry_forward_lines(archive, g.sport, {g.game_id}, [], now=check_at)
            latest = q.latest(conn, as_of=check_at, max_age=HOUR, honor_expiry=True)
            assert {key(x): (x.line, x.odds) for x in carry} == {k: v.value for k, v in latest.items()}
            observed = build.last_observed_totals(archive, g.sport, {g.game_id}, check_at).get(g.game_id, {})
            logged = q.last_observed(conn, as_of=check_at)
            for book, val in observed.items():
                result = logged[MarketKey(g.game_id, book, "total")]
                assert result.status == {"unavailable": "withdrawn"}.get(val["status"], val["status"])
                under = next(x for x in result.pair.quotes if x.key.side == "under")
                assert under.value == (val["line"], val["under"])
            for k in all_keys:
                collapsed = q.collapse(q.series(conn, k, as_of=now))
                assert points(collapsed) == hist["series"][legacy_key(k)]
                assert points(collapsed) == emitted[k]
        # Two independent legacy closing entry points, plus natural quote value.
        end = g.kickoff_utc + HOUR
        frozen = clv.freeze_from_series(hist, {g.game_id: g.kickoff_utc}, end)
        raw = [dict(game_id=k.game_id, book=k.book, market=k.market, side=k.side,
                    scraped_at=p[0], line=p[1], odds=p[2]) for k, seq in emitted.items() for p in seq]
        assert frozen == clv.freeze_from_rows(raw, {g.game_id: g.kickoff_utc}, end)
        closings = q.closing(conn, as_of=end, change_points=True)
        natural = q.closing(conn, as_of=end)
        for k, result in closings.items():
            expected = frozen[legacy_key(k)]
            assert result.value == (expected.line, expected.odds) == natural[k].value
            assert result.observed_at == legacy.parse_utc(expected.scraped_at)


def test_transaction_idempotency_clocks_alternates_and_schema(tmp_path):
    g = game()
    path = tmp_path / "shadow.sqlite"
    with closing(connect(path)) as conn:
        ln = line(g, prob_raw=.52, source_id="source", source_updated_at=T - HOUR,
                  expires_at=T + HOUR, outcome_ids={"under": "123"})
        append_odds(conn, [ln], games=[g], run_id="1", observed_at=T)
        append_odds(conn, [ln], games=[g], run_id="1", observed_at=T)
        assert conn.execute("SELECT count(*) FROM quote_observations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM game_observations").fetchone()[0] == 1
        append_odds(conn, [replace(ln, scraped_at=T + HOUR)], run_id="2", observed_at=T + HOUR)
        assert len(q.series(conn, key(ln), as_of=T + HOUR)) == 2
        # A malformed second line rolls back games, run, and first line too.
        bad = replace(ln, scraped_at=T.replace(tzinfo=None))
        with pytest.raises(ValueError, match="aware"):
            append_odds(conn, [replace(ln, odds=-115), bad], games=[g], run_id="bad", observed_at=T)
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert len(q.series(conn, key(ln), as_of=T)) == 1
        append_odds(conn, [replace(ln, is_main=False, line=50)], run_id="alt", observed_at=T)
        assert q.latest(conn, as_of=T)[key(ln)].line == 45
        assert len(q.series(conn, key(ln), as_of=T, main_only=False)) == 2
        # Knowledge clock: a later receipt with an old quote is invisible earlier.
        append_odds(conn, [replace(ln, odds=-120)], run_id="late", observed_at=T + 2 * HOUR)
        assert q.latest(conn, as_of=T)[key(ln)].odds == -110
        row = q.series(conn, key(ln), as_of=T)[0]
        assert json.loads(row.data["payload"])["outcome_ids"] == {"under": "123"}
        assert row.data["source_updated_at"].startswith("2026-09-27T22:")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE schema_version SET version=99")
        conn.commit()
    with pytest.raises(ValueError, match="schema"):
        connect(path)


def test_pairs_anchor_policy_and_provider_attestation():
    g = game()
    with closing(connect()) as conn:
        op = legacy.migrate(None, "openers")
        runs = [
            [line(g, market="spread", side="home", value=-3), line(g, market="spread", side="away", value=3.5)],
            [line(g, market="spread", side="home", value=-3)],
            [line(g, market="spread", side="away", value=4)],
            [line(g, market="spread", side="away", value=3)],
        ]
        keys = set()
        for i, rows in enumerate(runs):
            now = T + i * HOUR
            rows = [replace(x, scraped_at=now) for x in rows]
            keys.update(key(x) for x in rows)
            legacy.record_openers(op, rows, text(now))
            append_odds(conn, rows, run_id=str(i), games=[g], observed_at=now)
            assert_baselines(conn, op, now, keys)
        mk = MarketKey(g.game_id, "betonline", "spread")
        pair = q.first_seen(conn, as_of=now, policy="anchored_first_seen")[mk]
        assert pair.complete and not pair.same_capture
        assert mk not in q.first_seen(conn, as_of=now)  # no simultaneous valid pair yet
        snapshot = q.pair_at(conn, mk, at=T, as_of=now)
        assert not snapshot.complete and snapshot.same_capture
        for i, value in enumerate([42, 43]):
            now += HOUR
            rows = [line(g, value=46, now=now, opening_line=value, opening_odds=-105,
                         opening_source="provider", opened_at=T - HOUR)]
            legacy.record_openers(op, rows, text(now))
            append_odds(conn, rows, run_id=f"attest{i}", games=[g], observed_at=now)
            keys.add(key(rows[0]))
            assert_baselines(conn, op, now, keys)
        assert len(op["true_opener_conflicts"][rows[0].key]) == 1
        assert len(q.series(conn, key(rows[0]), as_of=now)) == 2


def test_age_expiry_withdrawal_return_and_sport_scoping():
    g, college = game(), game("cfb")
    a, b, c = line(g), line(g, "pinnacle", expires_at=T + timedelta(minutes=50)), line(college)
    with closing(connect()) as conn:
        append_odds(conn, [a, b, c], run_id="1", observed_at=T)
        assert len(q.latest(conn, as_of=T + HOUR, max_age=HOUR)) == 3
        assert not q.latest(conn, as_of=T + HOUR + timedelta(microseconds=1), max_age=HOUR)
        assert key(b) not in q.latest(conn, as_of=T + HOUR, honor_expiry=True)
        assert key(b) in q.latest(conn, as_of=T + timedelta(minutes=50), honor_expiry=True)
        record_withdrawals(conn, [], {("betonline", "nfl")}, run_id="empty", observed_at=T + HOUR)
        assert key(a) not in q.latest(conn, as_of=T + HOUR)
        assert key(c) in q.latest(conn, as_of=T + HOUR)
        assert q.last_observed(conn, as_of=T + HOUR)[key(a).pair].status == "withdrawn"
        assert key(a) in q.latest(conn, as_of=T + timedelta(minutes=59))
        append_odds(conn, [replace(a, scraped_at=T + 2 * HOUR)], run_id="return", observed_at=T + 2 * HOUR)
        assert key(a) in q.latest(conn, as_of=T + 2 * HOUR)


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_weekly_windows_replay_and_forecast_roundtrip(sport):
    g = game(sport, datetime(2026, 10, 4, 17, tzinfo=timezone.utc))
    op, hist = legacy.migrate(None, "openers"), legacy.migrate(None, "history")
    start = T - timedelta(hours=7 if sport == "cfb" else 1)
    end = start + timedelta(hours=12 if sport == "cfb" else 18)
    with closing(connect()) as conn:
        for i, now in enumerate([start - timedelta(seconds=1), start, start + HOUR, end, end + HOUR]):
            rows = [line(g, side=side, value=45 - i, now=now) for side in ("under", "over")]
            legacy.record_openers(op, rows, text(now))
            record_weekly_totals(op, hist, rows, [g], now)
            legacy.update_history(hist, rows, text(now))
            append_odds(conn, rows, games=[g], run_id=str(i), observed_at=now)
            actual = q.weekly_total(conn, g.game_id, as_of=now)
            expected = op["weekly_totals"][g.game_id]
            assert (actual["line"] if actual else None) == expected["line"]
            if actual:
                assert (actual["under"], actual["over"], actual["observed_at"]) == (
                    expected["under"], expected["over"], legacy.parse_utc(expected["ts"]))
        fc = WeatherForecast(g.game_id, "merged", run_time=T - HOUR, lead_hours=40,
                             temp_fg=65, wind_fg=20, wind_p90=30,
                             model_windows={"ifs": {"wind": 22}}, hourly=[WeatherPoint(T, wind=20)])
        append_forecasts(conn, [fc], run_id="wx", fetched_at=T, games=[g])
        append_forecasts(conn, [fc], run_id="wx", fetched_at=T, games=[g])
        assert len(q.forecasts(conn, as_of=T)) == 1
        payload = json.loads(q.forecasts(conn, as_of=T)[0]["payload"])
        assert payload["wind_fg"] == 20 and payload["model_windows"] == {"ifs": {"wind": 22}}
        assert q.forecasts(conn, as_of=T - timedelta(seconds=1)) == []
        changed = replace(g, kickoff_utc=g.kickoff_utc + HOUR)
        append_games(conn, [changed], run_id="schedule", observed_at=T + HOUR)
        assert q.game_facts(conn, as_of=T)[g.game_id]["kickoff"] != q.game_facts(conn, as_of=T + HOUR)[g.game_id]["kickoff"]


def test_type_i_failed_scrape_must_not_withdraw():
    """I-1: legacy marks all attempted books unavailable, even a failed scrape."""
    g, archive = game(), {"last": {}}
    ln = line(g)
    advance_archive(archive, [ln], [ln.book], g.sport, T)
    with closing(connect()) as conn:
        append_odds(conn, [ln], run_id="ok", observed_at=T)
        advance_archive(archive, [], [ln.book], g.sport, T + timedelta(minutes=10))
        append_odds(conn, [], attempted={(ln.book, g.sport)}, successful=set(), run_id="failed", observed_at=T + timedelta(minutes=10))
        assert not build.carry_forward_lines(archive, g.sport, {g.game_id}, [], now=T + timedelta(minutes=40))
        assert q.latest(conn, as_of=T + timedelta(minutes=40), max_age=HOUR)[key(ln)].value == (45, -110)


def test_type_i_run_clock_is_not_observation_clock():
    """I-2: update_history stamps a handed-off quote later than its observation."""
    g, hist = game(), {"series": {}}
    ln = line(g, now=T)
    stage_time = T + timedelta(minutes=10)
    legacy.update_history(hist, [ln], text(stage_time))
    with closing(connect()) as conn:
        append_odds(conn, [ln], run_id="slow", observed_at=stage_time)
        assert hist["series"][ln.key][0][0] == text(stage_time)
        assert not q.latest(conn, as_of=T)
        assert q.series(conn, key(ln), as_of=stage_time)[0].observed_at == ln.scraped_at
        assert q.in_force(conn, at=T, as_of=stage_time)[key(ln).pair].quotes[0].value == (45, -110)


def test_type_i_history_cap_loses_pre_kickoff_observations():
    """I-3: one pre-kick point + 120 post-kick moves evicts the legacy closing."""
    g, hist = game(kickoff=T + timedelta(minutes=1)), {"series": {}}
    with closing(connect()) as conn:
        for i in range(121):
            now = T + timedelta(minutes=2 * i)
            ln = line(g, value=45 + i, now=now)
            legacy.update_history(hist, [ln], text(now))
            append_odds(conn, [ln], games=[g], run_id=str(i), observed_at=now)
        assert clv.freeze_from_series(hist, {g.game_id: g.kickoff_utc}, now) == {}
        assert q.closing(conn, as_of=now)[key(ln)].line == 45
        assert points(q.collapse(q.series(conn, key(ln), as_of=now), cap=120)) == hist["series"][ln.key]


def test_type_i_change_only_weekly_backfill_loses_unchanged_receipt():
    """I-4: collection repeats in-window; later history-only reconstruction loses it."""
    g, op, hist = game(), {"openers": {}}, {"series": {}}
    with closing(connect()) as conn:
        for i, now in enumerate([T - 2 * HOUR, T]):
            ln = line(g, now=now)
            legacy.record_openers(op, [ln], text(now))
            legacy.update_history(hist, [ln], text(now))
            append_odds(conn, [ln], games=[g], run_id=str(i), observed_at=now)
        record_weekly_totals(op, hist, [], [g], T + HOUR)
        assert op["weekly_totals"][g.game_id]["line"] is None
        assert q.weekly_total(conn, g.game_id, as_of=T + HOUR)["line"] == 45


def test_type_ii_closing_price_spell_and_strict_kickoff():
    """II: legacy closes on the price spell clock, not last unchanged receipt."""
    g, hist = game(kickoff=T + 2 * HOUR), {"series": {}}
    with closing(connect()) as conn:
        for i in range(3):
            now = T + i * HOUR
            ln = line(g, value=50 if i == 2 else 45, now=now)
            legacy.update_history(hist, [ln], text(now))
            append_odds(conn, [ln], games=[g], run_id=str(i), observed_at=now)
        assert q.closing(conn, as_of=T + HOUR) == {}
        natural = q.closing(conn, as_of=now)[key(ln)]
        projected = q.closing(conn, as_of=now, change_points=True)[key(ln)]
        frozen = clv.freeze_from_series(hist, {g.game_id: g.kickoff_utc}, now)[ln.key]
        assert natural.observed_at == T + HOUR and projected.observed_at == T
        assert natural.line == projected.line == frozen.line == 45


def test_type_ii_t6_precedence_late_fallback_and_historical_quotes():
    """II: CFB T-6 beats attestation, permits late collection, survives withdrawal."""
    g = game("cfb", T + timedelta(days=5))  # target is yesterday
    ln = line(g, opening_line=42, opening_odds=-105, opening_source="provider", opened_at=T - 2 * HOUR)
    op, hist = legacy.migrate(None, "openers"), legacy.migrate(None, "history")
    with closing(connect()) as conn:
        legacy.record_openers(op, [ln], text(T))
        build.retarget_cfb_total_openers(op, hist, [ln], [g], T)
        append_odds(conn, [ln], games=[g], run_id="first", observed_at=T)
        target = g.kickoff_utc - timedelta(days=6)
        assert q.in_force(conn, at=target, as_of=T) == {}
        assert q.in_force(conn, at=target, as_of=T, late_fallback=True)[key(ln).pair].quotes[0].line == 45
        assert_baselines(conn, op, T, [key(ln)])
        assert q.baseline(conn, as_of=T, cfb_t6=False)[key(ln)].observation.line == 42
        legacy.update_history(hist, [ln], text(T))
        append_odds(conn, [], successful={(ln.book, g.sport)}, run_id="empty", observed_at=T + HOUR)
        build.retarget_cfb_total_openers(op, hist, [], [g], T + HOUR)
        assert q.in_force(conn, at=T + HOUR, as_of=T + HOUR) == {}
        assert q.in_force(conn, at=T + HOUR, as_of=T + HOUR, historical_quotes=True)[key(ln).pair].quotes[0].line == 45
        assert_baselines(conn, op, T + HOUR, [key(ln)])


def test_odds_stage_opt_in_has_identical_result_and_disabled_has_no_write(tmp_path, monkeypatch):
    g = game()
    rows = [line(g, side=s) for s in ("under", "over")]

    async def scrape(*args):
        return {"betonline": rows}, {"betonline": SimpleNamespace(fetch_errors={})}

    monkeypatch.setattr(build, "scrape_books", scrape)
    monkeypatch.setattr(build, "_merge_lines", lambda sport, games, lines, *args: (lines, []))
    monkeypatch.setattr(RunContext, "now_utc", property(lambda self: T))
    monkeypatch.delenv("OBSLOG_SHADOW_PATH", raising=False)

    def stage(label):
        ctx = RunContext("nfl", run_id="hook", started_at=T, git_sha="test")
        res = build.stage_odds(ctx, "nfl", [g], None, NullRawStore("nfl", "hook"), ["betonline"], tmp_path / label, 2026)
        return ctx, (asdict(res), json.dumps(build.json_out.odds_block(g.game_id, res.lines, res.openers), sort_keys=True, default=str))

    _, off = stage("off")
    assert not list(tmp_path.rglob("*.sqlite"))
    path = tmp_path / "shadow.sqlite"
    monkeypatch.setenv("OBSLOG_SHADOW_PATH", str(path))
    ctx, on = stage("on")
    assert on == off and not any(d.component.startswith("obslog") for d in ctx.degradations)
    with closing(connect(path)) as conn:
        assert len(q.latest(conn, as_of=datetime.now(timezone.utc))) == 2
    # Even failure before entering the helper is warning-only.
    monkeypatch.setattr(shadow, "odds", lambda *a: (_ for _ in ()).throw(RuntimeError("failure")))
    ctx, failed = stage("failed")
    assert failed == off and any(d.component == "obslog.odds" and d.severity == "warn" for d in ctx.degradations)


def test_weather_stage_opt_in_preserves_forecasts(request, tmp_path, monkeypatch):
    weather_state = request.getfixturevalue("weather")
    monkeypatch.delenv("OBSLOG_SHADOW_PATH", raising=False)
    _, off = weather_run(weather_state, tmp_path / "off")
    assert not list(tmp_path.rglob("*.sqlite"))
    path = tmp_path / "shadow.sqlite"
    monkeypatch.setenv("OBSLOG_SHADOW_PATH", str(path))
    ctx, on = weather_run(weather_state, tmp_path / "on")
    assert off == on and not any(d.component.startswith("obslog") for d in ctx.degradations)
    with closing(connect(path)) as conn:
        assert len(q.forecasts(conn, as_of=datetime.now(timezone.utc))) == len(on)


def test_shadow_failure_and_success_coverage(tmp_path, monkeypatch):
    g = game()
    degradations = []
    ctx = SimpleNamespace(run_id="shadow", now_utc=T, degradations=degradations,
                          degrade=lambda component, reason, severity: degradations.append(SimpleNamespace(component=component, reason=reason, severity=severity)))
    path = str(tmp_path / "shadow.sqlite")
    from pipeline.scrape_outcomes import ScrapeOutcome, ScrapeResult
    result = ScrapeResult('betonline', g.sport, ScrapeOutcome.OK, 1)
    shadow.odds(path, ctx, g.sport, [g], [line(g)], ["betonline"], {"betonline": result})
    assert not degradations
    with closing(connect(path)) as conn:
        assert len(q.latest(conn, as_of=datetime.now(timezone.utc))) == 1
    shadow.weather(str(tmp_path / "absent" / "bad.sqlite"), ctx, [g], {})
    assert degradations[-1].severity == "warn" and degradations[-1].component == "obslog.weather"
    monkeypatch.setattr(shadow, "connect", lambda _: (_ for _ in ()).throw(RuntimeError("failure")))
    shadow.odds(path, ctx, g.sport, [g], [], [], {})
    assert degradations[-1].component == "obslog.odds"
