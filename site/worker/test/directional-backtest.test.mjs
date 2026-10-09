import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function loadBacktest() {
  const ctx = vm.createContext({ Intl, Date });
  for (const name of ['app.js', 'backtest.js']) {
    vm.runInContext(readFileSync(new URL(`../../web/${name}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  }
  return ctx;
}

function group({ basis = 'forecast', sport = 'cfb', season = 2025, wind_band = 'all', direction = 'crosswind', n = 40 } = {}) {
  return { basis, sport, cohort: 'signal', season, wind_band, direction, n,
    wins: Math.floor(n / 2), losses: n - Math.floor(n / 2), pushes: 0, win_pct: 0.5,
    small_sample: n < 30,
    win_ci_low: 0.35, win_ci_high: 0.65, roi: -0.045, margin: 1.25,
    avg_move: 0.75, move_n: n - 2, avg_wind: 18.5 };
}

function report(groups, overrides = {}) {
  return { methodology: { forecast: 'Last pregame forecast.', actual: 'Game-time historical model.',
    direction: 'Crosswind uses stadium orientation; head/tail are combined.',
    grading: 'Closing-total under at assumed -110.', weather_window: 'Forecast is within seven days.' },
    coverage: [{ basis: 'forecast', sport: 'cfb', input_games: 12, included_games: 9,
      excluded: { missing_direction: 3 }, seasons: [2025, 2026] }], groups, comparisons: [], ...overrides };
}

function render(ctx, payload, { sport = '', basis = 'forecast', cohort = 'signal', season = 'all', band = 'all' } = {}) {
  ctx.payload = payload;
  ctx.sportFilter = sport;
  ctx.windBasis = basis;
  ctx.windCohort = cohort;
  ctx.windSeason = season;
  ctx.windBand = band;
  return vm.runInContext(`(() => {
    BT.windBasis = windBasis; BT.windCohort = windCohort; BT.windSeason = windSeason; BT.windBand = windBand;
    return directionalWindSectionHtml(normalizeBacktest(payload).directional_wind, sportFilter);
  })()`, ctx);
}

test('directional wind all-band groups render once per sport and direction with the sport visible', () => {
  const ctx = loadBacktest();
  const rows = [];
  for (const sport of ['cfb', 'nfl']) {
    for (const direction of ['crosswind', 'along_field', 'oblique']) {
      rows.push(group({ sport, season: 'all', direction, wind_band: 'all' }));
      rows.push(group({ sport, season: 'all', direction, wind_band: '14–20', n: 7 }));
      rows.push(group({ sport, season: 'all', direction, wind_band: '20–25', n: 11 }));
    }
  }
  const comparison = { basis: 'forecast', sport: 'cfb', cohort: 'signal', season: 'all',
    matched_cross_n: 22, matched_along_n: 19, margin_difference: 0.8, win_pct_difference: 0.03 };
  const html = render(ctx, { directional_wind: report(rows, { comparisons: [comparison] }) });
  const body = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>'));
  assert.equal((body.match(/<tr>/g) || []).length, 6);
  assert.equal((body.match(/<td class="left">CFB<\/td>/g) || []).length, 3);
  assert.equal((body.match(/<td class="left">NFL<\/td>/g) || []).length, 3);
  assert.match(body, /crosswind/);
  assert.doesNotMatch(body, /small sample/);
  assert.match(body, /38 of 40 games with both opener and close/);
  assert.match(html, /CFB matched comparison/);
});

test('directional wind filters apply season, global sport, actual basis, and wind band', () => {
  const ctx = loadBacktest();
  const rows = [
    group({ sport: 'cfb', season: 2025, direction: 'crosswind', wind_band: 'all' }),
    group({ sport: 'cfb', season: 2026, direction: 'along_field', wind_band: 'all' }),
    group({ sport: 'nfl', season: 2025, direction: 'oblique', wind_band: 'all' }),
    group({ basis: 'actual', sport: 'cfb', season: 2025, direction: 'along_field', wind_band: 'all' }),
    group({ basis: 'actual', sport: 'cfb', season: 2025, direction: 'crosswind', wind_band: '20–25' }),
  ];
  const comparison = { basis: 'forecast', sport: 'cfb', cohort: 'signal', season: 'all',
    matched_cross_n: 22, matched_along_n: 19, margin_difference: 0.8, win_pct_difference: 0.03 };
  const payload = { directional_wind: report(rows, { comparisons: [comparison] }) };
  let html = render(ctx, payload, { sport: 'cfb', season: '2025' });
  let body = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>'));
  assert.equal((body.match(/<tr>/g) || []).length, 1);
  assert.match(body, /crosswind/);
  assert.doesNotMatch(body, /NFL|along field/);
  assert.doesNotMatch(html, /CFB matched comparison/);

  html = render(ctx, payload, { sport: 'cfb', basis: 'actual', season: '2025' });
  body = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>'));
  assert.equal((body.match(/<tr>/g) || []).length, 1);
  assert.match(html, /Game-time historical model\./);
  assert.doesNotMatch(body, /crosswind/);
  assert.match(body, /along field/);

  html = render(ctx, payload, { sport: 'cfb', basis: 'actual', season: '2025', band: '20–25' });
  body = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>'));
  assert.equal((body.match(/<tr>/g) || []).length, 1);
  assert.match(body, /crosswind/);
  assert.doesNotMatch(html, /CFB matched comparison/);
});

test('directional wind escapes report text, marks small samples, and handles empty data', () => {
  const ctx = loadBacktest();
  const unsafe = group({ season: 'all', direction: '<img src=x onerror=alert(1)>', n: 40 });
  unsafe.wins = 10;
  unsafe.losses = 10;
  unsafe.pushes = 20;
  unsafe.small_sample = true;
  unsafe.move_n = 0;
  const payload = { directional_wind: report([unsafe], {
    methodology: { forecast: 'Forecast <unsafe>.', direction: 'Direction method.', grading: 'Grading method.',
      weather_window: 'Forecast is within seven days.' },
    coverage: [{ basis: 'forecast', sport: '<script>', input_games: 4, included_games: 1, excluded: { '<bad>': 3 } }],
  }) };
  const html = render(ctx, payload);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;SCRIPT&gt;/);
  assert.match(html, /&lt;bad&gt;: 3/);
  assert.doesNotMatch(html, /<script>|<img src=x/);
  assert.match(html, /small sample/);
  assert.match(html, /0 of 40 games with both opener and close/);
  assert.match(html, /Full methodology and exclusions/);
  assert.match(html, /Forecast is within seven days\./);

  delete unsafe.small_sample;
  const fallback = render(ctx, { directional_wind: report([unsafe], { methodology: payload.directional_wind.methodology }) });
  assert.match(fallback, /small sample/); // Fallback uses W+L, excluding pushes, not total n.

  const empty = render(ctx, { directional_wind: { methodology: {}, coverage: [], groups: [], comparisons: [] } });
  assert.match(empty, /No directional-wind samples for these filters\./);
  assert.match(empty, /No coverage report for this sport and weather basis\./);
});
