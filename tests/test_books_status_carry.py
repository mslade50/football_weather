"""Header book chips: a run that did not scrape a book (the BetOnline-only Playwright job)
must carry the previous run's chip forward instead of painting the book red."""
from __future__ import annotations

from pipeline.outputs import json_out

NOW = "2026-08-26T10:00:00Z"
PREV = {
    "pinnacle": {"count": 832, "baseline": 900, "status": "green", "last_ok": "2026-08-26T09:57:00Z"},
    "kalshi": {"count": 0, "baseline": 500, "status": "red", "last_ok": "2026-08-25T21:00:00Z"},
}


def test_unscraped_books_carry_previous_chip():
    counts = {"betonline": {"nfl": 0, "cfb": 0}}   # scraped, returned nothing -> genuinely red
    st = json_out.books_status(counts, ["betonline", "pinnacle", "kalshi", "novig"], None, NOW, PREV)
    assert st["betonline"]["status"] == "red" and "carried" not in st["betonline"]
    assert st["pinnacle"] == {**PREV["pinnacle"], "carried": True}
    assert st["kalshi"] == {**PREV["kalshi"], "carried": True}          # previous red stays red, not re-stamped
    assert st["novig"]["status"] == "red" and st["novig"]["count"] == 0  # never seen anywhere -> red


def test_scraped_zero_is_not_carried():
    counts = {"pinnacle": {"nfl": 0}}
    st = json_out.books_status(counts, ["pinnacle"], None, NOW, PREV)
    assert st["pinnacle"]["status"] == "red" and st["pinnacle"]["last_ok"] == PREV["pinnacle"]["last_ok"]
