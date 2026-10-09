"""Directional wind cohorts graded against closing totals; no network or messages.

python -m pipeline.directional_backtest --input games.parquet --output direction.json
Forecasts and retrospective historical-model weather are never pooled. Every group
contains one row per game, and ROI assumes a one-unit UNDER at -110 at the close.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import mean
from typing import Any

from utils.timeutil import parse_iso

COMPASS = {name: i * 22.5 for i, name in enumerate(
    ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'))}
DIRECTIONS = ('crosswind', 'along_field', 'oblique')


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def degrees(value: Any) -> float | None:
    n = number(value)
    if n is not None:
        return n % 360
    return COMPASS.get(str(value).strip().upper())


def components(wind: float, direction: float, orientation: float) -> tuple[float, float]:
    angle = math.radians(direction - orientation)
    return abs(wind * math.sin(angle)), abs(wind * math.cos(angle))


def direction_group(cross: float, along: float) -> str:
    if cross >= 2 * along:
        return 'crosswind'
    if along >= 2 * cross:
        return 'along_field'
    return 'oblique'


def wilson(wins: int, n: int) -> tuple[float | None, float | None]:
    if not n:
        return None, None
    z, p = 1.959963984540054, wins / n
    scale = 1 + z * z / n
    center = (p + z * z / (2 * n)) / scale
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / scale
    return max(0.0, center - half), min(1.0, center + half)


def stats(rows: list[dict]) -> dict:
    counts = Counter(r['result'] for r in rows)
    w, losses, p = (counts[x] for x in ('W', 'L', 'P'))
    lo, hi = wilson(w, w + losses)
    moves = [r['move'] for r in rows if r['move'] is not None]
    return {'n': len(rows), 'wins': w, 'losses': losses, 'pushes': p,
            'win_pct': w / (w + losses) if w + losses else None, 'win_ci_low': lo, 'win_ci_high': hi,
            'roi': (w * 100 / 110 - losses) / len(rows) if rows else None,
            'margin': mean(r['margin'] for r in rows) if rows else None,
            'avg_move': mean(moves) if moves else None, 'move_n': len(moves),
            'avg_wind': mean(r['wind'] for r in rows) if rows else None,
            'stadiums': len({r['stadium_id'] for r in rows}),
            'small_sample': w + losses < 30}


def signal_eligible(sport: str, wind: float, temp: float | None, spread: float | None) -> bool:
    if temp is None:
        return False
    if sport == 'cfb':
        return wind > 14 and temp < 70 and spread is not None and abs(spread) <= 10
    return sport == 'nfl' and wind > 15 and temp < 60


def wind_band(wind: float) -> str:
    return '14–20' if wind < 20 else '20–25' if wind < 25 else '25+'


def forecast_hours(row: Mapping) -> float | None:
    try:
        if not row.get('forecast_at') or not row.get('kickoff_utc'):
            return None
        return (parse_iso(row['kickoff_utc']) - parse_iso(row['forecast_at'])).total_seconds() / 3600
    except (ValueError, TypeError, AttributeError):
        return None


def matched_comparison(rows: list[dict], basis: str, sport: str, cohort: str) -> dict:
    strata: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if (r['direction'] not in ('crosswind', 'along_field') or r['temp'] is None
                or r['spread_open'] is None or r['rain'] is None or r['season'] is None):
            continue
        rain = r['rain']
        key = (r['season'], math.floor(r['wind'] / 5), math.floor(r['temp'] / 10),
               math.floor(abs(r['spread_open']) / 10), 0 if rain == 0 else 1 if rain <= 2 else 2 if rain <= 6 else 3)
        strata[key][r['direction']].append(r)
    matched = [s for s in strata.values() if s['crosswind'] and s['along_field']]
    weights = [2 * len(s['crosswind']) * len(s['along_field']) / (len(s['crosswind']) + len(s['along_field'])) for s in matched]
    margin_diffs = [mean(r['margin'] for r in s['crosswind']) - mean(r['margin'] for r in s['along_field']) for s in matched]
    resolved = [(s, w) for s, w in zip(matched, weights, strict=True)
                if any(r['result'] != 'P' for r in s['crosswind']) and any(r['result'] != 'P' for r in s['along_field'])]
    def win_rate(rs: list[dict]) -> float:
        return sum(r['result'] == 'W' for r in rs) / sum(r['result'] != 'P' for r in rs)
    return {'basis': basis, 'sport': sport, 'cohort': cohort, 'season': 'all', 'strata': len(matched),
            'matched_cross_n': sum(len(s['crosswind']) for s in matched),
            'matched_along_n': sum(len(s['along_field']) for s in matched),
            'margin_difference': sum(d * w for d, w in zip(margin_diffs, weights, strict=True)) / sum(weights) if weights else None,
            'win_pct_difference': sum((win_rate(s['crosswind']) - win_rate(s['along_field'])) * w for s, w in resolved) / sum(w for _, w in resolved) if resolved else None}


def build_report(inputs: Iterable[Mapping[str, Any]]) -> dict:
    rows = [dict(r) for r in inputs]
    seen, audit, coverage, groups, comparisons = set(), [], [], [], []
    duplicates = 0
    unique = []
    for r in rows:
        gid = r.get('game_id')
        if gid and gid in seen:
            duplicates += 1
            continue
        if gid:
            seen.add(gid)
        unique.append(r)
    for basis in ('forecast', 'actual'):
        suffix = 'fc' if basis == 'forecast' else 'act'
        for sport in ('cfb', 'nfl'):
            candidates = [r for r in unique if r.get('sport') == sport]
            excluded: Counter = Counter()
            included = []
            for r in candidates:
                wind, temp = number(r.get(f'wind_{suffix}')), number(r.get(f'temp_{suffix}'))
                direction = degrees(r.get(f'wind_dir_deg_{suffix}'))
                if direction is None:
                    direction = degrees(r.get(f'wind_dir_{suffix}'))
                orientation = number(r.get('orientation_deg'))
                total, points = number(r.get('total_close')), number(r.get('actual_total'))
                roof = r.get('roof_state')
                lead = number(r.get('lead_fc'))
                timestamp_lead = forecast_hours(r) if basis == 'forecast' else None
                reason = ('missing_game_id' if not r.get('game_id') else
                          'roof_closed_or_unknown' if roof not in ('outdoors', 'open') else
                          'ungraded_or_missing_close' if total is None or points is None else
                          'missing_orientation' if orientation is None else
                          'missing_wind' if wind is None else
                          'below_wind_threshold' if wind <= 14 else
                          'missing_direction' if direction is None else
                          'forecast_timestamp_missing_invalid_or_postgame' if basis == 'forecast' and (timestamp_lead is None or timestamp_lead <= 0) else
                          'forecast_lead_unknown_or_outside_7d' if basis == 'forecast' and (lead is None or not 0 <= lead <= 168 or timestamp_lead > 168) else None)
                if reason:
                    excluded[reason] += 1
                    continue
                cross, along = components(wind, direction, orientation)
                margin = total - points
                opening = number(r.get('total_open'))
                spread = number(r.get('spread_open'))
                item = {'game_id': r['game_id'], 'basis': basis, 'sport': sport, 'season': r.get('season'),
                        'stadium_id': r.get('stadium_id'), 'kickoff_utc': r.get('kickoff_utc'),
                        'wind': wind, 'temp': temp, 'rain': number(r.get(f'rain_{suffix}')),
                        'spread_open': spread, 'direction_deg': direction, 'orientation_deg': orientation,
                        'cross_mph': cross, 'along_mph': along, 'direction': direction_group(cross, along),
                        'wind_band': wind_band(wind), 'signal': signal_eligible(sport, wind, temp, spread),
                        'total_close': total, 'actual_total': points, 'margin': margin,
                        'move': opening - total if opening is not None else None,
                        'result': 'W' if margin > 0 else 'L' if margin < 0 else 'P',
                        'lead_fc': lead if basis == 'forecast' else None,
                        'source': r.get('src_forecast' if basis == 'forecast' else 'src_actual')}
                item['opening_spread_source'] = r.get('opening_spread_source') or r.get('ref_book')
                item['venue_source'] = r.get('venue_source')
                for key in ('closing_total_source', 'close_at', 'close_lead_h', 'source_timestamp', 'source_commit_at'):
                    item[key] = r.get(key)
                included.append(item)
                audit.append(item)
            seasons = sorted({r['season'] for r in included if r['season'] is not None})
            coverage.append({'basis': basis, 'sport': sport, 'input_games': len(candidates),
                             'included_games': len(included), 'excluded': dict(excluded), 'seasons': seasons})
            for cohort in ('signal', 'all_windy'):
                members = [r for r in included if cohort != 'signal' or r['signal']]
                comparisons.append(matched_comparison(members, basis, sport, cohort))
                for season in ['all', *seasons]:
                    season_rows = [r for r in members if season == 'all' or r['season'] == season]
                    for band in ('all', '14–20', '20–25', '25+'):
                        for direction in DIRECTIONS:
                            subset = [r for r in season_rows if r['direction'] == direction and (band == 'all' or r['wind_band'] == band)]
                            groups.append({'basis': basis, 'sport': sport, 'cohort': cohort, 'season': season,
                                           'wind_band': band, 'direction': direction, **stats(subset)})
    return {'schema_version': 1, 'methodology': {
        'forecast': 'Last pregame forecast within 7 days; grade UNDER against closing total, not a first-alert execution backtest.',
        'actual': 'Retrospective historical-model weather at game time; descriptive, not a forecast betting record.',
        'weather_window': 'Kickoff UTC hour and following two hourly samples, matching the existing backtest; model estimates are not stadium measurements. Source is retained per game.',
        'direction': 'Crosswind when cross >= 2 × along; along-field when along >= 2 × cross; otherwise oblique. Head/tail combined.',
        'components': 'Absolute wind × sin/cos(wind direction minus field orientation); model grid wind, not measured inside stadium.',
        'cohorts': 'Signal: CFB wind>14 mph, temp<70°F, |opening spread|<=10; NFL wind>15 mph, temp<60°F. All windy: wind>14 mph.',
        'grading': 'Closing-total UNDER at assumed -110, one unit per game; pushes return stake. Win rate excludes pushes; ROI includes them.',
        'closing_sources': 'Historical CFB uses the last archived pregame quote within 6 hours as a closing proxy; historical NFL uses nflverse total_line when available. See each game\'s source and quote time. Modern rows use retained backtest closings.',
        'margin': 'Closing total minus final points; positive favors UNDER.',
        'movement': 'Opening total minus retained close, not alert-entry CLV. Historical NFL may combine an archived opener with the nflverse close rather than the same book.',
        'uncertainty': '95% Wilson intervals on non-push win rates; fewer than 30 resolved games flagged. Stadium/season dependence is not modeled.',
        'matching': 'Crosswind minus along-field, matched within sport, season, 5 mph wind, 10°F temperature, 10-point absolute opening spread, and rain bands; harmonic sample weights. Observational, not causal.',
        'selection': 'Unknown/closed roofs, missing direction/orientation, ungraded games, and unknown/stale forecast leads excluded; no actual direction substituted for missing forecast direction.'
    }, 'duplicate_games_excluded': duplicates, 'coverage': coverage, 'groups': groups, 'comparisons': comparisons, 'games': audit}


def load_rows(path: Path) -> list[dict]:
    if path.suffix == '.parquet':
        import pandas as pd
        return json.loads(pd.read_parquet(path).to_json(orient='records'))
    data = json.loads(path.read_text(encoding='utf-8'))
    return data if isinstance(data, list) else data.get('games', [])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = build_report(load_rows(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2), encoding='utf-8')
    print(json.dumps(report['coverage'], indent=2))


if __name__ == '__main__':
    main()
