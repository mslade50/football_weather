import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { allocateDepth, sliceCost, kalshiDepth, polymarketDepth, previewGame } from '../execution-preview.js';
import { handleFetch } from '../index.js';

const kickoff = '2030-10-02T01:00:00Z';
const kalshiId = 'KXNCAAFTOTAL-30OCT01UNTTLSA-58';
const polyId = 'tsc-cfb-ntx-tulsa-2030-10-01-total-57pt5';
const game = { game_id: 'cfb:2030:5:north-texas@tulsa', sport: 'cfb', status: 'scheduled', kickoff_utc: kickoff,
  execution_markets: [{ book: 'kalshi', source_id: kalshiId, line: 57.5 },
    { book: 'polymarket_us', source_id: polyId, line: 57.5 }] };
const kalshi = { ticker: kalshiId, status: 'active', market_type: 'binary', strike_type: 'greater',
  floor_strike: 57.5, notional_value_dollars: '1.0000', title: 'Over 57.5 points scored',
  rules_primary: 'More than 57.5 points.', rules_secondary: 'Postponement: 48 hours.' };
const poly = { slug: polyId, active: true, closed: false, ep3Status: 'OPEN',
  sportsMarketType: 'football_team_full_game_total', line: 57.5, gameStartTime: kickoff,
  description: 'Overtime is included if played. Postponement: two weeks.', feeCoefficient: .0695,
  minimumTradeQty: .01, marketSides: [{ description: 'Under', long: false, tradable: true }] };

function exchangeFetch(overrides = {}, calls = []) {
  return async (url, options) => {
    calls.push({ url: String(url), options });
    url = new URL(url).origin + new URL(url).pathname;
    let payload;
    if (url.includes('/series/')) payload = { series: { fee_type: 'quadratic_with_maker_fees', fee_multiplier: 1 } };
    else if (url.endsWith('/orderbook')) payload = { orderbook_fp: { yes_dollars: [['0.5100', '100.99'], ['0.4500', '1000']], no_dollars: [['0.99', '9000']] } };
    else if (url.includes('/market/slug/')) payload = { market: { ...poly, ...overrides.poly } };
    else if (url.endsWith('/book')) payload = { marketData: { marketSlug: polyId, state: 'MARKET_STATE_OPEN',
      bids: [{ px: { value: '0.50', currency: 'USD' }, qty: '1000' }],
      offers: [{ px: { value: '.01', currency: 'USD' }, qty: '9999' }], ...overrides.book } };
    else payload = { market: { ...kalshi, ...overrides.kalshi } };
    return Response.json(payload);
  };
}

test('Preview converts opposing bids to under asks and reads current venue fee coefficients', async () => {
  const calls = [], fetcher = exchangeFetch({}, calls);
  const k = await kalshiDepth(game.execution_markets[0], game, fetcher);
  const p = await polymarketDepth(game.execution_markets[1], game, fetcher);
  assert.equal(k.levels[0].price, .49);
  assert.equal(k.levels[0].quantity, 100); // fractional liquidity is conservatively rounded down
  assert.equal(p.levels[0].price, .5);
  assert.equal(p.coefficient, .0695);
  assert.equal(k.coefficient, .07);
  assert.ok(calls.every(c => c.options.method === 'GET' && !c.options.headers.Authorization));
});

test('Allocation walks fee-inclusive depth across venues without exceeding the budget', () => {
  // A cheaper headline price loses after fees: .49 + .10*.49*.51 = .51499 > .50.
  const result = allocateDepth([
    { book: 'a', coefficient: .1, levels: [{ price: .49, quantity: 1000 }] },
    { book: 'b', coefficient: 0, levels: [{ price: .5, quantity: 100 }] },
  ], 100, .99);
  assert.deepEqual(result.allocations.map(a => [a.book, a.quantity]), [['b', 100], ['a', 97]]);
  assert.equal(result.spend, 99.96);
  assert.equal(result.fees, 2.43);
  assert.equal(result.unspent, .04);
  assert.equal(result.payout_if_win, 197);
  assert.ok(Math.abs(result.average_price - 99.96 / 197) < 1e-12);
  assert.equal(sliceCost(.5, 100, .07).total, 51750000n);
});

test('Price ceiling, insufficient liquidity, empty books and fee rounding leave money unspent', () => {
  const venues = [{ book: 'kalshi', coefficient: .07, levels: [{ price: .5, quantity: 2 }] }];
  assert.equal(allocateDepth(venues, 500, .5).spend, 0);
  const r = allocateDepth(venues, 500, .52);
  assert.equal(r.spend, 1.04);
  assert.equal(r.unspent, 498.96);
  assert.equal(allocateDepth([], 500, .99).average_price, null);
  for (let cents = 100; cents < 5000; cents += 17) {
    const r = allocateDepth([{ ...venues[0], levels: [{ price: .3333, quantity: 100000 }] }], cents / 100, .99);
    assert.ok(r.spend <= cents / 100);
    assert.equal(Math.round((r.spend + r.unspent) * 100), cents);
  }
});

