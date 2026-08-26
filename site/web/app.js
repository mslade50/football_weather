"use strict";
/*
 * Football Weather board — app shell (fetch / poll / sort / filter / hover / auth / refresh).
 * Adapted from golf_scraping/board/web/app.js. Views live in table.js / map.js / drawer.js /
 * status.js; this file owns STATE, DATA and the boot sequence.
 *
 * EXPECTED JSON SHAPES (pipeline/outputs/json_out.py, ARCHITECTURE §5 / §11):
 *
 * /data/meta.json
 *   { run_id, last_updated, season, week, sport_counts {nfl, cfb}, git_sha, model_version,
 *     next_run_eta, degradations [{component, reason, severity, run_id, ts}],
 *     books { <book>: { count, baseline, status: "green"|"amber"|"red", last_ok } },
 *     weeks? { nfl: [..], cfb: [..] } }
 *
 * /data/games_nfl.json, /data/games_cfb.json  → [GameCard] (or {meta, games:[GameCard]})
 *   GameCard = {
 *     game_id, sport, season, week, kickoff_utc, kickoff_local, tz, date_label, time_label,
 *     home {team_id, name, short}, away {team_id, name, short}, neutral, status,
 *     stadium {stadium_id, name, lat, lon, orient_deg, orient, roof_type, roof_state, elevation_m,
 *              year_built, wind_vol_static, wind_impact_static, weakest_wind_effect, avg_wind, avg_wind_month},
 *     travel_alt, home_temp, away_temp,
 *     weather {temp_fg, wind_fg, gust_fg, wind_dir_1h, wind_dir_2h, wind_dir_fg, wind_dir_deg, rain_fg,
 *              precip_prob, wind_vol_fc, wind_p10, wind_p90, wind_diff, cross_mph, head_mph, source,
 *              lead_hours, fetched_at, hourly [{t, temp, wind, gust, dir, precip, pop, p10, p90}]},
 *     impact {v1 {gs_fg_pct, away_fg_pct, components {...}}, v2 {...}, model_version},
 *     signal {label, level, color, size, flags [..], dow_base},
 *     odds { <book>: { spread {home_line, home_odds, away_odds, open_line, open_odds, updated_at},
 *                      total  {line, over, under, open_line, open_under, updated_at},
 *                      ml     {home, away, open_home, open_away} } },
 *     consensus {spread_open, spread_now, total_open, total_now, move_s, move_t, ref_book, n_books, thin},
 *     fair {my_total, my_spread, fair_total_v2, fair_spread_v2, edges [Edge], best_total, best_spread},
 *     alerts [alert_key], run_id }
 *   Edge = {game_id, book, market, side, line, odds, fair_line, fair_prob, vigfree_prob, edge_pts,
 *           edge_prob, confidence, tier "strong"|"edge"|"watch"|"none", model_version, ref_book, n_books}
 *
 * /api/history?game_id=&market=&book=   → D1 odds_history rows {ok, game_id, rows:[..]}:
 *   {scraped_at, game_id, book, market, side, line, odds, prob, fair_line, fair_prob, edge_pts, edge_prob, is_main, run_id}
 *   Fallback: /data/history.json  { schema_version, run_id, series: { "<game_id>|<market>|<side>|<book>": [[ts, line, odds], ...] },
 *                                    fair_series: { "<game_id>|<market>|<side>": [[ts, fair_line], ...] } }
 * /api/wx?game_id=  → {ok, game_id, rows:[..]} weather_history rows {fetched_at, lead_hours, temp_f, wind_mph, gust_mph, precip_mm, precip_prob, gs_fg}
 * /data/alerts_feed.json → last 200 sent alerts (alerts.js): array or {alerts:[..]} of
 *   {alert_key, family, sport, season, week, game_id, market, side, book, tier, line_open, line_now, odds,
 *    fair, edge_pts, clv_pts, closing_line, sent_at, first_sent_at, sends, status, text_html, run_id}
 *   (D1 `alerts` column names first_line/last_line/first_edge/last_edge/last_sent_at also accepted);
 *   fallback /api/alerts → {ok, rows}.
 * /data/backtest.json → Backtest tab (backtest.js): {run_id, generated_at, grid [bucket rows], stadium_results [..],
 *   games [matched games], clv {weeks, by_tier, by_league, by_book, by_model {v1, v2}}}; backtestHover(g) feeds the
 *   Record / ROI lines on the map popup, table hover and drawer via first-match bucket lookup.
 * /data/status.json → Status tab (status.js): {run_id, last_updated, season, week, git_sha, model_version,
 *   next_run_eta, stage_timings {stage: s}, books {..}, degradations [..], unresolved_names [..],
 *   heartbeat {ts, ..}, runs [last 20 D1 runs rows]}; fallback /api/status.
 * /auth/me → {role: "admin"|"viewer"} (also tolerates golf's {can_refresh_all}).
 */

