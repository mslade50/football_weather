"use strict";
// Game detail drawer: the three legacy tables (Weather / Odds by book / Game Info, from
// pages/nfl_weather.py + pages/cfb_weather.py), hourly strip, and a uPlot line-history chart
// fed by /api/history (D1 odds_history) with /data/history.json as fallback.

const DRAWER = { game: null, plot: null, wxPlots: [], histCache: {}, wxCache: {}, market: "total", book: "" };

function destroyWxPlots() {
  for (const pl of DRAWER.wxPlots) { try { pl.destroy(); } catch (_) { /* ignore */ } }
  DRAWER.wxPlots = [];
}

function setupDrawer() {
  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
}
function closeDrawer() {
  const d = document.getElementById("drawer");
  d.hidden = true;
  if (DRAWER.plot) { DRAWER.plot.destroy(); DRAWER.plot = null; }
  destroyWxPlots();
  DRAWER.game = null;
  STATE.game = null;
  writeHash();
}

const kv = (rows) => `<table class="kv">${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("")}</table>`;
const pct = (v) => (isNum(v) ? `${Number(v).toFixed(1)}%` : "—");
const yesno = (v) => (v == null ? "—" : String(v));

function weatherTable(g) {
  const wx = g.weather || {}, st = g.stadium || {}, v1 = (g.impact && g.impact.v1) || {};
  const v2 = g.impact && g.impact.v2;
  const rows = [
    ["Wind", `${fmtNum(wx.wind_fg, 1)} mph ${esc(wx.wind_dir_fg || "")}${isNum(wx.gust_fg) ? ` · gust ${fmtNum(wx.gust_fg, 0)}` : ""}`],
    ["Temp", `${fmtNum(wx.temp_fg, 0)} °F`],
    ["Rain", `${fmtNum(wx.rain_fg, 1)} mm${isNum(wx.precip_prob) ? ` · ${Math.round(Number(wx.precip_prob) * (wx.precip_prob <= 1 ? 100 : 1))}%` : ""}`],
    ["Impact", isDome(g) ? "dome / closed (0)" : `${pct(v1.gs_fg_pct)} · away ${pct(v1.away_fg_pct)}`],
    ...(v2 ? [["Impact v2", `${pct(v2.gs_fg_pct)} · away ${pct(v2.away_fg_pct)}${isNum(v2.conf) ? ` · conf ${Number(v2.conf).toFixed(2)}` : ""}`]] : []),
    ["Volatility", `${esc(st.wind_vol_static || "—")}${isNum(wx.wind_vol_fc) ? ` · fc ${fmtNum(wx.wind_vol_fc, 1)} (P10 ${fmtNum(wx.wind_p10, 0)} / P90 ${fmtNum(wx.wind_p90, 0)})` : ""}`],
    ["Relative Wind", isNum(wx.wind_diff) ? `${Number(wx.wind_diff) >= 0 ? "+" : ""}${fmtNum(wx.wind_diff, 1)} vs avg ${fmtNum(st.avg_wind_month ?? st.avg_wind, 1)}` : "—"],
    ["Cross / Head", isNum(wx.cross_mph) || isNum(wx.head_mph) ? `${fmtNum(wx.cross_mph, 1)} / ${fmtNum(wx.head_mph, 1)} mph` : "—"],
    ["Home_t", fmtNum(g.home_temp, 0)],
    ["Away_t", fmtNum(g.away_temp, 0)],
    ["Year", yesno(st.year_built)],
    ["Source", `${esc(wx.source || "—")}${isNum(wx.lead_hours) ? ` · lead ${Math.round(Number(wx.lead_hours))}h` : ""}${wx.fetched_at ? ` · ${esc(fmtShortET(wx.fetched_at))}` : ""}`],
    ...backtestRows(g),
  ];
  return kv(rows);
}
// Record / ROI of the first-match backtest bucket (backtest.js) + the stadium's under record
function backtestRows(g) {
  if (typeof backtestHover !== "function") return [];
  const rows = backtestHover(g).map(([k, v]) => [k, esc(v)]);
  const sr = typeof stadiumResultFor === "function" ? stadiumResultFor(g) : null;
  if (sr) rows.push(["Stadium under", `${esc(sr.record)}${isNum(sr.pct) ? ` <span class="sub">(${fmtNum(sr.pct, 3)})</span>` : ""}`]);
  return rows;
}

