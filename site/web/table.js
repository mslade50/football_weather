"use strict";
// Table view: one row per GameCard. Columns: kickoff (ET), matchup, stadium, temp/wind/gust/rain,
// gs/away impact, signal, consensus spread/total open→now, per-book spread + total open→now
// with edge chips, best edge, tier.

const MOVE_EPS = 0.05;

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

// column spec: [label, title, sortKey(g) or null]
function tableColumns(books) {
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
    ["Spread", "Consensus spread (home) open → now", cons("spread_now")],
    ["Total", "Consensus total open → now", cons("total_now")],
  ];
  for (const bk of books) {
    cols.push([`${bookLabel(bk)} S`, `${bookLabel(bk)} spread open → now; chip = edge pts vs fair`,
      (g) => { const e = edgeAt(g, bk, "spread"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; }]);
    cols.push([`${bookLabel(bk)} T`, `${bookLabel(bk)} total open → now; chip = edge pts vs fair`,
      (g) => { const e = edgeAt(g, bk, "total"); return e && isNum(e.edge_pts) ? Math.abs(e.edge_pts) : -Infinity; }]);
  }
  cols.push(["Best edge", "Largest |edge_pts| across books/markets", (g) => { const e = bestEdge(g); return e ? Math.abs(e.edge_pts) : -Infinity; }]);
  cols.push(["Tier", "Tier of the best edge", (g) => { const e = bestEdge(g); return ["none", "watch", "edge", "strong"].indexOf(e ? e.tier : "none"); }]);
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
  const cols = tableColumns(books);
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
    const c = g.consensus || {};
    const be = bestEdge(g);
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
      `<td>${openNow(c.spread_open, c.spread_now, fmtLine)}${moveTag(c.spread_open, c.spread_now)}${c.thin ? ' <span class="sub" title="thin consensus">thin</span>' : ""}</td>`,
      `<td>${openNow(c.total_open, c.total_now, fmtTotal)}${moveTag(c.total_open, c.total_now)}</td>`,
    ];
    for (const bk of books) { tds.push(bookSpreadCell(g, bk)); tds.push(bookTotalCell(g, bk)); }
    tds.push(`<td>${be ? `${esc(bookLabel(be.book))} ${esc(be.market)} ${esc(be.side || "")} ${be.market === "total" ? fmtTotal(be.line) : fmtLine(be.line)}${tierChip(be)}` : '<span class="muted">—</span>'}</td>`);
    tds.push(`<td>${be ? `<span class="tierchip ${esc(be.tier)}">${esc(be.tier)}</span>` : '<span class="muted">—</span>'}</td>`);
    return `<tr class="${dome ? "dome" : ""}" data-game="${esc(g.game_id)}">${tds.join("")}</tr>`;
  }).join("");
  tbody.querySelectorAll("td.game").forEach((td) => td.addEventListener("click", () => openDrawer(td.dataset.game)));
}
