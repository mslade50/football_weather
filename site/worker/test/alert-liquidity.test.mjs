import test from 'node:test';
import assert from 'node:assert/strict';
import { alertLiquidity } from '../alert-liquidity.js';

const kickoff = '2030-10-01T17:00:00Z';
const k = 'KXNFLTOTAL-30OCT01LARPHI-47', p = 'tsc-nfl-lar-phi-2030-10-01-total-46pt5';
function game() {
  return { game_id: 'nfl:2030:4:lar@phi', sport: 'nfl', kickoff_utc: kickoff,
    execution_markets: [{ book: 'kalshi', source_id: k, line: 46.5 }, { book: 'polymarket_us', source_id: p, line: 46.5 }],
    total_prices: { quotes: ['kalshi', 'polymarket_us'].map(book => ({ book, side: 'under', line: 46.5,
      win_prob: .6, push_prob: 0, updated_at: new Date().toISOString() })) } };
}
function fetcher({ empty = false, polyFee = 0, failKalshi = false, kalshiLine = 46.5 } = {}) {
  return async (url, options) => {
    assert.equal(options.method, 'GET');
    assert.equal(options.redirect, 'manual');
    assert.ok(new URL(url).searchParams.has('_preview'));
    const path = new URL(url).pathname;
    if (failKalshi && url.includes('kalshi.com')) return new Response('', { status: 429 });
    if (path.includes('/series/')) return Response.json({ series: { fee_type: 'quadratic', fee_multiplier: 0 } });
    if (path.endsWith('/orderbook')) return Response.json({ orderbook_fp: { yes_dollars: [['.59', '100']] } });
    if (path.includes('/market/slug/')) return Response.json({ market: { slug: p, active: true, ep3Status: 'OPEN',
      sportsMarketType: 'football_team_full_game_total', line: 46.5, gameStartTime: kickoff,
      description: 'Overtime is included', feeCoefficient: polyFee, minimumTradeQty: .01,
      marketSides: [{ description: 'under', long: false, tradable: true }] } });
    if (path.endsWith('/book')) return Response.json({ marketData: { marketSlug: p, state: 'MARKET_STATE_OPEN',
      bids: empty ? [] : [{ px: { value: '.60', currency: 'USD' }, qty: '6' },
        { px: { value: '.57', currency: 'USD' }, qty: '2000' }] } });
    return Response.json({ market: { ticker: k, status: 'active', market_type: 'binary', strike_type: 'greater',
      floor_strike: kalshiLine, notional_value_dollars: '1', title: `Full Game: over ${kalshiLine} points scored?`, rules_primary: 'Total', rules_secondary: '48 hours' } });
  };
}

test('Cash capacity aggregates all acceptable levels without treating $500 as a budget', async () => {
  const r = await alertLiquidity(game(), fetcher());
  assert.equal(r.quotes.find(q => q.book === 'polymarket_us').liquidity_shares, 6);
  assert.equal(r.quotes.find(q => q.book === 'polymarket_us').liquidity_dollars, 2.4);
  assert.deepEqual(r.allocations.map(a => [a.book, a.quantity, a.spend]),
    [['polymarket_us', 6, 2.4], ['kalshi', 100, 41], ['polymarket_us', 2000, 860]]);
  assert.equal(r.cash_stake_capacity, 903.4);
  assert.equal(r.cash_liquidity_verified, true);
  assert.equal(r.debit_capacity, 903.4);
  assert.equal(r.payout_capacity, 2106);
  assert.equal(r.unspent, 0);
});

test('Live taker fees can make the cheapest displayed ask rank behind another venue', async () => {
  const r = await alertLiquidity(game(), fetcher({ polyFee: .5 }));
  assert.equal(r.allocations[0].book, 'kalshi');
  assert.equal(r.quotes.find(q => q.book === 'polymarket_us').liquidity_dollars, 2.4);
  assert.equal(r.quotes.find(q => q.book === 'polymarket_us').liquidity_debit, 3.12);
  assert.equal(r.cash_stake_capacity, 903.4);
  assert.ok(r.debit_capacity > r.cash_stake_capacity);
});

test('Empty books and rate-limited venues cannot provide imaginary $500 coverage', async () => {
  const r = await alertLiquidity(game(), fetcher({ empty: true, failKalshi: true }));
  assert.equal(r.quotes.find(q => q.book === 'polymarket_us').liquidity_status, 'empty');
  assert.equal(r.quotes.find(q => q.book === 'kalshi').liquidity_status, 'unknown');
  assert.equal(r.spend, 0);
  assert.equal(r.unspent, 500);
  const stale = game(); stale.total_prices.quotes.forEach(q => { q.updated_at = '2020-01-01'; });
  assert.deepEqual((await alertLiquidity(stale, () => assert.fail('stale quote fetched'))).quotes, []);
  const ambiguous = game(); ambiguous.execution_markets.push(...ambiguous.execution_markets);
  assert.equal((await alertLiquidity(ambiguous, () => assert.fail('ambiguous mapping fetched'))).spend, 0);
});

test('Different totals retain their own modeled win probabilities when ranking liquidity', async () => {
  const g = game();
  g.execution_markets.find(r => r.book === 'kalshi').line = 47.5;
  g.total_prices.quotes.find(q => q.book === 'kalshi').line = 47.5;
  g.total_prices.quotes.find(q => q.book === 'kalshi').win_prob = .65;
  const r = await alertLiquidity(g, fetcher({ kalshiLine: 47.5 }));
  assert.equal(r.allocations[0].book, 'kalshi'); // .65/.41 > .60/.40
  assert.equal(r.allocations[0].line, 47.5);
});
