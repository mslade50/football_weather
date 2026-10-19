"""Generic fetch_errors capture, cross-run book health counters, book-health alerts."""
import asyncio
from datetime import datetime, timezone

import httpx

from pipeline import alerts as A
from pipeline import state as pstate
from pipeline.odds.base import BaseScraper

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class _Boom(BaseScraper):
    BOOK_NAME = "boom"
    MAX_RETRIES = 2
    RETRY_DELAY = 0.0

    async def scrape(self, sport, market=None, **kw):
        req = httpx.Request("GET", "https://x.test")
        raise httpx.HTTPStatusError("server said no", request=req, response=httpx.Response(503, request=req))


class _Ok(_Boom):
    async def scrape(self, sport, market=None, **kw):
        return []


def test_generic_fetch_errors_captured():
    s = _Boom()
    assert asyncio.run(s.scrape_with_retry("nfl")) == []
    msg = s.fetch_errors["nfl"]
    assert msg.startswith("HTTPStatusError") and "503" in msg and len(msg) <= 200


def test_no_error_recorded_on_success():
    s = _Ok()
    asyncio.run(s.scrape_with_retry("nfl"))
    assert not getattr(s, "fetch_errors", {})


def test_health_counters_and_untouched_books(tmp_path):
    h = pstate.load_book_health(tmp_path)
    pstate.update_book_health(h, "nfl", {"a": 5, "b": 0}, {"b": "boom"}, now="t1", horizon_games=3)
    pstate.update_book_health(h, "nfl", {"b": 0}, None, now="t2")   # 'a' not scraped this run
    a, b = h["books"]["a"]["nfl"], h["books"]["b"]["nfl"]
    assert a == {"rows": 5, "last_ok_utc": "t1", "consecutive_empty": 0, "last_error": None}
    assert b["consecutive_empty"] == 2 and b["last_ok_utc"] is None and b["rows"] == 0
    pstate.update_book_health(h, "nfl", {"b": 4}, None, now="t3")
    assert h["books"]["b"]["nfl"]["consecutive_empty"] == 0 and h["books"]["b"]["nfl"]["last_ok_utc"] == "t3"
    pstate.save_book_health(tmp_path, h)
    again = pstate.load_book_health(tmp_path)
    assert again["schema_version"] == 1
    exp = pstate.book_health_export(again)
    assert set(exp["a"]["nfl"]) == {"rows", "last_ok_utc", "consecutive_empty", "last_error"}


def test_alert_fires_once_then_recovery_once():
    h = {"books": {"bk": {"nfl": {"rows": 0, "last_ok_utc": None, "consecutive_empty": 2, "last_error": None}}},
         "horizon": {"nfl": 4}}
    alerts: dict = {}
    sent: list[str] = []
    cfg = A.Config(chat_default="1", book_health=True)

    def ok(text, chat):
        sent.append(text)
        return True

    assert A.send_book_health(h, alerts, ok, cfg, NOW) == 0          # below threshold
    h["books"]["bk"]["nfl"]["consecutive_empty"] = 3
    assert A.send_book_health(h, alerts, ok, cfg, NOW) == 1
    assert A.send_book_health(h, alerts, ok, cfg, NOW) == 0          # deduped
    h["books"]["bk"]["nfl"].update(rows=7, consecutive_empty=0)
    assert A.send_book_health(h, alerts, ok, cfg, NOW) == 1          # recovery
    assert A.send_book_health(h, alerts, ok, cfg, NOW) == 0
    assert len(sent) == 2 and "down" in sent[0] and "recovered" in sent[1]


def test_alert_marker_only_after_successful_send_and_needs_horizon():
    h = {"books": {"bk": {"nfl": {"rows": 0, "consecutive_empty": 5, "last_ok_utc": None, "last_error": "x"}}},
         "horizon": {"nfl": 0}}
    alerts: dict = {}
    cfg = A.Config(chat_default="1", book_health=True)
    assert A.send_book_health(h, alerts, lambda t, c: True, cfg, NOW) == 0   # no games in horizon
    h["horizon"]["nfl"] = 2
    assert A.send_book_health(h, alerts, lambda t, c: False, cfg, NOW) == 0  # send failed
    assert not alerts.get("book_health")
    assert A.send_book_health(h, alerts, lambda t, c: True, cfg, NOW) == 1


def test_config_env_default_on():
    assert A.Config.from_env({}).book_health is True
    assert A.Config.from_env({"BOOK_HEALTH_ALERTS": "0"}).book_health is False
