"""Disabled books are excluded from a build independently of notification policy."""
from pipeline import build


def test_disabled_book_is_dropped_from_run(monkeypatch):
    monkeypatch.setenv("BOOK_FANDUEL_ENABLED", "0")
    monkeypatch.setenv("BOOK_NOVIG_ENABLED", " 0 ")
    books = build.books_for_scope("light")
    assert "fanduel" not in books and "novig" not in books and "pinnacle" in books
    assert build.books_for_scope("odds", ["fanduel", "betonline"]) == ["betonline"]
    monkeypatch.delenv("BOOK_FANDUEL_ENABLED")
    assert "fanduel" in build.books_for_scope("light")
