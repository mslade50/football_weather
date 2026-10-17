import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function load() {
  const nodes = new Map();
  const ctx = vm.createContext({ Intl, document: { getElementById: (id) => {
    if (!nodes.has(id)) nodes.set(id, {});
    return nodes.get(id);
  } } });
  const app = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
  vm.runInContext(app.replace(/\nboot\(\);\s*$/, ''), ctx);
  vm.runInContext(readFileSync(new URL('../../web/status.js', import.meta.url), 'utf8'), ctx);
  vm.runInContext('renderBookChips = () => {};', ctx);
  return { ctx, nodes };
}

const EARLIER = '2026-10-17T12:59:48Z';
const THIS_RUN = '2026-10-17T16:00:51Z';

test('Each carried sport says it was kept and when weather and prices were last refreshed', () => {
  const { ctx, nodes } = load();
  ctx.meta = {
    run_id: '20261017T160051Z-gh39047551064-1',
    last_updated: THIS_RUN,
    retained_sports: {
      nfl: { from_run_id: '20261017T125948Z-gh39034528962-1-pw', games: 27, refreshed_at: EARLIER },
    },
  };
  vm.runInContext('renderHeader(meta); renderBanners(meta)', ctx);
  const banner = nodes.get('banners');
  assert.equal(banner.hidden, false);
  assert.match(banner.innerHTML, /NFL carried forward from the earlier run/);
  assert.match(banner.innerHTML, /Weather and prices were last refreshed 10\/17, 8:59 AM EDT/);
  assert.doesNotMatch(banner.innerHTML, /12:00 PM|4:00 PM|16:00/);
  assert.match(banner.innerHTML, /20261017T125948Z-gh39034528962-1-pw/);
  assert.equal(nodes.get('updated').textContent, 'Updated 10/17, 12:00 PM EDT');
  assert.match(nodes.get('updated').title, /8:59 AM EDT/);
});

test('A missing refreshed_at falls back to the time in from_run_id, per sport', () => {
  const { ctx, nodes } = load();
  ctx.meta = {
    retained_sports: {
      nfl: { from_run_id: '20261017T125948Z-gh1', games: 27 },
      cfb: { from_run_id: '20261017T150154Z-gh2', games: 108, refreshed_at: '2026-10-17T15:01:54Z' },
    },
  };
  vm.runInContext('renderBanners(meta)', ctx);
  const html = nodes.get('banners').innerHTML;
  assert.match(html, /NFL carried forward from the earlier run\. Weather and prices were last refreshed 10\/17, 8:59 AM EDT/);
  assert.match(html, /CFB carried forward from the earlier run\. Weather and prices were last refreshed 10\/17, 11:01 AM EDT/);
});

test('A full rebuild shows no carried-forward banner', () => {
  const { ctx, nodes } = load();
  vm.runInContext('renderBanners({last_updated: "2026-10-17T16:00:51Z", sport_counts: {nfl: 27, cfb: 108}})', ctx);
  const banner = nodes.get('banners');
  assert.equal(banner.innerHTML, '');
  assert.equal(banner.hidden, true);
});
