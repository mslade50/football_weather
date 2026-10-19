// Shared by the authenticated Worker and the browser; never renew source clocks.
export function quoteExpired(value, now = Date.now()) {
  if (value == null) return false;
  const expiry = Date.parse(value);
  return !Number.isFinite(expiry) || now > expiry;
}

const SPREAD_MEMBERS = ["betonline", "pinnacle"];
const BOOK_WEIGHTS = {pinnacle: 3, betonline: 2, fanduel: 1, draftkings: 1, kalshi: 1, novig: 1, prophetx: 0.75};
const DEFAULT_WEIGHT = 0.5;
const SPREAD_LABELS = {betonline: "bol", pinnacle: "pin"};

function finite(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function weightedMedian(posts) {
  const pts = posts.filter(post => post.line != null && post.weight > 0).sort((a, b) => a.line - b.line);
  if (!pts.length) return null;
  const total = pts.reduce((sum, post) => sum + post.weight, 0);
  let acc = 0;
  for (let i = 0; i < pts.length; i++) {
    acc += pts[i].weight;
    if (Math.abs(acc - total / 2) < 1e-9 && i + 1 < pts.length) return (pts[i].line + pts[i + 1].line) / 2;
    if (acc >= total / 2) return pts[i].line;
  }
  return pts[pts.length - 1].line;
}

function pricingPosts(card, market) {
  const posts = [];
  for (const [book, markets] of Object.entries(card.odds || {})) {
    if (book === "betcris" || book === "consensus" || !markets) continue;
    const quote = markets[market];
    if (!quote || quote.expired) continue;
    const line = finite(market === "spread" ? quote.home_line : quote.line);
    if (line == null) continue;
    posts.push({book, line, weight: BOOK_WEIGHTS[book] ?? DEFAULT_WEIGHT});
  }
  return posts;
}

function consensusFromOdds(card, market) {
  const posts = pricingPosts(card, market);
  if (market === "spread") {
    const members = SPREAD_MEMBERS.filter(book => posts.some(post => post.book === book));
    if (members.length) {
      const values = members.map(book => posts.find(post => post.book === book).line);
      return {line: Math.round(values.reduce((sum, value) => sum + value, 0) / values.length * 100) / 100,
        src: members.map(book => SPREAD_LABELS[book]).join("+"), n: posts.length};
    }
    return {line: weightedMedian(posts), src: posts.length ? "fallback" : null, n: posts.length};
  }
  return {line: weightedMedian(posts), src: null, n: posts.length};
}

function scaleFair(oldFair, oldConsensus, nextConsensus, pct) {
  if (nextConsensus == null) return null;
  const fair = finite(oldFair), old = finite(oldConsensus), impact = finite(pct);
  if (old != null && old !== 0 && fair != null) return fair * (nextConsensus / old);
  return impact == null ? nextConsensus : nextConsensus * (1 + impact / 100);
}

function homeRelative(edge) {
  const line = finite(edge.line);
  if (line == null) return null;
  return edge.market === "spread" && edge.side === "away" ? -line : line;
}

function edgePoints(edge, fairLine) {
  const line = homeRelative(edge);
  if (line == null || fairLine == null) return null;
  if (edge.market === "total") return edge.side === "under" ? line - fairLine : fairLine - line;
  if (edge.market === "spread") return edge.side === "home" ? line - fairLine : fairLine - line;
  return null;
}

function pickBest(edges, market) {
  const cands = edges.filter(edge => edge && edge.market === market && edge.book !== "betcris");
  const ranked = cands.filter(edge => finite(edge.edge_pts) != null);
  const pool = ranked.length ? ranked : cands;
  if (!pool.length) return null;
  return pool.reduce((best, edge) => {
    const pts = finite(edge.edge_pts) ?? -Infinity, bestPts = finite(best.edge_pts) ?? -Infinity;
    const prob = finite(edge.edge_prob) ?? -Infinity, bestProb = finite(best.edge_prob) ?? -Infinity;
    return pts > bestPts || (pts === bestPts && prob > bestProb) ? edge : best;
  });
}

function usedBetCris(src) {
  return String(src || "").split("+").includes("cris");
}

function retargetEdges(edges, market, fairLine, nBooks) {
  for (const edge of edges) {
    if (!edge || edge.market !== market) continue;
    const pts = edgePoints(edge, fairLine);
    edge.fair_line = fairLine;
    edge.n_books = nBooks;
    if (pts == null) {
      edge.edge_pts = null;
      edge.edge_prob = null;
    } else {
      edge.edge_pts = pts;
    }
  }
}

function applyMarket(card, market) {
  const next = consensusFromOdds(card, market);
  const consensus = card.consensus || (card.consensus = {});
  const nowKey = market === "total" ? "total_now" : "spread_now";
  const openKey = market === "total" ? "total_open" : "spread_open";
  const moveKey = market === "total" ? "move_t" : "move_s";
  const pctKey = market === "total" ? "gs_fg_pct" : "away_fg_pct";
  const old = finite(consensus[nowKey]);
  if (card.fair) {
    const fairKey = market === "total" ? "fair_total" : "fair_spread";
    const fairV2Key = market === "total" ? "fair_total_v2" : "fair_spread_v2";
    card.fair[fairKey] = scaleFair(card.fair[fairKey], old, next.line, card.impact?.v1?.[pctKey]);
    if (card.fair[fairV2Key] != null) card.fair[fairV2Key] = scaleFair(card.fair[fairV2Key], old, next.line, card.impact?.v2?.[pctKey]);
    retargetEdges(card.fair.edges, market, card.fair[fairKey], next.n);
  }
  consensus[nowKey] = next.line;
  if (market === "spread") consensus.spread_src = next.line == null ? null : next.src;
  const open = finite(consensus[openKey]);
  consensus[moveKey] = next.line != null && open != null ? next.line - open : null;
  consensus[market === "spread" ? "spread_n_books" : "total_n_books"] = next.n;
  consensus[market === "spread" ? "spread_thin" : "total_thin"] = next.n < 2;
  const spreadN = Number(consensus.spread_n_books) || 0, totalN = Number(consensus.total_n_books) || 0;
  consensus.n_books = Math.max(spreadN, totalN);
  consensus.thin = consensus.n_books < 2;
  if (consensus.ref_book === "betcris") consensus.ref_book = null;
}

function demoteReferenceEdges(card) {
  const fair = card.fair;
  const edges = Array.isArray(fair?.edges) ? fair.edges : [];
  const betcrisEdges = edges.filter(edge => edge && edge.book === "betcris");
  const touchTotal = betcrisEdges.some(edge => edge.market === "total");
  const touchSpread = betcrisEdges.some(edge => edge.market === "spread") || usedBetCris(card.consensus?.spread_src);
  if (fair && Array.isArray(fair.edges)) fair.edges = edges.filter(edge => edge && edge.book !== "betcris");
  if (!touchTotal && !touchSpread) {
    if (fair?.best_total?.book === "betcris") fair.best_total = pickBest(fair.edges, "total");
    if (fair?.best_spread?.book === "betcris") fair.best_spread = pickBest(fair.edges, "spread");
    return;
  }
  if (touchTotal) applyMarket(card, "total");
  if (touchSpread) applyMarket(card, "spread");
  if (!fair) return;
  for (const [key, market] of [["best_total", "total"], ["best_spread", "spread"]]) {
    if (fair[key]?.book === "betcris" || (market === "total" ? touchTotal : touchSpread)) fair[key] = pickBest(fair.edges, market);
  }
}

export function expireCardQuotes(original, now = Date.now()) {
  const card = structuredClone(original), removed = [];
  demoteReferenceEdges(card, now);
  if (card.total_prices) {
    const prices = card.total_prices;
    const evidence = [...(prices.quote_evidence || []), ...(prices.quotes || []).map(q =>
      Object.fromEntries(['book', 'side', 'line', 'updated_at', 'fetched_at', 'expires_at'].map(k => [k, q[k]])))];
    prices.quote_evidence = [...new Map(evidence.map(q => [JSON.stringify(q), q])).values()];
    for (const key of ['quotes', 'reference_quotes']) {
      prices[key] = (prices[key] || []).filter(q => !q.expired && !quoteExpired(q.expires_at, now)
        && !card.odds?.[q.book]?.total?.expired && !quoteExpired(card.odds?.[q.book]?.total?.expires_at, now));
    }
    for (const side of ['under', 'over']) {
      if (prices[`best_${side}`] && !prices.quotes.some(q => q.book === prices[`best_${side}`].book
        && q.side === side && q.line === prices[`best_${side}`].line)) prices[`best_${side}`] = null;
    }
  }
  for (const [book, markets] of Object.entries(card.odds || {})) {
    for (const [market, quote] of Object.entries(markets)) {
      // The server publication guard already removed this quote and recomputed
      // the card from surviving books. Do not mistake that intentional marker
      // for a newly expired quote and erase the refreshed card-level values.
      if (quote.expired) continue;
      if (!quoteExpired(quote.expires_at, now)) continue;
      removed.push(`${book}/${market}`);
      const reference = market === 'total' && quote.line != null ? {...Object.fromEntries(
        ['line', 'under', 'updated_at', 'source_updated_at', 'expires_at'].map(k => [k, quote[k]])), status:'expired'}
        : quote.last_observed;
      markets[market] = Object.fromEntries(Object.entries(quote).filter(([k]) => k.startsWith("open")
        || ["expires_at", "updated_at", "source_updated_at"].includes(k)));
      markets[market].expired = true;
      if (reference) markets[market].last_observed = reference;
    }
  }
  if (removed.length) card.expired_markets = [...new Set([...(card.expired_markets || []), ...removed])];
  // BetCris aging out only retires that cell. A pricing book's expiry still
  // means the published fair may include a stale number, so those comparisons
  // are withheld until the next calculation.
  const pricingRemoved = removed.filter(item => item.split("/")[0] !== "betcris");
  if (pricingRemoved.length) {
    const currentBooks = (market) => Object.values(card.odds || {}).filter((markets) => {
      const quote = markets?.[market];
      const line = market === "spread" ? quote?.home_line : quote?.line;
      return !quote?.expired && line != null && Number.isFinite(Number(line));
    }).length;
    const spreadBooks = currentBooks("spread"), totalBooks = currentBooks("total");
    card.consensus = { ...(card.consensus || {}), spread_now: null, total_now: null, move_s: null, move_t: null,
      spread_src: null, ref_book: null, n_books: 0, thin: true,
      spread_n_books: spreadBooks, spread_thin: spreadBooks < 2,
      total_n_books: totalBooks, total_thin: totalBooks < 2 };
    card.fair = Object.fromEntries(Object.keys(card.fair || {}).map(k => [k, k === "edges" ? [] : null]));
    // Raw surviving references remain useful, but their model comparisons may
    // include the removed provider. Fail closed only for those derived fields.
    if (card.total_prices) {
      card.total_prices.fair_total = null;
      card.total_prices.best_under = card.total_prices.best_over = null;
      for (const q of [...(card.total_prices.quotes || []), ...(card.total_prices.reference_quotes || [])])
        for (const key of ['ev_roi', 'fair_cost', 'win_prob', 'push_prob', 'loss_prob']) q[key] = null;
    }
  }
  return card;
}

export function expireQuoteMeta(original, now = Date.now()) {
  const meta = structuredClone(original), live = [];
  if (!("quote_expiries" in meta)) return meta;
  let removed = 0;
  for (const group of meta.quote_expiries || []) {
    if (!quoteExpired(group.expires_at, now)) { live.push(group); continue; }
    removed += group.count;
    const counts = meta.counts?.[group.book];
    for (const key of [group.sport, `${group.sport}.${group.market}`]) {
      if (counts && key in counts) counts[key] = Math.max(0, counts[key] - group.count);
    }
    const book = meta.books?.[group.book];
    if (book) {
      book.count = Math.max(0, (book.count || 0) - group.count);
      book.status = book.count ? "amber" : "red";
      book.reason = "Expired quotes excluded from current prices";
    }
  }
  meta.quote_expiries = live;
  if (removed) (meta.degradations ||= []).push({component: "odds.expiry", severity: "warn",
    reason: `${removed} expired quotes excluded; source timestamps unchanged`, run_id: meta.run_id,
    ts: new Date(now).toISOString()});
  return meta;
}

export function expirePayload(name, value, now = Date.now()) {
  if (/^games_(nfl|cfb)\.json$/.test(name)) {
    return Array.isArray(value) ? value.map(c => expireCardQuotes(c, now))
      : { ...value, games: (value.games || []).map(c => expireCardQuotes(c, now)) };
  }
  if (name === "meta.json") return expireQuoteMeta(value, now);
  if (name === "board.json") {
    return {...value, rows: (value.rows || []).map(original => {
      if (!quoteExpired(original.quote_expires_at, now)) return original;
      const row = {...original};
      for (const key of ["spread_now", "spread_src", "total_now", "ref_book", "fair_total", "fair_spread",
        "best_total_edge", "best_total_book", "best_spread_edge", "best_spread_book", "confidence",
        "fair_total_v2", "fair_spread_v2"]) row[key] = null;
      row.n_books = 0;
      row.spread_n_books = null;
      row.spread_thin = null;
      row.total_n_books = null;
      row.total_thin = null;
      return row;
    })};
  }
  if (name === "status.json") {
    const checked = expireQuoteMeta(value, now);
    return { ...checked, meta: {...value.meta, degradations: checked.degradations} };
  }
  return value; // Historical snapshots and opener/history series are immutable.
}

export function refreshOverdue(meta, now = Date.now()) {
  const eta = Date.parse(meta.next_run_eta), updated = Date.parse(meta.last_updated);
  return Number.isFinite(eta) && Number.isFinite(updated) && updated < eta && now > eta + 90 * 60000;
}