function oddsTable(g) {
  const c = g.consensus || {}, f = g.fair || {};
  const books = ["consensus", ...BOOKS.filter((b) => (g.odds || {})[b])];
  const head = `<tr><th>Book</th><th>Spread open</th><th>Spread now</th><th>Total open</th><th>Total now</th><th>Edge</th></tr>`;
  const body = books.map((bk) => {
    if (bk === "consensus") {
      return `<tr><td>Consensus${c.ref_book ? ` <span class="sub">(${esc(c.ref_book)}, n=${c.n_books ?? "?"})</span>` : ""}</td>`
        + `<td>${fmtLine(c.spread_open)}</td><td title="spread = avg of ${esc(c.spread_src || "?")}">${fmtLine(c.spread_now)}${c.spread_src ? ` <span class="sub">${esc(c.spread_src)}</span>` : ""}${moveTag(c.spread_open, c.spread_now)}</td>`
        + `<td>${fmtTotal(c.total_open)}</td><td>${fmtTotal(c.total_now)}${moveTag(c.total_open, c.total_now)}</td><td></td></tr>`;
    }
    const o = g.odds[bk] || {}, s = o.spread || {}, t = o.total || {};
    const es = edgeAt(g, bk, "spread"), et = edgeAt(g, bk, "total");
    return `<tr><td>${esc(bookLabel(bk))}</td>`
      + `<td>${fmtLine(s.open_line)} <span class="sub">${fmtOdds(s.open_odds)}</span></td>`
      + `<td>${fmtLine(s.home_line)} <span class="sub">${fmtOdds(s.home_odds)}/${fmtOdds(s.away_odds)}</span>${moveTag(s.open_line, s.home_line)}</td>`
      + `<td>${fmtTotal(t.open_line)} <span class="sub">u${fmtOdds(t.open_under)}</span></td>`
      + `<td>${fmtTotal(t.line)} <span class="sub">o${fmtOdds(t.over)}/u${fmtOdds(t.under)}</span>${moveTag(t.open_line, t.line)}</td>`
      + `<td>${tierChip(es)}${tierChip(et)}</td></tr>`;
  }).join("");
  const fairRow = `<tr><td class="fair">Fair</td><td></td><td class="fair">${fmtLine(f.my_spread)}${isNum(f.fair_spread_v2) ? ` <span class="sub">v2 ${fmtLine(f.fair_spread_v2)}</span>` : ""}</td>`
    + `<td></td><td class="fair">${fmtTotal(f.my_total)}${isNum(f.fair_total_v2) ? ` <span class="sub">v2 ${fmtTotal(f.fair_total_v2)}</span>` : ""}</td>`
    + `<td>${f.best_total ? `T ${esc(bookLabel(f.best_total.book || ""))} ${esc(f.best_total.side || "")}${tierChip(f.best_total)}` : ""}${f.best_spread ? ` S ${esc(bookLabel(f.best_spread.book || ""))} ${esc(f.best_spread.side || "")}${tierChip(f.best_spread)}` : ""}</td></tr>`;
  return `<table class="kv">${head}${body}${fairRow}</table>`;
}

function gameInfoTable(g) {
  const st = g.stadium || {}, wx = g.weather || {};
  const rows = [
    ["Date", esc(g.date_label || fmtET(g.kickoff_utc, { fmt: { hour: undefined, minute: undefined }, tz: false }))],
    ["Time", `${esc(g.time_label || "")} ET${g.kickoff_local ? ` · local ${esc(String(g.kickoff_local).slice(11, 16))} ${esc(g.tz || "")}` : ""}`],
    ["Orientation", isNum(st.orient_deg) ? `${Math.round(Number(st.orient_deg))}° ${esc(st.orient || "")}` : esc(st.orient || "—")],
    ["Wind Impact", esc(st.wind_impact_static || "—")],
    ["Wind_dir", `${esc(wx.wind_dir_1h || "—")} / ${esc(wx.wind_dir_2h || "—")}${isNum(wx.wind_dir_deg) ? ` (${Math.round(Number(wx.wind_dir_deg))}°)` : ""}`],
    ["Weakest Wind", esc(st.weakest_wind_effect || "—")],
    ["Roof", `${esc(st.roof_type || "—")}${st.roof_state ? ` · ${esc(st.roof_state)}` : ""}`],
    ["Elevation", isNum(st.elevation_m) ? `${Math.round(Number(st.elevation_m))} m${isNum(g.travel_alt) ? ` · travel Δ ${Math.round(Number(g.travel_alt))} m` : ""}` : "—"],
    ["Game Location", `${esc(st.name || "")}${isNum(st.lat) ? ` <span class="sub">${Number(st.lat).toFixed(3)}, ${Number(st.lon).toFixed(3)}</span>` : ""}`],
    ["Status", `${esc(g.status || "scheduled")}${g.neutral ? " · neutral" : ""}`],
    ["Game ID", `<span class="sub">${esc(g.game_id)}</span>`],
  ];
  return kv(rows);
}

// ── hourly strip: uPlot (wind + ensemble P10–P90 band, gust, temp) over a compact table ──
function hourlyPoints(g) {
  const h = (g.weather && Array.isArray(g.weather.hourly)) ? g.weather.hourly : [];
  return h.filter((p) => p && parseTs(p.t)).map((p) => ({ ...p, ts: parseTs(p.t).getTime() / 1000 })).sort((a, b) => a.ts - b.ts);
}
function hourlyStrip(g) {
  const h = hourlyPoints(g);
  if (!h.length) return "";
  const hasBand = h.some((p) => isNum(p.p10) && isNum(p.p90));
  const head = `<tr><th></th>${h.map((p) => `<th>${esc(fmtET(p.t, { tz: false, fmt: { month: undefined, day: undefined } }))}</th>`).join("")}</tr>`;
  const line = (k, f) => `<tr><td>${esc(k)}</td>${h.map((p) => `<td>${f(p)}</td>`).join("")}</tr>`;
  return `<h3>Hourly (kickoff −1h … +4h)${hasBand ? ' <span class="sub">band = ensemble P10–P90</span>' : ""}</h3>`
    + `<div class="chart" id="hourly-chart"></div><span class="chart-note" id="hourly-note"></span>`
    + `<div style="overflow:auto"><table class="kv">${head}`
    + line("Temp", (p) => fmtNum(p.temp, 0))
    + line("Wind", (p) => `${fmtNum(p.wind, 0)}${isNum(p.p10) && isNum(p.p90) ? ` <span class="sub">${fmtNum(p.p10, 0)}–${fmtNum(p.p90, 0)}</span>` : ""}`)
    + line("Gust", (p) => fmtNum(p.gust, 0))
    + line("Dir", (p) => (isNum(p.dir) ? `${Math.round(Number(p.dir))}°` : "—"))
    + line("Precip", (p) => `${fmtNum(p.precip, 1)}${isNum(p.pop) ? ` <span class="sub">${Math.round(Number(p.pop) * (p.pop <= 1 ? 100 : 1))}%</span>` : ""}`)
    + `</table></div>`;
}
const AXIS_STYLE = { stroke: "#8b949e", grid: { stroke: "#242c38" } };
const num = (v) => (isNum(v) ? Number(v) : null);

