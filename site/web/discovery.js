"use strict";
// UI discovery is independent of prices and liquidity. Unknown is never zero.
// Process screen (docs/PROCESS_SPEC.md). Keep in lockstep with pipeline/model/config.py.
const PROCESS_TEMP_MAX_F = 65;
const PROCESS_TEMP_GATE = false;
const PROCESS_WIND_WATCH_MPH = 12;
const PROCESS_WIND_FLAG_MPH = 15;
const PROCESS_RAIN_IGNORE_MM = 5;
const PROCESS_RAIN_WATCH_MM = 15;
const PROCESS_RAIN_SIGNAL_MM = 23;
const PROCESS_RAIN_AGREE_TOL_MM = 10;
const PROCESS_UNDER_PROB_MOVE_MAX = 0.04;
const WARM_WIND_TEMP_MIN_F = 65;
const WARM_WIND_LOW_MPH = 10;
const WARM_WIND_MID_MPH = 12;
const WARM_WIND_HIGH_MPH = 15;
const WARM_WIND_NOTE = "Watch: warm + wind. Exploratory list for exposed games at 65°F or warmer with sustained wind above 10 mph. Not an under signal and not an alert. 2024-25 CFB, final forecast: these games finished over the close about 61% of the time (n=124). Early forecasts were about 51-54% and unstable by year. Winds above 15 mph finished over about 38% of the time (n about 16-18). The split to keep watching is light-to-medium wind, above 10 mph through 15 mph. Counts under 30 are thin.";
const ECMWF_MODELS = new Set(["ecmwf_ifs025", "ecmwf_aifs025_single"]);
const EXCHANGE_BOOKS = new Set(["kalshi", "novig", "prophetx", "polymarket_us", "4cx"]);
const VERIFIED_OFFERS = new Map();
const finiteValue = v => typeof v === "number" && Number.isFinite(v);
function roofExposed(g) {
  // Same rule as pipeline/stadiums/roofs.py weather_exposed: a retractable roof
  // counts only when the game says it is open. Dome, closed, and indoor do not.
  const st = g.stadium || {};
  const type = String(st.roof_type || "").toLowerCase();
  const roof = String(st.roof_state || g.roof_state || "").toLowerCase();
  if (type === "dome" || roof === "dome" || roof === "closed" || roof === "indoor") return false;
  if (type === "retractable") return roof === "open";
  return true;
}
function hardEligible(g) {
  return roofExposed(g)
    && !(g.sport === "cfb" && finiteValue(g.consensus?.spread_open) && Math.abs(g.consensus.spread_open) > 10);
}
function upcomingGame(g) {
  return Date.parse(g.kickoff_utc) > Date.now() && !/final|cancel|postpon|suspend|live|progress/i.test(g.status || "");
}
function showAllBoardGames(state) {
  return !!state.showAllGames && (state.view === "map" || (state.view === "table" && state.tableMode !== "history"));
}
function ineligibleReason(g) {
  const st = g.stadium || {};
  const type = String(st.roof_type || "").toLowerCase();
  const roof = String(st.roof_state || g.roof_state || "").toLowerCase();
  const reasons = [];
  if (type === "dome" || roof === "dome" || roof === "indoor") reasons.push("Indoor / dome");
  else if (roof === "closed") reasons.push("Closed roof");
  else if (type === "retractable" && roof !== "open") reasons.push("Retractable roof not confirmed open");
  if (g.sport === "cfb" && finiteValue(g.consensus?.spread_open) && Math.abs(g.consensus.spread_open) > 10)
    reasons.push("Opening spread outside ±10");
  return reasons.join(" · ");
}
function crosswindFactor(dir, orient) {
  if (!finiteValue(dir) || !finiteValue(orient)) return null;
  return Math.abs(Math.sin((Number(dir) - Number(orient)) * Math.PI / 180));
}
function drivingForecast(g) {
  const w = g.weather || {};
  const source = String(w.signal_source || "");
  if (source.startsWith("ecmwf")) {
    return {temp: w.signal_temp_f, wind: w.signal_wind_mph, rain: w.signal_rain_mm, dir: w.signal_wind_dir_deg, source};
  }
  return {temp: w.temp_fg, wind: w.wind_fg, rain: w.rain_fg, dir: w.wind_dir_deg, source: source || "stitched"};
}
function modelRain(row) {
  const rain = row && (row.rain_mm ?? row.rain_fg);
  return finiteValue(rain) ? Number(rain) : null;
}
function ecmwfRainMm(models) {
  // IFS rain when that model has one; AIFS only when IFS has none. Not the stitch.
  if (!Array.isArray(models)) return null;
  let ifs = null, aifs = null, sawIfs = false;
  for (const row of models) {
    if (!row) continue;
    if (row.id === "ecmwf_ifs025") { sawIfs = true; ifs = modelRain(row); }
    else if (row.id === "ecmwf_aifs025_single") aifs = modelRain(row);
  }
  if (sawIfs && ifs != null) return ifs;
  return aifs;
}
function rainAgreement(models) {
  // Same partner rule as pipeline/model/process.py rain_agreement. The comparison
  // total is ECMWF's own window, not the short-range stitch.
  const ecmwfMm = ecmwfRainMm(models);
  if (!Array.isArray(models) || !models.length) return ecmwfMm == null ? "missing_ecmwf" : "unverified";
  if (ecmwfMm == null) return "missing_ecmwf";
  let best = null;
  let sawOther = false;
  for (const row of models) {
    if (!row || ECMWF_MODELS.has(row.id)) continue;
    const rain = modelRain(row);
    if (rain == null) continue;
    sawOther = true;
    if (rain < PROCESS_RAIN_SIGNAL_MM) continue;
    const gap = Math.abs(rain - Number(ecmwfMm));
    if (!best || gap < best.gap) best = {gap, rain};
  }
  if (!sawOther) return "unverified";
  if (!best) return "disagree";
  return Number(ecmwfMm) >= PROCESS_RAIN_SIGNAL_MM && best.gap <= PROCESS_RAIN_AGREE_TOL_MM ? "agree" : "disagree";
}
function warmWindLabel(bucket) {
  return bucket === "10_12" ? "10–12 mph" : bucket === "12_15" ? "12–15 mph" : bucket === "gt_15" ? ">15 mph" : "";
}
function fieldExposed(g) {
  return roofExposed(g);
}
function warmWindBucket(temp, wind) {
  if (!finiteValue(temp) || !finiteValue(wind) || Number(temp) < WARM_WIND_TEMP_MIN_F) return null;
  if (Number(wind) > WARM_WIND_HIGH_MPH) return "gt_15";
  if (Number(wind) > WARM_WIND_MID_MPH) return "12_15";
  if (Number(wind) > WARM_WIND_LOW_MPH) return "10_12";
  return null;
}
function warmWindTrack(g) {
  const saved = g.warm_wind;
  if (saved && typeof saved.tracked === "boolean") {
    return {tracked: saved.tracked === true, bucket: saved.bucket || null,
      label: saved.label || warmWindLabel(saved.bucket), temp: saved.temp_f, wind: saved.wind_mph};
  }
  if (!fieldExposed(g)) return {tracked: false, bucket: null, label: "", temp: null, wind: null};
  const drive = drivingForecast(g);
  const bucket = warmWindBucket(drive.temp, drive.wind);
  return {tracked: bucket != null, bucket, label: warmWindLabel(bucket), temp: drive.temp, wind: drive.wind};
}
function warmWindHtml(g) {
  const track = warmWindTrack(g);
  if (!track.tracked) return `<span class="discovery quiet">Not in Watch: warm + wind</span>`;
  return `<span class="discovery warm-wind">Watch: warm + wind · ${esc(track.label)}</span>`
    + `<span class="sub">Exploratory · not an alert · sample above 15 mph is thin</span>`;
}
function filterWarmWind(rows, state) {
  return rows.filter(g => {
    if (!upcomingGame(g) || !warmWindTrack(g).tracked) return false;
    if (state.week != null && String(g.week) !== String(state.week)) return false;
    const haystack = [gameLabel(g), g.home?.name, g.away?.name, g.stadium?.name].filter(Boolean).join(" ").toLowerCase().replace(/\s+/g, " ");
    const query = (state.q || "").trim().toLowerCase().replace(/\s+/g, " ");
    return !query || haystack.includes(query);
  });
}
function activeTempGate(g) {
  const proc = g.process || {};
  if (proc.temp_gate === true) {
    const max = finiteValue(proc.temp_gate_max_f) ? Number(proc.temp_gate_max_f) : PROCESS_TEMP_MAX_F;
    return {on: true, max};
  }
  return {on: PROCESS_TEMP_GATE, max: PROCESS_TEMP_MAX_F};
}
function discoveryState(g) {
  if (!hardEligible(g)) return {kind: "ineligible", label: ineligibleReason(g)};
  if (!upcomingGame(g))
    return {kind: "ineligible", label: "Not an upcoming scheduled game"};
  const w = g.weather || {}, cfb = g.sport === "cfb";
  if (cfb && !finiteValue(g.consensus?.spread_open)) return {kind: "unknown", label: "Opening spread unknown"};
  const drive = drivingForecast(g);
  const wind = drive.wind, temp = drive.temp, rain = drive.rain;
  if (!finiteValue(wind)) return {kind: "unknown", label: "Forecast incomplete"};
  const proc = g.process || {};
  const gate = activeTempGate(g);
  const tempKnown = finiteValue(temp);
  const tempPasses = !gate.on || (tempKnown && Number(temp) < gate.max);
  let windBand = "none";
  if (tempPasses && finiteValue(wind)) {
    const mph = Number(wind);
    windBand = mph >= PROCESS_WIND_FLAG_MPH ? "flag" : mph >= PROCESS_WIND_WATCH_MPH ? "watch" : "none";
  }
  let rainTier = "ignore";
  if (tempPasses && finiteValue(rain)) {
    const mm = Number(rain);
    rainTier = mm < PROCESS_RAIN_IGNORE_MM ? "ignore" : mm < PROCESS_RAIN_WATCH_MM ? "watch"
      : mm < PROCESS_RAIN_SIGNAL_MM ? "elevated" : "signal_amount";
  }
  const storedAgree = proc.rain_agreement;
  const agree = ["agree", "disagree", "unverified", "missing_ecmwf"].includes(storedAgree)
    ? storedAgree : rainAgreement(w.models);
  const rainSignal = rainTier === "signal_amount" && agree === "agree";
  if (rainTier === "signal_amount" && !rainSignal) rainTier = agree === "disagree" ? "disagree" : "unverified";
  const weatherHit = tempPasses && (windBand === "flag" || rainSignal);
  const rainNear = ["watch", "elevated", "unverified", "disagree"].includes(rainTier);
  const watch = tempPasses && !weatherHit && (windBand === "watch" || rainNear);
  // under_prob_move is the adverse sum (dropped total and shorter juice, neither cancelling).
  const move = finiteValue(proc.under_prob_move) ? Math.max(Number(proc.under_prob_move), 0) : null;
  const lineAdverse = finiteValue(proc.line_prob_move) ? Math.max(Number(proc.line_prob_move), 0) : 0;
  const bookAdverse = finiteValue(proc.book_adverse_max) ? Math.max(Number(proc.book_adverse_max), 0) : 0;
  const moveMax = finiteValue(proc.price_gate_max) ? Number(proc.price_gate_max) : PROCESS_UNDER_PROB_MOVE_MAX;
  const juiceConsensus = ["novig_consensus", "under_only_implied"].includes(proc.under_prob_basis);
  // One book's own adverse move blocks even when the cross-book average does not.
  const priceBlocked = proc.price_blocked === true || (weatherHit && (
    (juiceConsensus && move != null && move > moveMax) || lineAdverse > moveMax || bookAdverse > moveMax));
  // Probability is optional explicit backend evidence, never inferred from impact/confidence/PoP.
  const probability = finiteValue(g.discovery?.signal_probability) && g.discovery.signal_probability >= 0
    && g.discovery.signal_probability <= 1 ? g.discovery.signal_probability : null;
  let kind = "quiet", label = "Below screening margin";
  if (probability !== null && probability < .05) {
    kind = "quiet"; label = "Very low signal likelihood";
  } else if (priceBlocked && weatherHit) {
    kind = "moved"; label = "Under moved vs opener";
  } else if (weatherHit) {
    kind = "signal";
    label = rainSignal && windBand === "flag" ? "Signal · wind ≥15 mph · heavy rain"
      : rainSignal ? "Signal · heavy rain" : "Signal · wind ≥15 mph";
  } else if (watch) {
    kind = "near";
    const windNote = windBand === "watch" ? "Watch · 12–15 mph wind · not a flag" : "";
    const rainNote = rainTier === "watch" ? "Watch · rain 5–15 mm" : rainTier === "elevated" ? "Watch · rain 15–23 mm"
      : rainNear ? "Heavy rain, model agreement unverified" : "";
    label = windNote && rainNote ? `${windNote} · ${rainNote.replace(/^Watch · /, "")}` : (windNote || rainNote);
  }
  if (w.ecmwf_apart && (kind === "signal" || kind === "near")) label += " · ECMWF stands apart";
  if (proc.line_open_missing === true && (kind === "signal" || kind === "near" || kind === "moved"))
    label += " · opener line unknown";
  const note = probability === null ? "Signal likelihood unknown; screening margins are not probabilities"
    : `Signal likelihood ${(probability * 100).toFixed(1)}%`;
  return {kind, label, probability, note, windBand, rainTier};
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
    if (!expanded && state.focus !== "all" && !["signal", "near"].includes(d.kind)) return false;
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
    || !hardEligible(g) || !upcomingGame(g) || !r?.ok || r.game_id !== g.game_id
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
    if (!hardEligible(g)) return '<span class="offer-unusable">Not eligible</span>';
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
    return finiteValue(q.line) && (!finiteValue(selected) || q.line === selected) && !q.expired
      && age >= 0 && age <= 30000 && (!q.expires_at || Date.parse(q.expires_at) > Date.now());
  });
  const expired = (g.expired_markets || []).some(key => key.endsWith('/total') && EXCHANGE_BOOKS.has(key.split('/')[0])
    && (typeof STATE === 'undefined' || !STATE.book || key.split('/')[0] === STATE.book));
  const stale = [...quotes, ...evidence].some(q => {
    const age = Date.now() - Date.parse(quoteClock(q));
    return q.expired || !(age >= 0 && age <= 30000) || (q.expires_at && !(Date.parse(q.expires_at) > Date.now()));
  });
  if (!fresh && finiteValue(selected) && quotes.some(q => {
    const age = Date.now() - Date.parse(quoteClock(q));
    return finiteValue(q.line) && q.line !== selected && !q.expired && age >= 0 && age <= 30000
      && (!q.expires_at || Date.parse(q.expires_at) > Date.now());
  })) return {label: 'No exchange quote at selected total', note: 'Other totals observed; matching under / depth unavailable'};
  if (!fresh && (stale || expired)) return {label: "Exchange quotes stale", note: expired
    ? "Provider total expired; no current $500 depth check" : "No current $500 depth check"};
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
    || !hardEligible(g) || !upcomingGame(g) || !r?.ok || r.game_id !== g.game_id || r.side !== "under"
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
