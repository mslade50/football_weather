// This ledger records an explicit user statement. It never submits an order.
const KEY = 'board/bet_confirmations.json';
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
const fields = ['bet_id', 'game_id', 'book', 'line', 'stake', 'side'];
export async function betConfirmationsRoute(request, env, identity) {
  if (identity.role !== 'admin') return reply({ ok: false, error: 'Admin access required' }, 403);
  if (request.method === 'GET') {
    const object = await env.ODDS.get(KEY);
    return reply(object ? await object.json() : { schema_version: 1, bets: {} });
  }
  if (request.method !== 'POST') return reply({ ok: false, error: 'GET or POST required' }, 405);
  if (request.headers.get('origin') !== new URL(request.url).origin
      || !request.headers.get('content-type')?.startsWith('application/json'))
    return reply({ ok: false, error: 'Same-origin JSON confirmation required' }, 403);
  const text = await request.text();
  if (text.length > 4096) return reply({ ok: false, error: 'Confirmation too large' }, 400);
  let body;
  try { body = JSON.parse(text); } catch { return reply({ ok: false, error: 'Invalid JSON' }, 400); }
  if (!body || body.confirmed !== true || body.acknowledgement !== 'I placed this bet'
      || !/^[a-zA-Z0-9_-]{16,80}$/.test(body.bet_id || '')
      || !/^(nfl|cfb):\d{4}:\d{1,2}:[a-z0-9_.-]+@[a-z0-9_.-]+$/.test(body.game_id || '')
      || !['kalshi', 'polymarket_us', 'novig'].includes(body.book) || body.side !== 'under'
      || !Number.isFinite(body.line) || body.line < 10 || body.line > 150
      || !Number.isFinite(body.stake) || body.stake <= 0 || body.stake > 100000)
    return reply({ ok: false, error: 'Explicit placed-bet acknowledgement and valid details required' }, 400);
  for (let attempt = 0; attempt < 3; attempt++) {
    const existing = await env.ODDS.get(KEY);
    const ledger = existing ? await existing.json() : { schema_version: 1, bets: {} };
    if (ledger.schema_version !== 1 || !ledger.bets || typeof ledger.bets !== 'object' || Array.isArray(ledger.bets))
      return reply({ ok: false, error: 'Confirmation ledger unavailable' }, 503);
    const known = ledger.bets[body.bet_id];
    if (known) return fields.every(key => known[key] === body[key])
      ? reply({ ok: true, recorded: true, duplicate: true, bet: known })
      : reply({ ok: false, error: 'Confirmation id already has different immutable details' }, 409);
    if (existing && !existing.etag) return reply({ ok: false, error: 'Confirmation ownership receipt unavailable' }, 503);
    const bet = Object.fromEntries(fields.map(key => [key, body[key]]));
    Object.assign(bet, { confirmed: true, source: 'explicit_user_confirmation', confirmed_at: new Date().toISOString() });
    ledger.bets[body.bet_id] = bet;
    const onlyIf = existing ? new Headers({ 'If-Match': existing.httpEtag || `"${existing.etag}"` })
      : new Headers({ 'If-None-Match': '*' });
    const saved = await env.ODDS.put(KEY, JSON.stringify(ledger), { onlyIf, httpMetadata: { contentType: 'application/json' } });
    if (saved) return reply({ ok: true, recorded: true, bet });
  }
  return reply({ ok: false, error: 'Confirmation conflict; retry with the same id' }, 409);
}
