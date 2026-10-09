import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { pinPublication } from '../publication.js';
import { handleFetch } from '../index.js';

const hash = value => createHash('sha256').update(value).digest('hex');
function seal(store, run) {
  const meta = { run_id: run, git_sha: 'a'.repeat(40), sport_counts: { nfl: 1, cfb: 0 } };
  const payloads = { 'meta.json': JSON.stringify(meta),
    'games_nfl.json': JSON.stringify({ meta: { run_id: run }, games: [{ game_id: 'fixture', run_id: run }] }),
    'games_cfb.json': JSON.stringify({ meta: { run_id: run }, games: [] }) };
  const manifest = JSON.stringify({ schema_version: 1, run_id: run, git_sha: meta.git_sha,
    objects: Object.fromEntries(Object.entries(payloads).map(([name, text]) => [name, { sha256: hash(text), bytes: Buffer.byteLength(text) }])) });
  const generation = hash(manifest), prefix = `board/generations/${generation}/`;
  for (const [name, text] of Object.entries(payloads)) store.set(prefix + name, text);
  store.set(prefix + 'manifest.json', manifest);
  return { ...meta, publication: { schema_version: 1, generation, manifest_key: prefix + 'manifest.json', manifest_sha256: generation } };
}
function bucket(store) {
  return { get: async key => store.has(key) ? new Response(store.get(key)) : null,
    put: () => { throw new Error('Read-only test must not write'); } };
}
test('Pinned publication stays coherent across a new pointer and altered legacy objects', async () => {
  const store = new Map(), old = seal(store, 'old'); store.set('board/meta.json', JSON.stringify(old));
  const pinned = await pinPublication(bucket(store));
  const next = seal(store, 'next'); store.set('board/meta.json', JSON.stringify(next));
  store.set('board/games_nfl.json', '{"games":[{"run_id":"mixed"}]}');
  assert.equal((await (await pinned.bucket.get('board/games_nfl.json')).json()).games[0].run_id, 'old');
  const selected = await pinPublication(bucket(store), old.publication.generation);
  assert.equal(selected.meta.run_id, 'old');
  assert.equal(selected.meta.publication_status, 'manifest_verified');
});
test('Missing or corrupt immutable payload returns unavailable instead of mixed data', async () => {
  const store = new Map(), meta = seal(store, 'old'); store.set('board/meta.json', JSON.stringify(meta));
  store.set(`board/generations/${meta.publication.generation}/games_nfl.json`, '{"games":[]}');
  const response = await handleFetch(new Request('https://fixture.invalid/data/games_nfl.json'), { ODDS: bucket(store) });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).publication_status, 'unavailable');
  store.delete(meta.publication.manifest_key);
  assert.equal((await handleFetch(new Request('https://fixture.invalid/data/meta.json'), { ODDS: bucket(store) })).status, 503);
});

test('Quote response deadline includes a noncooperative R2 publication read', async () => {
  const started = performance.now();
  const env = { ODDS: { get: () => new Promise(() => {}), put: () => assert.fail('No quote writes') } };
  const response = await handleFetch(new Request('https://fixture.invalid/api/fresh-odds?game_id=nfl:2030:4:a@b'), env, 100);
  assert.equal(response.status, 503);
  assert.ok(performance.now() - started < 1000);
  assert.match((await response.json()).error, /publication verification/);
});

test('New delivery receipts are visible only on their current run; archived generations stay immutable', async () => {
  const store = new Map(), meta = seal(store, 'old'); store.set('board/meta.json', JSON.stringify(meta));
  store.set('board/alerts_live_feed.json', JSON.stringify({ meta: { run_id: 'old' }, alerts: [{ alert_key: 'sent-after-seal', sent_at: '2030-10-01T12:00:00Z' }] }));
  const pinned = await pinPublication(bucket(store));
  const feed = await (await pinned.bucket.get('board/alerts_feed.json')).json();
  assert.equal(feed.alerts[0].alert_key, 'sent-after-seal');
  assert.equal(feed.delivery_stream_status, 'current_run_receipts');
  const historical = await pinPublication(bucket(store), meta.publication.generation);
  assert.equal(await historical.bucket.get('board/alerts_feed.json'), null);
  store.set('board/alerts_live_feed.json', JSON.stringify({ meta: { run_id: 'other' }, alerts: [{ alert_key: 'wrong-run' }] }));
  assert.equal(await pinned.bucket.get('board/alerts_feed.json'), null);
});