function renderHourlyChart(g) {
  const host = document.getElementById("hourly-chart"), note = document.getElementById("hourly-note");
  if (!host || typeof uPlot === "undefined") return;
  const h = hourlyPoints(g);
  if (h.length < 2) { host.remove(); return; }
  const hasBand = h.some((p) => isNum(p.p10) && isNum(p.p90));
  const hasGust = h.some((p) => isNum(p.gust)), hasTemp = h.some((p) => isNum(p.temp));
  const data = [h.map((p) => p.ts), h.map((p) => num(p.wind))];
  const series = [
    { label: "time", value: (u, v) => (v == null ? "" : fmtET(v * 1000, { tz: false })) },
    { label: "wind", stroke: "#2f81f7", width: 2, value: (u, v) => (v == null ? "—" : `${v.toFixed(0)} mph`) },
  ];
  const bands = [];
  if (hasBand) {
    data.push(h.map((p) => num(p.p90)), h.map((p) => num(p.p10)));
    series.push({ label: "p90", stroke: "rgba(47,129,247,.35)", width: 1, value: (u, v) => (v == null ? "—" : v.toFixed(0)) });
    series.push({ label: "p10", stroke: "rgba(47,129,247,.35)", width: 1, value: (u, v) => (v == null ? "—" : v.toFixed(0)) });
    bands.push({ series: [series.length - 2, series.length - 1], fill: "rgba(47,129,247,.18)" });
  }
  if (hasGust) {
    data.push(h.map((p) => num(p.gust)));
    series.push({ label: "gust", stroke: "#f0883e", width: 1, dash: [4, 3], value: (u, v) => (v == null ? "—" : `${v.toFixed(0)} mph`) });
  }
  if (hasTemp) {
    data.push(h.map((p) => num(p.temp)));
    series.push({ label: "temp", stroke: "#e5484d", width: 1.5, scale: "f", value: (u, v) => (v == null ? "—" : `${v.toFixed(0)} °F`) });
  }
  const kickoff = parseTs(g.kickoff_utc);
  const kx = kickoff ? kickoff.getTime() / 1000 : null;
  const plot = new uPlot({
    width: Math.max(320, host.clientWidth || 640), height: 160, series, bands,
    scales: { x: { time: true }, y: { range: (u, min, max) => [0, Math.max(10, Math.ceil((max || 0) / 5) * 5 + 2)] }, f: {} },
    axes: [
      { ...AXIS_STYLE, values: (u, vals) => vals.map((v) => fmtET(v * 1000, { tz: false, fmt: { month: undefined, day: undefined } })) },
      { ...AXIS_STYLE, size: 44, label: "mph", labelSize: 12 },
      ...(hasTemp ? [{ ...AXIS_STYLE, scale: "f", side: 1, size: 44, grid: { show: false }, label: "°F", labelSize: 12 }] : []),
    ],
    cursor: { drag: { x: false, y: false } },
    hooks: { draw: [(u) => { if (kx != null) drawVLine(u, kx, "#c9a227", "kick"); }] },
  }, data, host);
  DRAWER.wxPlots.push(plot);
  if (note) note.textContent = `${h.length} h · ${hasBand ? "P10–P90 band · " : ""}${hasGust ? "gust dashed · " : ""}gold = kickoff`;
}
function drawVLine(u, x, color, label) {
  if (!isNum(x) || x < u.scales.x.min || x > u.scales.x.max) return;
  const ctx = u.ctx, dpr = window.devicePixelRatio || 1;
  const px = Math.round(u.valToPos(x, "x", true));
  ctx.save();
  ctx.strokeStyle = color; ctx.lineWidth = 1 * dpr; ctx.setLineDash([3 * dpr, 3 * dpr]);
  ctx.beginPath(); ctx.moveTo(px, u.bbox.top); ctx.lineTo(px, u.bbox.top + u.bbox.height); ctx.stroke();
  if (label) { ctx.fillStyle = color; ctx.font = `${10 * dpr}px sans-serif`; ctx.fillText(label, px + 3 * dpr, u.bbox.top + 10 * dpr); }
  ctx.restore();
}

