// Pure quote overlay shared by browser and Worker. Publication/model clocks stay intact.
export function residentEvidence(meta, live, now = Date.now()) {
  const unavailable = reason => ({ status: 'unavailable', reason, snapshots: {} });
  const heartbeatAge = now - Date.parse(live?.heartbeat_at);
  if (!live || live.schema_version !== 1 || live.status !== 'fresh' || !live.owner_id
      || !Number.isSafeInteger(live.sequence) || live.sequence < 1 || !(heartbeatAge >= 0 && heartbeatAge <= 45000))
    return unavailable('Resident collector heartbeat is absent, degraded, stale or future');
  if (live.base_run_id !== meta?.run_id || !/^[a-f0-9]{40}$/.test(live.git_sha || '') || live.git_sha !== meta?.git_sha)
    return unavailable('Resident collector publication or source revision mismatch');
  const snapshots = {};
  for (const [gid, snapshot] of Object.entries(live.snapshots || {})) {
    const checkedAge = now - Date.parse(snapshot?.checked_at);
    if (snapshot?.board_run_id !== meta.run_id || !(checkedAge >= 0 && checkedAge < 15000)
        || !(Date.parse(snapshot.expires_at) > now) || !Array.isArray(snapshot.quotes)) continue;
    const quotes = snapshot.quotes.filter(q => {
      const depthAge = now - Date.parse(q.depth_fetched_at);
      return ['kalshi', 'polymarket_us', 'novig'].includes(q.book) && q.side === 'under'
        && Number.isFinite(q.line) && q.line % 1 === .5 && Number.isFinite(q.cost_prob) && q.cost_prob > 0 && q.cost_prob < 1
        && q.liquidity_status === 'verified' && q.fee_status === 'current_fee_verified'
        && q.settlement_verified === true && q.rules_key && typeof q.rules === 'string' && q.rules.trim()
        && depthAge >= 0 && depthAge < 15000;
    });
    if (quotes.length) snapshots[gid] = { ...snapshot, quotes };
  }
  return { owner_id: live.owner_id, sequence: live.sequence, git_sha: live.git_sha, heartbeat_at: live.heartbeat_at,
    base_run_id: live.base_run_id, status: Object.keys(snapshots).length ? 'fresh' : 'degraded',
    fresh_games: Object.keys(snapshots).length, mapped_upcoming_games: live.mapped_upcoming_games,
    reason: Object.keys(snapshots).length ? 'Public depth references; account balances and eligibility unchecked' : 'Resident quotes expired or unavailable', snapshots };
}

export function mergeResidentCard(original, resident, now = Date.now()) {
  const snapshot = resident?.status === 'fresh' && resident.base_run_id === original.run_id ? resident.snapshots?.[original.game_id] : null;
  if (!snapshot || !(Date.parse(snapshot.expires_at) > now)) return original;
  const mapped = snapshot.quotes.filter(q => (original.execution_markets || []).some(r => r.book === q.book && r.line === q.line));
  if (!mapped.length) return original;
  const oldClock = Math.max(0, ...(original.total_prices?.quotes || []).map(q => Date.parse(q.fetched_at) || 0));
  if (Math.max(...mapped.map(q => Date.parse(q.depth_fetched_at))) < oldClock) return original;
  const center = original.consensus?.total_now ?? 45.5;
  const selected = [...mapped].sort((a, b) => Math.abs(a.line - center) - Math.abs(b.line - center))[0];
  return { ...original, resident_quote_generation: `${resident.owner_id}:${resident.sequence}`,
    fresh_odds: { status: 'public_depth_received', selected_line: selected.line, expires_at: snapshot.expires_at,
      request_id: snapshot.quote_request_id, checked_at: snapshot.checked_at },
    total_prices: { ...original.total_prices, best: null, best_under: null, best_over: null,
      recommendation_status: 'weather_watch', probability_status: 'empirically_unvalidated',
      quotes: mapped.map(q => ({ ...q, fetched_at: q.depth_fetched_at, expires_at: snapshot.expires_at,
        available_cash_stake: q.liquidity_dollars, fees: q.liquidity_fees })) } };
}
