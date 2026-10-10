"use strict";
// UI discovery is independent of prices and liquidity. Unknown is never zero.
const EXCHANGE_BOOKS = new Set(["kalshi", "novig", "prophetx", "polymarket_us", "4cx"]);
const VERIFIED_OFFERS = new Map();
const finiteValue = v => typeof v === "number" && Number.isFinite(v);
function hardEligible(g) {
  const st = g.stadium || {};
  const roof = String(st.roof_state || g.roof_state || st.roof_type || "").toLowerCase();
  return !["dome", "closed", "indoor"].includes(roof)
    && !(g.sport === "cfb" && finiteValue(g.consensus?.spread_open) && Math.abs(g.consensus.spread_open) > 10);
}
function discoveryState(g) {
  if (!hardEligible(g)) return {kind: "ineligible", label: "Ineligible roof / opening spread"};
  if (!(Date.parse(g.kickoff_utc) > Date.now()) || /final|cancel|postpon|suspend|live|progress/i.test(g.status || ""))
    return {kind: "ineligible", label: "Not an upcoming scheduled game"};
  const w = g.weather || {}, cfb = g.sport === "cfb";
  if (cfb && !finiteValue(g.consensus?.spread_open)) return {kind: "unknown", label: "Opening spread unknown"};
  const wind = w.wind_fg, temp = w.temp_fg, rain = w.rain_fg;
  if (!finiteValue(wind) || !finiteValue(temp)) return {kind: "unknown", label: "Forecast incomplete"};
  const windCut = cfb ? 14 : 8, tempCut = cfb ? 70 : 60;
  const heat = finiteValue(g.home_temp) && finiteValue(g.away_temp) && g.home_temp < 57 && g.away_temp < 57;
  const altitude = cfb && finiteValue(g.travel_alt) && g.travel_alt > 800;
  const signal = (wind > windCut && temp < tempCut) || (finiteValue(rain) && rain > 2 && temp < 50)
    || (heat && temp > 80) || (altitude && temp > 75);
  const near = (wind >= windCut - 3 && temp <= tempCut + 2)
    || (finiteValue(w.wind_p90) && w.wind_p90 > windCut && temp <= tempCut + 2)
    || (finiteValue(rain) && rain >= 1.5 && temp <= 52)
    || (heat && temp >= 78) || (altitude && temp >= 73);
  // Probability is optional explicit backend evidence, never inferred from impact/confidence/PoP.
  const probability = finiteValue(g.discovery?.signal_probability) && g.discovery.signal_probability >= 0
    && g.discovery.signal_probability <= 1 ? g.discovery.signal_probability : null;
  const kind = probability !== null && probability < .05 ? "quiet" : signal ? "signal" : near ? "near" : "quiet";
  const label = kind === "signal" ? "Signal" : kind === "near" ? "Near signal" : probability !== null && probability < .05
    ? "Very low signal likelihood" : "Below screening margin";
  return {kind, label, probability, note: probability === null ? "Signal likelihood unknown; screening margins are not probabilities"
    : `Signal likelihood ${(probability * 100).toFixed(1)}%`};
}
function discoveryHtml(g) {
  const s = discoveryState(g);
  return `<span class="discovery ${s.kind}">${esc(s.label)}</span><span class="sub">${esc(s.probability == null ? "Likelihood unknown" : s.note)}</span>`;
}
function filterDiscovery(rows, state, preset = null) {
  return rows.filter(g => {
    const d = discoveryState(g);
    if (d.kind === "ineligible") return false;
    if (state.focus !== "all" && !["signal", "near"].includes(d.kind)) return false;
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
    || !hardEligible(g) || !(Date.parse(g.kickoff_utc) > Date.now()) || !r?.ok || r.game_id !== g.game_id
    || r.stake_mode !== "principal" || r.settlement_verified !== true || r.side !== "under"
    || !finiteValue(r.line) || !finiteValue(r.principal) || r.principal < 500
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
function exchangeOfferHtml(g) {
  const r = verifiedOffer(g);
  return r ? `<b>U ${fmtTotal(r.line)} · ${(r.average_price * 100).toFixed(2)}¢ all-in</b>
    <span class="sub">$${r.principal.toFixed(2)} stake + $${r.fees.toFixed(2)} fees</span>
    <span class="sub">${esc(r.allocations.map(a => bookLabel(a.book)).join(" + "))}</span>
    <span class="sub">${esc(clockLabel(r.fetched_at))}</span><span class="sub">Depth ${esc(clockLabel(r.depth_fetched_at))}</span>`
    : '<span class="muted">No verified $500 stake</span><span class="sub">Fresh depth, fees and matching rules required. Tap game to inspect exchanges.</span>';
}