// ── forecast drift: /api/wx (D1 weather_history) → fallback /data/wx_history.json ────────
// normalized point: {ts (s), lead, wind, gust, temp, precip, pop, gs, p10, p90}
async function fetchWxDrift(gameId) {
  if (DRAWER.wxCache[gameId]) return DRAWER.wxCache[gameId];
  let pts = [];
  try {
    const j = await fetchJson(`api/wx?game_id=${encodeURIComponent(gameId)}`);
    const rows = Array.isArray(j) ? j : (j && Array.isArray(j.rows) ? j.rows : []);
    pts = rows.map((r) => {
      const d = parseTs(r.fetched_at);
      return d ? { ts: d.getTime() / 1000, lead: num(r.lead_hours), wind: num(r.wind_mph), gust: num(r.gust_mph), temp: num(r.temp_f),
        precip: num(r.precip_mm), pop: num(r.precip_prob), gs: num(r.gs_fg), p10: num(r.wind_p10), p90: num(r.wind_p90) } : null;
    }).filter(Boolean);
  } catch (_) { pts = []; }
  if (!pts.length) {
    try {
      if (!DATA.wxHistory) DATA.wxHistory = await fetchJson("data/wx_history.json?t=" + Date.now());
      const hist = DATA.wxHistory || {};
      const series = (hist.series && typeof hist.series === "object") ? hist.series : hist;
      const s = series[gameId];
      // json_out: [[ts, lead_h, wind, gust, temp, precip, pop, gs_fg]]
      if (Array.isArray(s)) {
        pts = s.map((r) => {
          if (!Array.isArray(r)) return null;
          const d = parseTs(r[0]);
          return d ? { ts: d.getTime() / 1000, lead: num(r[1]), wind: num(r[2]), gust: num(r[3]), temp: num(r[4]),
            precip: num(r[5]), pop: num(r[6]), gs: num(r[7]), p10: null, p90: null } : null;
        }).filter(Boolean);
      }
    } catch (_) { /* no fallback */ }
  }
  pts.sort((a, b) => a.ts - b.ts);
  DRAWER.wxCache[gameId] = pts;
  return pts;
}

async function renderDriftChart(g) {
  const host = document.getElementById("drift-chart"), note = document.getElementById("drift-note");
  if (!host || !note) return;
  note.textContent = "loading…";
  const pts = await fetchWxDrift(g.game_id);
  if (DRAWER.game !== g || document.getElementById("drift-chart") !== host) return;
  if (pts.length < 2) { note.textContent = pts.length ? "1 snapshot so far — drift appears after the next run" : "no forecast history yet"; host.remove(); return; }
  if (typeof uPlot === "undefined") { note.textContent = "uPlot missing"; return; }
  const hasBand = pts.some((r) => r.p10 != null && r.p90 != null);
  const hasGs = pts.some((r) => r.gs != null);
  const data = [pts.map((r) => r.ts), pts.map((r) => r.wind)];
  const series = [
    { label: "fetched", value: (u, v) => (v == null ? "" : fmtShortET(v * 1000)) },
    { label: "wind", stroke: "#2f81f7", width: 1.5, value: (u, v) => (v == null ? "—" : `${v.toFixed(0)} mph`) },
  ];
  const bands = [];
  if (hasBand) {
    data.push(pts.map((r) => r.p90), pts.map((r) => r.p10));
    series.push({ label: "p90", stroke: "rgba(47,129,247,.3)", width: 1, value: (u, v) => (v == null ? "—" : v.toFixed(0)) });
    series.push({ label: "p10", stroke: "rgba(47,129,247,.3)", width: 1, value: (u, v) => (v == null ? "—" : v.toFixed(0)) });
    bands.push({ series: [series.length - 2, series.length - 1], fill: "rgba(47,129,247,.16)" });
  }
  data.push(pts.map((r) => r.temp));
  series.push({ label: "temp", stroke: "#e5484d", width: 1, scale: "f", value: (u, v) => (v == null ? "—" : `${v.toFixed(0)} °F`) });
  if (hasGs) {
    data.push(pts.map((r) => r.gs));
    series.push({ label: "gs %", stroke: "#c9a227", width: 1, dash: [3, 3], scale: "pct", value: (u, v) => (v == null ? "—" : `${v.toFixed(1)}%`) });
  }
  const plot = new uPlot({
    width: Math.max(320, host.clientWidth || 640), height: 110, series, bands,
    scales: { x: { time: true }, y: { range: (u, min, max) => [0, Math.max(10, Math.ceil((max || 0) / 5) * 5 + 2)] }, f: {}, pct: {} },
    axes: [
      { ...AXIS_STYLE, values: (u, vals) => vals.map((v) => fmtShortET(v * 1000)) },
      { ...AXIS_STYLE, size: 40 },
      { ...AXIS_STYLE, scale: "f", side: 1, size: 36, grid: { show: false } },
    ],
    legend: { show: true },
    cursor: { drag: { x: false, y: false } },
  }, data, host);
  DRAWER.wxPlots.push(plot);
  const first = pts[0], last = pts[pts.length - 1];
  const dW = first.wind != null && last.wind != null ? last.wind - first.wind : null;
  const lead = (r) => (r.lead != null ? `${Math.round(r.lead)}h` : "?");
  note.textContent = `${pts.length} snapshots · lead ${lead(first)} → ${lead(last)}`
    + (dW != null ? ` · wind ${dW >= 0 ? "+" : ""}${dW.toFixed(1)} mph since first forecast` : "")
    + (hasGs && first.gs != null && last.gs != null ? ` · gs ${first.gs.toFixed(1)}% → ${last.gs.toFixed(1)}%` : "");
}

// ── stadium compass card ──────────────────────────────────────────────────
const COMPASS_DEG = { N: 0, NNE: 22.5, NE: 45, ENE: 67.5, E: 90, ESE: 112.5, SE: 135, SSE: 157.5, S: 180,
  SSW: 202.5, SW: 225, WSW: 247.5, W: 270, WNW: 292.5, NW: 315, NNW: 337.5 };
