// Public-depth simulation only. This module never authenticates with an exchange,
// submits an order, or reserves funds. Do not reuse a preview as an order ticket.
const KALSHI = "https://api.elections.kalshi.com/trade-api/v2";
const POLY = "https://gateway.polymarket.us/v1";
const NOVIG = "https://api.novig.com/v3/public/catalog";
const SCALE = 1000000n;
const CENT = 10000n;
export const PREVIEW_TTL_MS = 15000;
const BOOKS = ["kalshi", "polymarket_us", "novig", "prophetx", "4cx"];

function numeric(value) {
  return (typeof value === "number" || (typeof value === "string" && value.trim() !== ""))
    && Number.isFinite(Number(value)) ? Number(value) : NaN;
}
function fixed(value) {
  const n = numeric(value);
  if (!Number.isFinite(n) || n < 0 || n > 1000000) throw new Error("Invalid monetary value");
  return BigInt(Math.round(n * Number(SCALE)));
}
const ceilDiv = (a, b) => (a + b - 1n) / b;
const dollars = n => Number(n) / Number(SCALE);
function nearestQuantum(numerator, denominator, quantum, halfEven = false) {
  const divisor = denominator * quantum, whole = numerator / divisor, remainder = numerator % divisor;
  const up = remainder * 2n > divisor || (remainder * 2n === divisor && (!halfEven || whole % 2n === 1n));
  return (whole + (up ? 1n : 0n)) * quantum;
}

// Each displayed slice is priced as a separate taker limit order. Round its
// total debit UP to cents (also conservative for Poly's half-even fee rounding).
// No speculative weekly rebates. Whole contracts only in this first preview.
export function sliceCost(price, quantity, coefficient, contractValue = 1, feeModel = 'conservative_cent') {
  const p = fixed(price), q = BigInt(quantity), r = fixed(coefficient);
  const value = fixed(contractValue);
  const principal = ceilDiv(p * q * value, SCALE);
  const numerator = r * p * (SCALE - p) * q * value, denominator = SCALE * SCALE * SCALE;
  let fee, total;
  if (feeModel === 'kalshi_direct') {
    fee = ceilDiv(numerator, denominator);
    total = ceilDiv(principal + fee, 100n) * 100n;
  } else if (feeModel === 'polymarket_us_cent_half_even') {
    fee = nearestQuantum(numerator, denominator, CENT, true);
    total = principal + fee;
  } else if (feeModel === 'novig_5dp_half_up') {
    fee = nearestQuantum(numerator, denominator, 10n);
    total = principal + fee;
  } else if (feeModel === 'conservative_cent') {
    fee = ceilDiv(numerator, denominator);
    total = ceilDiv(principal + fee, CENT) * CENT;
  } else throw new Error('Unknown taker fee precision');
  return { total, principal, fee: total - principal };
}

export function allocateDepth(venues, budget, maxPrice) {
  let remaining = fixed(budget);
  const limit = fixed(maxPrice);
  const slices = venues.flatMap(v => v.levels.map(l => ({ ...l, book: v.book, coefficient: v.coefficient,
    contract_value: v.contract_value ?? 1, fee_model: v.fee_model ?? 'conservative_cent' })))
    .sort((a, b) => (a.price + a.coefficient * a.price * (1 - a.price))
      - (b.price + b.coefficient * b.price * (1 - b.price)) || a.book.localeCompare(b.book));
  const allocations = [];
  let principal = 0n, fees = 0n, payout = 0n;
  for (const level of slices) {
    let lo = 0, hi = Math.floor(level.quantity);
    // A level above the all-in ceiling cannot become eligible by increasing size.
    if (level.price + level.coefficient * level.price * (1 - level.price) > maxPrice) continue;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (sliceCost(level.price, mid, level.coefficient, level.contract_value, level.fee_model).total <= remaining) lo = mid;
      else hi = mid - 1;
    }
    if (!lo) continue;
    const cost = sliceCost(level.price, lo, level.coefficient, level.contract_value, level.fee_model);
    const levelPayout = fixed(level.contract_value) * BigInt(lo);
    if (cost.total * SCALE > limit * levelPayout) continue;
    remaining -= cost.total;
    principal += cost.principal;
    fees += cost.fee;
    payout += levelPayout;
    allocations.push({ book: level.book, quantity: lo, ask: level.price,
      contract_value: level.contract_value, fee_model: level.fee_model, payout_if_win: dollars(levelPayout),
      principal: dollars(cost.principal), fees: dollars(cost.fee), spend: dollars(cost.total),
      all_in_price: dollars(cost.total) / dollars(levelPayout) });
  }
  const spend = dollars(principal + fees);
  return { allocations, spend, principal: dollars(principal), fees: dollars(fees),
    unspent: dollars(remaining), payout_if_win: dollars(payout), profit_if_win: dollars(payout) - spend,
    average_price: payout ? spend / dollars(payout) : null,
    worst_price: allocations.length ? Math.max(...allocations.map(a => a.all_in_price)) : null };
}

