// Authenticated read-only depth retrieval. No workflow dispatch, order, or R2 write.
import { alertLiquidity } from './alert-liquidity.js';
import { DEPTH_ADAPTERS } from './execution-preview.js';

export async function freshOddsRoute(request, env, fetchImpl = fetch) {
  const respond = (body, status = 200) => Response.json(body, { status, headers: { 'cache-control': 'no-store' } });
  if (request.method !== 'GET') return respond({ ok: false, error: 'Fresh odds supports GET only' }, 405);
  const url = new URL(request.url), gameId = url.searchParams.get('game_id') || '';
  const requested = url.searchParams.has('line') ? Number(url.searchParams.get('line')) : null;
  if (!/^(nfl|cfb):\d{4}:\d{1,2}:[a-z0-9_.-]+@[a-z0-9_.-]+$/.test(gameId) || gameId.length > 150
      || (requested !== null && (!Number.isFinite(requested) || requested < .5 || requested > 150 || requested % 1 !== .5)))
    return respond({ ok: false, error: 'Select a game and an optional half-point total' }, 400);
  const started = Date.now(), deadline = AbortSignal.timeout(25000);
  let timer;
  try {
    const operation = async () => {
      const object = await env.ODDS.get(`board/games_${gameId.slice(0, 3)}.json`);
      const payload = object ? await object.json() : [];
      const game = (Array.isArray(payload) ? payload : payload.games || []).find(g => g.game_id === gameId);
      if (!game) return respond({ ok: false, error: 'Game is not on the current board' }, 404);
      if (!(Date.parse(game.kickoff_utc) > started) || /final|cancel|postpon|suspend|live|progress/i.test(game.status || ''))
        return respond({ ok: false, error: 'Pregame odds only; this game has started or is unavailable' }, 409);
      const all = (game.execution_markets || []).filter(r => Object.hasOwn(DEPTH_ADAPTERS, r.book)
        && Number.isFinite(r.line) && r.line % 1 === .5 && (requested === null || r.line === requested));
      const center = requested ?? game.consensus?.total_now ?? 45.5;
      const refs = all.sort((a, b) => Math.abs(a.line - center) - Math.abs(b.line - center) || a.book.localeCompare(b.book)).slice(0, 12);
      const old = game.total_prices?.quotes || [];
      const quotes = refs.map(r => ({ ...(old.find(q => q.book === r.book && q.line === r.line && q.side === 'under') || {}),
        book: r.book, line: r.line, side: 'under', push_prob: 0 }));
      const boundedFetch = (uri, options) => fetchImpl(uri, { ...options,
        signal: AbortSignal.any([deadline, options.signal]) });
      const snapshot = await alertLiquidity({ ...game, execution_markets: refs,
        total_prices: { ...game.total_prices, quotes } }, boundedFetch, Date.now(), { fresh: true });
      if (snapshot.capacity_status === 'expired') return respond({ ok: false, can_execute: false,
        error: 'Depth snapshot expired; request fresh odds again', elapsed_ms: Date.now() - started }, 503);
      return respond({ ok: true, mode: 'public_taker_depth', can_execute: false, game_id: gameId,
        board_run_id: game.run_id, model_observed_at: game.weather?.fetched_at ?? null,
        requested_line: requested, markets_checked: refs.length, markets_not_checked: Math.max(0, all.length - refs.length),
        fresh_quote_count: snapshot.quotes.filter(q => q.liquidity_status === 'verified').length,
        elapsed_ms: Date.now() - started, partial: refs.length === 0 || snapshot.quotes.some(q => q.liquidity_status === 'unknown'), ...snapshot });
    };
    return await Promise.race([operation(), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Fresh odds deadline exceeded')), 25000);
    })]);
  } catch (error) {
    return respond({ ok: false, can_execute: false, error: error.message || 'Fresh odds unavailable',
      elapsed_ms: Date.now() - started }, 503);
  } finally { clearTimeout(timer); }
}