// stadium.weakest_wind_effect → list of compass points where wind matters least (ARCH §7.5 dir_mult):
// 'x N' → every point except N; 'E/W' → {E, W}; 'all' → none; free text tokens otherwise
function weakDirs(text) {
  if (!text) return [];
  const t = String(text).trim();
  if (!t || /^all$/i.test(t) || /^none$/i.test(t)) return [];
  const toks = (s) => s.toUpperCase().split(/[\s,/&+]+/).filter((x) => COMPASS_DEG[x] != null);
  if (/^x\s+/i.test(t)) {
    const except = new Set(toks(t.slice(1)));
    return Object.keys(COMPASS_DEG).filter((k) => k.length <= 2 && !except.has(k));
  }
  return toks(t);
}
const polar = (deg, r) => { const a = (deg * Math.PI) / 180; return [r * Math.sin(a), -r * Math.cos(a)]; };
function wedgePath(deg, half, r0, r1) {
  const [x0, y0] = polar(deg - half, r0), [x1, y1] = polar(deg - half, r1);
  const [x2, y2] = polar(deg + half, r1), [x3, y3] = polar(deg + half, r0);
  return `M${x0.toFixed(1)},${y0.toFixed(1)} L${x1.toFixed(1)},${y1.toFixed(1)} A${r1},${r1} 0 0 1 ${x2.toFixed(1)},${y2.toFixed(1)} L${x3.toFixed(1)},${y3.toFixed(1)} A${r0},${r0} 0 0 0 ${x0.toFixed(1)},${y0.toFixed(1)} Z`;
}
function compassSvg(g) {
  const st = g.stadium || {}, wx = g.weather || {};
  const parts = [`<circle class="ring" r="50" />`];
  for (const [k, d] of Object.entries(COMPASS_DEG)) {
    if (k.length > 2) continue;
    const [x0, y0] = polar(d, 46), [x1, y1] = polar(d, 50);
    parts.push(`<line class="tick" x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" />`);
    if (k.length === 1) { const [lx, ly] = polar(d, 56); parts.push(`<text class="lbl" x="${lx.toFixed(1)}" y="${(ly + 3).toFixed(1)}">${k}</text>`); }
  }
  for (const k of weakDirs(st.weakest_wind_effect)) {
    parts.push(`<path class="weak" d="${wedgePath(COMPASS_DEG[k], k.length <= 2 ? 22.5 : 11.25, 40, 46)}"><title>weak wind effect from ${k}</title></path>`);
  }
  if (isNum(st.orient_deg)) {
    parts.push(`<rect class="field" x="-7" y="-34" width="14" height="68" rx="2" transform="rotate(${Number(st.orient_deg).toFixed(1)})"><title>field axis ${Math.round(Number(st.orient_deg))}°</title></rect>`);
  }
  if (isNum(wx.wind_dir_deg) && isNum(wx.wind_fg) && Number(wx.wind_fg) >= 1) {
    const len = Math.max(10, Math.min(38, Number(wx.wind_fg) * 1.2));
    // arrow starts at the rim on the "from" bearing and points across the field
    parts.push(`<g class="wind" transform="rotate(${(Number(wx.wind_dir_deg) + 180).toFixed(1)})">`
      + `<line x1="0" y1="${(48 - 2).toFixed(1)}" x2="0" y2="${(48 - len + 5).toFixed(1)}" />`
      + `<polygon points="0,${(48 - len).toFixed(1)} -4,${(48 - len + 7).toFixed(1)} 4,${(48 - len + 7).toFixed(1)}" />`
      + `<title>wind from ${Math.round(Number(wx.wind_dir_deg))}° · ${fmtNum(wx.wind_fg, 0)} mph</title></g>`);
  }
  return `<svg viewBox="-62 -62 124 124" role="img" aria-label="stadium compass">${parts.join("")}</svg>`;
}
function compassCard(g) {
  const st = g.stadium || {}, wx = g.weather || {};
  if (!g.stadium) return "";
  const climo = [];
  if (isNum(st.avg_wind_month)) climo.push(`${fmtNum(st.avg_wind_month, 1)} mph this month`);
  if (isNum(st.avg_wind)) climo.push(`${fmtNum(st.avg_wind, 1)} mph season`);
  if (isNum(g.home_temp)) climo.push(`${fmtNum(g.home_temp, 0)} °F home norm`);
  const rows = [
    ["Orientation", isNum(st.orient_deg) ? `${Math.round(Number(st.orient_deg))}° ${esc(st.orient || "")}${st.orient_src ? ` <span class="sub">(${esc(st.orient_src)})</span>` : ""}` : esc(st.orient || "—")],
    ["Weak wind", esc(st.weakest_wind_effect || "—")],
    ["Cross / Head", isNum(wx.cross_mph) || isNum(wx.head_mph) ? `${fmtNum(wx.cross_mph, 1)} / ${fmtNum(wx.head_mph, 1)} mph` : "—"],
    ["Roof", `${esc(st.roof_type || "—")}${st.roof_state ? ` · ${esc(st.roof_state)}` : ""}`],
    ["Surface", esc(st.surface || "—")],
    ["Elevation", isNum(st.elevation_m) ? `${Math.round(Number(st.elevation_m))} m${isNum(g.travel_alt) ? ` · travel Δ ${Math.round(Number(g.travel_alt))} m` : ""}` : "—"],
    ["Climatology", climo.length ? climo.join(" · ") : "—"],
    ["Built", yesno(st.year_built)],
  ];
  return `<div class="compass">${compassSvg(g)}${kv(rows)}</div>`;
}

