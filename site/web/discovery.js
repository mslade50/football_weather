"use strict";
// Signal decisions are published by Python. Older cards wait for a refresh.
const EXCHANGE_BOOKS = new Set(["kalshi", "novig", "prophetx", "polymarket_us", "4cx"]);
const VERIFIED_OFFERS = new Map();
const finiteValue = v => typeof v === "number" && Number.isFinite(v);
function publishedDecision(g) {
  return g?.process?.decision_version === "decision-v1" ? g.process : null;
}
function decisionEligible(g) { return publishedDecision(g)?.eligible === true; }
function upcomingGame(g) {
  return bettableBeforeKickoff(g);
}
function showAllBoardGames(state) {
  return !!state.showAllGames && (state.view === "map" || (state.view === "table" && state.tableMode !== "history"));
}
function reasonText(code, thresholds = {}) {
  const texts = {
    indoor: "Indoor / dome", closed_roof: "Closed roof", roof_unconfirmed: "Retractable roof not confirmed open",
    spread_outside: `Opening spread outside ±${thresholds.cfb_open_spread_max}`,
    not_upcoming: "Not an upcoming scheduled game", spread_unknown: "Opening spread unknown",
    forecast_incomplete: "Forecast incomplete", forecast_retained: "Forecast retained; fresh evidence unavailable",
    forecast_aged: "Point forecast aged", source_unavailable: "source unavailable", incomplete_point: "incomplete point",
    temp_gate: "Below screening margin", below_margin: "Below screening margin",
    low_probability: "Very low signal likelihood", price_moved: "Under moved vs opener",
    wind_signal: `wind ≥${thresholds.wind_flag_mph} mph`, rain_signal: "heavy rain",
    wind_watch: `Watch · ${thresholds.wind_watch_mph}–${thresholds.wind_flag_mph} mph wind · not a flag`,
    rain_watch: `Watch · rain ${thresholds.rain_ignore_mm}–${thresholds.rain_watch_mm} mm`,
    rain_elevated: `Watch · rain ${thresholds.rain_watch_mm}–${thresholds.rain_signal_mm} mm`,
    rain_unconfirmed: "Heavy rain, model agreement unverified", ecmwf_apart: "ECMWF stands apart",
    opener_unknown: "opener line unknown",
  };
  return texts[code] || "Refresh pending";
}
function ineligibleReason(g) {
  const record = publishedDecision(g);
  return record ? record.ineligible_reasons.map(code => reasonText(code, record.thresholds)).join(" · ") : "Refresh pending";
}
function crosswindFactor(dir, orient) {
  if (!finiteValue(dir) || !finiteValue(orient)) return null;
  return Math.abs(Math.sin((Number(dir) - Number(orient)) * Math.PI / 180));
}
function discoveryState(g) {
  const record = publishedDecision(g);
  if (!record) return {kind: "pending", label: "Refresh pending", probability: null, note: ""};
  const reasons = record.discovery_reasons || [];
  let label = reasons.map((code, index) => {
    let text = reasonText(code, record.thresholds);
    if (record.discovery_state === "near" && index > 0) text = text.replace(/^Watch · /, "");
    return text;
  }).join(" · ");
  if (record.discovery_state === "signal") label = "Signal · " + label;
  const probability = record.signal_probability ?? null;
  const note = probability === null ? "Signal likelihood unknown; screening margins are not probabilities"
    : `Signal likelihood ${(probability * 100).toFixed(1)}%`;
  const early = ["ineligible", "unknown"].includes(record.discovery_state);
  return early ? {kind: record.discovery_state, label}
    : {kind: record.discovery_state, label, probability, note, windBand: record.wind_band, rainTier: record.rain_tier};
}
function discoveryHtml(g) {
  const s = discoveryState(g);
  return `<span class="discovery ${s.kind}">${esc(s.label)}</span>${s.probability == null ? "" : `<span class="sub">${esc(s.note)}</span>`}`;
}
function filterDiscovery(rows, state, preset = null) {
  return rows.filter(g => {
    const d = discoveryState(g);
    if (!upcomingGame(g)) return false;
    const expanded = showAllBoardGames(state);
    if (!expanded && d.kind === "ineligible") return false;
    if (!expanded && state.focus !== "all" && !["signal", "near", "pending"].includes(d.kind)) return false;
    if (state.week != null && String(g.week) !== String(state.week)) return false;
    if (state.signal && signalTier(g.signal) !== state.signal) return false;
    if (preset && (!preset.sports.includes(g.sport) || !hasFlag(g, preset.flag))) return false;
    const haystack = [gameLabel(g), g.home?.name, g.away?.name, g.stadium?.name].filter(Boolean).join(" ").toLowerCase().replace(/\s+/g, " ");
    const query = (state.q || "").trim().toLowerCase().replace(/\s+/g, " ");
    return !query || haystack.includes(query);
  });
}
function stadiumCoordinates(g) {
  const st = g.stadium || {};
  return finiteValue(st.lat) && finiteValue(st.lon) && Math.abs(st.lat) <= 90 && Math.abs(st.lon) <= 180
    ? `${st.lat}, ${st.lon}` : "";
}
function coordinateControl(g) {
  const value = stadiumCoordinates(g);
  return value ? `<span class="coordinates" tabindex="0">${esc(value)}</span><button type="button" class="copy-coordinates controlbtn"
    data-coordinates="${esc(value)}" aria-label="Copy stadium latitude, longitude for Windy">Copy lat, lon</button><span class="copy-status sub" role="status"></span>`
    : '<span class="muted">Coordinates unavailable</span>';
}
async function copyCoordinates(button) {
  const value = button.dataset.coordinates, status = button.parentElement.querySelector(".copy-status");
  try { await navigator.clipboard.writeText(value); status.textContent = "Copied for Windy"; }
  catch (_) { status.textContent = "Copy unavailable; select the coordinates above"; }
}
function quoteClock(q) { return q.fetched_at || q.updated_at || null; }
function clockLabel(stamp) {
  const ms = Date.parse(stamp), age = Math.floor((Date.now() - ms) / 1000);
  return Number.isFinite(ms) && age >= 0 ? `${age}s old · ${new Date(ms).toISOString()}` : "Timestamp unknown / future";
}
function verifiedOffer(g) {
  const r = VERIFIED_OFFERS.get(g.game_id) || g.execution_preview;
  const age = Date.now() - Date.parse(r?.fetched_at), depthAge = Date.now() - Date.parse(r?.depth_fetched_at);
  if ((typeof LOAD_ERRORS !== "undefined" && (LOAD_ERRORS.meta || LOAD_ERRORS[g.sport]))
    || (typeof RAW_META !== 'undefined' && (RAW_META.publication_status !== 'manifest_verified'
      || r?.board_run_id !== g.run_id || r?.publication_generation !== RAW_META.publication?.generation))
    || !decisionEligible(g) || !upcomingGame(g) || !r?.ok || r.game_id !== g.game_id
    || r.stake_mode !== "principal" || r.settlement_verified !== true || r.side !== "under"
    || !finiteValue(r.line) || r.line % 1 !== .5 || r.line !== (g.fresh_odds?.selected_line ?? g.consensus?.total_now)
    || !finiteValue(r.principal) || r.principal < 500
    || !finiteValue(r.fees) || r.fees < 0 || !finiteValue(r.payout_if_win) || r.payout_if_win <= 0
    || !finiteValue(r.average_price) || r.average_price <= 0 || r.average_price >= 1
    || Math.abs(r.average_price - (r.principal + r.fees) / r.payout_if_win) > .000001
    || !(age >= 0 && age <= 30000 && depthAge >= 0 && depthAge <= 30000) || !(Date.parse(r.expires_at) > Date.now())
    || !Array.isArray(r.allocations) || !r.allocations.length
    || r.allocations.some(a => !EXCHANGE_BOOKS.has(a.book) || !finiteValue(a.principal) || a.principal < 0
      || !finiteValue(a.fees) || a.fees < 0 || a.line !== r.line || a.side !== "under" || !a.rules_key || a.rules_key !== r.rules_key)) return null;
  const principal = r.allocations.reduce((n, a) => n + a.principal, 0), fees = r.allocations.reduce((n, a) => n + a.fees, 0);
  return Math.abs(principal - r.principal) < .011 && Math.abs(fees - r.fees) < .011 ? r : null;
}
function clockAge(stamp) {
  const age = Math.floor((Date.now() - Date.parse(stamp)) / 1000);
  return Number.isFinite(age) && age >= 0 ? `${age}s old` : "time unknown / future";
}
function exchangeOfferHtml(g, detailed = false) {
  const r = verifiedOffer(g);
  if (!r) {
    if (!publishedDecision(g)) return '<span class="offer-unusable">Refresh pending</span>';
    if (!decisionEligible(g)) return '<span class="offer-unusable">Not eligible</span>';
    const shortfall = verifiedPrincipalShortfall(g);
    if (!shortfall) {
      const state = exchangeUnavailableState(g);
      return `<span class="offer-unusable">${esc(state.label)}</span><span class="sub">${esc(state.note)}</span>`;
    }
    return `<span class="offer-unusable">Checked depth below $500</span>
      <span class="sub">U ${fmtTotal(shortfall.line)} · $${shortfall.principal.toFixed(2)} principal + $${shortfall.fees.toFixed(2)} fees</span>
      <span class="sub">${esc(shortfall.allocations.map(a => bookLabel(a.book)).join(" + "))} · limit ${(shortfall.max_price * 100).toFixed(1)}¢ all-in</span>
      ${detailed ? `<span class="sub">Matching settlement verified</span><span class="sub">Quote ${esc(clockLabel(shortfall.fetched_at))}</span><span class="sub">Depth ${esc(clockLabel(shortfall.depth_fetched_at))}</span>
        <span class="sub">Expires ${esc(shortfall.expires_at)}</span>${shortfall.venues.map(v => `<span class="sub">${esc(bookLabel(v.book))}: ${esc(v.status)}${v.reason ? ` · ${esc(v.reason)}` : ""}</span>`).join("")}` : ""}`;
  }
  return `<b>U ${fmtTotal(r.line)} · ${(r.average_price * 100).toFixed(2)}¢ all-in</b>
    <span class="sub">$${r.principal.toFixed(2)} stake + $${r.fees.toFixed(2)} fees</span>
    <span class="sub">${esc(r.allocations.map(a => bookLabel(a.book)).join(" + "))}</span>
    <span class="sub">Quote ${esc(clockAge(r.fetched_at))} · depth ${esc(clockAge(r.depth_fetched_at))}</span>
    ${detailed ? `<span class="sub">Quote ${esc(clockLabel(r.fetched_at))}</span><span class="sub">Depth ${esc(clockLabel(r.depth_fetched_at))}</span>` : ""}`;
}

