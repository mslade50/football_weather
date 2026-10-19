import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { collectResidentBatch } from '../../../scripts/resident_quotes.mjs';

const game = { game_id: 'nfl:2030:4:lar@phi', sport: 'nfl', kickoff_utc: '2030-10-02T17:00:00Z',
  execution_markets: [{ book: 'kalshi', line: 46.5, source_id: 'KXNFLTOTAL-30OCT01LARPHI-46' }] };

test('Malformed provider bodies are captured before pure adapter validation and never supply depth', async () => {
  const root = await mkdtemp(join(tmpdir(), 'football-resident-test-'));
  try {
    const result = await collectResidentBatch([game], root, async (url, options) => {
      assert.equal(options.method, 'GET');
      assert.equal(options.headers.Authorization, undefined);
      return new Response('not valid JSON', { headers: { 'content-type': 'application/json' } });
    });
    assert.equal(result[game.game_id].cash_liquidity_verified, false);
    const manifest = (await readFile(join(root, 'manifest.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(manifest.length, 3);
    for (const row of manifest) {
      assert.match(row.sha256, /^[a-f0-9]{64}$/);
      assert.equal(await readFile(join(root, row.file), 'utf8'), 'not valid JSON');
    }
    assert.equal((await readdir(root)).length, 4);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('Resident batch has a hard deadline even when public fetch ignores cancellation', async () => {
  const root = await mkdtemp(join(tmpdir(), 'football-resident-test-'));
  try {
    const started = performance.now();
    const result = await collectResidentBatch([game], root, () => new Promise(() => {}), 100);
    assert.deepEqual(result, {});
    assert.ok(performance.now() - started < 1000);
  } finally { await rm(root, { recursive: true, force: true }); }
});
