// Alert snapshots share the execution preview's validated public depth and fee math.
import { DEPTH_ADAPTERS, sliceCost, PREVIEW_TTL_MS } from './execution-preview.js';

const MICROS = 1000000;
const REQUIRED_CASH_STAKE = 500;

export async function alertLiquidity(game, fetchImpl = fetch, now = Date.now(), { fresh = false } = {}) {
  const started = Date.now();
  const quotes = (game.total_prices?.quotes || []).filter(q => q.side === 'under'
    && Object.hasOwn(DEPTH_ADAPTERS, q.book)
    && (fresh || (Number.isFinite(q.win_prob) && q.win_prob >= 0 && q.win_prob <= 1))
    && q.push_prob === 0
    && (fresh || (now - Date.parse(q.updated_at) >= 0 && now - Date.parse(q.updated_at) <= 3600000)));
  const result = { checked_at: new Date(now).toISOString(), expires_at: new Date(now + PREVIEW_TTL_MS).toISOString(),
    quotes: [], allocations: [], required_cash_stake: REQUIRED_CASH_STAKE, cash_stake_capacity: 0,
    fee_capacity: 0, debit_capacity: 0, payout_capacity: 0, cash_liquidity_verified: false,
    // Capacity is independent of the user's chosen bet size. Never a $500 budget.
    capacity_status: 'unknown', spend: 0, unspent: REQUIRED_CASH_STAKE, can_execute: false,
    probability_status: game.total_prices?.probability_status || 'empirically_unvalidated' };
  if (!(Date.parse(game.kickoff_utc) > now) || /final|cancel|postpon|suspend|live|progress/i.test(game.status || '')) return result;
  const books = await Promise.all(quotes.map(async q => {
    const refs = (game.execution_markets || []).filter(r => r.book === q.book && r.line === q.line);
    const update = { book: q.book, line: q.line, side: 'under', liquidity_status: 'unknown' };
    if (refs.length !== 1) return { update: { ...update, liquidity_reason: 'Exact market mapping unavailable' } };
    try {
      const venue = await DEPTH_ADAPTERS[q.book](refs[0], game, fetchImpl);
      if (Date.now() >= Date.parse(game.kickoff_utc)) throw new Error('Game started');
      if (typeof venue.rules !== 'string' || !venue.rules.trim()) throw new Error('Settlement terms unavailable');
      // Conservative compatibility: identical actual terms at one venue and
      // exact line/side. Cross-venue policy equivalence is never inferred from
      // both markets being called "full-game total".
      const terms = venue.rules.trim().replace(/\s+/g, ' ');
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${q.book}:${terms}`));
      const rulesKey = `${q.book}:${[...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('')}`;
      const value = venue.contract_value ?? 1;
      const levels = venue.levels.map(l => {
        const cost = sliceCost(l.price, l.quantity, venue.coefficient, value, venue.fee_model);
        const payout = l.quantity * value;
        return { ...l, book: q.book, line: q.line, side: 'under', rules_key: rulesKey, settlement_verified: true,
          rules: venue.rules, rules_url: venue.rules_url, coefficient: venue.coefficient, fee_model: venue.fee_model,
          contract_value: value, cost, payout, roi: Number.isFinite(q.win_prob) && q.win_prob >= 0 && q.win_prob <= 1
            ? q.win_prob * payout / (Number(cost.total) / MICROS) - 1 : null };
      });
      const first = levels[0];
      if (!first) return { update: { ...update, liquidity_status: 'empty', liquidity_reason: 'No offers available' } };
      const cost = Number(first.cost.total) / MICROS / first.payout;
      return { levels, update: { ...update, liquidity_status: 'verified', liquidity_shares: first.quantity,
        execution_status: 'public_taker_depth_verified', fee_status: 'current_fee_verified',
        rules_key: rulesKey, settlement_verified: true, rules: venue.rules, rules_url: venue.rules_url,
        contract_value: first.contract_value,
        liquidity_dollars: Number(first.cost.principal) / MICROS,
        liquidity_fees: Number(first.cost.fee) / MICROS, liquidity_debit: Number(first.cost.total) / MICROS,
        liquidity_payout: first.payout, fee_model: venue.fee_model, fee_coefficient: venue.coefficient,
        cost_prob: cost, odds: Math.round(cost >= .5 ? -100 * cost / (1 - cost) : 100 * (1 - cost) / cost),
        ev_roi: first.roi, quote_observed_at: q.updated_at, depth_fetched_at: venue.depth_fetched_at,
        updated_at: venue.depth_fetched_at } };
    } catch (error) { return { update: { ...update, liquidity_reason: error.message } }; }
  }));
  if (Date.now() - started >= PREVIEW_TTL_MS) {
    result.capacity_status = 'expired';
    return result;
  }
  result.quotes = books.map(b => b.update);
  const levels = books.flatMap(b => b.levels || []).filter(l => Number.isFinite(l.roi) && l.roi > 0)
    .sort((a, b) => b.roi - a.roi || a.book.localeCompare(b.book));
  const groups = new Map();
  for (const level of levels) {
    const key = `${game.game_id}:under:${level.line}:${level.rules_key}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(level);
  }
  const compatible = [...groups].map(([key, group]) => ({ key, group,
    principal: group.reduce((n, l) => n + l.cost.principal, 0n),
    estimatedRoi: group.reduce((n, l) => n + l.roi * Number(l.cost.total), 0) / group.reduce((n, l) => n + Number(l.cost.total), 0),
  })).sort((a, b) => Number(b.principal >= BigInt(REQUIRED_CASH_STAKE * MICROS))
    - Number(a.principal >= BigInt(REQUIRED_CASH_STAKE * MICROS)) || b.estimatedRoi - a.estimatedRoi || a.key.localeCompare(b.key));
  result.compatible_groups = compatible.map(({ key, group, principal: cash, estimatedRoi }) => ({
    compatibility_key: key, rules_key: group[0].rules_key, side: 'under', line: group[0].line,
    settlement_verified: true, rules: group[0].rules, rules_url: group[0].rules_url,
    cash_stake_capacity: Number(cash) / MICROS,
    cash_liquidity_verified: cash >= BigInt(REQUIRED_CASH_STAKE * MICROS), estimated_roi: estimatedRoi,
  }));
  const selected = compatible[0];
  result.compatibility_key = selected?.key ?? null;
  result.rules_key = selected?.group[0].rules_key ?? null;
  result.settlement_verified = !!selected;
  result.line = selected?.group[0].line ?? null;
  result.side = 'under';
  let principal = 0n, fees = 0n, debit = 0n;
  for (const level of selected?.group || []) {
    const cost = level.cost;
    principal += cost.principal; fees += cost.fee; debit += cost.total;
    result.payout_capacity += level.payout;
    result.allocations.push({ book: level.book, line: level.line, side: 'under', rules_key: level.rules_key,
      settlement_verified: true, rules: level.rules, rules_url: level.rules_url, ev_roi: level.roi, quantity: level.quantity,
      ask: level.price, contract_value: level.contract_value, fee_model: level.fee_model,
      payout_if_win: level.payout, principal: Number(cost.principal) / MICROS,
      fees: Number(cost.fee) / MICROS, spend: Number(cost.total) / MICROS,
      all_in_price: Number(cost.total) / MICROS / level.payout,
      available_shares: level.quantity, available_dollars: Number(cost.principal) / MICROS });
  }
  result.cash_stake_capacity = Number(principal) / MICROS;
  result.fee_capacity = Number(fees) / MICROS;
  result.debit_capacity = result.spend = Number(debit) / MICROS;
  result.unspent = Math.max(0, REQUIRED_CASH_STAKE - result.cash_stake_capacity);
  result.cash_liquidity_verified = principal >= BigInt(REQUIRED_CASH_STAKE * MICROS);
  result.capacity_status = result.cash_liquidity_verified ? 'verified_cash_capacity' :
    result.quotes.some(q => q.liquidity_status === 'verified') ? 'insufficient_cash_capacity' : 'unknown';
  return result;
}
