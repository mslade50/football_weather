// Alert snapshots share the execution preview's validated public depth and fee math.
import { kalshiDepth, polymarketDepth, allocateDepth, sliceCost } from './execution-preview.js';

export async function alertLiquidity(game, fetchImpl = fetch, now = Date.now()) {
  const quotes = (game.total_prices?.quotes || []).filter(q => q.side === 'under'
    && ['kalshi', 'polymarket_us'].includes(q.book) && Number.isFinite(q.win_prob)
    && q.win_prob >= 0 && q.win_prob <= 1 && q.push_prob === 0
    && now - Date.parse(q.updated_at) >= 0 && now - Date.parse(q.updated_at) <= 3600000);
  const result = { checked_at: new Date(now).toISOString(), quotes: [], allocations: [], spend: 0, unspent: 500 };
  if (!(Date.parse(game.kickoff_utc) > now) || /final|cancel|postpon|suspend|live|progress/i.test(game.status || '')) return result;
  const books = await Promise.all(quotes.map(async q => {
    const refs = (game.execution_markets || []).filter(r => r.book === q.book && r.line === q.line);
    const update = { book: q.book, line: q.line, side: 'under', liquidity_status: 'unknown' };
    if (refs.length !== 1) return { update: { ...update, liquidity_reason: 'Exact market mapping unavailable' } };
    try {
      const venue = await (q.book === 'kalshi' ? kalshiDepth : polymarketDepth)(refs[0], game, fetchImpl);
      if (Date.now() >= Date.parse(game.kickoff_utc)) throw new Error('Game started');
      const levels = venue.levels.map(l => ({ ...l, book: q.book, line: q.line, coefficient: venue.coefficient,
        roi: q.win_prob / (l.price + venue.coefficient * l.price * (1 - l.price)) - 1 }));
      const first = levels[0];
      if (!first) return { update: { ...update, liquidity_status: 'empty', liquidity_reason: 'No offers available' } };
      const cost = first.price + venue.coefficient * first.price * (1 - first.price);
      return { levels, update: { ...update, liquidity_status: 'verified', liquidity_shares: first.quantity,
        liquidity_dollars: Number(sliceCost(first.price, first.quantity, venue.coefficient).total) / 1000000,
        cost_prob: cost, odds: Math.round(cost >= .5 ? -100 * cost / (1 - cost) : 100 * (1 - cost) / cost),
        ev_roi: first.roi, updated_at: new Date().toISOString() } };
    } catch (error) { return { update: { ...update, liquidity_reason: error.message } }; }
  }));
  result.quotes = books.map(b => b.update);
  const levels = books.flatMap(b => b.levels || []).sort((a, b) => b.roi - a.roi || a.book.localeCompare(b.book));
  for (const level of levels) {
    const fill = allocateDepth([{ book: level.book, coefficient: level.coefficient, levels: [level] }], result.unspent, .99);
    if (!fill.allocations.length) continue;
    const allocation = fill.allocations[0];
    result.allocations.push({ ...allocation, line: level.line, ev_roi: level.roi, available_shares: level.quantity,
      available_dollars: Number(sliceCost(level.price, level.quantity, level.coefficient).total) / 1000000 });
    result.spend = Math.round((result.spend + fill.spend) * 100) / 100;
    result.unspent = Math.round((500 - result.spend) * 100) / 100;
  }
  return result;
}
