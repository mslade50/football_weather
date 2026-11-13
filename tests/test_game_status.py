"""Normalized status, unchanged raw values, and the shared eligibility policy."""

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.contracts import Game
from pipeline.outputs import json_out
from utils.game_status import GameStatus, bettable_before_kickoff, normalize_status, status_code

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


@pytest.mark.parametrize('raw,code', [
    ('scheduled', 'scheduled'), ('tbd', 'tbd'), ('Live', 'live'), ('in progress', 'live'),
    ('in_play', 'live'), ('Halftime', 'live'), ('Final/OT', 'final'), ('cancelled', 'cancelled'),
    ('canceled', 'cancelled'), ('postponed', 'postponed'), ('suspended', 'suspended'),
    ('unknown', 'unknown'), ('unexpected source text', 'unspecified'), (None, 'unspecified'),
])
def test_source_normalization(raw, code):
    assert normalize_status(raw) == code
    game = Game('nfl:2026:10:a@b', 'nfl', 2026, 10, NOW + timedelta(hours=1), NOW, 'UTC', 'b', 'a', None, status=raw)
    assert game.status == raw and game.status_code == code
    assert game.to_dict()['status'] == raw
    assert bettable_before_kickoff(game, NOW) == (code in ('scheduled', 'tbd', 'unknown', 'unspecified'))


@pytest.mark.parametrize('code', list(GameStatus))
def test_published_code_is_authoritative_with_legacy_fallback(code):
    future = (NOW + timedelta(hours=1)).isoformat()
    card = {'status': 'final', 'status_code': code, 'kickoff_utc': future}
    assert status_code(card) == code
    assert bettable_before_kickoff(card, NOW) == (code in ('scheduled', 'tbd', 'unknown', 'unspecified'))
    card.pop('status_code')
    assert not bettable_before_kickoff(card, NOW)
    card['status'] = 'scheduled'
    assert bettable_before_kickoff(card, NOW)
    card['kickoff_utc'] = NOW.isoformat()
    assert not bettable_before_kickoff(card, NOW)
    card['kickoff_utc'] = 'invalid'
    assert not bettable_before_kickoff(card, NOW)


@pytest.mark.parametrize('status', ['live', 'suspended', 'in progress', 'in_play', 'halftime', 'Final/OT'])
def test_week_selection_drift_is_fixed(status):
    def game(week, hours, raw):
        return Game(f'nfl:2026:{week}:a@b', 'nfl', 2026, week, NOW + timedelta(hours=hours), NOW, 'UTC', 'b', 'a', None, status=raw)
    assert json_out.current_week([game(10, 1, status), game(11, 5, 'scheduled')], NOW) == 11
