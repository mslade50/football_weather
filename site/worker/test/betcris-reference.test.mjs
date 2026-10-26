import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {expireCardQuotes} from '../../web/current-quotes.mjs';

const now = Date.parse('2026-10-14T12:00:00Z');
class Clock extends Date { static now() { return now; } }

function board() {
  const ctx = vm.createContext({Date: Clock, console, localStorage: {getItem: () => null, setItem: () => {}}});
  for (const file of ['app.js', 'table.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  ctx.game = {game_id: 'nfl:2026:6:a@h', sport: 'nfl', kickoff_utc: '2026-10-18T17:00:00Z',
    home: {name: 'Home'}, away: {name: 'Away'}, fair: {edges: [
      {book: 'betcris', market: 'total', edge_pts: 4, tier: 'edge', side: 'under', fair_line: 44},
      {book: 'kalshi', market: 'total', edge_pts: 1, tier: 'edge', side: 'under', fair_line: 44},
    ]}, odds: {betcris: {total: {line: 48, under: -110, updated_at: '2026-10-14T11:40:00Z',
      source_updated_at: '2026-10-14T11:40:00Z', expires_at: '2026-10-14T12:10:00Z',
      actionable_until: '2026-10-14T11:50:00Z'}}}};
  return {ctx, run: code => vm.runInContext(code, ctx)};
}

test('A BetCris price past the provider TTL shows its age and cannot be the edge', () => {
  const page = board();
  const cell = page.run("bookTotalCell(game,'betcris')");
  assert.match(cell, /<b>48<\/b>/);
  assert.match(cell, /quote age: 20m old; quote status: reference/);
  assert.equal(cell.replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '48 -110');
  assert.equal(page.run("edgeAt(game,'betcris','total')"), null);
  assert.equal(page.run("bestEdge(game,'total').book"), 'kalshi');
});

test('A BetCris price inside the provider timer is still only a reference', () => {
  const page = board();
  page.run(`game.odds.betcris.total.source_updated_at = '2026-10-14T11:50:00Z';
            game.odds.betcris.total.actionable_until = '2026-10-14T12:20:00Z';`);
  const cell = page.run("bookTotalCell(game,'betcris')");
  assert.match(cell, /<b>48<\/b>/);
  assert.match(cell, /10m old/);
  assert.doesNotMatch(cell, /reference/);
  assert.equal(page.run("edgeAt(game,'betcris','total')"), null);
  assert.equal(page.run("bestEdge(game,'total').book"), 'kalshi');
});

test('The worker keeps the reference price and falls through to the next book', () => {
  const card = {fair: {edges: [
      {book: 'betcris', market: 'total', edge_pts: 4},
      {book: 'kalshi', market: 'total', edge_pts: 1},
    ], best_total: {book: 'betcris', market: 'total', edge_pts: 4}},
    odds: {betcris: {total: {line: 48, under: -110, expires_at: '2026-10-14T12:10:00Z',
      actionable_until: '2026-10-14T11:50:00Z'}}}};
  const served = expireCardQuotes(card, now);
  assert.equal(served.odds.betcris.total.line, 48);
  assert.deepEqual(served.fair.edges.map(edge => edge.book), ['kalshi']);
  assert.equal(served.fair.best_total.book, 'kalshi');
});

test('A published fair that used BetCris is rebuilt from the other books', () => {
  const card = {
    impact: {model_version: 'v1', v1: {gs_fg_pct: -10, away_fg_pct: 0}},
    consensus: {total_now: 50, total_open: 48, spread_now: -2.5, spread_open: -3, spread_src: 'cris+bol+pin', n_books: 3},
    fair: {
      fair_total: 45,
      fair_spread: -2.5,
      edges: [
        {book: 'betcris', market: 'total', side: 'under', line: 60, edge_pts: 15, edge_prob: 0.2},
        {book: 'kalshi', market: 'total', side: 'under', line: 47, edge_pts: 2, edge_prob: 0.05},
        {book: 'betcris', market: 'spread', side: 'home', line: -1, edge_pts: 1.5, edge_prob: 0.04},
        {book: 'pinnacle', market: 'spread', side: 'home', line: -3, edge_pts: 0.5, edge_prob: 0.02},
      ],
      best_total: {book: 'betcris', market: 'total', side: 'under', line: 60, edge_pts: 15, edge_prob: 0.2},
      best_spread: {book: 'betcris', market: 'spread', side: 'home', line: -1, edge_pts: 1.5},
    },
    odds: {
      betcris: {
        total: {line: 60, under: -110, expires_at: '2026-10-14T12:30:00Z', actionable_until: '2026-10-14T12:20:00Z'},
        spread: {home_line: -1, home_odds: -110, expires_at: '2026-10-14T12:30:00Z'},
      },
      pinnacle: {total: {line: 44, under: -110}, spread: {home_line: -3, home_odds: -110}},
      betonline: {total: {line: 46, under: -110}, spread: {home_line: -2.5, home_odds: -110}},
      kalshi: {total: {line: 47, under: -105}},
    },
  };
  const served = expireCardQuotes(card, now);
  assert.equal(served.odds.betcris.total.line, 60);
  assert.ok(served.fair.edges.every(edge => edge.book !== 'betcris'));
  assert.equal(served.consensus.total_now, 45);
  assert.equal(served.fair.fair_total, 40.5);
  assert.equal(served.consensus.spread_now, -2.75);
  assert.equal(served.consensus.spread_src, 'bol+pin');
  assert.equal(served.fair.fair_spread, -2.75);
  assert.equal(served.fair.best_total.book, 'kalshi');
  assert.equal(served.fair.best_spread.book, 'pinnacle');
  const kalshi = served.fair.edges.find(edge => edge.book === 'kalshi');
  assert.equal(kalshi.edge_pts, 6.5);
  assert.equal(kalshi.fair_line, 40.5);
  const again = expireCardQuotes(served, now);
  assert.equal(again.fair.fair_total, 40.5);
  assert.equal(again.consensus.spread_src, 'bol+pin');
  assert.equal(again.fair.best_total.book, 'kalshi');
});

test('An expired BetCris reference keeps a fresh Pinnacle, BetOnline, and Kalshi fair price', () => {
  const page = board();
  const card = page.ctx.game;
  card.consensus = {total_now: 45, total_open: 47, n_books: 3, thin: false};
  card.fair = {
    fair_total: 40.5,
    edges: [
      {book: 'kalshi', market: 'total', side: 'under', line: 47, edge_pts: 6.5},
      {book: 'pinnacle', market: 'total', side: 'under', line: 44.5, edge_pts: 4},
    ],
    best_total: {book: 'kalshi', market: 'total', edge_pts: 6.5},
  };
  card.odds = {
    betcris: {total: {line: 60, under: -110, open_line: 47, expires_at: '2026-10-14T11:30:00Z'}},
    pinnacle: {total: {line: 44.5, under: -110, expires_at: '2026-10-14T13:00:00Z'}},
    betonline: {total: {line: 46, under: -110, expires_at: '2026-10-14T13:00:00Z'}},
    kalshi: {total: {line: 47, under: -105, expires_at: '2026-10-14T13:00:00Z'}},
  };
  page.ctx.game = expireCardQuotes(card, now);
  const served = page.ctx.game;
  assert.equal(served.odds.betcris.total.expired, true);
  assert.equal(served.odds.betcris.total.line, undefined);
  assert.equal(served.odds.betcris.total.open_line, 47);
  assert.equal(served.odds.pinnacle.total.line, 44.5);
  assert.equal(served.odds.betonline.total.line, 46);
  assert.equal(served.odds.kalshi.total.line, 47);
  assert.equal(served.consensus.total_now, 45);
  assert.equal(served.fair.fair_total, 40.5);
  assert.deepEqual(served.fair.edges.map(edge => edge.book), ['kalshi', 'pinnacle']);
  assert.equal(served.fair.edges[0].edge_pts, 6.5);
  assert.equal(served.fair.best_total.book, 'kalshi');
  assert.deepEqual(served.expired_markets, ['betcris/total']);
  const cell = page.run("bookTotalCell(game,'betcris')");
  assert.match(cell, /<b>60<\/b>/);
  assert.match(cell, /reference status: expired/);
  assert.equal(cell.replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '60 -110');
});

test('A pricing book expiry still clears the fair line, edges, and best price', () => {
  const card = {
    consensus: {total_now: 49, total_open: 47, n_books: 3},
    fair: {
      fair_total: 45,
      edges: [{book: 'kalshi', market: 'total', side: 'under', line: 47, edge_pts: 2}],
      best_total: {book: 'kalshi', market: 'total', edge_pts: 2},
    },
    odds: {
      pinnacle: {total: {line: 44.5, under: -110, expires_at: '2026-10-14T11:30:00Z'}},
      betonline: {total: {line: 46, under: -110, expires_at: '2026-10-14T13:00:00Z'}},
      kalshi: {total: {line: 47, under: -105, expires_at: '2026-10-14T13:00:00Z'}},
    },
  };
  const served = expireCardQuotes(card, now);
  assert.equal(served.odds.pinnacle.total.expired, true);
  assert.equal(served.odds.pinnacle.total.line, undefined);
  assert.equal(served.odds.betonline.total.line, 46);
  assert.equal(served.odds.kalshi.total.line, 47);
  assert.equal(served.consensus.total_now, null);
  assert.equal(served.consensus.total_open, 47);
  assert.equal(served.consensus.total_n_books, 2);
  assert.equal(served.consensus.total_thin, false);
  assert.equal(served.consensus.spread_n_books, 0);
  assert.equal(served.consensus.n_books, 0);
  assert.equal(served.fair.fair_total, null);
  assert.deepEqual(served.fair.edges, []);
  assert.equal(served.fair.best_total, null);
});

test('All sportsbook cells show only the saved total and price; age and status stay in the quote card', () => {
  const page = board();
  for (const book of ['pinnacle', 'betonline', 'fanduel', 'betcris', 'kalshi', 'polymarket_us']) {
    for (const status of ['stale', 'expired', 'current', 'unavailable', 'unknown']) {
      page.ctx.game.odds[book] = {total: {last_observed: {line: 48, under: -110, status,
        updated_at: '2026-10-14T11:50:00Z', source_updated_at: '2026-10-14T10:00:00Z'}}};
      const cell = page.run(`bookTotalCell(game,'${book}')`);
      assert.equal(cell.replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '48 -110');
      assert.match(cell, /quote age: 2h old/);
      assert.match(cell, new RegExp(`reference status: ${status}`));
      const hover = page.run('HOVER[HK]');
      assert.ok(hover.lines.some(([label, value]) => label === 'quote age' && value === '2h old'));
      assert.ok(hover.lines.some(([label, value]) => label === 'reference status' && value === status));
    }
  }
  page.ctx.game.odds.pinnacle.total = {open_line: 45, open_under: -105};
  assert.equal(page.run("bookTotalCell(game,'pinnacle')").replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '— —');
  assert.equal(page.run("bookTotalCell(game,'never_observed')").replace(/<[^>]*>/g, ''), '—');
});
