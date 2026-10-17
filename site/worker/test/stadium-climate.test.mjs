import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {handleFetch} from '../index.js';
import {stadiumClimatePayload} from '../stadium-climate.js';

const MAX_BYTES = 200_000;

test('climate endpoint is authenticated GET-only and does not read R2 or D1', async () => {
  const env = {BOARD_PASSWORD: 'fixture', ODDS: {get() { throw new Error('No board reads allowed'); }}};
  const request = (method = 'GET', auth = true) => new Request('https://fixture.invalid/api/stadium-climate', {
    method, headers: auth ? {Authorization: 'Basic ' + Buffer.from('reader:fixture').toString('base64')} : {},
  });
  assert.equal((await handleFetch(request('GET', false), env)).status, 401);
  assert.equal((await handleFetch(request('POST'), env)).status, 405);
  const response = await handleFetch(request(), env);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.kind, 'stadium_game_window_climate');
  assert.equal(body.cell_columns.includes('n'), true);
  const hasRankKey = value => Array.isArray(value) ? value.some(hasRankKey)
    : value && typeof value === 'object' ? Object.keys(value).includes('rank') || Object.values(value).some(hasRankKey)
    : false;
  assert.equal(hasRankKey(body), false);
});

test('committed climate JSON stays under the size cap', () => {
  const bytes = readFileSync(new URL('../../../data/raw/stadium_game_climate.json', import.meta.url));
  assert.ok(bytes.length <= MAX_BYTES, `climate JSON is ${bytes.length} bytes`);
  assert.ok(bytes.length > 20_000);
  const payload = stadiumClimatePayload();
  assert.equal(payload.seasons[0], 2018);
  assert.equal(payload.seasons[1], 2025);
  assert.equal(payload.methodology.totals.includes('win-loss'), true);
});

function climateUi(payload, game) {
  const ctx = vm.createContext({
    console,
    document: {addEventListener() {}},
    esc: value => String(value ?? ''),
  });
  vm.runInContext(readFileSync(new URL('../../web/stadium-climate.js', import.meta.url), 'utf8'), ctx);
  ctx.payload = payload;
  ctx.game = game;
  vm.runInContext('STADIUM_CLIMATE.data = payload', ctx);
  return code => vm.runInContext(code, ctx);
}

function samplePayload() {
  return {
    schema_version: 1,
    kind: 'stadium_game_window_climate',
    seasons: [2018, 2025],
    windows: ['early', 'afternoon', 'prime', 'late'],
    window_hours_local: [[0, 15], [15, 18], [18, 21], [21, 24]],
    window_labels: ['before 3pm', '3-6pm', '6-9pm', 'after 9pm'],
    cell_columns: ['month', 'window', 'n', 'wind_p10', 'wind_p50', 'wind_p90', 'temp_p10', 'temp_p50', 'temp_p90', 'precip_p50', 'precip_p90', 'wind_ge12', 'wind_ge15', 'rain_ge5', 'rain_ge23'],
    small_sample_n: 8,
    methodology: {weather: 'ERA5 grid, not a field sensor.', totals: 'No betting totals, prices, or win-loss records.'},
    venues: {
      'lambeau-field': {
        n: 9,
        seasons: [2018, 2019, 2024],
        rows: [
          [12, 3, 2, 18.0, 20.0, 22.0, 20, 25, 30, 10.0, 24.0, 2, 2, 2, 1],
          [9, 0, 7, 8.0, 11.0, 14.0, 48, 55, 62, 0.0, 1.2, 2, 0, 1, 0],
        ],
      },
    },
  };
}

test('climate rows keep season order, show N, and do not rank a thin windy cell', () => {
  const source = readFileSync(new URL('../../web/stadium-climate.js', import.meta.url), 'utf8');
  assert.equal(/sort\([^)]*wind/.test(source), false);
  const run = climateUi(samplePayload(), {
    kickoff_utc: '2024-10-13T17:00:00Z',
    stadium: {stadium_id: 'lambeau-field', roof_type: 'open', timezone: 'America/Chicago'},
  });
  const view = run('stadiumClimateView(game)');
  assert.equal(view.status, 'ok');
  assert.match(view.intro, /Do not rank venues by thin cells/);
  assert.match(view.intro, /N is the number of games/);
  const measured = view.rows.filter(row => row.n > 0);
  assert.deepEqual(measured.map(row => row.month), [9, 12]);
  assert.equal(measured[0].n, 7);
  assert.match(measured[0].value, /N=7/);
  assert.match(measured[0].value, /wind ≥15 mph 0 of 7/);
  assert.match(measured[1].value, /N=2/);
  assert.match(measured[1].value, /small sample; not for ranking venues/);
  assert.equal(measured[1].thin, true);
  assert.equal(measured[0].thin, true);
  const html = run('stadiumClimateDetails(game)');
  assert.match(html, /Stadium game-window climate/);
  assert.ok(html.indexOf('September') < html.indexOf('December'));
  const parts = run("climateLocalParts('2024-01-14T01:20:00Z', 'America/Los_Angeles')");
  assert.equal(parts.month, 1);
  assert.equal(parts.hour, 17);
  assert.equal(run('climateWindowIndex(17, game && STADIUM_CLIMATE.data.window_hours_local)'), 1);
});

test('the current window shows N=0 instead of borrowing another cell, and domes stay unlabeled', () => {
  const run = climateUi(samplePayload(), {
    kickoff_utc: '2024-10-13T23:30:00Z',
    stadium: {stadium_id: 'lambeau-field', roof_type: 'open', timezone: 'America/Chicago'},
  });
  const view = run('stadiumClimateView(game)');
  assert.equal(view.rows[0].n, 0);
  assert.match(view.rows[0].value, /N=0/);
  assert.match(view.rows[0].label, /October/);
  const compact = run('stadiumClimateCompact(game)');
  assert.match(compact, /stadium-climate-full/);
  assert.match(compact, /stadium-climate-short/);
  assert.match(compact, /N=0/);
  assert.match(compact, /stadium-climate-short">[^<]*N=0/);
  run("game = {stadium: {stadium_id: 'ford-field', roof_type: 'dome', timezone: 'America/Detroit'}}");
  const dome = run('stadiumClimateView(game)');
  assert.equal(dome.status, 'dome');
  assert.equal(dome.rows.length, 0);
  assert.match(dome.intro, /Dome/);
  run("game = {stadium: {stadium_id: 'state-farm-stadium', roof_type: 'retractable', timezone: 'America/Phoenix'}}");
  assert.equal(run('stadiumClimateView(game).status'), 'retractable-none');
});

test('small-sample threshold comes from the payload', () => {
  const payload = samplePayload();
  payload.small_sample_n = 2;
  payload.venues['lambeau-field'].rows[1][2] = 2;
  const run = climateUi(payload, {
    kickoff_utc: '2024-09-15T17:00:00Z',
    stadium: {stadium_id: 'lambeau-field', roof_type: 'open', timezone: 'America/Chicago'},
  });
  const view = run('stadiumClimateView(game)');
  const september = view.rows.find(row => row.month === 9 && row.n === 2);
  assert.equal(september.thin, false);
  assert.doesNotMatch(september.value, /small sample/);
  assert.match(view.intro, /Do not rank venues by thin cells/);
});
