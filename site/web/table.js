"use strict";
// Table view: one row per GameCard. Columns: GAME (kickoff ET/local), STADIUM/WIND, TEMP, WIND, GUST, RAIN,
// GS %, AWAY %, SIGNAL, SPREAD (consensus = avg of Betcris/BetOnline/Pinnacle, src on hover),
// TOTAL (consensus, Pinnacle-weighted), then one TOTAL column per book (baseline → now, under price
// + edge chip on hover). Per-book SPREAD columns are hidden behind the "book spreads" checkbox
// (#bookspreads, remembered in localStorage). The Book filter narrows the per-book columns.

const MOVE_EPS = 0.05;
const BOOK_SPREADS_KEY = "fw.bookSpreads";
let BOOK_SPREADS = loadBookSpreads();

// Only the resolved stadium zone is venue evidence. GameCard.tz can default to
// Eastern, and kickoff_local can carry the schedule provider's zone instead.
function tableKickoffHtml(g) {
  const status = String(g.status || "").toLowerCase();
  if (status === "tbd" || /^tbd$/i.test(g.kickoff_utc || ""))
    return '<span class="sub kickoff-eastern">Kickoff TBD</span>';
  const utc = g.kickoff_utc;
  const date = typeof utc === "string" && /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(utc) ? parseTs(utc) : null;
  if (!date || status === "unknown") return '<span class="sub kickoff-eastern">Kickoff unknown</span>';

  const eastern = kickoffClock(date, "America/New_York");
  let html = `<span class="sub kickoff-eastern">${esc(eastern.date + ", " + eastern.time)}</span>`;
  const zone = typeof g.stadium?.timezone === "string" ? g.stadium.timezone.trim() : "";
  // Reject ambiguous fixed abbreviations such as EST; require an IANA location.
  if (!zone.includes("/") && zone !== "UTC")
    return html + '<span class="sub kickoff-local">Local time zone unknown</span>';
  try {
    const local = kickoffClock(date, zone);
    if (local.offset !== eastern.offset) {
      const label = (local.dateKey === eastern.dateKey ? "" : local.date + ", ") + local.time;
      html += `<span class="sub kickoff-local">Local: ${esc(label)}</span>`;
    }
  } catch (_) {
    html += '<span class="sub kickoff-local">Local time zone unknown</span>';
  }
  return html;
}
function kickoffClock(date, zone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, weekday: "short", year: "numeric", month: "numeric", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: "short",
  }).formatToParts(date);
  const value = key => parts.find(part => part.type === key)?.value;
  const offset = new Intl.DateTimeFormat("en-US", {timeZone: zone, timeZoneName: "shortOffset"})
    .formatToParts(date).find(part => part.type === "timeZoneName").value;
  // en-US ICU often labels London summer as GMT+1. Use its local BST/GMT names.
  const label = zone === "Europe/London" ? (offset === "GMT+1" ? "BST" : "GMT") : value("timeZoneName");
  return {
    date: `${value("weekday")}, ${value("month")}/${value("day")}`,
    dateKey: `${value("year")}-${value("month")}-${value("day")}`,
    time: `${value("hour")}:${value("minute")} ${value("dayPeriod")} ${label}`, offset,
  };
}

function loadBookSpreads() {
  try { return localStorage.getItem(BOOK_SPREADS_KEY) === "1"; } catch (_) { return false; }
}
function saveBookSpreads(on) {
  try { localStorage.setItem(BOOK_SPREADS_KEY, on ? "1" : "0"); } catch (_) { /* private mode / blocked storage */ }
}
// wired from app.js init: the header checkbox toggles the per-book SPREAD columns
function setupTableControls() {
  const el = document.getElementById("bookspreads");
  if (!el) return;
  el.checked = BOOK_SPREADS;
  el.addEventListener("change", (e) => {
    BOOK_SPREADS = !!e.target.checked;
    saveBookSpreads(BOOK_SPREADS);
    STATE.sort = null;   // ids are stable; reset anyway for a predictable order
    render();
  });
}

