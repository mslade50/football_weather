"use strict";
// Past weeks use saved pregame signals and settled results, never today's model.
const HIST = { season: null, week: null, signal: "", q: "", sort: "kickoff_utc", dir: 1 };

function readHistoricalHash(params) {
  HIST.season = params.get("histSeason"); HIST.week = params.get("histWeek");
  HIST.signal = params.get("histSignal") || "";
}
function writeHistoricalHash(params) {
  if (STATE.tableMode !== "history") return;
  params.set("past", "1");
  if (HIST.season != null) params.set("histSeason", HIST.season);
  if (HIST.week != null) params.set("histWeek", HIST.week);
  if (HIST.signal) params.set("histSignal", HIST.signal);
}
function historicalTier(row) {
  if (!row.signal_label || !row.signal_at) return "Unknown";
  const label = row.signal_level || row.signal_label;
  return /Very High/i.test(label) ? "Very High" : /High/i.test(label) ? "High"
    : /Mid/i.test(label) ? "Mid" : /Low/i.test(label) ? "Low" : /^No\b/i.test(label) ? "No" : "Unknown";
}
function historicalGames(data, sport, season, week, signal = "", query = "", now = Date.now()) {
  return (data.games || []).filter(r => r.sport === sport && r.season === Number(season)
    && (week == null || r.week === Number(week)) && parseTs(r.kickoff_utc)?.getTime() <= now
    && (!signal || historicalTier(r) === signal)
    && (!query || `${r.away} ${r.home} ${r.stadium}`.toLowerCase().includes(query.toLowerCase())));
}
function historicalRecord(rows) {
  return ["W", "L", "P"].map(result => rows.filter(r => r.under_result === result).length).join("–");
}
function historicalPlay(bet) {
  const side = bet.side === "home" ? bet.home : bet.side === "away" ? bet.away : bet.side;
  const line = bet.market === "total" ? fmtTotal(bet.first_line) : fmtLine(bet.first_line);
  return `${esc(side || "")} ${esc(line)} (${esc(fmtOdds(bet.first_odds))})`
    + ` · ${esc(bookLabel(bet.book))} · <b>${esc(bet.result || "Pending")}</b>`;
}
function historyRowsHtml(rows, bets) {
  return rows.map(r => {
    const tier = historicalTier(r);
    const signal = tier === "Unknown" ? "Unknown" : tier === "No" ? "No signal" : r.signal_label;
    const plays = bets.filter(b => b.game_id === r.game_id);
    const score = isNum(r.away_score) && isNum(r.home_score) ? `${r.away_score}–${r.home_score}` : "Pending";
    return `<tr><td class="left">${esc(r.away)} @ ${esc(r.home)}<span class="history-sub">${esc(fmtShortET(r.kickoff_utc))}</span></td>`
      + `<td class="left">${esc(r.stadium)}</td><td>${esc(signal)}${r.signal_at ? `<span class="history-sub">${esc(fmtShortET(r.signal_at))}</span>` : ""}</td>`
      + `<td>${fmtNum(r.wind_fg, 1)}</td><td>${fmtNum(r.wind_actual, 1)}</td><td>${fmtNum(r.temp_fg, 0)}</td>`
      + `<td>${fmtTotal(r.total_open)}</td><td>${fmtTotal(r.total_close)}<span class="history-sub">${esc(bookLabel(r.ref_book || ""))}</span></td>`
      + `<td>${esc(score)}<span class="history-sub">Total ${fmtTotal(r.total_actual)}</span></td><td>${esc(r.under_result || "Pending")}</td>`
      + `<td class="left">${plays.length ? plays.map(historicalPlay).join("<br>") : "No recorded play"}</td></tr>`;
  }).join("");
}
async function renderHistoricalTable() {
  const bar = document.getElementById("historybar"), head = document.querySelector("#table thead");
  const body = document.querySelector("#table tbody");
  if (!BT.loaded) {
    bar.textContent = "Loading past weeks…"; head.innerHTML = ""; body.innerHTML = "";
    await loadBacktest();
    if (STATE.view === "table" && STATE.tableMode === "history") render();
    return;
  }
  const data = BT.data, available = data.games.filter(r => r.sport === STATE.sport && parseTs(r.kickoff_utc)?.getTime() <= Date.now());
  const seasons = [...new Set(available.map(r => r.season).filter(isNum))].sort((a,b) => b-a);
  if (!seasons.includes(Number(HIST.season))) HIST.season = seasons[0] ?? null;
  const weeks = [...new Set(available.filter(r => r.season === Number(HIST.season)).map(r => r.week).filter(isNum))].sort((a,b) => b-a);
  if (HIST.week == null || !weeks.includes(Number(HIST.week))) HIST.week = weeks[0] ?? null;
  const options = (values, selected, label) => values.map(v => `<option value="${esc(v)}"${String(v) === String(selected) ? " selected" : ""}>${esc(label(v))}</option>`).join("");
  bar.innerHTML = `<label>Sport <select id="history-sport">${options(["nfl", "cfb"], STATE.sport, s => s.toUpperCase())}</select></label>`
    + `<label>Season <select id="history-season">${options(seasons, HIST.season, String)}</select></label>`
    + `<label>Week <select id="history-week">${options(weeks, HIST.week, w => `Week ${w}`)}</select></label>`
    + `<label>Pregame signal <select id="history-signal">${options(["", "Very High", "High", "Mid", "Low", "No", "Unknown"], HIST.signal, s => s === "" ? "All" : s === "No" ? "No signal" : s)}</select></label>`
    + `<input id="history-search" aria-label="Search historical games" placeholder="Team or stadium" value="${esc(HIST.q)}" />`
    + `<button id="history-reload" class="controlbtn">Reload results</button>`;
  for (const [id, key] of [["history-season", "season"], ["history-week", "week"], ["history-signal", "signal"]]) {
    document.getElementById(id).addEventListener("change", e => { HIST[key] = e.target.value; if (key === "season") HIST.week = null; render(); });
  }
  document.getElementById("history-sport").addEventListener("change", e => { HIST.season = HIST.week = null; setSport(e.target.value); });
  document.getElementById("history-search").addEventListener("change", e => { HIST.q = e.target.value; render(); });
  document.getElementById("history-reload").addEventListener("click", async e => { e.target.disabled = true; await loadBacktest(true); render(); });
  const rows = historicalGames(data, STATE.sport, HIST.season, HIST.week, HIST.signal, HIST.q);
  const cols = [["Game / kickoff ET", "kickoff_utc"], ["Stadium", "stadium"], ["Pregame signal / saved ET", "signal_label"],
    ["Forecast wind", "wind_fg"], ["Actual wind", "wind_actual"], ["Forecast °F", "temp_fg"], ["Open total", "total_open"],
    ["Close total", "total_close"], ["Score (away–home)", "total_actual"], ["Under at close", "under_result"], ["Alerted plays / results", null]];
  rows.sort((a,b) => {
    const x = a[HIST.sort], y = b[HIST.sort];
    if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
    return (typeof x === "number" ? x-y : String(x).localeCompare(String(y))) * HIST.dir;
  });
  head.innerHTML = `<tr>${cols.map(([label, key]) => `<th${key ? ` class="sortable" data-key="${key}"` : ""}>${esc(label)}${key === HIST.sort ? (HIST.dir === 1 ? " ▴" : " ▾") : ""}</th>`).join("")}</tr>`;
  head.querySelectorAll("th.sortable").forEach(th => th.addEventListener("click", () => { HIST.dir = HIST.sort === th.dataset.key ? -HIST.dir : 1; HIST.sort = th.dataset.key; render(); }));
  body.innerHTML = historyRowsHtml(rows, data.postmortem.season.bets) || `<tr><td colspan="11" class="empty">No archived games for these filters.</td></tr>`;
  const signalRows = rows.filter(r => !["No", "Unknown"].includes(historicalTier(r)));
  const noRows = rows.filter(r => historicalTier(r) === "No");
  document.getElementById("historyinfo").innerHTML = `${rows.length} ${rows.length === 1 ? "game" : "games"} · Under at close W–L–P: <b>${historicalRecord(rows)}</b>`
    + ` · Signal: ${historicalRecord(signalRows)} · No signal: ${historicalRecord(noRows)} · Unknown signal: ${rows.filter(r => historicalTier(r) === "Unknown").length}`
    + `<br>Signals are the last saved pregame snapshot; Unknown means no verified snapshot. Wind is mph. Closing-under results are separate from actual alerted plays.`
    + `<br>Results as of ${esc(data.generated_at ? fmtShortET(data.generated_at) + " ET" : "unavailable")}; updated by the scheduled backtest. Pending means no settled result is available.`;
  writeHash();
}
