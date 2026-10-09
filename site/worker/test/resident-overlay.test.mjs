import test from 'node:test';
import assert from 'node:assert/strict';
import { residentEvidence, mergeResidentCard } from '../../web/resident-quotes.mjs';

const now = Date.parse('2026-10-09T20:00:00Z'), sha = 'a'.repeat(40), gid = 'nfl:2026:6:a@b';
const meta = { run_id: 'fixture-run', git_sha: sha, last_updated: '2026-10-09T03:08:14Z' };
function live() {
  return { schema_version: 1, status: 'fresh', owner_id: 'fixture-owner', sequence: 7, git_sha: sha,
    base_run_id: meta.run_id, heartbeat_at: new Date(now - 1000).toISOString(), fresh_games: 999,
    snapshots: { [gid]: { board_run_id: meta.run_id, checked_at: new Date(now - 1000).toISOString(),
      expires_at: new Date(now + 14000).toISOString(), quotes: [{ book: 'kalshi', side: 'under', line: 46.5,
        cost_prob: .51, liquidity_status: 'verified', fee_status: 'current_fee_verified',
        settlement_verified: true, rules_key: 'fixture', rules: 'fixture actual terms',
        depth_fetched_at: new Date(now - 1200).toISOString(), liquidity_dollars: 300, liquidity_fees: 9 }] } } };
}
test('Resident overlay preserves all historical/model clocks and never qualifies cash without a preview', () => {
  const evidence = residentEvidence(meta, live(), now);
  assert.equal(evidence.fresh_games, 1);
  const game = { game_id: gid, run_id: meta.run_id, weather: { fetched_at: meta.last_updated },
    odds_baselines: { first_observed: { total: 55 } }, consensus: { total_now: 45.5 },
    execution_markets: [{ book: 'kalshi', line: 46.5 }], total_prices: { best: { old: true }, quotes: [] } };
  const merged = mergeResidentCard(game, evidence, now);
  assert.equal(merged.weather, game.weather);
  assert.equal(merged.consensus, game.consensus);
  assert.equal(merged.odds_baselines, game.odds_baselines);
  assert.equal(merged.fresh_odds.selected_line, 46.5);
  assert.equal(merged.total_prices.best, null);
  assert.equal(merged.total_prices.recommendation_status, 'weather_watch');
  assert.equal(merged.total_prices.quotes[0].fetched_at, live().snapshots[gid].quotes[0].depth_fetched_at);
  assert.equal(merged.execution_preview, undefined);
});
test('Unknown ownership/revision, stale/future/degraded heartbeat and expired/mixed quotes fail closed', () => {
  for (const change of [{ owner_id: '' }, { git_sha: 'b'.repeat(40) }, { base_run_id: 'other' },
    { heartbeat_at: new Date(now + 1).toISOString() }, { heartbeat_at: new Date(now - 45001).toISOString() },
    { status: 'degraded' }]) assert.equal(residentEvidence(meta, { ...live(), ...change }, now).status, 'unavailable');
  for (const change of [{ board_run_id: 'other' }, { expires_at: new Date(now).toISOString() },
    { checked_at: new Date(now - 15000).toISOString() }]) {
    const data = live(); Object.assign(data.snapshots[gid], change);
    assert.equal(residentEvidence(meta, data, now).status, 'degraded');
  }
  const data = live(); data.snapshots[gid].quotes[0].depth_fetched_at = new Date(now - 15000).toISOString();
  assert.equal(residentEvidence(meta, data, now).status, 'degraded');
});
