import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

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
  assert.match(cell, /quote age: observed 20 min ago; quote status: reference/);
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
  assert.match(cell, /observed 10 min ago/);
  assert.match(cell, /reference, not an entry/);
  assert.equal(page.run("edgeAt(game,'betcris','total')"), null);
  assert.equal(page.run("bestEdge(game,'total').book"), 'kalshi');
});

test('All sportsbook cells show only the saved total and price; age and status stay in the quote card', () => {
  const page = board();
  for (const book of ['pinnacle', 'betonline', 'fanduel', 'betcris', 'kalshi', 'polymarket_us']) {
    for (const status of ['stale', 'expired', 'current', 'unavailable', 'unknown']) {
      page.ctx.game.odds[book] = {total: {last_observed: {line: 48, under: -110, status,
        updated_at: '2026-10-14T11:50:00Z', source_updated_at: '2026-10-14T10:00:00Z'}}};
      const cell = page.run(`bookTotalCell(game,'${book}')`);
      assert.equal(cell.replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '48 -110');
      assert.match(cell, /quote age: observed 120 min ago/);
      assert.match(cell, new RegExp(`reference status: ${status}`));
      const hover = page.run('HOVER[HK]');
      assert.ok(hover.lines.some(([label, value]) => label === 'quote age' && value === 'observed 120 min ago'));
      assert.ok(hover.lines.some(([label, value]) => label === 'reference status' && value === status));
    }
  }
  page.ctx.game.odds.pinnacle.total = {open_line: 45, open_under: -105};
  assert.equal(page.run("bookTotalCell(game,'pinnacle')").replace(/<[^>]*>/g, '').trim().replace(/\s+/g, ' '), '— —');
  assert.equal(page.run("bookTotalCell(game,'never_observed')").replace(/<[^>]*>/g, ''), '—');
});