function openDrawer(gameId) {
  const g = findGame(gameId);
  if (!g) return;
  DRAWER.game = g;
  STATE.game = gameId;
  writeHash();
  const d = document.getElementById("drawer");
  const c = g.consensus || {};
  const spreadHead = isNum(c.spread_now)
    ? ` · spread ${fmtLine(c.spread_now)}${c.spread_src ? ` (${esc(c.spread_src)})` : ""}${isNum(c.total_now) ? ` · total ${fmtTotal(c.total_now)}` : ""}`
    : "";
  document.getElementById("drawer-title").innerHTML = `${esc(gameLabel(g))} ${signalPill(g.signal)}`
    + `<span class="sub">${esc(kickoffLabel(g))} ET · ${esc((g.stadium && g.stadium.name) || "")} · ${esc(String(g.sport).toUpperCase())} wk ${esc(g.week)}${spreadHead}</span>`;
  const books = BOOKS.filter((b) => (g.odds || {})[b]);
  if (!books.includes(DRAWER.book)) DRAWER.book = "";
  if (DRAWER.plot) { DRAWER.plot.destroy(); DRAWER.plot = null; }
  destroyWxPlots();
  document.getElementById("drawer-body").innerHTML = `
    <div class="drawer-grid">
      <div><h3>Weather</h3>${weatherTable(g)}</div>
      <div><h3>Game Info</h3>${gameInfoTable(g)}</div>
    </div>
    ${g.stadium ? `<h3>Stadium</h3>${compassCard(g)}` : ""}
    <h3>Odds by book (open → now)</h3><div style="overflow:auto">${oddsTable(g)}</div>
    ${hourlyStrip(g)}
    <h3>Forecast drift <span class="sub">(each pipeline run, kickoff-window mean)</span></h3>
    <div class="chart small" id="drift-chart"></div><span class="chart-note" id="drift-note">loading…</span>
    <h3>Line history</h3>
    <div class="histctl">
      <select id="hist-market"><option value="total">Total</option><option value="spread">Spread (home)</option><option value="ml">Moneyline (home)</option></select>
      <select id="hist-book"><option value="">All books</option>${books.map((b) => `<option value="${esc(b)}">${esc(bookLabel(b))}</option>`).join("")}</select>
      <span class="chart-note" id="hist-note">loading…</span>
    </div>
    <div class="chart" id="hist-chart"></div>
    <h3>Alerts</h3><div id="drawer-alerts" class="sub">${(g.alerts || []).length ? "loading…" : "none sent for this game"}</div>`;
  d.hidden = false;
  document.getElementById("hist-market").value = DRAWER.market;
  document.getElementById("hist-book").value = DRAWER.book;
  document.getElementById("hist-market").addEventListener("change", (e) => { DRAWER.market = e.target.value; loadHistory(g); });
  document.getElementById("hist-book").addEventListener("change", (e) => { DRAWER.book = e.target.value; loadHistory(g); });
  renderHourlyChart(g);
  renderDriftChart(g);
  loadHistory(g);
  renderDrawerAlerts(g);
}

// ── per-game alerts timeline (alerts_feed.json via alerts.js) ─────────────
const FAMILY_COLORS = { edge: "#2ea043", move: "#f0883e", gone: "#e5484d", wx: "#2f81f7", openers: "#a371f7", ops: "#8b949e" };
const familyColor = (fam) => FAMILY_COLORS[fam] || "#c9a227";

async function gameAlerts(g) {
  if (typeof loadAlerts !== "function") return [];
  try { await loadAlerts(); } catch (_) { return []; }
  const feed = alertsForGame(g.game_id);
  const seen = new Set(feed.map((a) => a.key));
  // keys on the card that the feed does not carry yet (sent this run, feed lagging) -> bare rows
  const extra = (g.alerts || []).filter((k) => !seen.has(k)).map((k) => normalizeAlert({ alert_key: k, game_id: g.game_id }));
  return [...feed, ...extra];
}

async function renderDrawerAlerts(g) {
  const host = document.getElementById("drawer-alerts");
  if (!host) return;
  const list = await gameAlerts(g);
  if (DRAWER.game !== g || document.getElementById("drawer-alerts") !== host) return;
  if (!list.length) { host.textContent = "none sent for this game"; return; }
  const rows = list.map((a) => {
    const mkt = a.market || "";
    const fmtL = (v) => (mkt === "spread" ? fmtLine(v) : mkt === "ml" ? fmtOdds(v) : fmtTotal(v));
    const line = isNum(a.line_open) || isNum(a.line_now) ? openNow(a.line_open, a.line_now, fmtL) : "";
    const clv = isNum(a.clv_pts) ? ` · CLV <span class="mv ${a.clv_pts >= 0 ? "up" : "dn"}">${a.clv_pts >= 0 ? "+" : ""}${Number(a.clv_pts).toFixed(1)}</span>` : "";
    const close = isNum(a.closing_line) ? ` <span class="sub">close ${fmtL(a.closing_line)}</span>` : "";
    return `<tr title="${esc(a.key)}"><td><span class="fam" style="color:${familyColor(a.family)}">▲ ${esc(familyLabel(a.family))}</span></td>`
      + `<td>${a.sent_at ? esc(fmtShortET(a.sent_at)) : "—"}</td>`
      + `<td>${esc([a.side ? a.side.toUpperCase() : "", a.book ? bookLabel(a.book) : ""].filter(Boolean).join(" @ "))}</td>`
      + `<td>${line}${isNum(a.edge) ? ` <span class="sub">edge ${a.edge >= 0 ? "+" : ""}${Number(a.edge).toFixed(1)}</span>` : ""}${clv}${close}</td>`
      + `<td><span class="pill st-${esc(a.status)}">${esc(a.status)}</span></td></tr>`;
  }).join("");
  host.innerHTML = `<table class="kv alerts-tl">${rows}</table>` + clvTimelineHtml(list);
}

