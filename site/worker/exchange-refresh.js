// Direct, request-correlated public quotes. No publication, dispatch or orders.
import { alertLiquidity } from './alert-liquidity.js';
import { DEPTH_ADAPTERS } from './execution-preview.js';
import { expireCardQuotes } from '../web/current-quotes.mjs';

export async function exchangeRefresh(env, { sport, request_id, requested_at }, fetchImpl = fetch,
  { deadlineMs = 25000 } = {}) {
  const started = Date.now(), requested = Date.parse(requested_at);
  if (!['nfl', 'cfb'].includes(sport) || !/^[a-zA-Z0-9_-]{1,100}$/.test(request_id || '')
      || !Number.isFinite(requested) || requested > started + 1000 || started - requested > 30000)
    throw new Error('Exchange refresh requires a current request identity and selected sport');
  const timeout = Math.min(25000, Math.max(1, deadlineMs));
  const controller = new AbortController();
  let timer;
  const operation = async () => {
    const [metaObject, gamesObject] = await Promise.all([
      env.ODDS.get('board/meta.json'), env.ODDS.get(`board/games_${sport}.json`),
    ]);
    if (!metaObject || !gamesObject) throw new Error('Published board unavailable');
    const [meta, payload] = await Promise.all([metaObject.json(), gamesObject.json()]);
    const games = Array.isArray(payload) ? payload : payload?.games;
    if (!meta?.run_id || !Array.isArray(games) || games.some(g => !g || g.run_id !== meta.run_id))
      throw new Error('Published board generation mismatch');
    const output = games.map(g => expireCardQuotes(g, started));
    let cursor = 0, freshCount = 0, checked = 0;
    const boundedFetch = (url, options) => fetchImpl(url, { ...options,
      signal: AbortSignal.any([controller.signal, options.signal]) });
    await Promise.all(Array.from({ length: Math.min(4, games.length) }, async () => {
      while (cursor < games.length && !controller.signal.aborted) {
        const index = cursor++, game = games[index];
        if (!(Date.parse(game.kickoff_utc) > Date.now()) || /final|cancel|postpon|suspend|live|progress/i.test(game.status || '')) continue;
        const center = game.consensus?.total_now ?? 45.5;
        const refs = (game.execution_markets || []).filter(r => Object.hasOwn(DEPTH_ADAPTERS, r.book)
          && Number.isFinite(r.line) && r.line % 1 === .5)
          .sort((a, b) => Math.abs(a.line - center) - Math.abs(b.line - center) || a.book.localeCompare(b.book)).slice(0, 12);
        if (!refs.length) { output[index].fresh_odds = { status: 'unmapped', request_id }; continue; }
        const old = game.total_prices?.quotes || [];
        const quotes = refs.map(r => ({ ...(old.find(q => q.book === r.book && q.line === r.line && q.side === 'under') || {}),
          book: r.book, line: r.line, side: 'under', push_prob: 0 }));
        const snapshot = await alertLiquidity({ ...game, execution_markets: refs,
          total_prices: { ...game.total_prices, quotes } }, boundedFetch, Date.now(), { fresh: true });
        checked++;
        if (snapshot.capacity_status === 'expired') {
          output[index].fresh_odds = { status: 'expired', request_id }; continue;
        }
        const updates = snapshot.quotes.filter(q => q.liquidity_status === 'verified');
        freshCount += updates.length;
        const nearest = [...updates].sort((a, b) => Math.abs(a.line - center) - Math.abs(b.line - center))[0];
        output[index].fresh_odds = { status: updates.length ? 'public_depth_received' : 'unavailable', request_id,
          checked_at: snapshot.checked_at, expires_at: snapshot.expires_at,
          selected_line: nearest?.line ?? null, quotes_checked: refs.length,
          markets_not_checked: Math.max(0, (game.execution_markets || []).length - refs.length),
          provider_results: snapshot.quotes };
        if (updates.length) output[index].total_prices = { ...output[index].total_prices,
          probability_status: game.total_prices?.probability_status || 'empirically_unvalidated',
          recommendation_status: 'weather_watch', best: null,
          quotes: updates.map(q => ({ ...q, fetched_at: q.depth_fetched_at,
            expires_at: snapshot.expires_at, available_cash_stake: q.liquidity_dollars, fees: q.liquidity_fees })) };
      }
    }));
    if (games.length && !freshCount) throw new Error('No fresh exchange quotes received; source clocks unchanged');
    const completed_at = new Date().toISOString();
    return { ok: true, request_id, requested_at, sport, scope: 'exchanges', completed_at,
      delivery_mode: 'direct_public_depth', published: false, can_execute: false,
      elapsed_ms: Date.now() - started, fresh_quote_count: freshCount, games_checked: checked,
      partial: output.some(g => g.fresh_odds?.status !== 'public_depth_received'),
      // Preserve publication/model generation and clocks; this is a direct
      // quote response, not a new weather publication or pipeline generation.
      meta: { ...meta, refresh: { request_id, sport, scope: 'exchanges', completed_at, published: false } }, games: output };
  };
  try {
    return await Promise.race([operation(), new Promise((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new Error('Exchange quote deadline exceeded')); }, timeout);
    })]);
  } finally { clearTimeout(timer); controller.abort(); }
}