const DATA = { meta: {}, games: { nfl: [], cfb: [] }, history: null };
const STATE = {
  view: "table", sport: "nfl", week: null, sort: null, dir: -1, q: "",
  signal: "", book: "", minEdge: null, showDomes: true, showWatch: true, game: null,
  preset: null,   // Signals preset id (signals.js PRESETS) — filters Table + maps while set
};
let BOOKS = [];
let LAST_UPDATED = null;
let HOVER = {};
let HK = 0;

const BOOK_LABELS = {
  pinnacle: "Pinnacle", fanduel: "FanDuel", draftkings: "DraftKings", betonline: "BetOnline",
  betcris: "Betcris", novig: "NoVig", prophetx: "ProphetX", kalshi: "Kalshi", consensus: "Consensus",
};
const bookLabel = (b) => BOOK_LABELS[b] || b;

const TIER_COLORS = {
  "No": "#2ea043", "Low (Wind)": "#2f81f7", "Low": "#2f81f7", "Low (Rain)": "#d0d7de",
  "Low (Temp)": "#e5484d", "Mid": "#f0883e", "High": "#a371f7", "Very High": "#8b0000",
};
const FLAG_COLORS = { "CFB Wind": "#a371f7", "NFL Wind": "#2f81f7", "Heat": "#e5484d", "Alt+Heat": "#8b4513" };
const CSS_COLOR = { green: "#2ea043", blue: "#2f81f7", black: "#d0d7de", red: "#e5484d", orange: "#f0883e",
  purple: "#a371f7", darkred: "#8b0000", saddlebrown: "#8b4513" };

