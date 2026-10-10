"use strict";
// Table view: one row per GameCard. Columns: GAME (kickoff ET), LAT/LON, TEMP, WIND, GUST, RAIN,
// GS %, AWAY %, SIGNAL, SPREAD (consensus = avg of Betcris/BetOnline/Pinnacle, src on hover),
// TOTAL (consensus, Pinnacle-weighted), then one TOTAL column per book (baseline → now, under price
// + edge chip on hover). Per-book SPREAD columns are hidden behind the "book spreads" checkbox
// (#bookspreads, remembered in localStorage). The Book filter narrows the per-book columns.

const MOVE_EPS = 0.05;
const BOOK_SPREADS_KEY = "fw.bookSpreads";
let BOOK_SPREADS = loadBookSpreads();

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
    STATE.sort = null;   // column indexes shift when the spread columns appear
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
  const label = signalLabel(sig);
  const matched = game && game.sport === "cfb" && typeof gameFlags === "function"
    ? gameFlags(game) : ((sig && sig.flags) || []);
  const flags = matched.length ? ` · ${matched.join(", ")}` : "";
  return `<span class="sig" style="background:${signalColor(sig)}" title="${esc(label + flags)}">${esc(label)}</span>`;
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
function bookSpreadCell(g, bk) {
  const o = (g.odds || {})[bk];
  const s = o && o.spread;
  if (!s || (!isNum(s.home_line) && !isNum(s.open_line))) return `<td class="muted">—</td>`;
  const e = edgeAt(g, bk, "spread");
  const hk = ++HK;
  HOVER[hk] = {
    label: `${bookLabel(bk)} spread (home) · ${gameLabel(g)}`,
    lines: [
      ["open", `${fmtLine(s.open_line)} ${fmtOdds(s.open_odds)}`],
      ["now", `${fmtLine(s.home_line)} ${fmtOdds(s.home_odds)} / ${fmtOdds(s.away_odds)}`],
      ...(e ? [["fair", `${fmtLine(e.fair_line)} (${e.ref_book || "consensus"}, n=${e.n_books || "?"})`], ["edge", isNum(e.edge_pts) ? `${Number(e.edge_pts).toFixed(2)} pts · ${e.tier}` : "Comparison unavailable"]] : []),
      ...(s.updated_at ? [["updated", fmtShortET(s.updated_at)]] : []),
    ],
  };
  return `<td class="book" data-hk="${hk}">${openNow(s.open_line, s.home_line, fmtLine)}${moveTag(s.open_line, s.home_line)}${tierChip(e)}</td>`;
}
function bookTotalCell(g, bk) {
  const o = (g.odds || {})[bk];
  const t = o && o.total;
  if (!t || (!isNum(t.line) && !isNum(t.open_line))) return `<td class="muted">—</td>`;
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
      ...(typeof backtestHover === "function" ? backtestHover(g) : []),   // Record / ROI by first-match bucket
    ],
  };
  return `<td class="book" data-hk="${hk}">${openNow(t.open_line, t.line, fmtTotal)}${moveTag(t.open_line, t.line)}${tierChip(e)}</td>`;
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
function bestPriceCell(g) { return `<td class="book best-price">${exchangeOfferHtml(g)}</td>`; }
function coordinateLabel(g) { return stadiumCoordinates(g); }

