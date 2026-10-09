import test from 'node:test';
import assert from 'node:assert/strict';
import { betConfirmationsRoute } from '../bet-confirmations.js';
const details = { bet_id: 'fixture-confirmation-id', game_id: 'nfl:2026:6:a@b', book: 'kalshi',
  line: 48.5, stake: 125, side: 'under', confirmed: true, acknowledgement: 'I placed this bet' };
const request = (body = details, origin = 'https://fixture.invalid') => new Request('https://fixture.invalid/api/bet-confirmations',
  { method: 'POST', headers: { origin, 'content-type': 'application/json' }, body: JSON.stringify(body) });
test('Confirmation requires acknowledgement and keeps immutable idempotent details', async () => {
  let state, etag = 0, writes = 0;
  const env = { ODDS: { get: async () => state ? { etag: String(etag), json: async () => structuredClone(state) } : null,
    put: async (key, text, options) => {
      assert.equal(key, 'board/bet_confirmations.json');
      assert.ok(options.onlyIf.has(etag ? 'If-Match' : 'If-None-Match'));
      state = JSON.parse(text); writes++; etag++; return { etag: String(etag) };
    } } };
  assert.equal((await betConfirmationsRoute(request(), env, { role: 'viewer' })).status, 403);
  assert.equal((await betConfirmationsRoute(request(details, 'https://other.invalid'), env, { role: 'admin' })).status, 403);
  assert.equal((await betConfirmationsRoute(request({ ...details, confirmed: false }), env, { role: 'admin' })).status, 400);
  assert.equal(writes, 0);
  assert.equal((await betConfirmationsRoute(request(), env, { role: 'admin' })).status, 200);
  assert.equal((await betConfirmationsRoute(request(), env, { role: 'admin' })).status, 200);
  assert.equal((await betConfirmationsRoute(request({ ...details, stake: 500 }), env, { role: 'admin' })).status, 409);
  assert.equal(writes, 1);
  assert.equal(state.bets[details.bet_id].source, 'explicit_user_confirmation');
});
test('A lost conditional write cannot claim successful confirmation', async () => {
  const env = { ODDS: { get: async () => null, put: async () => null } };
  const response = await betConfirmationsRoute(request(), env, { role: 'admin' });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).ok, false);
});
