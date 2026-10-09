import json

import pytest

from pipeline import state
from pipeline.outputs import d1_out, json_out

GID = 'cfb:2026:2:maine@nevada'
KEY = state.odds_key(GID, 'total', 'under', 'pinnacle')


def test_legacy_overwritten_first_migrates_to_reference_without_invented_original():
    prior = {'schema_version': 1, 'openers': {KEY: {'line': 50, 'odds': -110,
             'ts': '2026-09-01T00:00:00Z', 'basis': 't_minus_6d'}}}
    migrated = state.migrate(prior, 'openers')
    assert migrated['openers'] == {}
    assert migrated['references']['t_minus_6d'][KEY]['line'] == 50
    assert migrated['true_openers'] == {}
    assert prior['openers'][KEY]['basis'] == 't_minus_6d'
    missing = json_out.odds_block(GID, [], migrated)['pinnacle']['total']
    assert missing['open_line'] == 50 and missing['open_basis'] == 't_minus_6d'
    assert missing['line'] is None


def test_true_opening_requires_original_time_and_named_evidence_and_never_overwrites():
    op = state.migrate(None, 'openers')
    quote = {'game_id': GID, 'market': 'total', 'side': 'under', 'book': 'pinnacle',
             'line': 48, 'odds': -110, 'scraped_at': '2026-09-02T12:01:00Z',
             'opening_line': 55, 'opening_odds': -105, 'opened_at': '2026-09-01T12:00:00Z'}
    state.record_openers(op, [quote], '2026-09-02T12:00:00Z')
    assert op['true_openers'] == {}  # Typed values without provenance are insufficient.
    assert op['openers'][KEY]['ts'] == quote['scraped_at']  # Receipt may follow run start.
    quote['opening_source'] = 'fixture:original-market-event'
    state.record_openers(op, [quote], '2026-09-02T13:00:00Z')
    assert op['true_openers'][KEY]['line'] == 55
    assert state.get_baseline(op, KEY)['basis'] == 'true_opener'
    quote.update(opening_line=54, scraped_at='2026-09-02T14:00:00Z')
    state.record_openers(op, [quote], '2026-09-02T15:00:00Z')
    quote['scraped_at'] = '2026-09-02T16:00:00Z'
    state.record_openers(op, [quote], '2026-09-02T17:00:00Z')
    assert op['true_openers'][KEY]['line'] == 55
    assert len(op['true_opener_conflicts'][KEY]) == 1
    assert op['openers'][KEY]['line'] == 48
    assert state.prune_openers(op, []) == 0
    sql = d1_out.build_statements(openers=d1_out.opener_rows(op, [KEY], 'fixture'))
    assert sql and 'INSERT OR IGNORE' in sql[0] and 'ON CONFLICT' not in sql[0]


def test_failed_atomic_state_save_does_not_destroy_prior_baselines(tmp_path):
    prior = state.migrate(None, 'openers')
    state.save_openers(tmp_path, prior)
    path = tmp_path / state.OPENERS_FILE
    before = path.read_bytes()
    with pytest.raises(ValueError):
        state.save_openers(tmp_path, {**prior, 'openers': {KEY: {'line': float('nan')}}})
    assert path.read_bytes() == before
    assert json.loads(before)['schema_version'] == 2
