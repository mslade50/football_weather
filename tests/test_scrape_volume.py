"""check_scrape_volume (pipeline/build.py) — adapted from golf_scraping/board/build.py
``_check_scrape_volume`` with keys ``book|market`` and scrape_baseline.json state."""

from __future__ import annotations

from pathlib import Path

from pipeline import state as pstate
from pipeline.build import (
    CRITICAL_BOOKS,
    check_scrape_volume,
    format_volume_alert,
    scrape_counts,
)
from pipeline.contracts import GameLine

BOOKS = ["pinnacle", "betcris", "fanduel", "kalshi", "betonline"]


def _healthy(n: int = 30) -> dict[str, int]:
    out = {}
    for b in BOOKS:
        for m in ("spread", "total", "ml"):
            out[f"{b}|{m}"] = n
    return out


def _fresh() -> dict:
    return pstate.migrate(None, "baseline")


def test_cold_start_with_nothing_seen_is_silent():
    bl = _fresh()
    assert check_scrape_volume({"pinnacle|spread": 3}, bl, BOOKS) == []
    assert bl["alerted"] == []


def test_peaks_ratchet_and_first_healthy_run_is_silent():
    bl = _fresh()
    assert check_scrape_volume(_healthy(30), bl, BOOKS) == []
    assert bl["peaks"]["pinnacle|spread"] == 30
    assert check_scrape_volume(_healthy(20), bl, BOOKS) == []      # 20 >= 50% of 30: fine
    assert bl["peaks"]["pinnacle|spread"] == 30                     # never lowered
    assert bl["seen_books"]["pinnacle"] == 90


def test_drop_alerts_once_then_rearms_on_recovery():
    bl = _fresh()
    check_scrape_volume(_healthy(30), bl, BOOKS)
    counts = _healthy(30)
    counts["betcris|total"] = 5
    drops = check_scrape_volume(counts, bl, BOOKS)
    assert drops == [("betcris|total", 5, 30)]
    assert "betcris|total" in bl["alerted"]
    # sustained drop: no second ping
    assert check_scrape_volume(counts, bl, BOOKS) == []
    # recovery re-arms
    assert check_scrape_volume(_healthy(30), bl, BOOKS) == []
    assert "betcris|total" not in bl["alerted"]
    counts["betcris|total"] = 2
    assert check_scrape_volume(counts, bl, BOOKS) == [("betcris|total", 2, 30)]


def test_small_markets_below_min_peak_never_alert():
    bl = _fresh()
    counts = _healthy(30)
    counts["kalshi|ml"] = 6
    check_scrape_volume(counts, bl, BOOKS)
    counts["kalshi|ml"] = 0
    assert check_scrape_volume(counts, bl, BOOKS) == []


def test_dark_critical_book_from_cold_start_when_two_peers_report():
    """No baseline for betonline yet, but pinnacle + betcris healthy => DARK, not silence."""
    bl = _fresh()
    counts = _healthy(30)
    for m in ("spread", "total", "ml"):
        del counts[f"betonline|{m}"]
    drops = check_scrape_volume(counts, bl, BOOKS)
    assert drops == [("DARK|betonline", 0, None)]
    assert "DARK|betonline" in bl["alerted"]
    # once
    assert check_scrape_volume(counts, bl, BOOKS) == []
    # back: re-arm
    check_scrape_volume(_healthy(30), bl, BOOKS)
    assert "DARK|betonline" not in bl["alerted"]


def test_dark_line_subsumes_that_books_per_market_drops():
    bl = _fresh()
    check_scrape_volume(_healthy(30), bl, BOOKS)
    counts = _healthy(30)
    for m in ("spread", "total", "ml"):
        counts[f"pinnacle|{m}"] = 0
    drops = check_scrape_volume(counts, bl, BOOKS)
    assert drops == [("DARK|pinnacle", 0, None)]
    assert not any(k.startswith("pinnacle|") for k in bl["alerted"])


def test_non_critical_book_goes_dark_only_via_seen_books_high_water():
    bl = _fresh()
    counts = _healthy(30)
    for m in ("spread", "total", "ml"):
        counts[f"kalshi|{m}"] = 0
    # kalshi never reported: not dark (peers rule only covers CRITICAL_BOOKS)
    assert "kalshi" not in CRITICAL_BOOKS
    assert check_scrape_volume(counts, bl, BOOKS) == []
    # after it reported >=10 rows once, going to 0 is dark
    check_scrape_volume(_healthy(30), bl, BOOKS)
    assert check_scrape_volume(counts, bl, BOOKS) == [("DARK|kalshi", 0, None)]


def test_seen_books_survives_scope_reset(tmp_path: Path):
    bl = pstate.load_baseline(tmp_path, "nfl:2026:1")
    check_scrape_volume(_healthy(30), bl, BOOKS)
    pstate.save_baseline(tmp_path, bl)
    nxt = pstate.load_baseline(tmp_path, "nfl:2026:2")
    assert nxt["scope"] == "nfl:2026:2" and nxt["peaks"] == {} and nxt["alerted"] == []
    assert nxt["seen_books"]["betonline"] == 90
    counts = _healthy(30)
    for m in ("spread", "total", "ml"):
        counts[f"betonline|{m}"] = 0
    assert check_scrape_volume(counts, nxt, BOOKS) == [("DARK|betonline", 0, None)]


def test_unrequested_books_are_never_judged():
    """The light job never scrapes betonline: its absence must not read as DARK,
    and its peaks are untouched."""
    bl = _fresh()
    check_scrape_volume(_healthy(30), bl, BOOKS)
    light = [b for b in BOOKS if b != "betonline"]
    counts = {k: v for k, v in _healthy(30).items() if not k.startswith("betonline|")}
    assert check_scrape_volume(counts, bl, light) == []
    assert bl["peaks"]["betonline|spread"] == 30
    assert "DARK|betonline" not in bl["alerted"]


def test_scrape_counts_keys_and_ignores_alternates():
    def ln(book: str, market: str, side: str, line: float | None, main: bool = True) -> GameLine:
        return GameLine(sport="nfl", game_id="nfl:2026:1:buf@kc", book=book, market=market, side=side,
                        odds=-110, line=line, is_main=main)
    lines = [
        ln("pinnacle", "spread", "home", -3.0), ln("pinnacle", "spread", "away", 3.0),
        ln("pinnacle", "spread", "home", -3.5, main=False),
        ln("kalshi", "total", "over", 47.5), ln("kalshi", "ml", "home", None),
    ]
    assert scrape_counts(lines) == {"pinnacle|spread": 2, "kalshi|total": 1, "kalshi|ml": 1}


def test_format_volume_alert():
    text = format_volume_alert([("betcris|total", 5, 30), ("DARK|betonline", 0, None)], "nfl:2026:1")
    assert text.splitlines()[0].endswith("nfl:2026:1")
    assert "betonline: 0 rows - DARK while peers report" in text
    assert "betcris total: 5 (usual ~30)" in text