function moveTag(open, now, invert = false) {
  if (!isNum(open) || !isNum(now)) return "";
  const d = Number(now) - Number(open);
  if (Math.abs(d) < MOVE_EPS) return "";
  const up = invert ? d < 0 : d > 0;
  return `<span class="mv ${up ? "up" : "dn"}" title="changed ${d > 0 ? "+" : ""}${d.toFixed(1)} from baseline">${d > 0 ? "▲" : "▼"}${Math.abs(d).toFixed(1)}</span>`;
}
function openNow(open, now, fmt) {
  if (!isNum(open) && !isNum(now)) return `<span class="muted">—</span>`;
  const o = isNum(open) ? fmt(open) : "—";
  const n = isNum(now) ? fmt(now) : "—";
  return (isNum(open) && isNum(now) && Math.abs(open - now) < MOVE_EPS) ? n : `<span class="muted">${o}</span> → ${n}`;
}
function tierChip(e) {
  if (!e || !isNum(e.edge_pts)) return "";
  const tier = e.tier || "none";
  const txt = (e.edge_pts >= 0 ? "+" : "") + Number(e.edge_pts).toFixed(1);
  const tip = `${e.side || ""} ${fmtLine(e.line)} @ ${fmtOdds(e.odds)} · fair ${fmtTotal(e.fair_line)} · ${tier}`
    + (isNum(e.confidence) ? ` · conf ${Number(e.confidence).toFixed(2)}` : "");
  return `<span class="tierchip ${esc(tier)}" title="${esc(tip)}">${txt}</span>`;
}
function signalPill(sig, game) {
  if (game && !hardEligible(game)) return '<span class="muted">Ineligible</span>';
  let label = signalLabel(sig);
  const proc = (game && game.process) || {};
  const drivers = (sig && sig.drivers) || [];
  const rainDriven = drivers.includes("rain") || /\(Rain\)/i.test((sig && sig.label) || "");
  const disagreed = proc.rain_agreement && proc.rain_agreement !== "agree";
  if (rainDriven && disagreed) {
    if (proc.wind_band === "flag") label = "Low (Wind)";
    else label = proc.rain_agreement === "disagree" ? "Rain models disagree" : "Rain not confirmed";
  }
  const matched = game && game.sport === "cfb" && typeof gameFlags === "function"
    ? gameFlags(game) : ((sig && sig.flags) || []);
  const flags = matched.length ? ` · ${matched.join(", ")}` : "";
  const muted = rainDriven && disagreed && proc.wind_band !== "flag";
  return `<span class="sig" style="background:${muted ? "#8b949e" : signalColor(sig)}" title="${esc(label + flags)}">${esc(label)}</span>`;
}
function weatherCoverageHtml(g) {
  const wx = g.weather || {}, issues = [];
  const missing = ["temp_fg", "wind_fg", "rain_fg"].filter(key => !finiteValue(wx[key]));
  if (missing.length) issues.push("Forecast incomplete");
  if (wx.point_aged) issues.push("Point forecast aged");
  if (wx.ensemble_status === "retained_members_degraded" || (wx.ensemble_unverified_sources || []).length) issues.push("Ensemble unverified");
  else if (wx.ensemble_status === "partial_members_degraded") issues.push("Partial ensemble");
  else if (wx.ensemble_status === "unavailable_degraded") issues.push("Ensemble unavailable");
  else if (wx.ensemble_status === "aged_members") issues.push("Older ensemble");
  return issues.length ? `<span class="coverage-note">${esc(issues.join(" · "))}</span>` : "";
}
function spreadSrcLabel(src) {
  if (!src) return "?";
  return src === "fallback" ? "fallback (weighted median)" : `avg of ${src}`;
}
function marketCoverage(g, market) {
  const c = g.consensus || {}, saved = c[`${market}_n_books`];
  if (isNum(saved)) return Number(saved);
  // Older payloads collapsed spread and total counts. Count their live market
  // quotes independently until a refreshed payload supplies explicit counts.
  return Object.values(g.odds || {}).filter((markets) => {
    const q = markets[market];
    return q && !q.expired && isNum(market === "spread" ? q.home_line : q.line);
  }).length;
}
function totalBaselineLabel(g, quote, consensus = false) {
  const kind = quote[consensus ? "total_open_kind" : "open_kind"];
  if (kind === "true_open") return "true opener";
  if (kind === "first_seen") return "first seen";
  if (g.sport !== "cfb") return "first seen";
  const prefix = consensus ? "total_open" : "open";
  const seen = parseTs(quote[`${prefix}_ts`]), target = parseTs(quote[`${prefix}_target_ts`]);
  if (seen && target) return seen > target ? "first seen (after T−6d)" : "T−6d";
  return "baseline (T−6d / first seen)";
}
function consensusSpreadCell(g) {
  const c = g.consensus || {};
  if (!isNum(c.spread_now) && !isNum(c.spread_open)) return `<td class="muted">—</td>`;
  const hk = ++HK;
  const f = g.fair || {};
  const nBooks = marketCoverage(g, "spread");
  HOVER[hk] = {
    label: `Consensus spread (home) · ${gameLabel(g)}`,
    lines: [
      ["src", spreadSrcLabel(c.spread_src)],
      ["open", fmtLine(c.spread_open)],
      ["now", fmtLine(c.spread_now)],
      ["spread coverage", `${nBooks} current main-line book${nBooks === 1 ? "" : "s"}${nBooks < 2 ? "; comparison needs at least 2" : ""}`],
      ...(isNum(f.fair_spread) ? [["fair", fmtLine(f.fair_spread)]] : []),
    ],
  };
  return `<td data-hk="${hk}" title="${esc(spreadSrcLabel(c.spread_src))}">${openNow(c.spread_open, c.spread_now, fmtLine)}${moveTag(c.spread_open, c.spread_now)}`
    + `${nBooks < 2 ? ` <span class="sub" title="Fewer than two current main-line books; weather eligibility is independent">${nBooks === 1 ? "1 book" : "0 books"}</span>` : ""}</td>`;
}
function consensusTotalCell(g) {
  const c = g.consensus || {};
  if (!isNum(c.total_now) && !isNum(c.total_open)) return `<td class="muted">—</td>`;
  const hk = ++HK;
  const f = g.fair || {};
  const baseline = totalBaselineLabel(g, c, true);
  HOVER[hk] = {
    label: `Consensus total · ${gameLabel(g)}`,
    lines: [
      ["ref", `${c.ref_book || "?"} (${marketCoverage(g, "total")} total books)`],
      [baseline, fmtTotal(c.total_open)],
      ...(c.total_open_ts ? [["baseline observed", fmtShortET(c.total_open_ts)]] : []),
      ["now", fmtTotal(c.total_now)],
      ...(isNum(f.fair_total) ? [["fair", fmtTotal(f.fair_total)]] : []),
    ],
  };
  return `<td data-hk="${hk}">${openNow(c.total_open, c.total_now, fmtTotal)}${moveTag(c.total_open, c.total_now)}</td>`;
}
function quoteAgeText(ts) {
  const age = Date.now() - Date.parse(ts);
  if (!Number.isFinite(age) || age < 0) return "";
  const mins = Math.round(age / 60000);
  return mins < 1 ? "<1m old" : `${mins}m old`;
}
function betcrisReference(quote) {
  const until = Date.parse(quote?.actionable_until);
  return Number.isFinite(until) && Date.now() > until;
}
function betcrisAgeNote(quote) {
  if (!quote) return "";
  const age = quoteAgeText(quote.source_updated_at || quote.updated_at);
  const reference = betcrisReference(quote);
  if (!age && !reference) return "";
  return `${age || "age unknown"}${reference ? " · reference" : ""}`;
}
function blankOddsCell(g, bk, market, text) {
  const expired = Array.isArray(g.expired_markets) && g.expired_markets.includes(`${bk}/${market}`);
  return expired ? `<td class="muted" title="quote expired">${text}</td>` : `<td class="muted">${text}</td>`;
}
function bookSpreadCell(g, bk) {
  const o = (g.odds || {})[bk];
  const s = o && o.spread;
  if (!s || (!isNum(s.home_line) && !isNum(s.open_line))) return blankOddsCell(g, bk, "spread", "—");
  const e = edgeAt(g, bk, "spread");
  const hk = ++HK;
  HOVER[hk] = {
    label: `${bookLabel(bk)} spread (home) · ${gameLabel(g)}`,
    lines: [
      ["open", `${fmtLine(s.open_line)} ${fmtOdds(s.open_odds)}`],
      ["now", `${fmtLine(s.home_line)} ${fmtOdds(s.home_odds)} / ${fmtOdds(s.away_odds)}`],
      ...(e ? [["fair", `${fmtLine(e.fair_line)} (${e.ref_book || "consensus"}, n=${e.n_books || "?"})`], ["edge", isNum(e.edge_pts) ? `${Number(e.edge_pts).toFixed(2)} pts · ${e.tier}` : "Comparison unavailable"]] : []),
      ...(s.updated_at ? [["updated", fmtShortET(s.updated_at)]] : []),
      ...(bk === "betcris" && betcrisAgeNote(s) ? [["age", betcrisAgeNote(s)]] : []),
    ],
  };
  const age = bk === "betcris" ? betcrisAgeNote(s) : "";
  return `<td class="book" data-hk="${hk}">${openNow(s.open_line, s.home_line, fmtLine)}${moveTag(s.open_line, s.home_line)}${age ? `<span class="sub">${esc(age)}</span>` : ""}${tierChip(e)}</td>`;
}
function bookTotalCell(g, bk) {
  const o = (g.odds || {})[bk];
  const t = o && o.total;
  if (!t || (!isNum(t.line) && !isNum(t.open_line) && !t.last_observed)) return blankOddsCell(g, bk, "total", "No quote");
  const e = edgeAt(g, bk, "total");
  const hk = ++HK;
  const baseline = totalBaselineLabel(g, t);
  HOVER[hk] = {
    label: `${bookLabel(bk)} total · ${gameLabel(g)}`,
    lines: [
      [baseline, `${fmtTotal(t.open_line)} u${fmtOdds(t.open_under)}`],
      ...(t.open_ts ? [["baseline observed", fmtShortET(t.open_ts)]] : []),
      ["now", `${fmtTotal(t.line)} o${fmtOdds(t.over)} / u${fmtOdds(t.under)}`],
      ...(e ? [["fair", `${fmtTotal(e.fair_line)} (${e.ref_book || "consensus"}, n=${e.n_books || "?"})`], ["edge", isNum(e.edge_pts) ? `${Number(e.edge_pts).toFixed(2)} pts ${e.side || ""} · ${e.tier}` : "Comparison unavailable"]] : []),
      ...(t.updated_at ? [["updated", fmtShortET(t.updated_at)]] : []),
      ...(bk === "betcris" && betcrisAgeNote(t) ? [["age", betcrisAgeNote(t)]] : []),
      ...(t.last_observed ? [["last observed (reference only)", `U ${fmtTotal(t.last_observed.line)} ${fmtOdds(t.last_observed.under)}`],
        ["source clock", t.last_observed.updated_at || "Unknown"], ["reference status", t.last_observed.status]] : []),
      ...(typeof backtestHover === "function" ? backtestHover(g) : []),   // Record / ROI by first-match bucket
    ],
  };
  const evidence = HOVER[hk].lines.map(([label, value]) => `${label}: ${value}`).join("; ");
  const ref = t.last_observed;
  const unavailable = ref ? ({stale:'Stale reference', expired:'Expired reference', unavailable:'Source unavailable', unknown:'Unverified reference', current:'Not refreshed'}[ref.status] || 'Unavailable') : 'Unavailable';
  return `<td class="book compact-total" data-hk="${hk}"><button type="button" class="book-total-detail" data-game="${esc(g.game_id)}"
    title="${esc(evidence)}" aria-label="${esc(bookLabel(bk) + ' total details; ' + evidence)}"><b>${isNum(t.line) && !t.expired ? 'U ' + fmtTotal(t.line) : unavailable}</b>
    <span class="sub">${isNum(t.line) && !t.expired ? fmtOdds(t.under) : ref ? 'Last U ' + fmtTotal(ref.line) + ' · ' + fmtOdds(ref.under) : 'Baseline ' + fmtTotal(t.open_line)}${bk === "betcris" && betcrisAgeNote(t) ? ` · ${esc(betcrisAgeNote(t))}` : ""}</span></button></td>`;
}