async function getJson(url, fetchImpl) {
  // Poly's gateway can cache books for 30s even with no-cache and omit Age.
  // A unique query was verified against both public APIs; never reuse that cache.
  const freshUrl = new URL(url);
  freshUrl.searchParams.set("_preview", crypto.randomUUID());
  const response = await fetchImpl(freshUrl.toString(), { method: "GET", headers: { Accept: "application/json", "Cache-Control": "no-cache" },
    cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(8000) });
  if (!response.ok) throw new Error(`Depth service returned HTTP ${response.status}`);
  if (numeric(response.headers.get("age")) > 5) throw new Error("Exchange returned a cached snapshot");
  return response.json();
}

function depth(rows, priceOf, quantityOf, maxQuantity = 1000000) {
  if (!Array.isArray(rows)) throw new Error("Order-book depth missing");
  const levels = new Map();
  for (const row of rows) {
    const price = priceOf(row), quantity = quantityOf(row);
    if (!Number.isFinite(price) || !Number.isFinite(quantity) || price <= 0 || price >= 1 || quantity < 0)
      throw new Error("Invalid order-book level");
    if (!quantity) continue;
    const key = Math.round(price * 1000000);
    levels.set(key, (levels.get(key) || 0) + quantity);
  }
  return [...levels].map(([p, q]) => ({ price: p / 1000000, quantity: Math.min(maxQuantity, Math.floor(q)) }))
    .filter(l => l.quantity > 0).sort((a, b) => a.price - b.price);
}

export async function kalshiDepth(ref, game, fetchImpl = fetch) {
  const series = game.sport === "cfb" ? "KXNCAAFTOTAL" : "KXNFLTOTAL";
  if (!new RegExp(`^${series}-[A-Z0-9]+-[0-9]+$`).test(ref.source_id)) throw new Error("Unverified market identity");
  const id = encodeURIComponent(ref.source_id);
  const [meta, book, fee] = await Promise.all([
    getJson(`${KALSHI}/markets/${id}`, fetchImpl),
    getJson(`${KALSHI}/markets/${id}/orderbook`, fetchImpl),
    getJson(`${KALSHI}/series/${series}`, fetchImpl),
  ]);
  const m = meta.market, s = fee.series;
  if (!m || m.ticker !== ref.source_id || m.status !== "active" || m.market_type !== "binary"
      || m.strike_type !== "greater" || numeric(m.floor_strike) !== ref.line
      || numeric(m.notional_value_dollars) !== 1 || !/^(?:Full Game: )?Over /i.test(m.title || "")
      || !m.rules_primary || !m.rules_secondary)
    throw new Error("Market is closed or its total/settlement could not be verified");
  const multiplier = numeric(s?.fee_multiplier);
  if (!["quadratic", "quadratic_with_maker_fees"].includes(s?.fee_type)
      || !Number.isFinite(multiplier) || multiplier < 0 || multiplier > 10)
    throw new Error("Current taker fees unavailable");
  return { book: "kalshi", source_id: ref.source_id, coefficient: .07 * multiplier, fee_model: 'kalshi_direct',
    // Buying NO (under) takes the complementary YES bid, never the YES ask.
    levels: depth(book.orderbook_fp?.yes_dollars, r => 1 - numeric(r[0]), r => numeric(r[1])),
    rules: `${m.rules_primary}\n${m.rules_secondary}`,
    rules_url: "https://assets.kalshi.com/contract_terms/FOOTBALLTOTALS.pdf" };
}

