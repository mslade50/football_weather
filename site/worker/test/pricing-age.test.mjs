import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const priced = Date.parse('2026-11-13T12:00:00Z');
function board() {
  let now = priced;
  class Clock extends Date {static now() {return now;}}
  const ctx = vm.createContext({Date: Clock, console});
  for (const file of ['app.js', 'table.js', 'drawer.js'])
    vm.runInContext(readFileSync(new URL('../../web/game-status.js', import.meta.url), 'utf8') + '\n' + readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  ctx.game = {sport: 'nfl', home: {}, away: {}, fair: {priced_at: new Date(priced).toISOString(), fair_total: 45.6},
    consensus: {total_now: 48, total_n_books: 2}, odds: {pinnacle: {total: {line: 48, under: -110,
      updated_at: '2026-11-13T11:23:00Z', expires_at: '2026-11-13T11:59:59Z'}}}};
  return {ctx, run: code => vm.runInContext(code, ctx), advance: ms => {now = priced + ms;}};
}

test('Quote hover shows observation age and retains saved prices past provider expiry', () => {
  const f = board();
  const cell = f.run("bookTotalCell(game, 'pinnacle')");
  assert.match(cell, /observed 37 min ago/);
  assert.match(cell, /<b>48<\/b>/);
  assert.doesNotMatch(cell, /quote status: expired/);
  assert.equal(f.run("quoteAgeText('invalid')"), '');
  assert.equal(f.run("quoteAgeText('2026-11-14T12:00:00Z')"), '');
});

test('Derived stale marker flips only after 150 minutes without changing any data', () => {
  const f = board(), original = JSON.stringify(f.ctx.game);
  for (const minutes of [0, 149, 150]) {
    f.advance(minutes * 60000);
    assert.doesNotMatch(f.run('consensusTotalCell(game)'), /pricing-stale|priced \d+ min ago/);
  }
  f.advance(150 * 60000 + 1);
  assert.match(f.run('consensusTotalCell(game)'), /pricing-stale.*priced 150 min ago/);
  assert.match(f.run('JSON.stringify(HOVER[HK])'), /priced 150 min ago/);
  assert.match(f.run('oddsTable(game)'), /pricing-stale.*priced 150 min ago/);
  assert.equal(JSON.stringify(f.ctx.game), original);
});
