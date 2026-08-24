"use strict";
// Alerts tab: the Telegram alert feed (/data/alerts_feed.json, written by json_out.py from
// alerts.json + D1 `alerts`; falls back to /api/alerts D1 rows). One row per alert key:
// key, family, sport, game, book, line open -> now, edge, sent_at, status, CLV. Clicking a
// row deep-links to the game drawer (#sport=..&week=..&game=..).
//
// Tolerant reader: the feed may be an array or {alerts|rows|items: [...]} and rows may use
// either the feed names (alert_key, sent_at, line_open/line_now, edge_pts) or the D1
// column names (first_sent_at/last_sent_at, first_line/last_line, first_edge/last_edge).

const ALERTS = { rows: null, family: "", status: "", sport: "", q: "", sort: "sent", loaded: false };

const FAMILY_LABELS = {
  edge: "Edge", move: "Move", gone: "Edge gone", wx: "Forecast move", openers: "Openers",
  ops: "Ops", degr: "Degradation", stadium: "Stadium", names: "Names", noref: "No ref", digest: "Digest",
};
const familyLabel = (f) => FAMILY_LABELS[f] || f || "?";

function alertRowsOf(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const k of ["alerts", "rows", "items", "feed"]) if (Array.isArray(payload[k])) return payload[k];
  return [];
}

const firstNum = (...vals) => { for (const v of vals) if (isNum(v)) return Number(v); return null; };
const firstStr = (...vals) => { for (const v of vals) if (v !== null && v !== undefined && v !== "") return String(v); return null; };

// Normalize one feed / D1 row into the shape the table renders.
function normalizeAlert(raw) {
  const a = raw || {};
  const key = firstStr(a.alert_key, a.key) || "";
  const parts = key.split("|");
  const family = firstStr(a.family, parts[0]) || "?";
  const gameId = firstStr(a.game_id, family === "edge" && parts.length >= 8 ? parts[3] : null);
  const g = gameId ? findGame(gameId) : null;
  const sport = firstStr(a.sport, g && g.sport, gameId ? gameId.split(":")[0] : null);
  const season = firstNum(a.season, g && g.season, gameId ? Number(gameId.split(":")[1]) : null);
  const week = firstNum(a.week, g && g.week, gameId ? Number(gameId.split(":")[2]) : null);
  return {
    key, family, sport, season, week,
    game_id: gameId,
    game: firstStr(a.game, g ? gameLabel(g) : null, gameId ? gameId.split(":").slice(3).join(":").replace("@", " @ ").toUpperCase() : null) || "—",
    market: firstStr(a.market, family === "edge" && parts.length >= 8 ? parts[4] : null),
    side: firstStr(a.side, family === "edge" && parts.length >= 8 ? parts[5] : null),
    book: firstStr(a.book, family === "edge" && parts.length >= 8 ? parts[6] : null),
    tier: firstStr(a.tier),
    line_open: firstNum(a.line_open, a.open_line, a.first_line, a.line),
    line_now: firstNum(a.line_now, a.last_line, a.line),
    odds: firstNum(a.odds, a.last_odds, a.first_odds),
    fair: firstNum(a.fair, a.fair_line, a.last_fair, a.first_fair),
    edge: firstNum(a.edge, a.edge_pts, a.last_edge, a.first_edge),
    edge_open: firstNum(a.edge_open, a.first_edge),
    clv_pts: firstNum(a.clv_pts, a.clv),
    closing_line: firstNum(a.closing_line),
    sent_at: firstStr(a.sent_at, a.last_sent_at, a.first_sent_at, a.ts),
    first_sent_at: firstStr(a.first_sent_at, a.sent_at),
    sends: firstNum(a.sends) ?? 1,
    status: firstStr(a.status) || "open",
    text: firstStr(a.text, a.text_plain, a.message) || "",
    text_html: firstStr(a.text_html) || "",
    model_version: firstStr(a.model_version),
    run_id: firstStr(a.run_id),
  };
}

async function loadAlerts(force = false) {
  if (ALERTS.rows && !force) return ALERTS.rows;
  let rows = [];
  try {
    rows = alertRowsOf(await fetchJson("data/alerts_feed.json?t=" + Date.now()));
  } catch (_) { rows = []; }
  if (!rows.length) {
    try {
      const j = await fetchJson("api/alerts");
      rows = alertRowsOf(j);
    } catch (_) { /* no D1 either */ }
  }
  ALERTS.rows = rows.map(normalizeAlert).filter((a) => a.key || a.game_id);
  ALERTS.rows.sort((x, y) => String(y.sent_at || "").localeCompare(String(x.sent_at || "")));
  ALERTS.loaded = true;
  return ALERTS.rows;
}

// alerts for one game (drawer timeline + uPlot markers)
function alertsForGame(gameId) {
  return (ALERTS.rows || []).filter((a) => a.game_id === gameId);
}

