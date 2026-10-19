import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { novigDepth, allocateDepth, previewGame, sliceCost } from '../execution-preview.js';
import { alertLiquidity } from '../alert-liquidity.js';

const eventId = '01a0caf2-e862-7da0-9a40-1eaaf5b531e9';
const marketId = '01a0eb45-b723-7103-b452-71a27addf9be';
const over = '01a0eb45-b723-7103-b452-71becb7e2b2a';
const under = '01a0eb45-b723-7103-b452-71c486830299';
const kickoff = '2030-10-01T17:00:00Z';
const ref = { book: 'novig', line: 46.5, source_id: `${eventId}:${marketId}`, outcome_ids: { over, under } };
const game = { game_id: 'nfl:2030:4:lar@phi', sport: 'nfl', kickoff_utc: kickoff, execution_markets: [ref],
  total_prices: { quotes: [{ book: 'novig', side: 'under', line: 46.5, win_prob: .6, push_prob: 0,
    updated_at: new Date().toISOString() }] } };

// Scrubbed public API shape; reversed outcomes and misleading names deliberately
// ensure side selection depends on the typed scraper IDs, not display text/order.
function fetcher({ event = {}, market = {}, book = {} } = {}) {
  return async (url, options) => {
    assert.equal(options.method, 'GET');
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(new URL(url).origin, 'https://api.novig.com');
    assert.ok(new URL(url).searchParams.has('_preview'));
    const path = new URL(url).pathname;
    if (path.endsWith('/book')) return Response.json({ marketId, seq: 333,
      orders: { [under]: [{ price: '.99', qty: 9999999 }], [over]: [
        { price: '.6', qty: 300 }, { price: '.6', qty: 300 }, { price: '.5', qty: 200000 },
      ] }, ...book });
    if (path.includes('/events/')) return Response.json({ eventId, sport: 'FOOTBALL', league: 'NFL',
      startsTs: Date.parse(kickoff), status: 'OPEN_PREGAME', ...event });
    return Response.json({ marketId, eventId, marketType: 'TOTAL', strike: '46.5', status: 'OPEN',
      startsTs: Date.parse(kickoff), voids: 'FMV', fee: { coefficient: '.03', charged: 'WHEN_LIVE' },
      outcomes: [{ outcomeId: under, name: 'Over', status: 'TBD' },
        { outcomeId: over, name: 'Under', status: 'TBD' }], ...market });
  };
}

test('Novig opposing depth uses native one-cent contracts and zero pregame WHEN_LIVE fees', async () => {
  const venue = await novigDepth(ref, game, fetcher());
  assert.deepEqual(venue.levels, [{ price: .4, quantity: 600 }, { price: .5, quantity: 200000 }]);
  assert.equal(venue.coefficient, 0);
  assert.equal(venue.contract_value, .01);
  assert.match(venue.rules, /not a guaranteed refund/);
  assert.equal(sliceCost(.4, 600, 0, .01).total, 2400000n);
  const allocation = allocateDepth([venue], 500, .99);
  assert.deepEqual(allocation.allocations.map(a => [a.quantity, a.spend]), [[600, 2.4], [99520, 497.6]]);
  assert.equal(allocation.payout_if_win, 1001.2);
  assert.equal(allocation.spend, 500);
  assert.equal(allocation.average_price, 500 / 1001.2);
});

test('Different contract sizes rank by fee-inclusive cost per payout dollar and respect limits', async () => {
  const venue = await novigDepth(ref, game, fetcher({ market: { fee: { coefficient: '.03', charged: 'ALWAYS' } } }));
  assert.equal(venue.coefficient, .03);
  assert.equal(sliceCost(.4, 600, .03, .01).total, 2450000n); // conservative cent ceiling
  const result = allocateDepth([venue, { book: 'other', coefficient: 0, levels: [{ price: .405, quantity: 10 }] }], 10, .99);
  assert.equal(result.allocations[0].book, 'other');
  assert.ok(result.spend <= 10);
  assert.equal(allocateDepth([venue], 500, .4).spend, 0);
  // Tiny raw orders remain visible, with conservative rounding at the budget boundary.
  const tiny = { ...venue, coefficient: 0, levels: [{ price: .4, quantity: 1 }] };
  assert.equal(allocateDepth([tiny], 1, .99).spend, .004);
  for (let cents = 100; cents < 5000; cents += 37) {
    const r = allocateDepth([venue], cents / 100, .99);
    assert.ok(r.spend <= cents / 100);
    assert.equal(Math.round((r.spend + r.unspent) * 100), cents);
  }
});

