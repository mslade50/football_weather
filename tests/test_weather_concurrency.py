"""Concurrent Open-Meteo point batches: same results/order/accounting, less wall-clock."""
from __future__ import annotations

import threading
import time

import pytest

from pipeline import build
from pipeline.weather.rate_limit import MINUTE_BUDGET, RequestBudget

POINTS = [(30.0 + i * 0.01, -90.0) for i in range(40)]
URL = "https://api.open-meteo.com/v1/forecast"


def _fetcher(delay: float, budget: RequestBudget | None = None, fail_prefix: str | None = None):
    calls: list[str] = []

    def fetch(batch, source_prefix, capture=None, **kw):
        calls.append(source_prefix)
        if budget is not None:
            budget.acquire(URL, {"latitude": ",".join(str(p[0]) for p in batch), "models": "a", "hourly": "x",
                                 "forecast_days": "1"})
        time.sleep(delay)
        if fail_prefix and source_prefix.endswith(fail_prefix):
            raise RuntimeError("boom")
        if capture:
            capture(source_prefix, {"n": len(batch)}, URL)
        return [("fc", p) for p in batch]

    return fetch, calls


def _run(monkeypatch, workers, fetcher, **kw):
    monkeypatch.setenv("FOOTBALL_WEATHER_WORKERS", str(workers))
    seen: list[list] = []
    captured: list[str] = []
    t0 = time.perf_counter()
    out = build._fetch_point_batches(
        POINTS, fetcher, batch_size=5, source_prefix="x", received=seen.append,
        capture=lambda s, p, u=None: captured.append(s), **kw)
    return out, seen, sorted(captured), time.perf_counter() - t0


def test_concurrent_matches_sequential_and_is_faster(monkeypatch):
    fetch, _ = _fetcher(0.05)
    (seq, seq_fail), seq_seen, seq_cap, t_seq = _run(monkeypatch, 1, fetch)
    (con, con_fail), con_seen, con_cap, t_con = _run(monkeypatch, 4, fetch)
    assert list(seq) == list(con) == POINTS and seq == con
    assert seq_fail == con_fail == [] and seq_seen == con_seen and seq_cap == con_cap
    assert len(con_cap) == 8
    assert t_con < t_seq * 0.6


def test_failures_ordered_and_other_batches_kept(monkeypatch):
    fetch, _ = _fetcher(0.0, fail_prefix="batch03")
    (seq, seq_fail), *_ = _run(monkeypatch, 1, fetch)
    (con, con_fail), *_ = _run(monkeypatch, 4, fetch)
    assert seq == con and len(con) == 35
    assert [(i, n) for i, n, _ in con_fail] == [(3, 5)] == [(i, n) for i, n, _ in seq_fail]


def test_budget_accounting_exact_under_concurrency(monkeypatch):
    budget = RequestBudget()
    fetch, calls = _fetcher(0.01, budget)
    _run(monkeypatch, 4, fetch)
    assert len(calls) == 8 and len(budget.requests) == 8


def test_budget_acquire_thread_safe_blocks_at_limit():
    now = [0.0]
    lock = threading.Lock()

    def sleep(d):
        with lock:
            now[0] += d

    budget = RequestBudget(clock=lambda: now[0], sleep=sleep)
    params = {"latitude": ",".join(["1"] * 50), "models": "a,b,c", "hourly": "x", "forecast_days": "1"}
    threads = [threading.Thread(target=budget.acquire, args=(URL, params)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(budget.requests) == 20
    for start, _ in budget.requests:
        assert sum(w for s, w in budget.requests if start <= s < start + 60) <= MINUTE_BUDGET


def test_callable_batch_size_stays_sequential(monkeypatch):
    monkeypatch.setenv("FOOTBALL_WEATHER_WORKERS", "4")
    threads: set[int] = set()

    def fetch(batch, source_prefix, **kw):
        threads.add(threading.get_ident())
        return list(batch)

    out, fail = build._fetch_point_batches(POINTS, fetch, batch_size=lambda rem: 10, source_prefix="x")
    assert len(out) == 40 and not fail and threads == {threading.get_ident()}


@pytest.mark.parametrize("val,exp", [("1", 1), ("", 3), ("abc", 3), ("0", 1), ("99", 8), ("4", 4)])
def test_workers_env(monkeypatch, val, exp):
    monkeypatch.setenv("FOOTBALL_WEATHER_WORKERS", val)
    assert build.weather_workers() == exp
