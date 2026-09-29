import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from pipeline import alert_liquidity as L
from pipeline import alerts as A
from tests.test_alerts_rules import CFG, NOW, Recorder, _edge, _fresh, card


def sample():
    c = card()
    c["_alert_at"] = NOW.isoformat()
    c["execution_markets"] = [{"book": "polymarket_us", "source_id": "mapped", "line": 46.5}]
    c["total_prices"] = {"quotes": [dict(book="polymarket_us", side="under", line=46.5, odds=100,
                                         ev_roi=.1, updated_at=NOW.isoformat())]}
    return c


def snapshot():
    return dict(checked_at=NOW.isoformat(), quotes=[dict(book="polymarket_us", side="under", line=46.5,
                odds=150, ev_roi=.5, updated_at=NOW.isoformat(), liquidity_status="verified",
                liquidity_shares=6, liquidity_dollars=2.4)], spend=2.4, unspent=497.6,
                allocations=[dict(book="polymarket_us", line=46.5, all_in_price=.4, quantity=6, spend=2.4,
                                  available_shares=6, available_dollars=2.4)])


def test_bridge_updates_price_and_size_together_without_mutating_source_quotes(monkeypatch):
    c = sample()
    c["odds"]["polymarket_us"] = {"total": dict(line=46.5, under=100, updated_at=NOW.isoformat())}
    old_books = c["odds"]
    original = deepcopy(c["total_prices"])
    def run(args, **kw):
        assert args[0] == "node" and kw["timeout"] == 60 and not kw.get("shell")
        assert json.loads(kw["input"])[0]["game_id"] == c["game_id"]
        return SimpleNamespace(stdout=json.dumps({c["game_id"]: snapshot()}))
    monkeypatch.setattr(L.subprocess, "run", run)
    monkeypatch.setattr(L, "now_utc", lambda: NOW)
    old_prices = c["total_prices"]
    L.enrich_liquidity([c])
    assert old_prices == original
    assert A._play_edge(c)["odds"] == 150
    assert c["odds"]["polymarket_us"]["total"]["under"] == 150
    assert old_books["polymarket_us"]["total"]["under"] == 100
    text = A.format_edge(c, A._play_edge(c))
    assert "6 shares / $2.40 available incl. fees" in text
    assert "use 6 shares / $2.40" in text
    assert "$497.60 unallocated" in text
    summary = A.open_signal_summaries({"nfl": [c]}, CFG, NOW)[0].text
    assert "6 shares / $2.40" in summary and "rain 0.8 mm" in summary


def test_missing_node_or_stale_depth_never_invents_size(monkeypatch):
    c = sample()
    def missing(*args, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(L.subprocess, "run", missing)
    L.enrich_liquidity([c])
    assert "size unverified" in A.format_edge(c, A._play_edge(c))
    assert "coverage unverified" in A.format_edge(c, A._play_edge(c))
    monkeypatch.setattr(L.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps({c["game_id"]: snapshot()})))
    monkeypatch.setattr(L, "now_utc", lambda: NOW + timedelta(minutes=2))
    L.enrich_liquidity([c])
    assert "alert_liquidity" not in c


def test_empty_verified_book_cannot_return_through_posted_quote_fallback():
    c = sample()
    c["total_prices"]["quotes"][0]["liquidity_status"] = "empty"
    c["odds"] = {"polymarket_us": {"total": dict(line=46.5, under=100, updated_at=NOW.isoformat())}}
    assert A._play_edge(c)["odds"] is None


def test_long_price_ladders_are_sent_in_full_without_marking_partial_delivery():
    c = sample()
    c["alert_liquidity"] = snapshot()
    c["alert_liquidity"]["allocations"] *= 65
    text = A.format_edge(c, _edge())
    pages = A.telegram_pages(text)
    assert len(pages) > 1 and all(len(p) <= A.TELEGRAM_MAX_CHARS for p in pages)
    assert "65) Polymarket US" in "\n".join(pages)
    summaries = A.open_signal_summaries({"nfl": [c]}, CFG, NOW)
    assert all(len(s.text) <= A.TELEGRAM_MAX_CHARS for s in summaries)
    assert "65) Polymarket US" in "\n".join(s.text for s in summaries)
    alerts, _ = _fresh()
    sender, outcome = Recorder(), A.Outcome()
    candidate = A.Candidate("test", "edge", "nfl", text)
    assert A._alert_once(sender, alerts, NOW, outcome, CFG)(candidate)
    assert outcome.n_messages == len(pages) and outcome.n_sent == 1
    alerts, _ = _fresh()
    outcome, calls = A.Outcome(), []
    def fail_second(text, chat):
        calls.append(text)
        return len(calls) == 1
    assert not A._alert_once(fail_second, alerts, NOW, outcome, CFG)(candidate)
    assert not alerts["sent"] and outcome.failed == [candidate]
