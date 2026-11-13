"""Outcome consumers must not use diagnostic wording as a protocol."""

from types import SimpleNamespace

import pytest

from pipeline import alerts, state
from pipeline.obslog import shadow
from pipeline.outputs import d1_out
from pipeline.run_context import RunContext
from pipeline.scrape_outcomes import ScrapeOutcome, ScrapeResult, scraper_result


@pytest.mark.parametrize('code', list(ScrapeOutcome))
def test_outcome_consumers(code, monkeypatch):
    rows = 4 if code == ScrapeOutcome.OK else 0
    result = ScrapeResult('book', 'nfl', code, rows, 'opaque diagnostic')
    health = {'books': {}, 'horizon': {'nfl': 1}}
    for _ in range(3):
        state.update_book_health(health, 'nfl', {'book': rows}, now='now', results={'book': result})
    skipped = code in (ScrapeOutcome.SKIPPED, ScrapeOutcome.NOT_REQUESTED)
    if skipped:
        assert not health['books']
    else:
        entry = health['books']['book']['nfl']
        assert entry['outcome_code'] == code
        assert entry['consecutive_empty'] == (0 if code in (ScrapeOutcome.OK, ScrapeOutcome.UPSTREAM_STALE) else 3)
        assert bool(alerts.book_health_messages(health, {})) == (code not in (ScrapeOutcome.OK, ScrapeOutcome.UPSTREAM_STALE))

    ctx = RunContext('nfl', git_sha='test')
    ctx.degrade('odds', 'unrelated sentence', outcome_code=code, empty_book=True)
    assert d1_out.run_status(ctx) == ('warn' if code not in (ScrapeOutcome.OK, ScrapeOutcome.NOT_REQUESTED) else 'ok')
    ctx.degradations.clear()
    ctx.degrade('odds', 'returned 0 lines upstream stale')
    assert d1_out.run_status(ctx) == 'ok'

    captured = {}
    class Connection:
        def close(self):
            pass
    monkeypatch.setattr(shadow, 'connect', lambda _: Connection())
    monkeypatch.setattr(shadow, 'append_odds', lambda *a, **kw: captured.update(kw))
    shadow.odds('unused', ctx, 'nfl', [object()], [], ['book'], {'book': result})
    assert captured['successful'] == ({('book', 'nfl')} if code in (ScrapeOutcome.OK, ScrapeOutcome.EMPTY) else set())


@pytest.mark.parametrize('text,rows,expected', [
    (None, 3, 'ok'), (None, 0, 'empty'), ('returned 0 lines', 0, 'empty'),
    ('college-football: upstream stale, age 100s', 0, 'upstream_stale'),
    ('ConnectTimeout: slow', 0, 'timeout'), ('timed out', 0, 'timeout'), ('HTTP 403', 0, 'fetch_error'),
])
def test_legacy_health_migrates_once(text, rows, expected):
    old = {'schema_version': 1, 'books': {'book': {'nfl': {'rows': rows, 'last_error': text}}}}
    current = state.migrate(old, 'book_health')
    assert current['schema_version'] == 2
    entry = current['books']['book']['nfl']
    assert entry['outcome_code'] == expected
    entry['last_error'] = 'upstream stale'
    assert state.migrate(current, 'book_health')['books']['book']['nfl']['outcome_code'] == expected


def test_scraper_adapter_does_not_match_text_and_keeps_partial_rows():
    scraper = SimpleNamespace(fetch_errors={'nfl': 'upstream stale: arbitrary exception'})
    result = scraper_result('other', 'nfl', 4, scraper)
    assert result.outcome == ScrapeOutcome.FETCH_ERROR and not result.coverage_ok
    health = {}
    state.update_book_health(health, 'nfl', {'other': 4}, now='now', results={'other': result})
    assert health['books']['other']['nfl']['last_error'] is None
