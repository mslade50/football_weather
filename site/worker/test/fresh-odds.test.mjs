import test from 'node:test';
import assert from 'node:assert/strict';
import { freshOddsRoute } from '../fresh-odds.js';
import { handleFetch } from '../index.js';

const eventId = '01a0caf2-e862-7da0-9a40-1eaaf5b531e9', marketId = '01a0eb45-b723-7103-b452-71a27addf9be';
const over = '01a0eb45-b723-7103-b452-71becb7e2b2a', under = '01a0eb45-b723-7103-b452-71c486830299';
const kickoff = '2030-10-01T17:00:00Z';
const ref = { book: 'novig', line: 46.5, source_id: `${eventId}:${marketId}`, outcome_ids: { over, under } };
const game = { game_id: 'nfl:2030:4:lar@phi', sport: 'nfl', kickoff_utc: kickoff, run_id: 'original-board-generation',
  weather: { fetched_at: '2020-01-01T00:00:00Z' }, execution_markets: [ref],
  total_prices: { quotes: [{ book: 'novig', line: 46.5, side: 'under', push_prob: 0, win_prob: .6,
    updated_at: '2020-01-01T00:00:00Z' }] } };
const url = `https://board.test/api/fresh-odds?game_id=${encodeURIComponent(game.game_id)}`;
function environment(row = game) {
  return { BOARD_PASSWORD: 'viewer', ODDS: {
    get: async key => { assert.equal(key, 'board/games_nfl.json'); return { json: async () => [row] }; },
    put: () => assert.fail('Fresh odds must not publish') },
    GH_DISPATCH_TOKEN: 'fake-dispatch-token', DB: { prepare: () => assert.fail('No database or dispatch on quote path') } };
}
function exchangeFetch(uri, options) {
  const path = new URL(uri).pathname;
  assert.equal(new URL(uri).origin, 'https://api.novig.com');
  assert.equal(options.method, 'GET');
  assert.equal(options.headers.Authorization, undefined);
  if (path.endsWith('/book')) return Promise.resolve(Response.json({ marketId, seq: 5, orders: { [over]: [{ price: '.5', qty: 100000 }] } }));
  if (path.includes('/events/')) return Promise.resolve(Response.json({ eventId, sport: 'FOOTBALL', league: 'NFL', startsTs: Date.parse(kickoff), status: 'OPEN_PREGAME' }));
  return Promise.resolve(Response.json({ eventId, marketId, startsTs: Date.parse(kickoff), marketType: 'TOTAL', strike: '46.5', status: 'OPEN', voids: 'FMV',
    fee: { coefficient: '.03', charged: 'WHEN_LIVE' }, outcomes: [{ outcomeId: over, status: 'TBD' }, { outcomeId: under, status: 'TBD' }] }));
}

test('Fresh endpoint retrieves taker depth from old mappings without relabeling old model or quote clocks', async () => {
  const start = performance.now();
  const response = await freshOddsRoute(new Request(url), environment(), exchangeFetch);
  const result = await response.json();
  assert.equal(response.status, 200);
  assert.equal(result.fresh_quote_count, 1);
  assert.equal(result.cash_stake_capacity, 500);
  assert.equal(result.cash_liquidity_verified, true);
  assert.equal(result.board_run_id, game.run_id);
  assert.equal(result.model_observed_at, game.weather.fetched_at);
  assert.equal(result.quotes[0].quote_observed_at, '2020-01-01T00:00:00Z');
  assert.ok(Date.parse(result.quotes[0].depth_fetched_at) > Date.parse(result.model_observed_at));
  assert.equal(result.can_execute, false);
  assert.ok(performance.now() - start < 30000);
  assert.equal(response.headers.get('cache-control'), 'no-store');
});

test('Failed providers produce unknown partial depth rather than fabricated freshness or capacity', async () => {
  const response = await freshOddsRoute(new Request(url), environment(), async () => new Response('', { status: 429 }));
  const result = await response.json();
  assert.equal(result.partial, true);
  assert.equal(result.fresh_quote_count, 0);
  assert.equal(result.cash_liquidity_verified, false);
  assert.equal(result.capacity_status, 'unknown');
});

test('Authenticated viewers can read depth; writes, invalid games, unsupported strikes and started games fail closed', async () => {
  const env = environment();
  assert.equal((await handleFetch(new Request(url), env)).status, 401);
  const headers = { Authorization: `Basic ${Buffer.from('viewer:viewer').toString('base64')}` };
  assert.equal((await handleFetch(new Request(url, { method: 'POST', headers }), env)).status, 405);
  const original = globalThis.fetch;
  globalThis.fetch = exchangeFetch;
  try { assert.equal((await handleFetch(new Request(url, { headers }), env)).status, 200); }
  finally { globalThis.fetch = original; }
  assert.equal((await freshOddsRoute(new Request(`${url}&line=47`), env, () => assert.fail('bad strike fetched'))).status, 400);
  assert.equal((await freshOddsRoute(new Request(url.replace(encodeURIComponent(game.game_id), 'invalid')), env)).status, 400);
  assert.equal((await freshOddsRoute(new Request(url), environment({ ...game, kickoff_utc: '2020-01-01' }), () => assert.fail('started game fetched'))).status, 409);
});

test('An unresponsive provider cannot hold the quote request for sixty seconds', { timeout: 30000 }, async () => {
  const start = performance.now();
  // Deliberately ignore the supplied AbortSignal: the route still owns a hard
  // response deadline if an upstream implementation does not honor cancellation.
  const response = await freshOddsRoute(new Request(url), environment(), () => new Promise(() => {}));
  const elapsed = performance.now() - start;
  assert.equal(response.status, 503);
  const result = await response.json();
  assert.match(result.error, /deadline/);
  assert.equal(result.can_execute, false);
  assert.ok(elapsed < 30000, `response took ${elapsed}ms`);
});