// Explain failed qualification without renewing source clocks or promoting references.
function exchangeUnavailableState(g) {
  if (!upcomingGame(g)) return {label: "Liquidity unverified", note: "Pre-game checks only"};
  if (typeof LOAD_ERRORS !== "undefined" && (LOAD_ERRORS.meta || LOAD_ERRORS[g.sport]))
    return {label: "Board unavailable", note: "Reload publication before checking depth"};
  if (typeof RAW_META !== "undefined" && RAW_META.publication_status !== "manifest_verified")
    return {label: "Publication unverified", note: "Current generation unavailable"};
  const preview = VERIFIED_OFFERS.get(g.game_id) || g.execution_preview;
  if (preview) {
    if (!(Date.parse(preview.expires_at) > Date.now()))
      return {label: "Stake check expired", note: "Check current depth in details"};
    if (preview.line !== (g.fresh_odds?.selected_line ?? g.consensus?.total_now))
      return {label: "Under line changed", note: "Check depth at the current total"};
    return {label: "Liquidity unverified", note: "Stake / fees / rules did not qualify"};
  }
  const quotes = (g.total_prices?.quotes || []).filter(q => EXCHANGE_BOOKS.has(q.book) && q.side === "under"
    && (typeof STATE === "undefined" || !STATE.book || q.book === STATE.book));
  const evidence = (g.total_prices?.quote_evidence || []).filter(q => EXCHANGE_BOOKS.has(q.book) && q.side === 'under'
    && (typeof STATE === 'undefined' || !STATE.book || q.book === STATE.book));
  const selected = g.fresh_odds?.selected_line ?? g.consensus?.total_now;
  const fresh = [...quotes].sort((a, b) => Date.parse(quoteClock(b)) - Date.parse(quoteClock(a))).find(q => {
    const age = Date.now() - Date.parse(quoteClock(q));
    return finiteValue(q.line) && (!finiteValue(selected) || q.line === selected)
      && age >= 0 && age <= 30000 && (!q.expires_at || Date.parse(q.expires_at) > Date.now());
  });
  const stale = [...quotes, ...evidence].some(q => {
    const age = Date.now() - Date.parse(quoteClock(q));
    return !(age >= 0 && age <= 30000) || (q.expires_at && !(Date.parse(q.expires_at) > Date.now()));
  });
  if (!fresh && finiteValue(selected) && quotes.some(q => {
    const age = Date.now() - Date.parse(quoteClock(q));
    return finiteValue(q.line) && q.line !== selected && age >= 0 && age <= 30000
      && (!q.expires_at || Date.parse(q.expires_at) > Date.now());
  })) return {label: 'No exchange quote at selected total', note: 'Other totals observed; matching under / depth unavailable'};
  if (!fresh && stale) return {label: "Exchange quotes stale", note: "No current $500 depth check"};
  if (fresh) return {label: "Liquidity unverified", note: `U ${fmtTotal(fresh.line)} ${fmtOdds(fresh.odds)} · ${bookLabel(fresh.book)} reference; $500 depth unchecked`};
  if (evidence.length) return {label: 'Liquidity unverified', note: 'Exchange source observed; price / matching rules / $500 depth unverified'};
  return {label: "No exchange quote", note: "Current under / depth unavailable"};
}

