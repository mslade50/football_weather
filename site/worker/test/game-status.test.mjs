import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { gameStatusCode, bettableBeforeKickoff } from '../game-status.js';

const now = Date.parse('2026-11-13T00:00:00Z');
const kickoff_utc = '2026-11-13T01:00:00Z';
const browser = vm.createContext({ Date });
vm.runInContext(readFileSync(new URL('../../web/game-status.js', import.meta.url), 'utf8'), browser);

test('Browser and Worker share code authority, fallback and kickoff boundaries', () => {
  const cases = [['scheduled', 'scheduled'], ['tbd', 'tbd'], ['live', 'live'], ['in progress', 'live'],
    ['in_play', 'live'], ['Halftime', 'live'], ['Final/OT', 'final'], ['canceled', 'cancelled'],
    ['postponed', 'postponed'], ['suspended', 'suspended'], ['unknown', 'unknown'], ['', 'unspecified']];
  for (const [status, expected] of cases) {
    for (const status_code of [undefined, expected]) {
      const game = { status, kickoff_utc, ...(status_code ? { status_code } : {}) };
      browser.game = game; browser.now = now;
      assert.equal(gameStatusCode(game), expected);
      assert.equal(vm.runInContext('gameStatusCode(game)', browser), expected);
      const allowed = ['scheduled', 'tbd', 'unknown', 'unspecified'].includes(expected);
      assert.equal(bettableBeforeKickoff(game, now), allowed);
      assert.equal(vm.runInContext('bettableBeforeKickoff(game, now)', browser), allowed);
    }
  }
  assert.equal(bettableBeforeKickoff({ status: 'final', status_code: 'scheduled', kickoff_utc }, now), true);
  assert.equal(bettableBeforeKickoff({ status: 'scheduled', status_code: 'live', kickoff_utc }, now), false);
  for (const kickoff_utc of [new Date(now).toISOString(), '2020-01-01', 'invalid', undefined]) {
    assert.equal(bettableBeforeKickoff({ status_code: 'scheduled', kickoff_utc }, now), false);
  }
});

test('Python, browser and Worker agree on the shared status table', () => {
  const { cases } = JSON.parse(readFileSync(new URL('../../../tests/fixtures/game_status_table.json', import.meta.url), 'utf8'));
  const kickoffs = { future: '2026-11-13T01:00:00+00:00', past: new Date(now).toISOString(), invalid: 'invalid' };
  for (const c of cases) {
    const game = { status: c.status, kickoff_utc: kickoffs[c.kickoff], ...(c.status_code === null ? {} : { status_code: c.status_code }) };
    browser.game = game; browser.now = now;
    const label = JSON.stringify(c);
    assert.equal(bettableBeforeKickoff(game, now), c.bettable, `worker ${label}`);
    assert.equal(vm.runInContext('bettableBeforeKickoff(game, now)', browser), c.bettable, `browser ${label}`);
  }
});
