import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const NOW = Date.parse('2026-10-10T18:00:00Z');
class Clock extends Date { static now() { return NOW; } }
function load() {
  const document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null, getElementById: () => null};
  const store = {};
  const ctx = vm.createContext({Date: Clock, URLSearchParams, document, clearTimeout() {}, setTimeout() {},
    localStorage: {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }},
    location: {hash: ''}, history: {replaceState() {}}});
  for (const file of ['discovery.js', 'app.js', 'signals.js', 'table.js', 'status.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  return ctx;
}
const ctx = load();
const run = code => vm.runInContext(code, ctx);
const ids = rows => rows.map(g => g.game_id);
const g = (id, temp, kick = '2026-10-11T18:00:00Z') => ({game_id: id, kickoff_utc: kick, weather: temp == null ? {} : {temp_fg: temp}});

test('missing values sort last in both directions, ties stable', () => {
  ctx.rows = [g('a', 40), g('b', null), g('c', 70), g('d', null, '2026-10-11T17:00:00Z'), g('e', 55), g('f', 55, '2026-10-11T16:00:00Z')];
  for (const dir of [-1, 1]) {
    const out = run(`sortGames(rows.slice(), tableColumns([], false).find(c => c[4] === "temp")[2], ${dir})`);
    const order = ids(out);
    assert.deepEqual(order.slice(-2), ['d', 'b'], `dir ${dir}: blanks last, kickoff tie-break`);
    assert.deepEqual(order.slice(0, 4), dir < 0 ? ['c', 'f', 'e', 'a'] : ['a', 'f', 'e', 'c']);
  }
});

test('string columns sort consistently with blanks last', () => {
  ctx.keyfn = x => x.s;
  const rows = [{game_id: '1', s: '40, -80'}, {game_id: '2', s: ''}, {game_id: '3', s: '35, -90'}];
  for (const dir of [-1, 1]) {
    ctx.r = rows.slice();
    const out = ids(run(`sortGames(r, keyfn, ${dir})`));
    assert.equal(out[2], '2');
    assert.deepEqual(out.slice(0, 2), dir < 0 ? ['1', '3'] : ['3', '1']);
  }
});

test('sort key is a stable column id that survives book filter changes', () => {
  const all = run(`tableColumns(["pinnacle", "betcris"], true).map(c => c[4])`);
  const one = run(`tableColumns(["betcris"], true).map(c => c[4])`);
  assert.ok(all.includes('betcris:t') && one.includes('betcris:t'));
  assert.equal(new Set(all).size, all.length);
  assert.equal(run(`sortColumnId(tableColumns(["betcris"], true), "betcris:t")[4]`), 'betcris:t');
  assert.equal(run(`sortColumnId(tableColumns(["betcris"], true), "pinnacle:t")`), null);
  assert.equal(run(`sortColumnId(tableColumns([], false), "temp")[0]`), 'Temp');
});

test('default week per sport', () => {
  ctx.games = [{week: 6, kickoff_utc: '2026-10-04T18:00:00Z', status: 'final'}, {week: 7, kickoff_utc: '2026-10-11T18:00:00Z'}, {week: 8, kickoff_utc: '2026-10-18T18:00:00Z'}];
  assert.equal(run(`pickDefaultWeek([6,7,8], games, {weeks: {nfl: 8, cfb: 7}}, "nfl")`), '8');
  assert.equal(run(`pickDefaultWeek([6,7,8], games, {weeks: {nfl: 8, cfb: 7}}, "cfb")`), '7');
  assert.equal(run(`pickDefaultWeek([6,7,8], games, {weeks: {cfb: [8, 9]}}, "cfb")`), '8');
  assert.equal(run(`pickDefaultWeek([6,7,8], games, {week: 6}, "cfb")`), '7');
  assert.equal(run(`pickDefaultWeek([6,7,8], games, {weeks: {cfb: 99}}, "cfb")`), '7');
  assert.equal(run(`pickDefaultWeek([6], [{week: 6, kickoff_utc: "2026-10-01T00:00:00Z"}], {}, "nfl")`), '6');
  assert.equal(run(`pickDefaultWeek([], [], {}, "nfl")`), null);
});

test('book health absent and present', () => {
  assert.equal(run(`bookHealthFor({}, "betcris", "cfb")`), null);
  assert.match(run(`bookHealthHtml({})`), /No per-book health/);
  run(`DATA.meta = {}`);
  assert.equal(run(`warnBookHeader("Betcris T", "betcris", "cfb").tip`), '');
  run(`DATA.meta = {book_health: {betcris: {cfb: {rows: 0, last_ok_utc: "2026-10-10T18:02:00Z", consecutive_empty: 3, last_error: "timeout"},
    nfl: {rows: 12, last_ok_utc: "2026-10-10T18:02:00Z", consecutive_empty: 0, last_error: null}}}}`);
  const w = run(`warnBookHeader("Betcris T", "betcris", "cfb")`);
  assert.match(w.label, /⚠/);
  assert.match(w.tip, /0 lines for 3 runs/);
  assert.match(w.tip, /timeout/);
  assert.equal(run(`warnBookHeader("Betcris T", "betcris", "nfl").tip`), '');
  const html = run(`bookHealthHtml(DATA.meta)`);
  assert.match(html, /CFB/);
  assert.match(html, /timeout/);
});

test('game state badge and expired-quote tooltip', () => {
  assert.equal(run(`gameStarted({status: "final", kickoff_utc: "2026-10-12T00:00:00Z"})`), 'Final');
  assert.equal(run(`gameStarted({status: "scheduled", kickoff_utc: "2026-10-10T12:00:00Z"})`), 'Live');
  assert.equal(run(`gameStarted({status: "scheduled", kickoff_utc: "2026-10-11T12:00:00Z"})`), '');
  assert.match(run(`blankOddsCell({expired_markets: ["novig/total"]}, "novig", "total", "No quote")`), /quote expired/);
  assert.doesNotMatch(run(`blankOddsCell({}, "novig", "total", "No quote")`), /expired/);
});