// column spec: [label, title, sortKey(g) or null, cell(g) or null (fixed cells are built inline)]
function totalPriceQuotes(g, side = null) {
  if (!(Date.parse(g.kickoff_utc) > Date.now())) return [];
  return ((g.total_prices || {}).quotes || []).filter(q => EXCHANGE_BOOKS.has(q.book)
    && (!side || q.side === side) && (!STATE.book || q.book === STATE.book))
    .sort((a, b) => (Date.parse(quoteClock(b)) || 0) - (Date.parse(quoteClock(a)) || 0));
}
function pricePercent(value) { return isNum(value) ? `${(Number(value) * 100).toFixed(1)}%` : "—"; }
function roiLabel(value) { return `${value > 0 ? "+" : ""}${(value * 100).toFixed(1)}%`; }
function totalPriceLabel(quote) {
  return `${quote.side === "under" ? "U" : "O"} ${fmtTotal(quote.line)} · ${fmtOdds(quote.odds)}`;
}
function coordinateLabel(g) { return stadiumCoordinates(g); }
function tableStadiumHtml(g) {
  const evidence = typeof stadiumSignalEvidence === "function" ? stadiumSignalEvidence(g) : [];
  const hk = ++HK;
  HOVER[hk] = {label: `Stadium history · ${gameLabel(g)}`, lines: evidence};
  return `<td class="left stadium-cell" data-hk="${hk}"><button type="button" class="stadium-detail" data-game="${esc(g.game_id)}" aria-label="Stadium orientation, wind and historical signal details">
    <span class="compass compact-compass">${typeof compassSvg === "function" ? compassSvg(g) : 'Diagram unavailable'}</span><span class="stadium-summary">${esc(g.stadium?.name || "Stadium unavailable")}
    <span class="sub">${isNum(g.weather?.wind_fg) ? fmtNum(g.weather.wind_fg, 1) + ' mph' : 'Wind unavailable'} ${esc(g.weather?.wind_dir_fg || '')}</span>
    ${!isNum(g.stadium?.orient_deg) ? '<span class="sub">Axis unknown</span>' : ''}${!isNum(g.weather?.wind_dir_deg) ? '<span class="sub">Direction unknown</span>' : ''}</span></button>
    ${typeof stadiumSignalCompact === "function" ? stadiumSignalCompact(g) : ''}${typeof stadiumClimateCompact === "function" ? stadiumClimateCompact(g) : ''}${coordinateControl(g)}</td>`;
}

