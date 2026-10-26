import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function load() {
  const ctx = vm.createContext({Intl});
  vm.runInContext(readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  vm.runInContext(readFileSync(new URL('../../web/status.js', import.meta.url), 'utf8'), ctx);
  return ctx;
}

test('Paper scorecard distinguishes no data, missing results, secondary tests and stale capture', () => {
  const ctx = load();
  assert.match(vm.runInContext('paperScorecardHtml(null, null, {})', ctx), /Awaiting a successful capture/);
  ctx.health = {status: 'ok', last_run_id: 'r1'};
  ctx.report = {last_capture: new Date().toISOString(), capture_runs: 1, games: 2, groups: {
    'cfb|12|fixed': {entries: 1, clv_n: 0, raw_clv: null, excess_clv: null, matched_n: 0, roi: null, settled_n: 0, weeks: 1},
    'cfb|12|best': {entries: 1, clv_n: 0, raw_clv: null, excess_clv: null, matched_n: 0, roi: null, settled_n: 0, weeks: 1},
  }, missed: {'cfb|12|fixed': 3}};
  const html = vm.runInContext("paperScorecardHtml(health, report, {run_id:'r1'})", ctx);
  assert.match(html, /capturing/);
  assert.match(html, /1 \/ 3/);
  assert.match(html, /— \(0\)/);
  assert.match(html, /needs 8 weeks/);
  assert.match(html, /pending first weekly run/);
  assert.match(html, /CFB 12 mph fixed book is primary/);
  assert.match(vm.runInContext("paperScorecardHtml(health, report, {run_id:'r2'})", ctx), /check capture/);
  ctx.health.reason = '<img src=x onerror=alert(1)>';
  const escaped = vm.runInContext('paperScorecardHtml(health, report, {})', ctx);
  assert.doesNotMatch(escaped, /<img/);
  assert.match(escaped, /&lt;img/);
});
