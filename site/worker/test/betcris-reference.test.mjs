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
  assert.match(cell, /U 48/);
  assert.match(cell, /20m old · reference/);
  assert.equal(page.run("edgeAt(game,'betcris','total')"), null);
  assert.equal(page.run("bestEdge(game,'total').book"), 'kalshi');
});

test('The worker keeps the reference price and drops only the BetCris edge', () => {
  const card = {fair: {edges: [{book: 'betcris', market: 'total'}, {book: 'kalshi', market: 'total'}],
    best_total: {book: 'betcris'}}, odds: {betcris: {total: {line: 48, under: -110, expires_at: '2026-10-14T12:10:00Z',
    actionable_until: '2026-10-14T11:50:00Z'}}}};
  const served = expireCardQuotes(card, now);
  assert.equal(served.odds.betcris.total.line, 48);
  assert.deepEqual(served.fair.edges, [{book: 'kalshi', market: 'total'}]);
  assert.equal(served.fair.best_total, null);
});