export async function polymarketDepth(ref, game, fetchImpl = fetch) {
  if (!new RegExp(`^tsc-${game.sport}-[a-z0-9-]+-total-[0-9]+pt5$`).test(ref.source_id))
    throw new Error("Unverified market identity");
  const id = encodeURIComponent(ref.source_id);
  const [meta, payload] = await Promise.all([
    getJson(`${POLY}/market/slug/${id}`, fetchImpl), getJson(`${POLY}/markets/${id}/book`, fetchImpl),
  ]);
  const m = meta.market, b = payload.marketData;
  const coefficient = numeric(m?.feeCoefficient);
  const under = m?.marketSides?.find(s => s.description?.toLowerCase() === "under");
  if (!m || m.slug !== ref.source_id || !m.active || m.closed || m.hidden || m.archived
      || m.ep3Status !== "OPEN" || m.sportsMarketType !== "football_team_full_game_total"
      || numeric(m.line) !== ref.line || under?.long !== false || under?.tradable !== true
      || Date.parse(m.gameStartTime) !== Date.parse(game.kickoff_utc)
      || !/overtime is included/i.test(m.description || "")
      || b?.marketSlug !== ref.source_id || b.state !== "MARKET_STATE_OPEN")
    throw new Error("Market is closed or its total/settlement could not be verified");
  if (!Number.isFinite(coefficient) || coefficient < 0 || coefficient > 1)
    throw new Error("Current taker fees unavailable");
  if (!(numeric(m.minimumTradeQty) > 0 && numeric(m.minimumTradeQty) <= 1))
    throw new Error("Unsupported minimum trade size");
  return { book: "polymarket_us", source_id: ref.source_id, coefficient, fee_model: 'polymarket_us_cent_half_even',
    levels: depth(b.bids, r => r.px?.currency === "USD" ? 1 - numeric(r.px.value) : NaN, r => numeric(r.qty)),
    rules: m.description, rules_url: "https://docs.polymarket.us/markets/market-rules" };
}

// https://docs.novig.com/api/concepts/money: a contract pays ONE CENT.
// Side identities come from the scraper's typed outcomes, never array position
// or public display names. A half-point TOTAL has no normal-game push.
export async function novigDepth(ref, game, fetchImpl = fetch) {
  const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  if (!new RegExp(`^${uuid}:${uuid}$`, 'i').test(ref.source_id)
      || !new RegExp(`^${uuid}$`, 'i').test(ref.outcome_ids?.over)
      || !new RegExp(`^${uuid}$`, 'i').test(ref.outcome_ids?.under)
      || ref.outcome_ids.over === ref.outcome_ids.under)
    throw new Error('Novig outcome mapping unavailable; refresh exchange lines');
  const [eventId, marketId] = ref.source_id.split(':');
  const [event, market, book] = await Promise.all([
    getJson(`${NOVIG}/events/${eventId}`, fetchImpl),
    getJson(`${NOVIG}/markets/${marketId}`, fetchImpl),
    getJson(`${NOVIG}/markets/${marketId}/book`, fetchImpl),
  ]);
  const outcomes = market.outcomes;
  if (event.eventId !== eventId || event.sport !== 'FOOTBALL'
      || event.league !== ({ nfl: 'NFL', cfb: 'NCAAF' })[game.sport] || event.status !== 'OPEN_PREGAME'
      || event.startsTs !== Date.parse(game.kickoff_utc) || event.startsTs <= Date.now()
      || market.eventId !== eventId || market.marketId !== marketId || market.startsTs !== event.startsTs
      || market.status !== 'OPEN' || market.marketType !== 'TOTAL' || numeric(market.strike) !== ref.line
      || ref.line % 1 !== .5 || !['FMV', 'PUSH'].includes(market.voids)
      || !Array.isArray(outcomes) || outcomes.length !== 2
      || !Object.values(ref.outcome_ids).every(id => outcomes.some(o => o.outcomeId === id && o.status === 'TBD'))
      || book.marketId !== marketId || !Number.isSafeInteger(book.seq) || book.seq < 0
      || !book.orders || Object.keys(book.orders).some(id => !outcomes.some(o => o.outcomeId === id)))
    throw new Error('Novig market is closed or its total/settlement could not be verified');
  const rate = numeric(market.fee?.coefficient), charged = market.fee?.charged;
  if (!Number.isFinite(rate) || rate < 0 || rate > 1 || !['WHEN_LIVE', 'ALWAYS'].includes(charged))
    throw new Error('Current taker fees unavailable');
  return { book: 'novig', source_id: ref.source_id, contract_value: .01,
    coefficient: charged === 'WHEN_LIVE' ? 0 : rate, fee_model: 'novig_5dp_half_up', submission: 'manual',
    levels: depth(book.orders[ref.outcome_ids.over] ?? [], r => 1 - numeric(r.price),
      r => Number.isSafeInteger(r.qty) && r.qty > 0 ? r.qty : NaN, 100000000),
    rules: `Full-game total. Each contract pays $0.01 if it wins. Void settlement: ${market.voids === 'FMV'
      ? 'fair market value, not a guaranteed refund' : 'refund at fill cost'}. Pregame taker fee schedule: ${charged}.`,
    rules_url: 'https://docs.novig.com/api/concepts/event-lifecycle' };
}