// Display-only evidence for a partial checked stake. This never qualifies an offer.
function verifiedPrincipalShortfall(g) {
  const r = VERIFIED_OFFERS.get(g.game_id) || g.execution_preview;
  const fresh = stamp => { const age = Date.now() - Date.parse(stamp); return age >= 0 && age <= 30000; };
  if ((typeof LOAD_ERRORS !== "undefined" && (LOAD_ERRORS.meta || LOAD_ERRORS[g.sport]))
    || (typeof RAW_META !== "undefined" && (RAW_META.publication_status !== "manifest_verified"
      || r?.board_run_id !== g.run_id || r?.publication_generation !== RAW_META.publication?.generation))
    || !decisionEligible(g) || !upcomingGame(g) || !r?.ok || r.game_id !== g.game_id || r.side !== "under"
    || r.stake_mode !== "principal" || r.cash_liquidity_verified !== false || r.settlement_verified !== true
    || !finiteValue(r.line) || r.line !== (g.fresh_odds?.selected_line ?? g.consensus?.total_now)
    || !finiteValue(r.principal) || r.principal <= 0 || r.principal >= 500
    || !finiteValue(r.fees) || r.fees < 0 || !finiteValue(r.payout_if_win) || r.payout_if_win <= 0
    || !finiteValue(r.average_price) || r.average_price <= 0 || r.average_price >= 1
    || Math.abs(r.average_price - (r.principal + r.fees) / r.payout_if_win) > .000001
    || !fresh(r.fetched_at) || !fresh(r.depth_fetched_at) || !(Date.parse(r.expires_at) > Date.now())
    || !finiteValue(r.max_price) || r.max_price <= 0 || r.max_price >= 1 || r.average_price > r.max_price + .000001
    || !finiteValue(r.worst_price) || r.worst_price <= 0 || r.worst_price > r.max_price + .000001
    || !Array.isArray(r.allocations) || !r.allocations.length
    || !Array.isArray(r.venues) || r.venues.some(v => !v || !EXCHANGE_BOOKS.has(v.book) || !["available", "empty", "unavailable"].includes(v.status))
    || !r.venues.some(v => v?.status === "available" && v.settlement_verified === true
      && v.rules_key === r.rules_key && v.depth_fetched_at === r.depth_fetched_at && r.allocations.every(a => a?.book === v.book))
    || r.allocations.some(a => !EXCHANGE_BOOKS.has(a.book) || !finiteValue(a.principal) || a.principal < 0
      || !finiteValue(a.fees) || a.fees < 0 || a.line !== r.line || a.side !== "under" || !a.rules_key || a.rules_key !== r.rules_key
      || !finiteValue(a.payout_if_win) || a.payout_if_win <= 0 || !finiteValue(a.all_in_price) || a.all_in_price <= 0
      || a.all_in_price > r.max_price + .000001 || Math.abs(a.all_in_price - (a.principal + a.fees) / a.payout_if_win) > .000001)) return null;
  const principal = r.allocations.reduce((n,a) => n+a.principal,0), fees = r.allocations.reduce((n,a) => n+a.fees,0);
  const payout = r.allocations.reduce((n,a) => n+a.payout_if_win,0), worst = Math.max(...r.allocations.map(a=>a.all_in_price));
  return Math.abs(principal-r.principal) < .011 && Math.abs(fees-r.fees) < .011
    && Math.abs(payout-r.payout_if_win) < .011 && Math.abs(worst-r.worst_price) < .000001 ? r : null;
}
