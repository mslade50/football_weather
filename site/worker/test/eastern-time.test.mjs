import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('Kickoff, details and update header use Eastern regardless of venue or viewer timezone', () => {
  const previous = process.env.TZ;
  process.env.TZ = 'Asia/Tokyo';
  try {
    const nodes = new Map();
    const ctx = vm.createContext({ Intl, document: { getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, {});
      return nodes.get(id);
    } } });
    const app = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    vm.runInContext(app.replace(/\nboot\(\);\s*$/, ''), ctx);
    vm.runInContext(readFileSync(new URL('../../web/drawer.js', import.meta.url), 'utf8'), ctx);
    vm.runInContext(readFileSync(new URL('../../web/status.js', import.meta.url), 'utf8'), ctx);
    vm.runInContext('renderBookChips = () => {};', ctx);
    ctx.game = { kickoff_utc: '2026-09-28T02:00:00Z', kickoff_local: '2026-09-27T19:00:00-07:00',
      date_label: 'WRONG DATE', time_label: '07:00 PM', tz: 'America/Los_Angeles' };
    assert.equal(vm.runInContext('kickoffLabel(game)', ctx), 'Sun, 9/27, 10:00 PM EDT');
    const details = vm.runInContext('gameInfoTable(game)', ctx);
    assert.match(details, /10:00 PM EDT/);
    assert.doesNotMatch(details, /07:00|local|Los_Angeles|WRONG/);
    assert.equal(vm.runInContext('fmtET("2026-12-06T21:00:00Z")', ctx), '12/6, 4:00 PM EST');
    vm.runInContext('renderHeader({last_updated: game.kickoff_utc})', ctx);
    assert.equal(nodes.get('updated').textContent, 'Updated 9/27, 10:00 PM EDT');
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
});