function filteredAlerts() {
  let rows = ALERTS.rows || [];
  if (ALERTS.family) rows = rows.filter((a) => a.family === ALERTS.family);
  if (ALERTS.status) rows = rows.filter((a) => a.status === ALERTS.status);
  if (ALERTS.sport) rows = rows.filter((a) => a.sport === ALERTS.sport);
  if (ALERTS.q) rows = rows.filter((a) => `${a.game} ${a.book || ""} ${a.key}`.toLowerCase().includes(ALERTS.q));
  return rows;
}

function fmtLineFor(market, v) {
  if (!isNum(v)) return "—";
  return market === "spread" ? fmtLine(v) : market === "ml" ? fmtOdds(v) : fmtTotal(v);
}
function fmtEdge(v) {
  if (!isNum(v)) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(1);
}

function alertRowHtml(a) {
  const lineCell = isNum(a.line_open) || isNum(a.line_now)
    ? openNow(a.line_open, a.line_now, (v) => fmtLineFor(a.market, v)) + (a.market !== "ml" && isNum(a.odds) ? ` <span class="sub">${fmtOdds(a.odds)}</span>` : "")
    : `<span class="muted">—</span>`;
  const pick = [a.side ? a.side.toUpperCase() : null, a.market && a.market !== "total" ? a.market : null].filter(Boolean).join(" ");
  const clv = isNum(a.clv_pts) ? `<span class="mv ${a.clv_pts >= 0 ? "up" : "dn"}">${fmtEdge(a.clv_pts)}</span>` : `<span class="muted">—</span>`;
  const tip = [a.key, a.text, a.model_version ? `model ${a.model_version}` : null, a.run_id ? `run ${a.run_id}` : null].filter(Boolean).join("\n");
  const deep = a.game_id ? ` data-game="${esc(a.game_id)}" data-sport="${esc(a.sport || "")}" data-week="${esc(a.week ?? "")}"` : "";
  return `<tr class="alert-row ${a.game_id ? "link" : ""} st-${esc(a.status)}"${deep} title="${esc(tip)}">`
    + `<td class="left"><span class="fam fam-${esc(a.family)}">${esc(familyLabel(a.family))}</span>${a.tier ? `<span class="tierchip ${esc(a.tier)}">${esc(a.tier)}</span>` : ""}</td>`
    + `<td class="left">${esc((a.sport || "").toUpperCase())}${a.week != null ? ` <span class="sub">wk ${esc(a.week)}</span>` : ""}</td>`
    + `<td class="left game">${esc(a.game)}${pick ? `<span class="sub">${esc(pick)}</span>` : ""}</td>`
    + `<td class="left">${esc(a.book ? bookLabel(a.book) : "—")}</td>`
    + `<td>${lineCell}</td>`
    + `<td>${isNum(a.fair) ? fmtLineFor(a.market, a.fair) : "—"}</td>`
    + `<td>${fmtEdge(a.edge)}${isNum(a.edge_open) && isNum(a.edge) && Math.abs(a.edge_open - a.edge) >= 0.05 ? ` <span class="sub">(${fmtEdge(a.edge_open)} at send)</span>` : ""}</td>`
    + `<td>${isNum(a.closing_line) ? fmtLineFor(a.market, a.closing_line) : `<span class="muted">—</span>`}</td>`
    + `<td>${clv}${isNum(a.clv_pts) && isNum(a.line_open) && a.line_open ? ` <span class="sub">${(Number(a.clv_pts) / Math.abs(Number(a.line_open)) * 100).toFixed(1)}%</span>` : ""}</td>`
    + `<td class="left">${esc(fmtShortET(a.sent_at))}${a.sends > 1 ? ` <span class="sub">×${a.sends}</span>` : ""}</td>`
    + `<td class="left"><span class="pill st-${esc(a.status)}">${esc(a.status)}</span></td>`
    + `</tr>`;
}

// CLV summary of the rows on screen: n with a closing line, avg CLV pts, +CLV share, v1 vs v2
function clvSummary(rows) {
  const withClv = rows.filter((a) => a.family === "edge" && isNum(a.clv_pts));
  if (!withClv.length) return null;
  const stat = (xs) => ({ n: xs.length, avg: xs.reduce((acc, a) => acc + Number(a.clv_pts), 0) / xs.length,
    pos: xs.filter((a) => Number(a.clv_pts) > 0).length });
  const all = stat(withClv);
  const byModel = {};
  for (const a of withClv) (byModel[a.model_version || "v1"] ||= []).push(a);
  return { all, models: Object.fromEntries(Object.entries(byModel).map(([k, xs]) => [k, stat(xs)])) };
}
function clvSummaryHtmlAlerts(rows) {
  const s = clvSummary(rows);
  if (!s) return "";
  const one = (label, st) => `<b>${esc(label)}</b> n=${st.n} avg ${fmtEdge(st.avg)} · +CLV ${st.pos}/${st.n}`;
  const models = Object.keys(s.models).sort();
  return `<span class="sub al-clv" title="closing line value of the EDGE alerts on screen">CLV: ${one("all", s.all)}`
    + (models.length > 1 ? models.map((k) => ` · ${one(k, s.models[k])}`).join("") : "") + `</span>`;
}

