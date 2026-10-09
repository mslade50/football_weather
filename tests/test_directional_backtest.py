import math

import pytest

from pipeline import directional_backtest as d


def game(gid='a', **kwargs):
    return dict(game_id=gid, sport='cfb', season=2025, stadium_id='field', roof_state='outdoors',
                orientation_deg=0, wind_fc=18, wind_dir_fc='E', temp_fc=55, rain_fc=0,
                forecast_at='2025-09-01T15:00:00Z', kickoff_utc='2025-09-01T18:00:00Z',
                lead_fc=3, total_close=45, total_open=48, actual_total=42, spread_open=7,
                **kwargs)


def group(report, direction='crosswind', **kwargs):
    expected = dict(basis='forecast', sport='cfb', cohort='signal', season='all', wind_band='all', direction=direction)
    expected.update(kwargs)
    return next(g for g in report['groups'] if all(g[k] == v for k, v in expected.items()))


def changed(gid='a', **kwargs):
    row = game(gid)
    row.update(kwargs)
    return row


def test_components_respect_field_axis_reversal_and_compass_wrap():
    assert d.components(20, 90, 0) == pytest.approx((20, 0), abs=1e-10)
    assert d.components(20, 90, 180) == pytest.approx((20, 0), abs=1e-10)
    assert d.components(20, 180, 0) == pytest.approx((0, 20), abs=1e-10)
    assert d.components(20, 45, 0) == pytest.approx((math.sqrt(200), math.sqrt(200)))
    assert d.degrees('nnw') == 337.5
    assert d.degrees(360) == 0
    assert d.direction_group(15, 7.5) == 'crosswind'
    assert d.direction_group(7.5, 15) == 'along_field'
    assert d.direction_group(10, 10) == 'oblique'


def test_grade_push_and_real_denominators():
    report = d.build_report([game('win'), changed('loss', actual_total=50), changed('push', actual_total=45)])
    g = group(report)
    assert (g['n'], g['wins'], g['losses'], g['pushes']) == (3, 1, 1, 1)
    assert g['win_pct'] == .5
    assert g['roi'] == pytest.approx((100/110 - 1) / 3)
    assert g['margin'] == pytest.approx(-2/3)
    assert g['avg_move'] == 3
    assert g['win_ci_low'] < .5 < g['win_ci_high']


def test_missing_forecast_direction_does_not_use_actual_and_reports_exclusions():
    report = d.build_report([changed(wind_dir_fc=None, wind_act=18, wind_dir_act='E', temp_act=55, rain_act=0),
                             changed('closed', roof_state='closed'), changed('unknown', orientation_deg=None)])
    assert group(report)['n'] == 0
    assert group(report, basis='actual')['n'] == 1
    coverage = next(c for c in report['coverage'] if c['basis'] == 'forecast' and c['sport'] == 'cfb')
    assert coverage['excluded'] == dict(missing_direction=1, roof_closed_or_unknown=1, missing_orientation=1)


def test_no_postgame_or_stale_forecasts_and_no_duplicate_bets():
    report = d.build_report([game(), game(), changed('late', forecast_at='2025-10-01T20:00:00Z', kickoff_utc='2025-10-01T19:00:00Z'),
                             changed('old', lead_fc=169), changed('missing', lead_fc=None),
                             changed('no_timestamp', forecast_at=None), changed('no_kickoff', kickoff_utc=None),
                             changed('invalid_timestamp', forecast_at='invalid'),
                             changed('stale_timestamp', forecast_at='2025-08-01T15:00:00Z', lead_fc=3)])
    assert group(report)['n'] == 1
    assert report['duplicate_games_excluded'] == 1
    assert group(report, season=2025)['n'] == 1


def test_signal_boundaries_and_actual_signal_uses_actual_weather():
    assert d.signal_eligible('cfb', 14.01, 69.9, -10)
    assert not d.signal_eligible('cfb', 14, 69, 7)
    assert not d.signal_eligible('cfb', 20, 70, 7)
    assert not d.signal_eligible('cfb', 20, 65, 10.01)
    assert d.signal_eligible('nfl', 15.01, 59.9, None)
    assert not d.signal_eligible('nfl', 15, 50, 7)
    report = d.build_report([changed(temp_fc=75, temp_act=55, wind_act=18, wind_dir_act='E', rain_act=0)])
    assert group(report)['n'] == 0
    assert group(report, cohort='all_windy')['n'] == 1
    assert group(report, basis='actual')['n'] == 1


