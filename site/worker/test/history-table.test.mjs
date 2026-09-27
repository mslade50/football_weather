import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('Historical table separates saved No signals, missing snapshots, and closing versus alerted results', () => {
  const ctx = vm.createContext({ Intl, Date });
  for (const name of ['app.js', 'backtest.js', 'history-table.js']) {
    vm.runInContext(readFileSync(new URL(`../../web/${name}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  }
  ctx.payload = { games: [
    { game_id: 'nfl:2025:1:a@b', sport: 'nfl', season: 2025, week: 1, kickoff_utc: '2025-09-01T17:00:00Z',
      signal_label: 'No', signal_level: 'No', signal_at: '2025-09-01T16:00:00Z', under_result: 'P',
      away_name: '<Away>', home_name: 'Home', away_score: 20, home_score: 20, actual_total: 40, total_close: 40 },
    { game_id: 'nfl:2026:1:a@b', sport: 'nfl', season: 2026, week: 1, kickoff_utc: '2026-09-01T17:00:00Z', under_result: 'L' },
    { game_id: 'cfb:2025:1:a@b', sport: 'cfb', season: 2025, week: 1, kickoff_utc: '2025-09-01T17:00:00Z', under_result: 'W' },
  ], postmortem: { season: { bets: [
    { game_id: 'nfl:2025:1:a@b', side: 'under', market: 'total', first_line: 41, first_odds: -110, book: 'betonline', result: 'W' },
  ] } } };
  vm.runInContext('const data = normalizeBacktest(payload); const selected = historicalGames(data, "nfl", 2025, 1, "No");', ctx);
  assert.equal(vm.runInContext('selected.length', ctx), 1);
  assert.equal(vm.runInContext('historicalTier(data.games[1])', ctx), 'Unknown');
  assert.equal(vm.runInContext('historicalGames(data, "nfl", 2026, 1, "No").length', ctx), 0);
  assert.equal(vm.runInContext('historicalRecord(selected)', ctx), '0–0–1');
  const html = vm.runInContext('historyRowsHtml(selected, data.postmortem.season.bets)', ctx);
  assert.match(html, /No signal/);
  assert.match(html, /&lt;Away&gt;/);
  assert.match(html, /20–20/);
  assert.match(html, /<td>P<\/td>/);
  assert.match(html, /41.*BetOnline.*<b>W<\/b>/);
  assert.equal(vm.runInContext('historicalGames(data, "nfl", 2025, 1, "", "", 0).length', ctx), 0);
});