function alertsControlsHtml(rows) {
  const fams = [...new Set((ALERTS.rows || []).map((a) => a.family))].sort();
  const statuses = [...new Set((ALERTS.rows || []).map((a) => a.status))].sort();
  return `<div class="controls alertctl">
    <select id="al-sport"><option value="">Sport: all</option><option value="nfl">NFL</option><option value="cfb">CFB</option></select>
    <select id="al-family"><option value="">Family: all</option>${fams.map((f) => `<option value="${esc(f)}">${esc(familyLabel(f))}</option>`).join("")}</select>
    <select id="al-status"><option value="">Status: all</option>${statuses.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select>
    <select id="al-sort" title="Sort"><option value="sent">Sort: sent</option><option value="clv">Sort: CLV</option><option value="edge">Sort: edge</option></select>
    <input id="al-q" placeholder="Filter game / book / key…" />
    <span class="sub" id="al-count"></span>
    <button class="controlbtn" id="al-reload" type="button" title="Re-fetch alerts_feed.json">↻</button>
    ${clvSummaryHtmlAlerts(rows || [])}
  </div>`;
}

async function renderAlerts() {
  const host = document.getElementById("alertswrap");
  if (!host) return;
  if (!ALERTS.loaded) {
    host.innerHTML = `<div class="empty">loading alerts…</div>`;
    await loadAlerts();
    if (STATE.view !== "alerts") return;
  }
  const rows = filteredAlerts().slice();
  if (ALERTS.sort === "clv") rows.sort((x, y) => (isNum(y.clv_pts) ? y.clv_pts : -1e9) - (isNum(x.clv_pts) ? x.clv_pts : -1e9));
  else if (ALERTS.sort === "edge") rows.sort((x, y) => Math.abs(isNum(y.edge) ? y.edge : 0) - Math.abs(isNum(x.edge) ? x.edge : 0));
  const head = `<tr><th class="left">Family</th><th class="left">Sport</th><th class="left">Game</th><th class="left">Book</th>
    <th title="line at first send → line at last run">Line open → now</th><th>Fair</th><th title="edge pts at last run (at send in parentheses)">Edge</th>
    <th title="closing line (last odds_history row before kickoff, same book/market/side)">Close</th>
    <th title="closing line value: pts gained vs the close from the side alerted (CLV % = pts / sent line)">CLV</th><th class="left">Sent (ET)</th><th class="left">Status</th></tr>`;
  const body = rows.length ? rows.map(alertRowHtml).join("")
    : `<tr><td colspan="11" class="empty">${(ALERTS.rows || []).length ? "no alerts match the filters" : "no alerts sent yet (alerts_feed.json empty)"}</td></tr>`;
  host.innerHTML = alertsControlsHtml(rows) + `<div class="wrap"><table class="alerts"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  document.getElementById("al-count").textContent = `${rows.length} / ${(ALERTS.rows || []).length} alerts`;
  document.getElementById("al-sport").value = ALERTS.sport;
  document.getElementById("al-family").value = ALERTS.family;
  document.getElementById("al-status").value = ALERTS.status;
  document.getElementById("al-sort").value = ALERTS.sort || "sent";
  document.getElementById("al-q").value = ALERTS.q;
  document.getElementById("al-sort").addEventListener("change", (ev) => { ALERTS.sort = ev.target.value; renderAlerts(); });
  document.getElementById("al-sport").addEventListener("change", (ev) => { ALERTS.sport = ev.target.value; renderAlerts(); });
  document.getElementById("al-family").addEventListener("change", (ev) => { ALERTS.family = ev.target.value; renderAlerts(); });
  document.getElementById("al-status").addEventListener("change", (ev) => { ALERTS.status = ev.target.value; renderAlerts(); });
  document.getElementById("al-q").addEventListener("input", (ev) => { ALERTS.q = ev.target.value.toLowerCase().trim(); renderAlerts(); });
  document.getElementById("al-reload").addEventListener("click", async () => { await loadAlerts(true); renderAlerts(); });
  host.querySelectorAll("tr.alert-row.link").forEach((tr) => tr.addEventListener("click", () => openAlertGame(tr.dataset)));
}

// Deep link: switch sport/week to the alert's game and open its drawer (stay on the Alerts tab
// when the game is not on the board any more, e.g. a past week).
function openAlertGame(ds) {
  const gid = ds.game;
  if (!gid) return;
  const g = findGame(gid);
  if (!g) {
    STATE.game = gid;
    writeHash();
    return;
  }
  STATE.sport = g.sport || ds.sport || STATE.sport;
  if (g.week != null) STATE.week = String(g.week);
  STATE.view = "table";
  populateWeeks();
  render();
  openDrawer(gid);
}