test('Preview isolates a failed venue, excludes different totals and never claims balances or execution', async () => {
  const result = await previewGame(game, { line: 57.5, budget: 500, maxPrice: .99 }, exchangeFetch({ kalshi: { status: 'closed' } }));
  assert.equal(result.mode, 'preview_only');
  assert.equal(result.can_execute, false);
  assert.equal(result.balances_checked, false);
  assert.ok(result.allocations.every(a => a.book === 'polymarket_us'));
  assert.equal(result.venues.filter(v => v.status === 'unavailable').length, 4);
  const other = await previewGame(game, { line: 58.5, budget: 500, maxPrice: .99 }, () => assert.fail('no matching markets'));
  assert.equal(other.spend, 0);
  await assert.rejects(previewGame({ ...game, kickoff_utc: '2020-01-01' }, { line: 57.5, budget: 500, maxPrice: .99 }), /Pre-game/);
});

test('Market validation rejects unknown fees, opposite outcomes, other kickoffs and malformed depth', async () => {
  for (const override of [{ feeCoefficient: null }, { feeCoefficient: -1 }, { line: 58.5 },
    { gameStartTime: '2030-10-03T01:00:00Z' }, { marketSides: [{ description: 'Under', long: true, tradable: true }] }]) {
    await assert.rejects(polymarketDepth(game.execution_markets[1], game, exchangeFetch({ poly: override })));
  }
  await assert.rejects(polymarketDepth(game.execution_markets[1], game, exchangeFetch({ book: { bids: [{ px: { value: '.4', currency: 'USD' }, qty: '-10' }] } })));
  await assert.rejects(kalshiDepth(game.execution_markets[0], game, exchangeFetch({ kalshi: { strike_type: 'less' } })));
  await assert.rejects(kalshiDepth(game.execution_markets[0], game, async () => Response.json({}, { headers: { age: '60' } })), /cached/);
});

test('Preview endpoint requires board auth, validates inputs, uses server mappings and rejects order writes', async () => {
  const env = { BOARD_PASSWORD: 'viewer', ODDS: { get: async () => ({ json: async () => [game] }) } };
  const auth = { Authorization: `Basic ${Buffer.from('user:viewer').toString('base64')}` };
  const url = `https://board.test/api/execution-preview?game_id=${encodeURIComponent(game.game_id)}&line=57.5&budget=500&max_price=.99`;
  assert.equal((await handleFetch(new Request(url), env)).status, 401);
  assert.equal((await handleFetch(new Request(url, { headers: auth, method: 'POST' }), env)).status, 405);
  for (const bad of ['budget=NaN', 'budget=-500', 'budget=10001', 'line=57', 'max_price=1.1']) {
    const u = new URL(url), [key, value] = bad.split('=');
    u.searchParams.set(key, value);
    assert.equal((await handleFetch(new Request(u, { headers: auth }), env)).status, 400);
  }
  const original = globalThis.fetch;
  globalThis.fetch = exchangeFetch();
  try {
    const response = await handleFetch(new Request(url, { headers: auth }), env);
    const result = await response.json();
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual([...new Set(result.allocations.map(a => a.book))], ['kalshi', 'polymarket_us']);
    assert.ok(result.spend <= 500);
  } finally { globalThis.fetch = original; }
});

test('Preview UI defaults to $500, exact totals, escapes venue errors, and has no execution button', () => {
  const ctx = vm.createContext({ Date, fmtOdds: String, fmtET: String, bookLabel: String,
    esc: s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;') });
  vm.runInContext(readFileSync(new URL('../../web/execution-preview.js', import.meta.url), 'utf8'), ctx);
  ctx.game = game;
  const html = vm.runInContext('executionPreviewPanel(game)', ctx);
  assert.match(html, /value="500"/);
  assert.match(html, /Under 57.5/);
  assert.match(html, /Preview only/);
  assert.doesNotMatch(html, /Place order|Confirm trade/);
  ctx.result = { ...allocateDepth([], 500, .99), line: 57.5, max_price: .99, fetched_at: kickoff,
    venues: [{ book: 'novig', reason: '<script>bad</script>' }], notes: [] };
  const output = vm.runInContext('executionResultHtml(result)', ctx);
  assert.match(output, /&lt;script&gt;/);
  assert.match(output, /\$500.00 unspent/);
});
