import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { exchangeRefresh } from '../exchange-refresh.js';

const kickoff = '2030-10-01T17:00:00Z';
const ticker = 'KXNFLTOTAL-30OCT01LARPHI-46';
const game = { game_id: 'nfl:2030:4:lar@phi', sport: 'nfl', kickoff_utc: kickoff, run_id: 'original-generation',
  consensus: { total_now: 46.5, total_open: 49.5 }, weather: { fetched_at: '2020-01-01T00:00:00Z' },
  execution_markets: [{ book: 'kalshi', line: 46.5, source_id: ticker }],
  total_prices: { quotes: [{ book: 'kalshi', line: 46.5, side: 'under', push_prob: 0, win_prob: .6,
    updated_at: '2020-01-01T00:00:00Z' }] } };
const meta = { run_id: game.run_id, git_sha: 'published-revision', last_updated: '2020-01-01T00:00:00Z' };
function environment(rows = [game]) {
  return { ODDS: { get: async key => ({ json: async () => key === 'board/meta.json' ? meta : rows }),
    put: () => assert.fail('No publication or state writes') }, DB: { prepare: () => assert.fail('No database writes') } };
}
function request() { return { sport: 'nfl', request_id: 'fixture-request', requested_at: new Date().toISOString() }; }
function fetcher(url, options) {
  assert.equal(new URL(url).origin, 'https://api.elections.kalshi.com');
  assert.equal(options.method, 'GET');
  assert.equal(options.headers.Authorization, undefined);
  if (String(url).includes('/series/')) return Promise.resolve(Response.json({ series: { fee_type: 'quadratic', fee_multiplier: 1 } }));
  if (String(url).includes('/orderbook')) return Promise.resolve(Response.json({ orderbook_fp: { yes_dollars: [['.5', '2000']] } }));
  return Promise.resolve(Response.json({ market: { ticker, status: 'active', market_type: 'binary', strike_type: 'greater',
    floor_strike: 46.5, notional_value_dollars: '1', title: 'Over 46.5 points', rules_primary: 'Full game includes OT.', rules_secondary: '48-hour postponement.' } }));
}

test('Direct refresh correlates request, preserves publication/model/source clocks and is accepted by UI', async () => {
  const req = request(), result = await exchangeRefresh(environment(), req, fetcher);
  assert.equal(result.request_id, req.request_id);
  assert.equal(result.delivery_mode, 'direct_public_depth');
  assert.equal(result.published, false);
  assert.equal(result.fresh_quote_count, 1);
  assert.equal(result.meta.last_updated, meta.last_updated);
  assert.equal(result.games[0].run_id, game.run_id);
  assert.equal(result.games[0].weather.fetched_at, game.weather.fetched_at);
  assert.equal(result.games[0].consensus.total_open, 49.5);
  const quote = result.games[0].total_prices.quotes[0];
  assert.equal(quote.quote_observed_at, '2020-01-01T00:00:00Z');
  assert.ok(Date.parse(quote.fetched_at) >= Date.parse(req.requested_at));
  assert.ok(Date.parse(result.completed_at) >= Date.parse(req.requested_at));
  const ctx = vm.createContext({ Date, payload: result, request: { id: req.request_id, sport: req.sport,
    scope: 'exchanges', started: Date.parse(req.requested_at) }, document: { getElementById: () => null } });
  vm.runInContext(readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  vm.runInContext('render = () => {}', ctx);
  assert.equal(vm.runInContext('applyRefresh(payload, request)', ctx), true);
});

test('Mixed generations, malformed requests and all-provider failures stay unavailable', async () => {
  await assert.rejects(exchangeRefresh(environment([{ ...game, run_id: 'other' }]), request(), fetcher), /generation mismatch/);
  await assert.rejects(exchangeRefresh(environment(), { ...request(), sport: 'all' }, fetcher), /selected sport/);
  await assert.rejects(exchangeRefresh(environment(), { ...request(), request_id: '../bad' }, fetcher), /request identity/);
  await assert.rejects(exchangeRefresh(environment(), request(), async () => new Response('', { status: 429 })), /No fresh exchange/);
});

test('A noncooperative upstream cannot exceed the refresh-owned response deadline', async () => {
  const started = performance.now();
  await assert.rejects(exchangeRefresh(environment(), request(), () => new Promise(() => {}), { deadlineMs: 100 }), /deadline/);
  assert.ok(performance.now() - started < 1000);
});