function signalColor(sig) {
  if (!sig) return TIER_COLORS.No;
  if (sig.color && CSS_COLOR[sig.color]) return CSS_COLOR[sig.color];
  if (sig.color && /^#/.test(sig.color)) return sig.color;
  return TIER_COLORS[signalLabel(sig)] || TIER_COLORS.No;
}
// pipeline/model/signals.py labels are "No Impact" / "Low Impact" / "Low (Wind|Rain|Temp)" /
// "Mid Impact" / "High Impact" / "Very High Impact"; the UI uses the short form.
function signalLabel(sig) {
  const l = (sig && sig.label) || "No";
  return String(l).replace(/\s*Impact$/i, "") || "No";
}
function signalTier(sig) {
  const l = signalLabel(sig);
  return l.startsWith("Low") ? "Low" : l;
}

const isNum = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
const fmtNum = (v, nd = 1) => (isNum(v) ? Number(v).toFixed(nd) : "—");
const fmtOdds = (o) => (o === null || o === undefined || o === 0 || !isNum(o)) ? "—" : (o > 0 ? "+" + o : "" + o);
const fmtLine = (l) => {
  if (!isNum(l)) return "—";
  const n = Number(l);
  return (n > 0 ? "+" : "") + (Number.isInteger(n) ? n.toFixed(0) : n.toFixed(1));
};
const fmtTotal = (l) => (isNum(l) ? (Number.isInteger(Number(l)) ? Number(l).toFixed(0) : Number(l).toFixed(1)) : "—");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function parseTs(s) {
  if (!s) return null;
  if (typeof s === "number") return new Date(s < 1e12 ? s * 1000 : s);
  let str = String(s).trim().replace(" UTC", "").replace(" ", "T");
  if (!/[Zz]$|[+-]\d\d:?\d\d$/.test(str)) str += "Z";   // naive timestamps are UTC
  const d = new Date(str);
  return isNaN(d) ? null : d;
}
function fmtET(s, opts = {}) {
  const d = parseTs(s);
  if (!d) return s || "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", month: "numeric", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: opts.tz === false ? undefined : "short", ...opts.fmt,
  }).format(d);
}
function fmtLocal(s) {
  const d = parseTs(s);
  if (!d) return "";
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: "short" }).format(d);
}
// "7/9 4:28p" (ET) for compact hover / history lists
function fmtShortET(s) {
  const d = parseTs(s);
  if (!d) return s || "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true,
  }).format(d).replace(", ", " ").replace(/\s?AM/, "a").replace(/\s?PM/, "p");
}
// Kickoff label: prefer pipeline date_label/time_label (ET), else derive from kickoff_utc.
function kickoffLabel(g) {
  if (g.date_label || g.time_label) return `${g.date_label || ""} ${g.time_label || ""}`.trim();
  return fmtET(g.kickoff_utc, { tz: false });
}
function gameLabel(g) {
  const a = (g.away && (g.away.short || g.away.name)) || g.away_id || "?";
  const h = (g.home && (g.home.short || g.home.name)) || g.home_id || "?";
  return `${a} @ ${h}`;
}
function isDome(g) {
  const rs = (g.stadium && g.stadium.roof_state) || g.roof_state;
  if (rs) return rs === "dome" || rs === "closed";
  return g.stadium && g.stadium.roof_type === "dome";
}
function edgesOf(g) {
  const es = (g.fair && g.fair.edges) || [];
  return es.filter((e) => STATE.showWatch || (e.tier !== "watch" && e.tier !== "none"));
}
function bestEdge(g, market = null, book = null) {
  let best = null;
  for (const e of edgesOf(g)) {
    if (market && e.market !== market) continue;
    if (book && e.book !== book) continue;
    if (!isNum(e.edge_pts)) continue;
    if (!best || Math.abs(e.edge_pts) > Math.abs(best.edge_pts)) best = e;
  }
  return best;
}
function edgeAt(g, book, market) {
  return edgesOf(g).find((e) => e.book === book && e.market === market) || null;
}

// ── game list for the current sport/week + filters ────────────────────────
function currentGames() {
  let rows = (DATA.games[STATE.sport] || []).slice();
  if (STATE.week != null) rows = rows.filter((g) => String(g.week) === String(STATE.week));
  if (!STATE.showDomes) rows = rows.filter((g) => !isDome(g));
  if (STATE.signal) rows = rows.filter((g) => signalTier(g.signal) === STATE.signal);
  if (STATE.preset && typeof activePreset === "function" && activePreset()) {
    const flag = activePreset().flag;
    rows = rows.filter((g) => hasFlag(g, flag));
  }
  if (STATE.q) {
    rows = rows.filter((g) => [gameLabel(g), g.home && g.home.name, g.away && g.away.name,
      g.stadium && g.stadium.name].filter(Boolean).join(" ").toLowerCase().includes(STATE.q));
  }
  if (STATE.minEdge != null) {
    rows = rows.filter((g) => {
      const be = bestEdge(g, null, STATE.book || null);
      return be && Math.abs(be.edge_pts) >= STATE.minEdge;
    });
  }
  return rows;
}
function gamesForSport(sport) {
  return (DATA.games[sport] || []).filter((g) => STATE.week == null || String(g.week) === String(STATE.week)
    || sport !== STATE.sport);
}
function findGame(id) {
  for (const s of ["nfl", "cfb"]) {
    const g = (DATA.games[s] || []).find((x) => x.game_id === id);
    if (g) return g;
  }
  return null;
}
function booksInData() {
  const seen = new Set(BOOKS);
  for (const s of ["nfl", "cfb"]) for (const g of DATA.games[s] || []) for (const b of Object.keys(g.odds || {})) seen.add(b);
  seen.delete("consensus");
  return [...seen];
}