test('Novig rejects missing mappings, mismatched events/outcomes, in-play, unknown fees and malformed depth', async () => {
  for (const override of [
    { event: { status: 'OPEN_INGAME' } }, { event: { eventId: marketId } }, { event: { league: 'NCAAF' } },
    { event: { startsTs: Date.parse(kickoff) + 1000 } }, { market: { strike: '47.5' } },
    { market: { status: 'CLOSED' } }, { market: { marketType: 'TEAM_TOTAL' } }, { market: { eventId: marketId } },
    { market: { voids: 'UNKNOWN' } }, { market: { outcomes: [] } }, { market: { fee: { charged: 'ALWAYS' } } },
    { market: { fee: { coefficient: '.03', charged: 'UNKNOWN' } } }, { book: { marketId: eventId } },
    { book: { orders: { [over]: [{ price: '.5', qty: -1 }] } } },
    { book: { orders: { [over]: [{ price: '.5', qty: 1.5 }] } } },
    { book: { orders: { [eventId]: [] } } },
  ]) await assert.rejects(novigDepth(ref, game, fetcher(override)));
  await assert.rejects(novigDepth({ ...ref, outcome_ids: {} }, game, () => assert.fail('missing identity fetched')));
  await assert.rejects(novigDepth(ref, game, async () => new Response('', { status: 429 })), /429/);
  await assert.rejects(novigDepth(ref, game, async () => Response.json({}, { headers: { age: '60' } })), /cached/);
  const empty = await novigDepth(ref, game, fetcher({ book: { orders: { [under]: [{ price: '.5', qty: 2000 }] } } }));
  assert.deepEqual(empty.levels, []);
});

test('Novig participates in preview and alert $500 ladders without suggesting an executed fill', async () => {
  const result = await previewGame(game, { line: 46.5, budget: 500, maxPrice: .99 }, fetcher());
  assert.equal(result.can_execute, false);
  assert.equal(result.venues.find(v => v.book === 'novig').submission, 'manual');
  const alert = await alertLiquidity(game, fetcher());
  assert.equal(alert.quotes[0].liquidity_dollars, 2.4);
  assert.equal(alert.quotes[0].liquidity_shares, 600);
  assert.equal(alert.quotes[0].contract_value, .01);
  assert.equal(alert.quotes[0].odds, 150);
  assert.deepEqual(alert.allocations.map(a => a.spend), [2.4, 1000]);
  assert.equal(alert.cash_stake_capacity, 1002.4);
  assert.equal(alert.cash_liquidity_verified, true);
  assert.equal(alert.unspent, 0);
  assert.equal((await alertLiquidity(game, fetcher({ event: { status: 'OPEN_INGAME' } }))).spend, 0);
  assert.equal((await alertLiquidity(game, fetcher({ book: { orders: {} } }))).quotes[0].liquidity_status, 'empty');

  const ctx = vm.createContext({ result, Date, fmtOdds: String, fmtET: String, bookLabel: String, esc: String });
  vm.runInContext(readFileSync(new URL('../../web/execution-preview.js', import.meta.url), 'utf8'), ctx);
  const html = vm.runInContext('executionResultHtml(result)', ctx);
  assert.match(html, /submit manually/);
  assert.match(html, /limit price 40.0%/);
  assert.match(html, /stake \$2.40/);
  assert.match(html, /1¢ each/);
  assert.match(html, /Pending your confirmation/);
  assert.match(html, /does not verify or save fills/);
});

test('$500 means principal cash stake; fees, native contract count and payout do not satisfy it', async () => {
  const priced = { orders: { [over]: [{ price: '.505', qty: 101010 }] } };
  const result = await alertLiquidity(game, fetcher({ book: priced,
    market: { fee: { coefficient: '.03', charged: 'ALWAYS' } } }));
  assert.equal(result.cash_stake_capacity, 499.9995);
  assert.ok(result.debit_capacity > 500 && result.payout_capacity > 1000);
  assert.equal(result.cash_liquidity_verified, false);
  assert.equal(result.capacity_status, 'insufficient_cash_capacity');
  const small = await alertLiquidity(game, fetcher({ book: { orders: { [over]: [{ price: '.5', qty: 500 }] } } }));
  assert.equal(small.cash_stake_capacity, 2.5);
  assert.equal(small.cash_liquidity_verified, false);
  const enough = await alertLiquidity(game, fetcher({ book: { orders: { [over]: [{ price: '.5', qty: 100000 }] } } }));
  assert.equal(enough.cash_stake_capacity, 500);
  assert.equal(enough.cash_liquidity_verified, true);
});

test('Unfavorable fee-inclusive offers stay visible as quotes but cannot pad acceptable capacity', async () => {
  const lower = { ...game, total_prices: { quotes: [{ ...game.total_prices.quotes[0], win_prob: .39 }] } };
  const result = await alertLiquidity(lower, fetcher());
  assert.equal(result.quotes[0].liquidity_status, 'verified');
  assert.deepEqual(result.allocations, []);
  assert.equal(result.cash_stake_capacity, 0);
  assert.equal(result.cash_liquidity_verified, false);
});
