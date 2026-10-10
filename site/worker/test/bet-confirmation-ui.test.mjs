import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const game = 'nfl:2026:6:a@b';
function harness() {
  const requests = [], saved = new Map(); let id = 0;
  const storage = {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)};
  const ctx = vm.createContext({AbortSignal, crypto: {randomUUID: () => `fixture-confirmation-${++id}`},
    sessionStorage: storage, fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}))});
  vm.runInContext(readFileSync(new URL('../../web/bet-confirmation.js', import.meta.url), 'utf8'), ctx);
  const create = () => vm.runInContext(`new PlacedBetConfirmation('${game}')`, ctx);
  const controller = create();
  function fill(c = controller) { for (const [key, value] of Object.entries({book: 'kalshi', line: '48.5', stake: '125'})) c.update(key, value); }
  const details = index => JSON.parse(requests[index].options.body);
  const bet = index => ({...details(index), source: 'explicit_user_confirmation', confirmed_at: '2026-10-09T22:00:00Z'});
  const respond = (index, payload, status = 200) => requests[index].resolve({ok: status >= 200 && status < 300, status, json: async () => payload});
  return {requests, controller, create, fill, details, bet, respond, storage};
}

test('Opening, editing and absent acknowledgement never POST; actual sub-$500 stake is recorded explicitly', async () => {
  const h = harness(); h.fill();
  assert.equal(await h.controller.submit(false), false);
  assert.equal(await h.controller.submit('true'), false);
  assert.equal(h.requests.length, 0);
  const pending = h.controller.submit(true);
  assert.equal(h.requests[0].options.method, 'POST');
  assert.equal(h.details(0).stake, 125);
  assert.equal(h.details(0).acknowledgement, 'I placed this bet');
  h.respond(0, {ok: true, recorded: true, bet: h.bet(0)});
  assert.equal(await pending, true);
  assert.equal(h.controller.phase, 'confirmed');
  assert.equal(await h.controller.submit(true), false);
  assert.equal(h.requests.length, 1);
});

test('Uncertain network outcome retains immutable ID/details across edits, reload and explicit retry', async () => {
  const h = harness(); h.fill();
  const first = h.controller.submit(true); h.requests[0].reject(new Error('timeout')); await first;
  assert.equal(h.controller.phase, 'uncertain');
  h.controller.update('stake', '500');
  const restored = h.create();
  assert.equal(restored.pending.bet_id, h.details(0).bet_id);
  assert.equal(await restored.submit(false), false);
  const retry = restored.submit(true);
  assert.deepEqual(h.details(1), h.details(0));
  h.respond(1, {ok: true, recorded: true, duplicate: true, bet: h.bet(1)}); await retry;
  assert.equal(restored.phase, 'confirmed');
});

test('Saved success is rechecked after reload; another bet requires fresh stake and a different ID', async () => {
  const h = harness(); h.fill(); const first = h.controller.submit(true);
  h.respond(0, {ok: true, recorded: true, bet: h.bet(0)}); await first;
  const restored = h.create(); assert.equal(restored.phase, 'uncertain');
  const check = restored.read(); assert.equal(h.requests[1].options.method, 'GET');
  h.respond(1, {schema_version: 1, bets: {[h.bet(0).bet_id]: h.bet(0)}}); await check;
  assert.equal(restored.phase, 'confirmed');
  restored.another(); assert.equal(restored.phase, 'editing'); assert.equal(restored.draft.stake, '');
  assert.equal(await restored.submit(true), false);
  restored.update('stake', '80'); const second = restored.submit(true);
  assert.notEqual(h.details(2).bet_id, h.details(0).bet_id);
  h.respond(2, {ok: true, recorded: true, bet: h.bet(2)}); await second;
});

test('Conflict and malformed success never claim recording; unavailable reads preserve confirmation state', async () => {
  const h = harness(); h.fill(); const pending = h.controller.submit(true);
  h.respond(0, {ok: false, error: 'Confirmation id already has different immutable details'}, 409); await pending;
  assert.equal(h.controller.phase, 'conflict');
  assert.equal(await h.controller.submit(true), false);
  const check = h.controller.read(); h.respond(1, {schema_version: 1, bets: []});
  await assert.rejects(check, /unavailable/);
  assert.equal(h.controller.phase, 'conflict');
  const other = harness(); other.fill(); const malformed = other.controller.submit(true);
  other.respond(0, {ok: true, recorded: true, bet: {...other.bet(0), stake: 500}}); await malformed;
  assert.equal(other.controller.phase, 'uncertain');
});

test('Read reconciliation during a POST cannot reset accepted confirmation on a later timeout', async () => {
  const h = harness(); h.fill(); const post = h.controller.submit(true);
  const check = h.controller.read(); h.respond(1, {schema_version: 1, bets: {[h.bet(0).bet_id]: h.bet(0)}}); await check;
  assert.equal(h.controller.phase, 'confirmed');
  h.requests[0].reject(new Error('late timeout'));
  assert.equal(await post, true);
  assert.equal(h.controller.phase, 'confirmed');
  assert.equal(h.controller.pending, null);
});

test('Conditional-write contention leaves the same confirmation retryable', async () => {
  const h = harness(); h.fill(); const first = h.controller.submit(true);
  h.respond(0, {ok: false, error: 'Confirmation conflict; retry with the same id'}, 409); await first;
  assert.equal(h.controller.phase, 'uncertain');
  const retry = h.controller.submit(true); assert.deepEqual(h.details(1), h.details(0));
  h.respond(1, {ok: true, recorded: true, bet: h.bet(1)}); await retry;
  assert.equal(h.controller.phase, 'confirmed');
});

test('Confirmation form requires actual venue/line/stake and survives price-only drawer refresh', () => {
  const h = harness(); assert.equal(h.controller.valid(), false);
  h.fill(); assert.equal(h.controller.valid(), true);
  h.controller.update('stake', '0'); assert.equal(h.controller.valid(), false);
  const drawer = readFileSync(new URL('../../web/drawer.js', import.meta.url), 'utf8');
  const refresh = drawer.slice(drawer.indexOf('function refreshDrawerQuotes'), drawer.indexOf('function renderDrawerTitle'));
  assert.doesNotMatch(refresh, /confirmationControls|placed-bet/);
  const markup = readFileSync(new URL('../../web/bet-confirmation.js', import.meta.url), 'utf8');
  assert.match(markup, /ack.checked = false/);
  assert.match(markup, /if \(!IS_ADMIN\) return ''/);
  assert.match(markup, /document.getElementById\('placed-bet-form'\) !== form/);
});