// ── hash state ────────────────────────────────────────────────────────────
function readHash() {
  const h = location.hash.replace(/^#/, "");
  const params = new URLSearchParams(h);
  if (params.get("sport") === "nfl" || params.get("sport") === "cfb") STATE.sport = params.get("sport");
  if (params.get("view")) STATE.view = params.get("view");
  if (params.get("week")) STATE.week = params.get("week");
  if (params.get("game")) STATE.game = params.get("game");
  if (params.get("signal")) STATE.signal = params.get("signal");
  if (params.get("book")) STATE.book = params.get("book");
  if (params.get("minEdge")) STATE.minEdge = parseFloat(params.get("minEdge"));
  STATE.preset = params.get("preset") || null;
}
function writeHash() {
  const params = new URLSearchParams();
  params.set("sport", STATE.sport); params.set("view", STATE.view);
  if (STATE.week != null) params.set("week", STATE.week);
  if (STATE.game) params.set("game", STATE.game);
  if (STATE.signal) params.set("signal", STATE.signal);
  if (STATE.book) params.set("book", STATE.book);
  if (STATE.minEdge != null) params.set("minEdge", STATE.minEdge);
  if (STATE.preset) params.set("preset", STATE.preset);
  const next = "#" + params.toString();
  if (location.hash !== next) history.replaceState(null, "", next);
}

// ── render dispatch ───────────────────────────────────────────────────────
function render() {
  HOVER = {}; HK = 0;
  document.querySelectorAll(".tab").forEach((t) => {
    const active = t.dataset.view === STATE.view && (t.dataset.view !== "map" || t.dataset.sport === STATE.sport);
    t.classList.toggle("active", active);
  });
  document.getElementById("sport").value = STATE.sport;
  const view = STATE.view;
  const isMap = view === "map", isAlerts = view === "alerts", isStatus = view === "status", isSignals = view === "signals";
  const isBacktest = view === "backtest";
  const isGames = !isAlerts && !isStatus && !isBacktest;
  document.getElementById("tablewrap").style.display = isGames && !isMap ? "" : "none";
  document.getElementById("mapwrap").style.display = isMap ? "" : "none";
  document.getElementById("signalsbar").style.display = isSignals ? "" : "none";
  document.getElementById("alertswrap").style.display = isAlerts ? "" : "none";
  const btWrap = document.getElementById("backtestwrap");
  if (btWrap) btWrap.style.display = isBacktest ? "" : "none";
  document.getElementById("statuswrap").style.display = isStatus ? "" : "none";
  const ctl = document.querySelector(".controls:not(.alertctl)");
  if (ctl) ctl.style.display = isGames ? "" : "none";
  const pc = document.getElementById("presetchip");
  if (pc) {
    const preset = typeof activePreset === "function" ? activePreset() : null;
    pc.style.display = preset && !isSignals ? "" : "none";
    if (preset) pc.innerHTML = `<span class="dot" style="background:${FLAG_COLORS[preset.flag] || "#8b949e"}"></span>${esc(preset.label)} ✕`;
  }
  writeHash();
  if (isAlerts) { renderAlerts(); return; }
  if (isBacktest) { renderBacktest(); return; }
  if (isStatus) { renderStatus(); return; }
  if (isSignals) { renderSignals(); return; }
  const rows = currentGames();
  document.getElementById("rowcount").textContent = rows.length ? `${rows.length} games` : "";
  if (isMap) renderMap(rows); else renderTable(rows);
}
function switchView(view, sport) {
  STATE.view = view;
  if (sport) STATE.sport = sport;
  STATE.sort = null;
  populateWeeks();
  render();
}
function setSport(sport) {
  STATE.sport = sport; STATE.week = null; STATE.sort = null;
  populateWeeks();
  render();
}
function populateWeeks() {
  const sel = document.getElementById("week");
  const weeks = [...new Set((DATA.games[STATE.sport] || []).map((g) => g.week).filter((w) => w != null))].sort((a, b) => a - b);
  const metaWeek = (DATA.meta.weeks && DATA.meta.weeks[STATE.sport]) || null;
  if (STATE.week == null || !weeks.map(String).includes(String(STATE.week))) {
    STATE.week = weeks.length ? String(weeks.includes(DATA.meta.week) ? DATA.meta.week : weeks[0]) : null;
  }
  sel.innerHTML = weeks.map((w) => `<option value="${w}">Week ${w}</option>`).join("") || `<option value="">—</option>`;
  if (STATE.week != null) sel.value = String(STATE.week);
  sel.style.display = weeks.length > 1 ? "" : "none";
  void metaWeek;
}
function populateBooks() {
  const sel = document.getElementById("book");
  sel.innerHTML = `<option value="">Book: all</option>` + BOOKS.map((b) => `<option value="${b}">${esc(bookLabel(b))}</option>`).join("");
  sel.value = BOOKS.includes(STATE.book) ? STATE.book : "";
}

// ── hover card ────────────────────────────────────────────────────────────
function hoverHtml(d) {
  let out = "";
  if (d.label) out += `<div class="hc-h">${esc(d.label)}</div>`;
  if (d.lines && d.lines.length) {
    out += `<div class="hc-list">` + d.lines.map(([t, o]) =>
      `<div class="hc-li"><span class="t">${esc(t)}</span><span class="o">${o}</span></div>`).join("") + `</div>`;
  }
  if (d.more) out += `<div class="hc-more">${esc(d.more)}</div>`;
  return out;
}
function setupHover() {
  const card = document.getElementById("hovercard");
  const tbody = document.querySelector("#table tbody");
  tbody.addEventListener("mouseover", (e) => {
    const td = e.target.closest("td[data-hk]");
    if (!td) return;
    const d = HOVER[td.dataset.hk];
    if (!d) return;
    card.innerHTML = hoverHtml(d);
    card.style.display = "block";
    const r = td.getBoundingClientRect();
    const cw = card.offsetWidth || 150, ch = card.offsetHeight || 60;
    card.style.left = Math.max(6, Math.min(window.innerWidth - cw - 8, r.left)) + "px";
    card.style.top = (r.bottom + 6 + ch > window.innerHeight && r.top - ch - 6 > 0 ? r.top - ch - 6 : r.bottom + 6) + "px";
  });
  tbody.addEventListener("mouseout", (e) => {
    if (e.target.closest("td[data-hk]")) card.style.display = "none";
  });
}

// ── fetch helpers ─────────────────────────────────────────────────────────
async function fetchJson(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
  return r.json();
}
function normalizeGames(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.games)) return payload.games;
  return [];
}