// ── sorting (pure; unit-tested in site/worker/test/table-sort.test.mjs) ──────
// Missing = null/undefined/NaN/+-Infinity/"" (sort keys use -Infinity for "no value"). Missing always
// sorts last in either direction; ties break on kickoff then game_id so the order is deterministic.
function sortMissing(v) {
  return v == null || v === "" || v === -Infinity || v === Infinity || (typeof v === "number" && Number.isNaN(v));
}
function compareSortValues(x, y, dir) {
  const mx = sortMissing(x), my = sortMissing(y);
  if (mx || my) return mx && my ? 0 : mx ? 1 : -1;
  let c;
  if (typeof x === "string" || typeof y === "string") c = String(x).localeCompare(String(y));
  else c = x === y ? 0 : x < y ? -1 : 1;
  return c * (dir < 0 ? -1 : 1);
}
function sortGames(rows, key, dir) {
  const kick = (g) => { const t = Date.parse(g.kickoff_utc); return Number.isNaN(t) ? Infinity : t; };
  return rows.sort((a, b) => compareSortValues(key(a), key(b), dir)
    || (kick(a) === kick(b) ? 0 : kick(a) < kick(b) ? -1 : 1)
    || String(a.game_id).localeCompare(String(b.game_id)));
}
// column spec: [label, title, sortKey, cell, id]; STATE.sort holds the id string so the sort survives
// the Book filter / spread-column toggle changing the column set.
const FIXED_COLUMN_IDS = ["game", "discovery", "stadium", "temp", "wind", "gust", "rain", "gs", "away", "signal", "spread", "total"];
function sortColumnId(cols, id) { return cols.find((c) => c[4] === id) || null; }
function warnBookHeader(label, bk, sport) {
  const h = typeof bookHealthFor === "function" ? bookHealthFor(DATA.meta, bk, sport) : null;
  if (!h || !(h.consecutive_empty > 0)) return {label, tip: ""};
  const ok = h.last_ok_utc && typeof fmtShortET === "function" ? fmtShortET(h.last_ok_utc) : "never";
  const tip = `${bookLabel(bk)}: 0 lines for ${h.consecutive_empty} run${h.consecutive_empty === 1 ? "" : "s"}, last OK ${ok}`
    + (h.last_error ? ` — ${h.last_error}` : "");
  return {label: `${label} ⚠`, tip};
}
function gameStarted(g) {
  const st = String(g.status || "").toLowerCase();
  if (/final/.test(st)) return "Final";
  if (/live|progress|in_play|half/.test(st)) return "Live";
  if (/cancel|postpon|suspend|tbd|unknown/.test(st)) return "";
  const t = Date.parse(g.kickoff_utc);
  return !Number.isNaN(t) && t < Date.now() ? "Live" : "";
}
function gameStateBadge(g) {
  const s = gameStarted(g);
  return s ? ` <span class="pill ${s === "Final" ? "ok" : "warn"} game-state" title="Game ${s === "Final" ? "finished" : "started"}">${s}</span>` : "";
}

