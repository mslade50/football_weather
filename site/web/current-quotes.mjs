// Shared by the authenticated Worker and the browser; never renew source clocks.
export function quoteExpired(value, now = Date.now()) {
  if (value == null) return false;
  const expiry = Date.parse(value);
  return !Number.isFinite(expiry) || now > expiry;
}

export function expireCardQuotes(original, now = Date.now()) {
  const card = structuredClone(original), removed = [];
  for (const [book, markets] of Object.entries(card.odds || {})) {
    for (const [market, quote] of Object.entries(markets)) {
      // The server publication guard already removed this quote and recomputed
      // the card from surviving books. Do not mistake that intentional marker
      // for a newly expired quote and erase the refreshed card-level values.
      if (quote.expired) continue;
      if (!quoteExpired(quote.expires_at, now)) continue;
      removed.push(`${book}/${market}`);
      markets[market] = Object.fromEntries(Object.entries(quote).filter(([k]) => k.startsWith("open")
        || ["expires_at", "updated_at", "source_updated_at"].includes(k)));
      markets[market].expired = true;
    }
  }
  if (removed.length) {
    card.expired_markets = removed;
    card.consensus = { ...(card.consensus || {}), spread_now: null, total_now: null, move_s: null, move_t: null,
      spread_src: null, ref_book: null, n_books: 0, thin: true };
    card.fair = Object.fromEntries(Object.keys(card.fair || {}).map(k => [k, k === "edges" ? [] : null]));
    card.total_prices = null;
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