function tableColumns(books, withSpreads = BOOK_SPREADS) {
  const w = (k) => (g) => (g.weather && isNum(g.weather[k]) ? Number(g.weather[k]) : -Infinity);
  const cons = (k) => (g) => (g.consensus && isNum(g.consensus[k]) ? Number(g.consensus[k]) : -Infinity);
  const cols = [
    ["Game", "Away @ Home · kickoff ET. Click for detail.", (g) => parseTs(g.kickoff_utc) ? parseTs(g.kickoff_utc).getTime() : 0],
    ["Discovery", "Weather signal and screening margin; likelihood is separate from impact severity", g => ({signal: 0, near: 1, unknown: 2, quiet: 3})[discoveryState(g).kind]],
    ["Exchange under", "Verified $500 principal stake; fees additional, fresh matching depth and settlement rules", g => verifiedOffer(g)?.average_price ?? Infinity],
    ["Lat, Lon", "Exact supplied stadium coordinates; copy into Windy", coordinateLabel],
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
  for (const bk of books) {
    if (withSpreads) {
      cols.push([`${bookLabel(bk)} S`, `${bookLabel(bk)} spread open → now; chip = edge pts vs fair`,
        (g) => { const e = edgeAt(g, bk, "spread"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; },
        (g) => bookSpreadCell(g, bk)]);
    }
    cols.push([`${bookLabel(bk)} T`, `${bookLabel(bk)} total baseline → now; CFB targets kickoff minus 6 days with first-observed fallback; hover = baseline time and under price; edge chip = pts vs fair`,
      (g) => { const e = edgeAt(g, bk, "total"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; },
      (g) => bookTotalCell(g, bk)]);
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
  thead.innerHTML = "<tr>" + cols.map(([label, title], i) => {
    const arrow = STATE.sort === i ? (STATE.dir < 0 ? " ▾" : " ▴") : "";
    return `<th data-col="${i}" tabindex="0" role="button" aria-sort="${STATE.sort === i ? (STATE.dir < 0 ? "descending" : "ascending") : "none"}" class="sortable" title="${esc(title)}">${esc(label)}${arrow}</th>`;
  }).join("") + "</tr>";
  thead.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
    const c = +th.dataset.col; STATE.dir = STATE.sort === c ? -STATE.dir : -1; STATE.sort = c; render();
  }));

  thead.querySelectorAll("th.sortable").forEach(th => th.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); th.click(); }
  }));
  rows = rows.filter(hardEligible);
  if (STATE.sort != null && cols[STATE.sort]) {
    const key = cols[STATE.sort][2];
    rows.sort((a, b) => {
      const x = key(a), y = key(b);
      if (typeof x === "string" || typeof y === "string") return String(x).localeCompare(String(y)) * -STATE.dir;
      return (x - y) * STATE.dir;
    });
  } else if (!opts.keepOrder) {
    rows.sort((a, b) => (parseTs(a.kickoff_utc) || 0) - (parseTs(b.kickoff_utc) || 0));
  }
  if (!rows.length) {
    tbody.innerHTML = `<tr><td class="empty" colspan="${cols.length}">${typeof LOAD_ERRORS !== "undefined" && (LOAD_ERRORS.meta || LOAD_ERRORS[STATE.sport]) ? "Game data unavailable; reload to retry." : "No eligible candidates for these filters. Inspect low likelihood / unknown to review other open-air games."}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((g) => {
    const wx = g.weather || {};
    const st = g.stadium || {};
    const dome = isDome(g);
    const v1 = (g.impact && g.impact.v1) || {};
    const coordinates = coordinateLabel(g);
    const tds = [
      `<td class="game" data-game="${esc(g.game_id)}"><button type="button" class="game-detail" data-game="${esc(g.game_id)}">${esc(gameLabel(g))}</button>${g.neutral ? ' <span class="sub">(N)</span>' : ""}<span class="sub">${esc(kickoffLabel(g))}</span></td>`,
      `<td class="left">${discoveryHtml(g)}${signalPill(g.signal, g)}${weatherCoverageHtml(g)}</td>`,
      bestPriceCell(g),
      `<td class="left" title="${esc(st.name || "Coordinates unavailable")}">${coordinateControl(g)}</td>`,
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
    return `<tr class="${dome ? "dome" : ""}" data-game="${esc(g.game_id)}">${tds.join("")}</tr>`;
  }).join("");
  tbody.querySelectorAll(".game-detail").forEach((button) => button.addEventListener("click", () => openDrawer(button.dataset.game)));
}
