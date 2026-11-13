import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

function fixture() {
  const nodes = new Map();
  const ctx = vm.createContext({Intl, document: {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {addEventListener() {}});
    return nodes.get(id);
  }}});
  for (const name of ['game-status.js', 'app.js', 'drawer.js', 'status.js']) {
    vm.runInContext(readFileSync(new URL(`../../web/${name}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  }
  vm.runInContext('STATE.view = "status"; STATUS.loaded = true; STATUS.data = {runs: [], heartbeat: {ts: "2000-01-01T00:00:00Z"}};', ctx);
  return {ctx, nodes};
}

for (const [status, css] of [['fresh', 'ok'], ['stale', 'warn'], ['unknown', 'muted']]) {
  test(`Status renders published ${status} with no browser heartbeat threshold`, async () => {
    const {ctx, nodes} = fixture();
    ctx.health = {status, reason: 'Python observation', threshold_hours: 20, evaluated_at: '2026-11-13T12:00:00Z'};
    await vm.runInContext('STATUS.data.heartbeat_health = health; renderStatus()', ctx);
    const html = nodes.get('statuswrap').innerHTML;
    assert.match(html, new RegExp(`pill ${css}">${status}</span>`));
    assert.match(html, /Python observation.*checked/);
  });
}

test('Older status without a published verdict shows neutral unknown', async () => {
  const {ctx, nodes} = fixture();
  await vm.runInContext('renderStatus()', ctx);
  assert.match(nodes.get('statuswrap').innerHTML, /pill muted">unknown<\/span>/);
  assert.match(nodes.get('statuswrap').innerHTML, /No published heartbeat verdict/);
});
