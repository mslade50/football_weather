"use strict";
// Table view: one row per GameCard. Columns: GAME (kickoff ET), STADIUM, TEMP, WIND, GUST, RAIN,
// GS %, AWAY %, SIGNAL, SPREAD (consensus = avg of Betcris/BetOnline/Pinnacle, src on hover),
// TOTAL (consensus, Pinnacle-weighted), then one TOTAL column per book (open → now, under price
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
  return `<span class="mv ${up ? "up" : "dn"}" title="moved ${d > 0 ? "+" : ""}${d.toFixed(1)} since open">${d > 0 ? "▲" : "▼"}${Math.abs(d).toFixed(1)}</span>`;
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
function signalPill(sig) {
  const label = signalLabel(sig);
  const flags = (sig && sig.flags && sig.flags.length) ? ` · ${sig.flags.join(", ")}` : "";
  return `<span class="sig" style="background:${signalColor(sig)}" title="${esc(label + flags)}">${esc(label)}</span>`;
}
function spreadSrcLabel(src) {
  if (!src) return "?";
  return src === "fallback" ? "fallback (weighted median)" : `avg of ${src}`;
}
function consensusSpreadCell(g) {
  const c = g.consensus || {};
  if (!isNum(c.spread_now) && !isNum(c.spread_open)) return `<td class="muted">—</td>`;
  const hk = ++HK;
  const f = g.fair || {};
  HOVER[hk] = {
    label: `Consensus spread (home) · ${gameLabel(g)}`,
    lines: [
      ["src", spreadSrcLabel(c.spread_src)],
      ["open", fmtLine(c.spread_open)],
      ["now", fmtLine(c.spread_now)],
      ["books", `n=${c.n_books ?? "?"}${c.thin ? " (thin)" : ""}`],
      ...(isNum(f.fair_spread) ? [["fair", fmtLine(f.fair_spread)]] : []),
    ],
  };
  return `<td data-hk="${hk}" title="${esc(spreadSrcLabel(c.spread_src))}">${openNow(c.spread_open, c.spread_now, fmtLine)}${moveTag(c.spread_open, c.spread_now)}`
    + `${c.thin ? ' <span class="sub" title="thin consensus">thin</span>' : ""}</td>`;
}
function consensusTotalCell(g) {
  const c = g.consensus || {};
  if (!isNum(c.total_now) && !isNum(c.total_open)) return `<td class="muted">—</td>`;
  const hk = ++HK;
  const f = g.fair || {};
  HOVER[hk] = {
    label: `Consensus total · ${gameLabel(g)}`,
    lines: [
      ["ref", `${c.ref_book || "?"} (n=${c.n_books ?? "?"})`],
      ["open", fmtTotal(c.total_open)],
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
      ...(e ? [["fair", `${fmtLine(e.fair_line)} (${e.ref_book || "consensus"}, n=${e.n_books || "?"})`], ["edge", `${Number(e.edge_pts).toFixed(2)} pts · ${e.tier}`]] : []),
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
  HOVER[hk] = {
    label: `${bookLabel(bk)} total · ${gameLabel(g)}`,
    lines: [
      ["open", `${fmtTotal(t.open_line)} u${fmtOdds(t.open_under)}`],
      ["now", `${fmtTotal(t.line)} o${fmtOdds(t.over)} / u${fmtOdds(t.under)}`],
      ...(e ? [["fair", `${fmtTotal(e.fair_line)} (${e.ref_book || "consensus"}, n=${e.n_books || "?"})`], ["edge", `${Number(e.edge_pts).toFixed(2)} pts ${e.side || ""} · ${e.tier}`]] : []),
      ...(t.updated_at ? [["updated", fmtShortET(t.updated_at)]] : []),
      ...(typeof backtestHover === "function" ? backtestHover(g) : []),   // Record / ROI by first-match bucket
    ],
  };
  return `<td class="book" data-hk="${hk}">${openNow(t.open_line, t.line, fmtTotal)}${moveTag(t.open_line, t.line)}${tierChip(e)}</td>`;
}

// column spec: [label, title, sortKey(g) or null, cell(g) or null (fixed cells are built inline)]
function tableColumns(books, withSpreads = BOOK_SPREADS) {
  const w = (k) => (g) => (g.weather && isNum(g.weather[k]) ? Number(g.weather[k]) : -Infinity);
  const cons = (k) => (g) => (g.consensus && isNum(g.consensus[k]) ? Number(g.consensus[k]) : -Infinity);
  const cols = [
    ["Game", "Away @ Home · kickoff ET. Click for detail.", (g) => parseTs(g.kickoff_utc) ? parseTs(g.kickoff_utc).getTime() : 0],
    ["Stadium", "Venue (roof)", (g) => (g.stadium && g.stadium.name) || ""],
    ["Temp", "Forecast temp °F at kickoff (3h mean)", w("temp_fg")],
    ["Wind", "Forecast wind mph (3h mean) · direction", w("wind_fg")],
    ["Gust", "Forecast gust mph", w("gust_fg")],
    ["Rain", "Rain mm over kickoff..+2h · precip prob", w("rain_fg")],
    ["GS %", "v1 game-score impact % (negative = under lean)", (g) => impactPct(g, "gs_fg_pct")],
    ["Away %", "v1 away-team impact %", (g) => impactPct(g, "away_fg_pct")],
    ["Signal", "Impact tier + combined flags", (g) => ["No", "Low", "Mid", "High", "Very High"].indexOf(signalTier(g.signal))],
    ["Spread", "Consensus spread (home) open → now = average of Betcris / BetOnline / Pinnacle (hover for the books used)", cons("spread_now")],
    ["Total", "Consensus total open → now (Pinnacle-weighted)", cons("total_now")],
  ];
  for (const bk of books) {
    if (withSpreads) {
      cols.push([`${bookLabel(bk)} S`, `${bookLabel(bk)} spread open → now; chip = edge pts vs fair`,
        (g) => { const e = edgeAt(g, bk, "spread"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; },
        (g) => bookSpreadCell(g, bk)]);
    }
    cols.push([`${bookLabel(bk)} T`, `${bookLabel(bk)} total open → now; hover = under price, edge chip = pts vs fair`,
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
    return `<th data-col="${i}" class="sortable" title="${esc(title)}">${esc(label)}${arrow}</th>`;
  }).join("") + "</tr>";
  thead.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
    const c = +th.dataset.col; STATE.dir = STATE.sort === c ? -STATE.dir : -1; STATE.sort = c; render();
  }));

  rows = rows.slice();
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
    tbody.innerHTML = `<tr><td class="empty" colspan="${cols.length}">No games for this sport/week/filters.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((g) => {
    const wx = g.weather || {};
    const st = g.stadium || {};
    const dome = isDome(g);
    const v1 = (g.impact && g.impact.v1) || {};
    const roof = st.roof_state || st.roof_type || "";
    const tds = [
      `<td class="game" data-game="${esc(g.game_id)}">${esc(gameLabel(g))}${g.neutral ? ' <span class="sub">(N)</span>' : ""}<span class="sub">${esc(kickoffLabel(g))}</span></td>`,
      `<td class="left">${esc(st.name || "")}${roof && roof !== "outdoors" && roof !== "open" ? ` <span class="sub">(${esc(roof)})</span>` : ""}</td>`,
      `<td>${fmtNum(wx.temp_fg, 0)}</td>`,
      `<td>${fmtNum(wx.wind_fg, 1)}${wx.wind_dir_fg ? ` <span class="wx">${esc(wx.wind_dir_fg)}</span>` : ""}</td>`,
      `<td>${fmtNum(wx.gust_fg, 0)}</td>`,
      `<td>${fmtNum(wx.rain_fg, 1)}${isNum(wx.precip_prob) ? ` <span class="wx">${Math.round(Number(wx.precip_prob) * (wx.precip_prob <= 1 ? 100 : 1))}%</span>` : ""}</td>`,
      `<td>${dome ? '<span class="muted">dome</span>' : fmtNum(v1.gs_fg_pct, 1)}</td>`,
      `<td>${dome ? '<span class="muted">—</span>' : fmtNum(v1.away_fg_pct, 1)}</td>`,
      `<td>${signalPill(g.signal)}</td>`,
      consensusSpreadCell(g),
      consensusTotalCell(g),
    ];
    for (const col of cols) { if (typeof col[3] === "function") tds.push(col[3](g)); }
    return `<tr class="${dome ? "dome" : ""}" data-game="${esc(g.game_id)}">${tds.join("")}</tr>`;
  }).join("");
  tbody.querySelectorAll("td.game").forEach((td) => td.addEventListener("click", () => openDrawer(td.dataset.game)));
}
