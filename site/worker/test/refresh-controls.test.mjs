import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { handleFetch } from '../index.js';

test('Admin exchange refresh loads its direct correlated result; full refresh dispatches selected sport', async () => {
  const nodes = new Map(['refreshmsg', 'lightrefreshbtn', 'refreshbtn'].map(id => [id, {
    hidden: true, disabled: false, addEventListener(type, callback) { this[type] = callback; },
  }]));
  const buttons = [nodes.get('lightrefreshbtn'), nodes.get('refreshbtn')];
  const env = { BOARD_PASSWORD: 'viewer', BOARD_ADMIN_USERNAME: 'admin', BOARD_ADMIN_PASSWORD: 'secret',
    GH_DISPATCH_TOKEN: 'test-token' };
  const dispatches = [];
  const originalFetch = globalThis.fetch;
  let fail = false;
  env.ODDS = { get: async key => {
    if (fail) throw new Error('Snapshot unavailable');
    return { json: async () => key === 'board/meta.json' ? { run_id: 'published', last_updated: '2020-01-01T00:00:00Z' } : [] };
  }, put: () => assert.fail('Direct quotes do not publish') };
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/runs')) return Response.json({ workflow_runs: [] });
    dispatches.push(JSON.parse(init.body));
    return new Response(null, { status: fail ? 500 : 204 });
  };
  try {
    const ctx = vm.createContext({ AbortController, clearTimeout: () => {}, Date, document: { getElementById: id => nodes.get(id), querySelectorAll: () => buttons },
      setTimeout: () => {}, fetch: async (url, init) => handleFetch(new Request('https://board.example/refresh', {
        ...init, headers: { ...init.headers, Authorization: `Basic ${Buffer.from('admin:secret').toString('base64')}` },
      }), env) });
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    vm.runInContext(source.replace(/\nboot\(\);\s*$/, ''), ctx);
    vm.runInContext('pollForNewData = (baseline, done) => done(); render = () => {}; STATE.sport = "cfb";', ctx);
    vm.runInContext('setupRefresh({role: "viewer"})', ctx);
    assert.ok(buttons.every(b => b.hidden && !b.click));
    vm.runInContext('setupRefresh({role: "admin"})', ctx);
    assert.ok(buttons.every(b => !b.hidden));
    await buttons[0].click();
    assert.match(nodes.get('refreshmsg').textContent, /exchange quote result loaded.*Weather publication unchanged/);
    await buttons[1].click();
    assert.deepEqual(dispatches.map(d => d.inputs), [{ sport: 'cfb', scope: 'full' }]);
    assert.ok(dispatches.every(d => d.ref === 'main'));
    fail = true;
    await buttons[0].click();
    assert.match(nodes.get('refreshmsg').textContent, /Refresh failed/);
    assert.ok(buttons.every(b => !b.disabled));
  } finally { globalThis.fetch = originalFetch; }
});
