"""Schema v3 change-only storage: growth is bounded and every query keeps its meaning."""

import random
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import state as legacy
from pipeline.contracts import Game, GameLine, WeatherForecast, WeatherPoint
from pipeline.obslog import MarketKey, QuoteKey, append_forecasts, append_odds, connect
from pipeline.obslog import queries as q
from tests.test_obslog import HOUR, T, game, key, line, text

MIN = timedelta(minutes=1)
BETONLINE = {("betonline", "nfl")}


def run(conn, lines, n, *, ok=BETONLINE, attempted=None, g=None, now=None):
    now = now or T + n * 20 * MIN
    lines = [replace(x, scraped_at=now) for x in lines]
    append_odds(conn, lines, games=[g or game()], run_id=f"r{n}", observed_at=now,
                attempted=attempted if attempted is not None else set(ok), successful=set(ok))
    return now


def count(conn, table="quote_observations"):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_identical_runs_add_constant_rows_not_one_per_run():
    g, rows = game(), [line(game(), side=s) for s in ("under", "over")]
    sizes = []
    with closing(connect()) as conn:
        for n in range(1, 101):
            run(conn, rows, n, g=g)
            if n in (1, 10, 100):
                sizes.append((count(conn), count(conn, "game_observations")))
    assert sizes == [(2, 1)] * 3  # O(1) quote and game rows; only coverage grows (3 markets/run)


def test_latest_max_age_is_judged_by_last_confirmation_not_last_change():
    g, rows = game(), [line(game(), side=s) for s in ("under", "over")]
    with closing(connect()) as conn:
        run(conn, rows, 0, g=g)
        last = run(conn, rows, 5, g=g)  # unchanged, confirmed in the most recent successful run
        assert count(conn) == 2
        found = q.latest(conn, as_of=last + 50 * MIN, max_age=HOUR)
        assert set(found) == {key(r) for r in rows}
        assert found[key(rows[0])].observed_at == last  # confirmation clock, not the change clock
        assert not q.latest(conn, as_of=last + HOUR + MIN, max_age=HOUR)
        assert key(rows[0]) in q.latest(conn, as_of=T + 100 * MIN, max_age=HOUR)  # change clock alone (T) is 100 min old
        # A scrape that did not succeed does not confirm: age keeps running from the last good one.
        run(conn, [], 6, ok=set(), attempted=BETONLINE, g=g)
        assert not q.latest(conn, as_of=last + HOUR + MIN, max_age=HOUR)
        assert key(rows[0]) in q.latest(conn, as_of=last + HOUR, max_age=HOUR)


def test_other_book_success_never_confirms():
    g = game()
    a, b = line(g), line(g, "pinnacle")
    with closing(connect()) as conn:
        run(conn, [a, b], 0, g=g, ok={("betonline", "nfl"), ("pinnacle", "nfl")})
        last = run(conn, [a], 3, g=g, ok={("betonline", "nfl")}, now=T + 3 * HOUR)
        found = q.latest(conn, as_of=last, max_age=HOUR)
        assert key(a) in found and key(b) not in found  # pinnacle was not scraped within max_age
        assert key(b) in q.latest(conn, as_of=last)  # never withdrawn by someone else's scrape


def test_change_revert_withdraw_reappear_history_latest_and_closing():
    g = game(kickoff=T + 5 * HOUR)
    k = QuoteKey(g.game_id, "betonline", "total", "under")
    plan = [45, 45, 46, 46, 45, 45, None, None, 45, 45]  # None: absent from a successful scrape
    with closing(connect()) as conn:
        for n, value in enumerate(plan):
            run(conn, [] if value is None else [line(g, value=value, now=T + n * HOUR)], n, g=g, now=T + n * HOUR)
        rows = q.series(conn, k, as_of=T + 20 * HOUR)
        assert [(r.kind, r.line) for r in rows] == [("quote", 45), ("quote", 46), ("quote", 45), ("withdrawn", 45), ("quote", 45)]
        assert [r.line for r in q.collapse(rows)] == [45, 46, 45]  # withdrawal ignored, equal repeat collapsed
        assert [(r.observed_at, r.line) for r in q.collapse(rows, include_withdrawals=True) if r.kind == "quote"] == [
            (T, 45), (T + 2 * HOUR, 46), (T + 4 * HOUR, 45), (T + 8 * HOUR, 45)]
        # The pre-v3 per-run view is still derivable and agrees.
        full = q.series(conn, k, as_of=T + 20 * HOUR, expand=True)
        assert [r.line for r in full if r.kind == "quote"] == [v for v in plan if v]
        # latest through each phase
        assert q.latest(conn, as_of=T + 3 * HOUR)[k].line == 46
        assert q.latest(conn, as_of=T + 5 * HOUR)[k].line == 45
        assert k not in q.latest(conn, as_of=T + 7 * HOUR)
        assert q.last_observed(conn, as_of=T + 7 * HOUR)[k.pair].status == "withdrawn"
        assert q.latest(conn, as_of=T + 9 * HOUR)[k].observed_at == T + 9 * HOUR
        # closing: last quote before kickoff (T+5h): the reverted 45, confirmed at T+4h; spell starts T+4h
        assert q.closing(conn, as_of=T + 6 * HOUR)[k].observed_at == T + 4 * HOUR
        assert q.closing(conn, as_of=T + 6 * HOUR, change_points=True)[k].observed_at == T + 4 * HOUR
        # historical closing ignores the later withdrawal and reappearance
        assert q.closing(conn, as_of=T + 20 * HOUR)[k].line == 45


