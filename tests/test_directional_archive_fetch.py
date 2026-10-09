import json
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from scripts import prepare_directional_archive as archive
from utils.timeutil import UTC


def row(gid, lat=35, kickoff='2024-10-05T17:30:00Z'):
    return dict(game_id=gid, actual_total=40, roof_state='outdoors', lat=lat, lon=-84,
                kickoff_utc=kickoff, wind_act=None, temp_act=None, rain_act=None,
                wind_dir_deg_act=None, src_actual=None)


def fake_client(monkeypatch, payload, calls, status=200):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params):
            calls.append(params)
            return httpx.Response(status, json=payload, request=httpx.Request('GET', url))

    monkeypatch.setattr(archive.httpx, 'Client', Client)
    monkeypatch.setattr(archive.time, 'sleep', lambda _: None)


def hourly(wind=(15, 18, 21, 24)):
    return {'hourly': {'time': [f'2024-10-05T{h}:00' for h in (17, 18, 19, 20)],
                       'wind_speed_10m': list(wind), 'temperature_2m': [50, 53, 56, 59],
                       'wind_direction_10m': [350, 0, 10, 20], 'precipitation': [1, 2, 3, 4]}}


def test_batched_era5_keeps_each_kickoff_window_and_resumes_from_cache(monkeypatch, tmp_path):
    calls = []
    fake_client(monkeypatch, [hourly(), hourly()], calls)
    games = [row('a'), row('b', lat=36, kickoff='2024-10-05T18:30:00Z')]
    result = archive._fetch_actuals(games, tmp_path, 10)
    assert result['cached_actuals'] == 2 and len(calls) == 1
    assert calls[0]['models'] == 'era5' and calls[0]['latitude'] == '35.0,36.0'
    assert (games[0]['wind_act'], games[0]['rain_act']) == (18, 6)
    assert (games[1]['wind_act'], games[1]['rain_act']) == (21, 9)
    assert min(games[0]['wind_dir_deg_act'], 360 - games[0]['wind_dir_deg_act']) < 1e-8
    assert games[1]['wind_dir_deg_act'] == pytest.approx(10)
    assert games[0]['src_actual'] == games[1]['src_actual'] == 'ERA5'
    restored = [row('a'), row('b', lat=36, kickoff='2024-10-05T18:30:00Z')]
    archive._fetch_actuals(restored, tmp_path, 0)
    assert len(calls) == 1 and restored[0]['wind_act'] == 18


def test_partial_wind_is_retryable_and_location_mismatch_does_not_shift_games(monkeypatch, tmp_path):
    calls = []
    fake_client(monkeypatch, hourly(wind=(15, None, 21, 24)), calls)
    games = [row('a')]
    result = archive._fetch_actuals(games, tmp_path, 10)
    assert result['required_actual_missing'] == 1 and games[0]['wind_act'] is None
    assert json.loads((tmp_path / 'era5_actuals.json').read_text()) == {}
    fake_client(monkeypatch, [hourly()], calls)
    result = archive._fetch_actuals([row('a'), row('b', lat=36)], tmp_path, 10)
    assert result['response_location_mismatch'] == 1 and result['cached_actuals'] == 0


def test_night_game_matches_eastern_date_and_postgame_availability_is_excluded():
    book = SimpleNamespace(resolve_team=lambda sport, name, fuzzy: name.lower())
    game = SimpleNamespace(sport='cfb', away_id='duke', home_id='gt',
                           kickoff_utc=datetime(2024, 10, 6, 0, tzinfo=UTC))
    schedule = [{'game': game}]
    raw = {'Game': 'Duke @ GT', 'Date': 'Sat 10/5'}
    assert archive._candidate_for(raw, datetime(2024, 10, 5, 14, tzinfo=UTC), schedule, 'cfb', Counter(), book) is schedule[0]
    assert archive._candidate_for(raw, datetime(2024, 10, 6, 1, tzinfo=UTC), schedule, 'cfb', Counter(), book) is None