function tableColumns(books, withSpreads = BOOK_SPREADS) {
  const w = (k) => (g) => (g.weather && isNum(g.weather[k]) ? Number(g.weather[k]) : -Infinity);
  const cons = (k) => (g) => (g.consensus && isNum(g.consensus[k]) ? Number(g.consensus[k]) : -Infinity);
  const cols = [
    ["Game", "Away @ Home · kickoff Eastern (EDT/EST), then stadium local time if different. Click for detail.", (g) => parseTs(g.kickoff_utc) ? parseTs(g.kickoff_utc).getTime() : 0],
    ["Discovery", "Process screen: cold + sustained wind or heavy rain. Likelihood is separate from impact severity", g => ({signal: 0, near: 1, moved: 2, unknown: 3, quiet: 4})[discoveryState(g).kind] ?? 5],
    ["Stadium / wind", "Field axis and wind blowing from the reported bearing. Exact coordinates and retrospective same-venue signal records; details show rules and coverage.", coordinateLabel],
    ["Temp", "Forecast temp °F at kickoff (3h mean)", w("temp_fg")],
    ["Wind", "Forecast wind mph (3h mean) · direction", w("wind_fg")],
    ["Gust", "Forecast gust mph", w("gust_fg")],
    ["Rain", "Rain mm over kickoff..+2h · precip prob", w("rain_fg")],
    ["GS %", "v1 game-score impact % (negative = under lean)", (g) => impactPct(g, "gs_fg_pct")],
    ["Away %", "v1 away-team impact %", (g) => impactPct(g, "away_fg_pct")],
    ["Signal", "Weather signal severity + matched filters", (g) => ["No", "Low", "Mid", "High", "Very High"].indexOf(signalTier(g.signal))],
    ["Spread", "Consensus spread (home) open → now = average of Betcris / BetOnline / Pinnacle (hover for the books used)", cons("spread_now")],
    ["Total", "Consensus baseline → now. CFB targets kickoff minus 6 days, falling back to first observed if collected later; NFL uses first seen. Pinnacle-weighted.", cons("total_now")],
  ];
  cols.forEach((c, i) => { c[4] = FIXED_COLUMN_IDS[i]; });
  for (const bk of books) {
    if (withSpreads) {
      cols.push([`${bookLabel(bk)} S`, `${bookLabel(bk)} spread open → now; chip = edge pts vs fair`,
        (g) => { const e = edgeAt(g, bk, "spread"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; },
        (g) => bookSpreadCell(g, bk), `${bk}:s`]);
    }
    cols.push([`${bookLabel(bk)} T`, `${bookLabel(bk)} current under total / price; baseline, movement, source time and edge in hover or details`,
      (g) => { const e = edgeAt(g, bk, "total"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; },
      (g) => bookTotalCell(g, bk), `${bk}:t`]);
  }
  return cols;
}
function impactPct(g, key) {
  const v1 = g.impact && g.impact.v1;
  return v1 && isNum(v1[key]) ? Number(v1[key]) : -Infinity;
}

// opts.keepOrder: caller already ordered rows (Signals presets) → skip the default kickoff sort
function renderTable(rows, opts = {}) {
  const thead = document.querySelector("#table thead");
  const tbody = document.querySelector("#table tbody");
  const books = STATE.book ? [STATE.book] : BOOKS;
  const cols = tableColumns(books, BOOK_SPREADS);
  const active = sortColumnId(cols, STATE.sort);
  thead.innerHTML = "<tr>" + cols.map(([label, title, , , id]) => {
    const on = !!active && active[4] === id;
    const arrow = on ? (STATE.dir < 0 ? " ▾" : " ▴") : "";
    const bk = /:[st]$/.test(id) ? id.replace(/:[st]$/, "") : null;
    const warn = bk ? warnBookHeader(label, bk, STATE.sport) : {label, tip: ""};
    return `<th data-col="${esc(id)}" tabindex="0" role="button" aria-sort="${on ? (STATE.dir < 0 ? "descending" : "ascending") : "none"}" class="sortable${warn.tip ? " book-warn" : ""}" title="${esc(warn.tip ? warn.tip + "\n" + title : title)}">${esc(warn.label)}${arrow}</th>`;
  }).join("") + "</tr>";
  thead.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
    const c = th.dataset.col; STATE.dir = STATE.sort === c ? -STATE.dir : -1; STATE.sort = c; render();
  }));

  thead.querySelectorAll("th.sortable").forEach(th => th.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); th.click(); }
  }));
  rows = rows.filter(g => upcomingGame(g) && (showAllBoardGames(STATE) || hardEligible(g)));
  const sortCol = STATE.sort != null ? sortColumnId(cols, STATE.sort) : null;
  if (sortCol) {
    sortGames(rows, sortCol[2], STATE.dir);
    // started/final games sink below upcoming ones regardless of the chosen column
    if (rows.some(gameStarted)) rows = rows.filter((g) => !gameStarted(g)).concat(rows.filter(gameStarted));
  } else if (!opts.keepOrder) {
    rows.sort((a, b) => (parseTs(a.kickoff_utc) || 0) - (parseTs(b.kickoff_utc) || 0) || String(a.game_id).localeCompare(String(b.game_id)));
  }
  if (!rows.length) {
    tbody.innerHTML = `<tr><td class="empty" colspan="${cols.length}">${typeof LOAD_ERRORS !== "undefined" && (LOAD_ERRORS.meta || LOAD_ERRORS[STATE.sport]) ? "Game data unavailable; reload to retry." : "No eligible candidates for these filters. Inspect low likelihood / unknown to review other open-air games."}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((g) => {
    const wx = g.weather || {};
    const dome = isDome(g);
    const v1 = (g.impact && g.impact.v1) || {};
    const tds = [
      `<td class="game" data-game="${esc(g.game_id)}"><button type="button" class="game-detail" data-game="${esc(g.game_id)}">${esc(gameLabel(g))}</button>${g.neutral ? ' <span class="sub">(N)</span>' : ""}${gameStateBadge(g)}${tableKickoffHtml(g)}</td>`,
      `<td class="left">${discoveryHtml(g)}${signalPill(g.signal, g)}${weatherCoverageHtml(g)}</td>`,
      tableStadiumHtml(g),
      `<td>${fmtNum(wx.temp_fg, 0)}</td>`,
      `<td>${fmtNum(wx.wind_fg, 1)}${wx.wind_dir_fg ? ` <span class="wx">${esc(wx.wind_dir_fg)}</span>` : ""}</td>`,
      `<td>${fmtNum(wx.gust_fg, 0)}</td>`,
      `<td>${fmtNum(wx.rain_fg, 1)}${isNum(wx.precip_prob) ? ` <span class="wx">${Math.round(Number(wx.precip_prob) * (wx.precip_prob <= 1 ? 100 : 1))}%</span>` : ""}</td>`,
      `<td>${dome ? '<span class="muted">dome</span>' : fmtNum(v1.gs_fg_pct, 1)}</td>`,
      `<td>${dome ? '<span class="muted">—</span>' : fmtNum(v1.away_fg_pct, 1)}</td>`,
      `<td>${signalPill(g.signal, g)}</td>`,
      consensusSpreadCell(g),
      consensusTotalCell(g),
    ];
    for (const col of cols) { if (typeof col[3] === "function") tds.push(col[3](g)); }
    return `<tr class="${dome ? "dome" : ""}${gameStarted(g) ? " started" : ""}" data-game="${esc(g.game_id)}">${tds.join("")}</tr>`;
  }).join("");
  tbody.querySelectorAll(".game-detail").forEach((button) => button.addEventListener("click", () => openDrawer(button.dataset.game)));
  tbody.querySelectorAll(".stadium-detail, .book-total-detail").forEach(button => button.addEventListener("click", () => {
    openDrawer(button.dataset.game);
    if (button.classList.contains("stadium-detail")) {
      const history = document.querySelector("#drawer-stadium-history details");
      if (history) { history.open = true; history.querySelector("summary").focus(); }
    }
    if (button.classList.contains("book-total-detail")) document.getElementById("drawer-odds-heading")?.focus();
  }));
}