def test_closing_uses_last_confirmation_before_kickoff():
    g = game(kickoff=T + 3 * HOUR + 30 * MIN)
    with closing(connect()) as conn:
        for n in range(6):
            run(conn, [line(g, now=T + n * HOUR)], n, g=g, now=T + n * HOUR)
        k = key(line(g))
        assert q.closing(conn, as_of=T + 6 * HOUR)[k].observed_at == T + 3 * HOUR
        assert q.closing(conn, as_of=T + 6 * HOUR, change_points=True)[k].observed_at == T


def test_failed_or_partial_scrape_neither_confirms_nor_withdraws():
    g = game()
    rows = [line(g, side=s) for s in ("under", "over")]
    with closing(connect()) as conn:
        run(conn, rows, 0, g=g)
        before = count(conn, "coverage")
        # Failed scrape, partial scrape (one side, not marked successful): nothing withdrawn.
        run(conn, [], 1, ok=set(), attempted=BETONLINE, g=g)
        assert count(conn, "coverage") == before  # a failed scrape records nothing
        run(conn, rows[:1], 2, ok=set(), attempted=BETONLINE, g=g)
        assert count(conn, "coverage") == before + 1 and count(conn) == 2  # lists one side; no quote row
        assert not [r for r in q.series(conn, key(rows[1]), as_of=T + 5 * HOUR) if r.kind == "withdrawn"]
        # No confirmation from the failed runs: the over side is as old as run 0.
        found = q.latest(conn, as_of=T + 80 * MIN)
        assert found[key(rows[1])].observed_at == T
        # The partial scrape did literally observe the under side, so that row was logged.
        assert found[key(rows[0])].observed_at == T + 40 * MIN


def test_weekly_total_and_pair_at_see_unchanged_confirmations():
    g = game(kickoff=datetime(2026, 10, 4, 17, tzinfo=timezone.utc))
    start = T - timedelta(hours=1)  # nfl window opens 18:00 Eastern Sunday = 22:00Z
    rows = [line(g, side=s, now=start - 2 * HOUR) for s in ("under", "over")]
    with closing(connect()) as conn:
        run(conn, rows, 0, g=g, now=start - 2 * HOUR)
        run(conn, rows, 1, g=g, now=start + 10 * MIN)  # unchanged, first confirmation in the window
        assert count(conn) == 2
        first = q.weekly_total(conn, g.game_id, as_of=start + HOUR)
        assert first["observed_at"] == start + 10 * MIN and (first["under"], first["over"]) == (-110, -110)
        mk = MarketKey(g.game_id, "betonline", "total")
        pair = q.pair_at(conn, mk, at=start + HOUR, as_of=start + HOUR)
        assert pair.complete and pair.same_capture and {x.observed_at for x in pair.quotes} == {start + 10 * MIN}
        # One side changes: the unchanged mate is part of that run's snapshot, never a cross-run mate.
        run(conn, [rows[0], replace(rows[1], odds=-115)], 2, g=g, now=start + 30 * MIN)
        pair = q.pair_at(conn, mk, at=start + HOUR, as_of=start + HOUR)
        assert pair.same_capture and {x.odds for x in pair.quotes} == {-110, -115}