// ── refresh (admin) + poll ────────────────────────────────────────────────
function setupRefresh(auth) {
  const msg = document.getElementById("refreshmsg");
  const isAdmin = !!(auth && (auth.role === "admin" || auth.can_refresh_all));
  document.querySelectorAll("[data-admin-refresh]").forEach((btn) => { btn.hidden = !isAdmin; });
  if (!isAdmin) return;
  const buttons = [
    { btn: document.getElementById("lightrefreshbtn"), scope: "light", note: "Re-scraping API books… new data in ~1–2 min" },
    { btn: document.getElementById("refreshbtn"), scope: "full", note: "Full run… new data in ~3–5 min" },
  ].filter((b) => b.btn);
  const setDisabled = (v) => buttons.forEach(({ btn }) => { btn.disabled = v; });
  buttons.forEach(({ btn, scope, note }) => {
    btn.addEventListener("click", async () => {
      setDisabled(true);
      const baseline = LAST_UPDATED;
      msg.textContent = "Triggering…";
      try {
        const r = await fetch("refresh", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ sport: STATE.sport, scope }) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || !j.ok) throw new Error(j.error || ("HTTP " + r.status));
        msg.textContent = j.already_running ? "A run is already in progress — waiting for new data…" : note;
        pollForNewData(baseline, () => setDisabled(false), msg);
      } catch (e) {
        msg.textContent = "Refresh failed: " + e.message;
        setDisabled(false);
        setTimeout(() => { if (msg.textContent.startsWith("Refresh failed")) msg.textContent = ""; }, 9000);
      }
    });
  });
}
function pollForNewData(baseline, reenable, msg) {
  let tries = 0;
  const MAX = 28;
  const iv = setInterval(async () => {
    tries++;
    try {
      const m = await fetchJson("data/meta.json?t=" + Date.now());
      if (m && m.last_updated && m.last_updated !== baseline) {
        clearInterval(iv);
        msg.textContent = "New data ready — reloading…";
        setTimeout(() => location.reload(), 700);
        return;
      }
    } catch (_) { /* transient */ }
    if (tries >= MAX) {
      clearInterval(iv);
      msg.textContent = "Still working (or nothing new) — reload manually later.";
      reenable();
      setTimeout(() => { msg.textContent = ""; }, 12000);
    }
  }, 15000);
}
// Background poll: every 5 min check meta.json; if a new run landed, reload quietly.
function startMetaPoll() {
  setInterval(async () => {
    try {
      const m = await fetchJson("data/meta.json?t=" + Date.now());
      // reload only when a newer run landed and the user isn't reading a drawer
      if (m && m.last_updated && LAST_UPDATED && m.last_updated !== LAST_UPDATED && document.getElementById("drawer").hidden) {
        location.reload();
      }
    } catch (_) { /* ignore */ }
  }, 5 * 60 * 1000);
}