def test_matched_comparison_does_not_compare_different_seasons_or_wind_bands():
    report = d.build_report([game('cross'), changed('along', wind_dir_fc='N', actual_total=50),
                             changed('strong', wind_dir_fc='N', wind_fc=26, actual_total=20),
                             changed('other_year', wind_dir_fc='N', season=2024, actual_total=20)])
    comparison = next(c for c in report['comparisons'] if c['basis'] == 'forecast' and c['sport'] == 'cfb' and c['cohort'] == 'signal')
    assert comparison['strata'] == 1
    assert comparison['matched_cross_n'] == comparison['matched_along_n'] == 1
    assert comparison['margin_difference'] == 8
    assert comparison['win_pct_difference'] == 1


def test_numeric_direction_and_zero_orientation_are_retained_in_backtest_rows():
    from datetime import datetime, timezone

    from pipeline.backtest import apply_final_weather, row_from_snapshots, rows_from_games
    row = row_from_snapshots('cfb:2025:1:a@b', [(datetime(2025, 9, 1, tzinfo=timezone.utc), {
        'game_id': 'cfb:2025:1:a@b', 'kickoff_utc': '2025-09-01T18:00:00Z',
        'stadium': {'orient_deg': 0}, 'weather': {'wind_dir_deg': 91.25}})])
    assert row.orientation_deg == 0
    assert row.wind_dir_deg_fc == 91.25
    assert row.forecast_at == '2025-09-01T00:00:00Z'
    rows = rows_from_games([{'game_id': 'nfl:2025:1:a@b', 'stadium_id': 'field', 'kickoff_utc': '2025-09-01T18:00:00Z'}],
                           [{'stadium_id': 'field', 'orientation_deg': 0}])
    apply_final_weather(rows, [{'game_id': 'nfl:2025:1:a@b', 'fetched_at': '2025-09-01T17:00:00Z', 'wind_dir_deg': 0}])
    assert rows['nfl:2025:1:a@b'].orientation_deg == 0
    assert rows['nfl:2025:1:a@b'].wind_dir_deg_fc == 0


def test_historical_input_integration_and_parquet_round_trip(tmp_path):
    import json

    import pandas as pd

    from pipeline.backtest import assemble, write_outputs
    report = assemble([], [], directional_rows=[game()])
    write_outputs(report, board_dir=tmp_path / 'board', parquet_dir=tmp_path / 'parquet')
    payload = json.loads((tmp_path / 'board/backtest.json').read_text(encoding='utf-8'))
    assert group(payload['directional_wind'])['n'] == 1
    groups = pd.read_parquet(tmp_path / 'parquet/directional_groups.parquet')
    assert set(groups['season']) == {'all', '2025'}
    games = pd.read_parquet(tmp_path / 'parquet/directional_games.parquet')
    assert len(games) == 1
    assert games.iloc[0]['direction'] == 'crosswind'


def test_d1_forecast_does_not_fill_snapshot_direction_from_another_vintage():
    from pipeline.backtest import GameRow, apply_final_weather
    row = GameRow(game_id='a', sport='cfb', kickoff_utc='2025-09-01T18:00:00Z',
                  forecast_at='2025-09-01T15:00:00Z', wind_fc=18, temp_fc=55,
                  wind_dir_fc=None, wind_dir_deg_fc=None, src_forecast='snapshot:original')
    weather = [{'game_id': 'a', 'fetched_at': '2025-09-01T17:00:00Z',
                'wind_mph': 10, 'temp_f': 75, 'wind_dir_deg': 0}]
    assert apply_final_weather({'a': row}, weather) == 0
    assert row.wind_dir_deg_fc is None and row.wind_fc == 18
    # A postgame fallback is replaced with one coherent pregame D1 packet.
    row.forecast_at = '2025-09-01T19:00:00Z'
    assert apply_final_weather({'a': row}, weather) == 1
    assert (row.wind_fc, row.temp_fc, row.wind_dir_deg_fc) == (10, 75, 0)
    assert row.forecast_at == '2025-09-01T17:00:00Z'
    assert row.src_forecast.startswith('d1_weather:')