def test_first_complete_pair_assembled_from_a_carried_unchanged_side():
    g = game()
    with closing(connect()) as conn:
        spread = lambda side, n, now: line(g, market="spread", side=side, value=n, now=now)  # noqa: E731
        run(conn, [spread("home", -3, T), spread("away", 4, T)], 0, g=g, now=T)  # mismatched
        run(conn, [spread("home", -3, T + HOUR), spread("away", 3, T + HOUR)], 1, g=g, now=T + HOUR)
        mk = MarketKey(g.game_id, "betonline", "spread")
        pair = q.first_seen(conn, as_of=T + 2 * HOUR)[mk]
        assert pair.complete and pair.same_capture
        assert {x.observed_at for x in pair.quotes} == {T + HOUR}  # the repeat row it replaces


def _legacy_vs_log(runs, g):
    """Feed identical lines to the state store and the log; return (store openers, log baseline)."""
    op = legacy.migrate(None, "openers")
    with closing(connect()) as conn:
        for n, lines in enumerate(runs):
            now = T + n * HOUR
            lines = [replace(x, scraped_at=now) for x in lines]
            legacy.record_openers(op, lines, text(now))
            append_odds(conn, lines, games=[g], run_id=str(n), observed_at=now,
                        attempted=BETONLINE, successful=BETONLINE)
        base = q.baseline(conn, as_of=T + len(runs) * HOUR, cfb_t6=False)
    return op, base


def test_baseline_takes_first_line_per_side_like_the_store():
    g = game("cfb")
    novig = lambda side, n, **kw: line(g, "novig", "spread", side, n, odds=-144, **kw)  # noqa: E731
    runs = [[novig("home", -4.5, is_main=False), novig("home", -3.5), novig("away", 3.5)],
            [novig("home", -3.5), novig("away", 3.5)]]
    op, base = _legacy_vs_log(runs, g)
    for side in ("home", "away"):
        k = QuoteKey(g.game_id, "novig", "spread", side)
        expected = legacy.get_baseline(op, legacy.odds_key(g.game_id, "spread", side, "novig"))
        assert (base[k].observation.line if k in base else None) == (expected["line"] if expected else None)


def test_forecast_rows_only_when_the_forecast_changes():
    g = game()
    fc = WeatherForecast(g.game_id, "merged", lead_hours=40, wind_fg=12, hourly=[WeatherPoint(T, wind=12)])
    with closing(connect()) as conn:
        for n in range(20):
            append_forecasts(conn, [replace(fc, lead_hours=40 - n)], games=[g], run_id=str(n), fetched_at=T + n * HOUR)
        assert count(conn, "forecast_observations") == 1 and count(conn, "game_observations") == 1
        append_forecasts(conn, [replace(fc, wind_fg=15)], games=[g], run_id="x", fetched_at=T + 30 * HOUR)
        append_forecasts(conn, [fc], games=[g], run_id="y", fetched_at=T + 31 * HOUR)  # revert is a change
        assert count(conn, "forecast_observations") == 3
        assert [json_wind(r) for r in q.forecasts(conn, as_of=T + 40 * HOUR, latest_only=False)] == [12, 15, 12]


def json_wind(row):
    import json
    return json.loads(row["payload"])["wind_fg"]