// ── CLV timeline: one bar per EDGE alert (sent line → closing line), in send order ──
// Bar length ∝ |clv_pts| (scaled to the largest in the game); green = beat the close.
function clvTimelineHtml(list) {
  const pts = list.filter((a) => a.family === "edge" && isNum(a.clv_pts))
    .sort((x, y) => String(x.first_sent_at || x.sent_at || "").localeCompare(String(y.first_sent_at || y.sent_at || "")));
  if (!pts.length) return "";
  const maxAbs = Math.max(0.5, ...pts.map((a) => Math.abs(Number(a.clv_pts))));
  const avg = pts.reduce((acc, a) => acc + Number(a.clv_pts), 0) / pts.length;
  const pos = pts.filter((a) => Number(a.clv_pts) > 0).length;
  const bars = pts.map((a) => {
    const v = Number(a.clv_pts);
    const w = Math.round((Math.abs(v) / maxAbs) * 50);
    const mkt = a.market || "";
    const fmtL = (x) => (mkt === "spread" ? fmtLine(x) : fmtTotal(x));
    const label = `${a.side ? a.side.toUpperCase() : ""} ${fmtL(a.line_open)} → ${isNum(a.closing_line) ? fmtL(a.closing_line) : "?"} @ ${a.book ? bookLabel(a.book) : "?"}`;
    return `<div class="clv-row" title="${esc(a.key)}">`
      + `<span class="clv-t">${esc(fmtShortET(a.first_sent_at || a.sent_at))}</span>`
      + `<span class="clv-l">${esc(label.trim())}</span>`
      + `<span class="clv-bar"><i class="neg" style="width:${v < 0 ? w : 0}%"></i><i class="pos" style="width:${v > 0 ? w : 0}%"></i></span>`
      + `<span class="clv-v mv ${v >= 0 ? "up" : "dn"}">${v >= 0 ? "+" : ""}${v.toFixed(1)}</span></div>`;
  }).join("");
  return `<h3>CLV timeline <span class="sub">${pts.length} edge alert${pts.length === 1 ? "" : "s"} · avg ${avg >= 0 ? "+" : ""}${avg.toFixed(2)} · +CLV ${pos}/${pts.length}</span></h3>`
    + `<div class="clv-tl">${bars}</div>`;
}

// uPlot draw hook: one vertical marker per alert sent for this game/market (▲ at the top,
// colored by family; hover tooltip via the chart note lists them).
function alertMarkersFor(g) {
  if (typeof alertsForGame !== "function") return [];
  return alertsForGame(g.game_id)
    .filter((a) => a.sent_at && (!a.market || a.market === DRAWER.market) && (!DRAWER.book || !a.book || a.book === DRAWER.book))
    .map((a) => {
      const ts = parseTs(a.sent_at);
      return ts ? { x: ts.getTime() / 1000, family: a.family, label: `${familyLabel(a.family)} ${a.book ? bookLabel(a.book) : ""} ${fmtShortET(a.sent_at)}`.trim(), y: a.line_open } : null;
    })
    .filter(Boolean);
}
function drawAlertMarkers(u, markers) {
  if (!markers.length) return;
  const ctx = u.ctx;
  const dpr = window.devicePixelRatio || 1;
  const { left, top, height } = u.bbox;
  const xmin = u.scales.x.min, xmax = u.scales.x.max;
  ctx.save();
  for (const m of markers) {
    if (!isNum(m.x) || m.x < xmin || m.x > xmax) continue;
    const x = Math.round(u.valToPos(m.x, "x", true));
    const color = familyColor(m.family);
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.lineWidth = 1 * dpr;
    ctx.setLineDash([3 * dpr, 3 * dpr]);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
    ctx.setLineDash([]);
    const s = 5 * dpr;
    ctx.beginPath(); ctx.moveTo(x, top + 1); ctx.lineTo(x - s, top + 1 + 2 * s); ctx.lineTo(x + s, top + 1 + 2 * s); ctx.closePath(); ctx.fill();
    if (isNum(m.y) && u.scales.y && m.y >= u.scales.y.min && m.y <= u.scales.y.max) {
      const y = Math.round(u.valToPos(m.y, "y", true));
      ctx.beginPath(); ctx.arc(x, y, 3 * dpr, 0, Math.PI * 2); ctx.fill();
    }
  }
  ctx.restore();
  void left;
}