// ── boot ──────────────────────────────────────────────────────────────────
async function boot() {
  readHash();
  const bust = "?t=" + Date.now();
  const [meta, nfl, cfb, auth] = await Promise.all([
    fetchJson(`data/meta.json${bust}`).catch(() => ({})),
    fetchJson(`data/games_nfl.json${bust}`).catch(() => []),
    fetchJson(`data/games_cfb.json${bust}`).catch(() => []),
    fetch(`auth/me${bust}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  DATA.meta = meta || {};
  DATA.games.nfl = normalizeGames(nfl);
  DATA.games.cfb = normalizeGames(cfb);
  // No sport in the URL and the default sport has no games on the board (NFL before its
  // 10-day window opens, CFB in January): open on the sport that does.
  if (!/(^|[#?&])sport=/.test(location.hash) && !(DATA.games[STATE.sport] || []).length) {
    const alt = STATE.sport === "nfl" ? "cfb" : "nfl";
    if ((DATA.games[alt] || []).length) STATE.sport = alt;
  }
  LAST_UPDATED = DATA.meta.last_updated || null;
  BOOKS = Object.keys(DATA.meta.books || {});
  BOOKS = booksInData();

  renderHeader(DATA.meta);
  renderBanners(DATA.meta);
  renderStatusbar(DATA.meta);
  populateWeeks();
  populateBooks();

  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view, t.dataset.sport)));
  document.getElementById("sport").addEventListener("change", (e) => setSport(e.target.value));
  document.getElementById("week").addEventListener("change", (e) => { STATE.week = e.target.value || null; render(); });
  document.getElementById("signal").addEventListener("change", (e) => { STATE.signal = e.target.value; render(); });
  document.getElementById("book").addEventListener("change", (e) => { STATE.book = e.target.value; render(); });
  document.getElementById("minedge").addEventListener("input", (e) => {
    const v = parseFloat(e.target.value); STATE.minEdge = Number.isFinite(v) ? v : null; render();
  });
  document.getElementById("showdomes").addEventListener("change", (e) => { STATE.showDomes = e.target.checked; render(); });
  document.getElementById("showwatch").addEventListener("change", (e) => { STATE.showWatch = e.target.checked; render(); });
  document.getElementById("search").addEventListener("input", (e) => { STATE.q = e.target.value.toLowerCase().trim(); render(); });
  const presetChipEl = document.getElementById("presetchip");
  if (presetChipEl) presetChipEl.addEventListener("click", () => setPreset(null));
  document.getElementById("signal").value = STATE.signal;
  if (STATE.minEdge != null) document.getElementById("minedge").value = STATE.minEdge;
  window.addEventListener("hashchange", () => {
    const before = STATE.game;
    readHash();
    if (STATE.game && STATE.game !== before) openDrawer(STATE.game);
    render();
  });

  setupHover();
  setupDrawer();
  setupRefresh(auth);
  startMetaPoll();
  render();
  if (STATE.game) openDrawer(STATE.game);
  // warm the alert feed so drawer markers / timelines are ready (Alerts tab reuses the cache)
  if (STATE.view !== "alerts") loadAlerts().catch(() => {});
  // warm the backtest grid so hover Record / ROI is ready; re-render once it lands
  if (typeof loadBacktest === "function" && STATE.view !== "backtest") {
    loadBacktest().then(() => { if (STATE.view === "table" || STATE.view === "map") render(); }).catch(() => {});
  }
}

boot();