def test_size_budget_120_runs_3500_keys_with_churn(tmp_path, capsys):
    """An 8-day week (~15 runs/day = 120 runs) shaped like the real production log.

    Measured on the real file (two runs 15 minutes apart, a Friday): about 3,460
    main keys, 5.1% of main quotes changed semantically between runs, 17% of
    forecasts changed (23 of 155 plus 3 new), thousands of alternate lines per
    run (never logged), and CFB groups for 4 of 6 books that are never
    "successful" (raw rows the schedule drops) yet repeat unchanged every run.
    """
    rng = random.Random(7)
    books = ["betonline", "pinnacle", "fanduel", "novig", "betcris", "kalshi", "polymarket_us", "circa"]
    partial = {"fanduel", "novig", "kalshi", "polymarket_us"}  # coverage_ok never true: partial every run
    kick = T + 6 * timedelta(days=1)
    games = [Game(f"nfl:2026:4:a{i}@h{i}", "nfl", 2026, 4, kick, kick, "America/New_York", f"h{i}", f"a{i}", "stadium")
             for i in range(72)]
    state = {}
    for g in games:
        for b in books:
            for market, sides in (("ml", ("home", "away")), ("spread", ("home", "away")), ("total", ("over", "under"))):
                base = rng.choice([-3.5, -7, 44.5, 47]) if market != "ml" else None
                for s in sides:
                    state[(g.game_id, b, market, s)] = [base, rng.choice([-110, -105, -115, 120, -140])]
    keys = list(state)
    assert len(state) == 3456
    path = tmp_path / "big.sqlite"
    successful = {(b, "nfl") for b in books if b not in partial}
    attempted = {(b, "nfl") for b in books}
    forecasts = {g.game_id: WeatherForecast(g.game_id, "merged", lead_hours=170, wind_fg=10, temp_fg=60, rain_fg_mm=0.0,
                                           hourly=[WeatherPoint(kick + i * HOUR, wind=10, temp=60, precip=0) for i in range(4)])
                 for g in games}
    with closing(connect(path)) as conn:
        for n in range(120):
            now = T + n * timedelta(minutes=96)
            for k in rng.sample(keys, round(len(keys) * 0.06)):  # 6% (measured 5.1%) of main quotes move per run
                state[k][1] = rng.choice([x for x in (-110, -105, -115, 120, -140, -125, -102) if x != state[k][1]])
            lines = []
            for (gid, b, market, s), (ln, odds) in state.items():
                # betcris-style rolling expiry moves with every scrape
                extra = dict(expires_at=now + timedelta(minutes=30), actionable_until=now + HOUR) if b == "betcris" else {}
                lines.append(GameLine("nfl", gid, b, market, s, odds, ln, scraped_at=now, **extra))
            # alternates are most of production's volume (kalshi/polymarket ladders); they are not logged
            lines += [GameLine("nfl", games[i % 72].game_id, "kalshi", "total", "over", -110, 30.5 + i, is_main=False, scraped_at=now)
                      for i in range(600)]
            append_odds(conn, lines, games=games, run_id=f"r{n}", observed_at=now, attempted=attempted, successful=successful)
            moved = [replace(f, lead_hours=170 - n / 4, run_time=now,
                             wind_fg=f.wind_fg + (rng.random() < .17), hourly=f.hourly) for f in forecasts.values()]
            forecasts = {f.game_id: f for f in moved}
            append_forecasts(conn, moved, games=games, run_id=f"w{n}", fetched_at=now)
            if n == 0:
                conn.commit()
                first = Path(path).stat().st_size
        rows = count(conn)
        assert count(conn, "quote_observations") == rows
        assert conn.execute("SELECT count(*) FROM quote_observations WHERE line NOT IN (-3.5, -7, 44.5, 47)").fetchone()[0] == 0  # no alternate
    size = Path(path).stat().st_size
    mib = size / 2**20
    with capsys.disabled():
        print(f"\nobslog 120 runs x {len(state)} main keys (+600 alternates/run): first run {first / 2**20:.2f} MiB, "
              f"total {mib:.1f} MiB, {rows} quote rows, {size / rows:.0f} bytes/row all-in; "
              f"8 days at 15 runs/day = 120 runs, so the week is this figure")
    assert size < 48 * 2**20, f"{mib:.1f} MiB exceeds the 48 MiB budget (week target 96 MiB, cache bound 192 MiB)"
    assert rows < len(state) * 120 * 0.15  # far below one row per quote per run


def test_rolling_expiry_is_not_a_change_and_shifts_with_the_confirmation():
    """BetCris-style expires_at = observed + ttl moves every scrape; it must not cost a row per run."""
    g = game()
    with closing(connect()) as conn:
        for n in range(4):
            now = T + n * 20 * MIN
            run(conn, [line(g, expires_at=now + 30 * MIN, actionable_until=now + HOUR)], n, g=g, now=now)
        assert count(conn) == 1
        k = key(line(g))
        at = T + 3 * 20 * MIN
        assert k in q.latest(conn, as_of=at + 30 * MIN, honor_expiry=True)
        assert k not in q.latest(conn, as_of=at + 31 * MIN, honor_expiry=True)
        assert q.latest(conn, as_of=at)[k].data["actionable_until"] == text(at + HOUR).replace("Z", ".000000Z")


def test_unsuccessful_group_with_unchanged_lines_adds_no_quote_rows():
    """Production: CFB groups whose raw/mapped counts differ are never 'successful' but repeat every run."""
    g = game("cfb")
    rows = [line(g, "fanduel", market="ml", side=s) for s in ("home", "away")]
    with closing(connect()) as conn:
        for n in range(30):
            run(conn, rows, n, ok=set(), attempted={("fanduel", "cfb")}, g=g)
        assert count(conn) == 2
        assert not [r for r in q.series(conn, key(rows[0]), as_of=T + 20 * HOUR) if r.kind == "withdrawn"]
        found = q.latest(conn, as_of=T + 29 * 20 * MIN, max_age=HOUR)
        assert found[key(rows[0])].observed_at == T + 29 * 20 * MIN