// ── history: /api/history → rows; fallback /data/history.json ─────────────
async function fetchHistoryRows(gameId, market) {
  const key = `${gameId}|${market}`;
  if (DRAWER.histCache[key]) return DRAWER.histCache[key];
  let rows = [];
  try {
    const j = await fetchJson(`api/history?game_id=${encodeURIComponent(gameId)}&market=${encodeURIComponent(market)}`);
    rows = Array.isArray(j) ? j : (j && Array.isArray(j.rows) ? j.rows : (j && Array.isArray(j.results) ? j.results : []));
  } catch (_) {
    rows = [];
  }
  if (!rows.length) {
    try {
      if (!DATA.history) DATA.history = await fetchJson("data/history.json?t=" + Date.now());
      const hist = DATA.history || {};
      // json_out.write_board: {schema_version, run_id, series: {key: [[ts, line, odds]]}, fair_series: {key: [[ts, fair_line]]}}
      const series = (hist.series && typeof hist.series === "object") ? hist.series : hist;
      const fairSeries = (hist.fair_series && typeof hist.fair_series === "object") ? hist.fair_series : {};
      const fairAt = (gid, mkt, side, ts) => {
        const fs = fairSeries[`${gid}|${mkt}|${side}`];
        if (!Array.isArray(fs)) return null;
        let v = null;
        for (const [fts, fl] of fs) { if (String(fts) <= String(ts)) v = fl; else break; }
        return v;
      };
      for (const [k, s] of Object.entries(series)) {
        if (!Array.isArray(s)) continue;
        const [gid, mkt, side, book] = k.split("|");
        if (gid !== gameId || mkt !== market) continue;
        for (const [ts, line, odds] of s) rows.push({ scraped_at: ts, game_id: gid, book, market: mkt, side, line, odds, fair_line: fairAt(gid, mkt, side, ts) });
      }
    } catch (_) { /* no fallback available */ }
  }
  DRAWER.histCache[key] = rows;
  return rows;
}

function historySeries(rows, market, bookFilter) {
  // one series per book, main side only: total→over line, spread→home line, ml→home odds
  const side = market === "total" ? "over" : "home";
  const byBook = {};
  for (const r of rows) {
    if (r.side && r.side !== side) continue;
    if (bookFilter && r.book !== bookFilter) continue;
    if (r.is_main === 0 || r.is_main === false) continue;
    const t = parseTs(r.scraped_at);
    const v = market === "ml" ? r.odds : r.line;
    if (!t || !isNum(v)) continue;
    (byBook[r.book] = byBook[r.book] || []).push([t.getTime() / 1000, Number(v), isNum(r.fair_line) ? Number(r.fair_line) : null]);
  }
  for (const s of Object.values(byBook)) s.sort((a, b) => a[0] - b[0]);
  return byBook;
}

const SERIES_COLORS = ["#2f81f7", "#f0883e", "#a371f7", "#7ee2a8", "#e5484d", "#d0d7de", "#c9a227", "#8b4513"];

async function loadHistory(g) {
  const note = document.getElementById("hist-note");
  const host = document.getElementById("hist-chart");
  if (!note || !host) return;
  note.textContent = "loading…";
  const rows = await fetchHistoryRows(g.game_id, DRAWER.market);
  if (DRAWER.game !== g || document.getElementById("hist-chart") !== host) return;
  const byBook = historySeries(rows, DRAWER.market, DRAWER.book);
  const books = Object.keys(byBook);
  if (DRAWER.plot) { DRAWER.plot.destroy(); DRAWER.plot = null; }
  host.innerHTML = "";
  if (!books.length) { note.textContent = "no history yet"; return; }
  if (typeof uPlot === "undefined") { note.textContent = "uPlot missing (vendor/uPlot.iife.min.js)"; return; }

  // union of timestamps → step-aligned matrix (carry last value forward per book)
  const ts = [...new Set(books.flatMap((b) => byBook[b].map((p) => p[0])))].sort((a, b) => a - b);
  const data = [ts];
  const fairPts = new Map();
  for (const b of books) {
    const pts = byBook[b];
    let i = 0, last = null;
    data.push(ts.map((t) => {
      while (i < pts.length && pts[i][0] <= t) { last = pts[i][1]; if (pts[i][2] != null) fairPts.set(t, pts[i][2]); i++; }
      return last;
    }));
  }
  const hasFair = fairPts.size > 0;
  if (hasFair) {
    let lastFair = null;
    data.push(ts.map((t) => { if (fairPts.has(t)) lastFair = fairPts.get(t); return lastFair; }));
  }
  const series = [
    { label: "time", value: (u, v) => (v == null ? "" : fmtShortET(v * 1000)) },
    ...books.map((b, i) => ({ label: bookLabel(b), stroke: SERIES_COLORS[i % SERIES_COLORS.length], width: 1.5, paths: uPlot.paths.stepped({ align: 1 }),
      value: (u, v) => (v == null ? "—" : DRAWER.market === "ml" ? fmtOdds(v) : DRAWER.market === "total" ? fmtTotal(v) : fmtLine(v)) })),
    ...(hasFair ? [{ label: "fair", stroke: "#c9a227", width: 1, dash: [4, 4], value: (u, v) => (v == null ? "—" : fmtTotal(v)) }] : []),
  ];
  const width = Math.max(320, host.clientWidth || 640);
  if (typeof loadAlerts === "function") await loadAlerts().catch(() => {});
  if (DRAWER.game !== g || document.getElementById("hist-chart") !== host) return;
  const markers = alertMarkersFor(g);
  DRAWER.plot = new uPlot({
    width, height: 220, series,
    axes: [
      { stroke: "#8b949e", grid: { stroke: "#242c38" }, values: (u, vals) => vals.map((v) => fmtShortET(v * 1000)) },
      { stroke: "#8b949e", grid: { stroke: "#242c38" }, size: 56 },
    ],
    scales: { x: { time: true } },
    cursor: { drag: { x: false, y: false } },
    hooks: { draw: [(u) => drawAlertMarkers(u, markers)] },
  }, data, host);
  note.textContent = `${rows.length} rows · ${books.length} book${books.length === 1 ? "" : "s"}${hasFair ? " · fair dashed" : ""}`
    + (markers.length ? ` · ${markers.length} alert${markers.length === 1 ? "" : "s"} ▲` : "");
  note.title = markers.map((m) => m.label).join("\n");
}