export const DEPTH_ADAPTERS = { kalshi: kalshiDepth, polymarket_us: polymarketDepth, novig: novigDepth };

export async function previewGame(game, { line, budget, maxPrice }, fetchImpl = fetch, now = Date.now()) {
  const started = Date.now();
  if (!(Date.parse(game.kickoff_utc) > now) || /final|cancel|postpon|suspend|live|progress/i.test(game.status || ""))
    throw new Error("Pre-game previews only; this game has started or is unavailable");
  const refs = (game.execution_markets || []).filter(r => r.line === line);
  const results = await Promise.all(BOOKS.map(async book => {
    const reason = { prophetx: "Depth adapter not connected", "4cx": "Account and depth access not connected" }[book];
    if (reason) return { book, status: "unavailable", reason };
    const matches = refs.filter(r => r.book === book);
    if (matches.length !== 1) return { book, status: "unavailable", reason: matches.length
      ? "Ambiguous market mapping" : "No mapped market at this exact total; refresh exchange lines if needed" };
    try {
      const v = await DEPTH_ADAPTERS[book](matches[0], game, fetchImpl);
      return { ...v, status: v.levels.length ? "available" : "empty", fetched_at: new Date().toISOString() };
    } catch (error) {
      return { book, status: "unavailable", reason: error.name === "TimeoutError"
        ? "Exchange depth request timed out" : error.message };
    }
  }));
  const finished = Date.now();
  if (finished >= Date.parse(game.kickoff_utc)) throw new Error("Game started while fetching depth");
  if (finished - started >= PREVIEW_TTL_MS) throw new Error("Depth snapshot expired; preview again");
  const available = results.filter(v => v.status === "available");
  return { ok: true, mode: "preview_only", can_execute: false, balances_checked: false,
    game_id: game.game_id, line, side: "under", budget, max_price: maxPrice,
    fetched_at: new Date(started).toISOString(), expires_at: new Date(started + PREVIEW_TTL_MS).toISOString(),
    ...allocateDepth(available, budget, maxPrice),
    venues: results.map(({ levels, ...v }) => ({ ...v, depth_levels: levels?.length || 0 })),
    notes: ["Public liquidity simulation; account balances and trading eligibility are not checked. No orders are sent.",
      "Same full-game total only. Payout assumes a normally completed game; postponement and cancellation rules differ by exchange.",
      "Whole native contracts: Novig pays 1¢ each; Kalshi and Polymarket US pay $1 each. Taker fees use venue precision; Kalshi assumes a direct member account. Partial fills can change rounding. Delayed rebates excluded; prices are not reserved.",
      "Novig allocations require manual submission in Novig. A preview is not an order or a confirmed fill."] };
}

export async function executionPreviewRoute(request, env) {
  const respond = (body, status = 200) => Response.json(body, { status, headers: { "cache-control": "no-store" } });
  if (request.method !== "GET") return respond({ ok: false, error: "Preview supports GET only; execution is disabled" }, 405);
  const url = new URL(request.url);
  const gameId = url.searchParams.get("game_id") || "";
  const line = numeric(url.searchParams.get("line")), budget = numeric(url.searchParams.get("budget"));
  const maxPrice = numeric(url.searchParams.get("max_price"));
  if (!/^(nfl|cfb):\d{4}:\d{1,2}:[a-z0-9_.-]+@[a-z0-9_.-]+$/.test(gameId) || gameId.length > 150
      || !Number.isFinite(line) || line < .5 || line > 150 || line % 1 !== .5
      || !Number.isFinite(budget) || budget < 1 || budget > 10000 || Math.abs(budget * 100 - Math.round(budget * 100)) > .000001
      || !Number.isFinite(maxPrice) || maxPrice < .01 || maxPrice > .99)
    return respond({ ok: false, error: "Select a half-point total, a $1–$10,000 budget, and a 1–99¢ all-in price limit" }, 400);
  try {
    const object = await env.ODDS.get(`board/games_${gameId.slice(0, 3)}.json`);
    const data = object ? await object.json() : [];
    const game = (Array.isArray(data) ? data : data.games || []).find(g => g.game_id === gameId);
    if (!game) return respond({ ok: false, error: "Game is not on the current board" }, 404);
    return respond(await previewGame(game, { line, budget, maxPrice }));
  } catch (error) {
    return respond({ ok: false, error: error.message || "Preview unavailable" }, 502);
  }
}
