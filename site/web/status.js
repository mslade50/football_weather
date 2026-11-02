"use strict";
// Header: sport/week label, Eastern update / next-run times, book chips
// from meta.books, degradation banners, run-health statusbar.

// Server `meta.refresh` wins when the Worker computed it. Otherwise the
// published ETA plus 90 minutes is the same rule /api/status uses.
function refreshLate(meta) {
  const flag = meta && meta.refresh;
  if (flag && (flag.overdue || flag.missed)) return flag;
  if (typeof QUOTES !== "undefined" && QUOTES && typeof QUOTES.refreshOverdue === "function" && QUOTES.refreshOverdue(meta)) {
    return { overdue: true, missed: false, reason: "Expected refresh is overdue; trigger outcome unknown" };
  }
  return null;
}

function renderHeader(meta) {
  const ev = document.getElementById("event");
  const parts = [];
  if (meta.season) parts.push(`${meta.season}`);
  if (meta.week != null) parts.push(`Week ${meta.week}`);
  const counts = meta.sport_counts || {};
  const cnt = ["nfl", "cfb"].filter((s) => counts[s] != null).map((s) => `${s.toUpperCase()} ${counts[s]}`).join(" · ");
  ev.textContent = (parts.join(" · ") || "No run yet") + (cnt ? ` (${cnt})` : "");

  const upd = document.getElementById("updated");
  if (meta.last_updated) {
    const et = fmtET(meta.last_updated);
    upd.textContent = `Updated ${et}`;
    const carried = carriedSportNotices(meta).map((notice) => notice.text).join(" ");
    upd.title = `run_id ${meta.run_id || "?"}` + (meta.git_sha ? ` · ${String(meta.git_sha).slice(0, 7)}` : "")
      + (meta.model_version ? ` · model ${meta.model_version}` : "")
      + (carried ? ` · ${carried}` : "");
  } else {
    upd.textContent = "";
  }
  const nr = document.getElementById("nextrun");
  const late = refreshLate(meta);
  if (late) {
    const when = meta.next_run_eta ? ` (expected ${fmtET(meta.next_run_eta)})` : "";
    nr.textContent = late.missed ? `· refresh missed${when}` : `· refresh overdue${when}`;
  } else {
    nr.textContent = meta.next_run_eta ? `· next run ~${fmtET(meta.next_run_eta)}` : "";
  }
  if (nr.classList) nr.classList.toggle("overdue", !!late);

  renderBookChips(meta.books || {});
}

function renderBookChips(books) {
  const el = document.getElementById("bookchips");
  if (!el) return;
  el.hidden = true;
  const order = [...BOOKS, ...Object.keys(books).filter((b) => !BOOKS.includes(b))];
  el.innerHTML = order.filter((b) => b !== "consensus").map((b) => {
    const bs = books[b] || {};
    const status = bs.status || (bs.count > 0 ? "green" : "red");
    const tip = [
      `${bookLabel(b)}: ${bs.count != null ? bs.count + " lines" : "no data"}`,
      bs.baseline != null ? `baseline ${bs.baseline}` : null,
      bs.last_ok ? `last ok ${fmtShortET(bs.last_ok)}` : null,
      bs.reason || null,
    ].filter(Boolean).join(" · ");
    return `<span class="chip ${esc(status)}" title="${esc(tip)}">${esc(bookLabel(b))}${bs.count != null ? ` ${bs.count}` : ""}</span>`;
  }).join("");
}

// Run ids start with YYYYMMDDTHHMMSSZ. That clock is the earlier build when a
// sport was copied forward and the games file did not keep its own timestamp.
function retainedRunTime(runId) {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(String(runId || ""));
  if (!match) return null;
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}Z`;
}
function carriedSportNotices(meta) {
  const retained = meta && meta.retained_sports;
  if (!retained || typeof retained !== "object") return [];
  return ["nfl", "cfb"].filter((sport) => retained[sport] && typeof retained[sport] === "object").map((sport) => {
    const info = retained[sport];
    const raw = info.refreshed_at || retainedRunTime(info.from_run_id);
    const when = raw && typeof fmtET === "function" ? (fmtET(raw) || raw) : (raw || "an earlier run");
    return {
      sport,
      from_run_id: info.from_run_id || "",
      text: `${sport.toUpperCase()} carried forward from the earlier run. Weather and prices were last refreshed ${when}.`,
    };
  });
}
// Shared warnings have one home; the Status tab still carries the full run history.
// Carried sports stay visible here: their cards keep the new run id, so the
// header clock is this run, not when that sport's weather and prices were built.
function renderBanners(meta) {
  const el = document.getElementById("banners");
  if (!el) return;
  const notices = carriedSportNotices(meta || {});
  if (!notices.length) { el.innerHTML = ""; el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = notices.map((notice) =>
    `<div class="banner carried" title="${esc(notice.from_run_id)}">${esc(notice.text)}</div>`).join("");
}
const HEALTH_RENDER = new WeakMap();
function leagueWarningList(meta, refresh) {
  const rich = meta && (meta.retained_sports || meta.sport_counts);
  if (rich && typeof QUOTES !== "undefined" && QUOTES && typeof QUOTES.leagueBoardWarnings === "function") {
    return QUOTES.leagueBoardWarnings(meta) || [];
  }
  if (refresh && Array.isArray(refresh.leagues)) return refresh.leagues;
  if (meta && meta.refresh && Array.isArray(meta.refresh.leagues)) return meta.refresh.leagues;
  if (typeof QUOTES !== "undefined" && QUOTES && typeof QUOTES.leagueBoardWarnings === "function") {
    return QUOTES.leagueBoardWarnings(meta) || [];
  }
  return [];
}
function boardDiagnostics(meta) {
  const grouped = new Map();
  const add = (component, severity, reason, stamp = null) => {
    const text = String(reason || "Details unavailable").trim().replace(/\s+/g, " ");
    const key = JSON.stringify([component, severity, text]);
    const existing = grouped.get(key);
    if (existing) { existing.count++; if (stamp) existing.stamps.add(stamp); }
    else grouped.set(key, {component, severity, reason: text, count: 1, stamps: new Set(stamp ? [stamp] : [])});
  };
  for (const d of meta.degradations || []) if (d && (d.severity || "warn") !== "info")
    add(d.component || "pipeline", d.severity || "warn", d.reason, d.ts);
  const late = refreshLate(meta);
  if (late) add("scheduler", "warn", late.reason || "Expected refresh is overdue; trigger outcome unknown");
  const leagues = leagueWarningList(meta, meta && meta.refresh);
  for (const warning of leagues) add("scheduler", "warn", warning.reason || `${String(warning.sport || "league").toUpperCase()} board is older than 3h`);
  const loads = typeof LOAD_ERRORS !== "undefined" ? Object.entries(LOAD_ERRORS).filter(([, reason]) => reason) : [];
  for (const [source, reason] of loads) add("load", "error", `${source}: ${reason}`);
  const books = meta.books || {}, names = Object.keys(books).filter(b => b !== "consensus");
  const red = names.filter(b => (books[b].status || "green") === "red"), amber = names.filter(b => books[b].status === "amber");
  // Preserve the existing health predicate. This changes presentation, not price qualification or validation.
  const ok = !red.length && !amber.length && !late && !leagues.length && !loads.length
    && meta.resident?.status === "fresh" && meta.publication_status === "manifest_verified"
    && !(meta.degradations || []).some(d => (d.severity || "warn") !== "info");
  return {ok, late, leagues, names, red, amber, loads, items: [...grouped.values()].map(d => ({...d, stamps: [...d.stamps]}))};
}
function renderStatusbar(meta) {
  const el = document.getElementById("statusbar");
  if (!el) return;
  const health = boardDiagnostics(meta), errors = health.items.filter(d => ["error", "critical"].includes(d.severity));
  const state = health.ok ? "OK" : health.loads.length ? "Board unavailable" : "Degraded";
  const publication = meta.publication_status === "manifest_verified" ? "Publication verified" : "Publication unverified";
  const quotes = meta.resident?.status === "fresh" ? `Resident quotes fresh${meta.resident.fresh_games != null ? ` (${meta.resident.fresh_games} games)` : ""}` : `Resident quotes ${meta.resident?.status || "unavailable"}`;
  const notices = health.items.length + health.red.length + health.amber.length;
  const signature = JSON.stringify([state, publication, quotes, health, meta.books, meta.unresolved_names, meta.run_id, meta.publication, meta.model_version]);
  if (HEALTH_RENDER.get(el) === signature) return;
  const previous = el.querySelector?.("#board-health-details");
  const expanded = !!previous?.open;
  const booksExpanded = !!el.querySelector?.("#board-health-books")?.open;
  const bookScroll = el.querySelector?.(".health-books .execution-scroll");
  const scroll = bookScroll ? {top: bookScroll.scrollTop, left: bookScroll.scrollLeft} : null;
  const focusId = el.contains?.(document.activeElement) ? document.activeElement?.id : null;
  const grouped = new Map();
  for (const d of health.items) (grouped.get(d.component) || grouped.set(d.component, []).get(d.component)).push(d);
  const diagnostics = [...grouped].map(([component, items]) => `<section class="health-group"><h3>${esc(component)}</h3><ul>${items.map(d =>
    `<li class="${["error", "critical"].includes(d.severity) ? "health-error" : ""}">${esc(d.reason)}${d.count > 1 ? ` <span class="sub">(${d.count} reports)</span>` : ""}${d.stamps.length ? `<span class="sub">Reported ${d.stamps.map(stamp => esc(fmtShortET(stamp))).join(", ")}</span>` : ""}</li>`).join("")}</ul></section>`).join("");
  const late = refreshLate(meta);
  const latePill = late ? `<span class="pill warn">${late.missed ? "Refresh missed" : "Refresh overdue"}</span>` : "";
  const leaguePill = (health.leagues || []).length ? `<span class="pill warn">League board stale</span>` : "";
  el.innerHTML = `<div class="health-overview"><span class="pill ${health.ok ? "ok" : "warn"}">${state}</span>${latePill}${leaguePill}
      <span class="health-fact ${meta.publication_status === "manifest_verified" ? "" : "health-error"}">${publication}</span>
      <span class="health-fact ${meta.resident?.status === "fresh" ? "" : "health-error"}">${esc(quotes)}</span>
      ${errors.length ? `<b class="health-error">${errors.length} ${errors.length === 1 ? "failure" : "failures"}</b><span class="health-error health-critical">${esc(errors[0].component)}: ${esc(errors[0].reason.length > 96 ? errors[0].reason.slice(0, 96) + "…" : errors[0].reason)}</span>` : ""}</div>
    <details id="board-health-details" ${expanded ? "open" : ""}><summary id="board-health-summary">Status details${notices ? ` · ${notices} ${notices === 1 ? "notice" : "notices"}` : ""}</summary>
      <div class="health-details"><p class="health-guidance">Liquidity unverified means current stake evidence is missing, stale or incompatible. Checked depth below $500 describes only verified compatible depth at the checked line, rules and price limit. Quotes need fresh depth, taker fees and compatible settlement rules. Fees are additional. Open a game for exact source clocks and venue details.</p>
      <p class="health-guidance">Signals and near signals are weather screens. Unknown likelihood is not a probability estimate. Show all games can display ineligible games; they remain ineligible for signals and stake quotes; liquidity never determines discovery.</p>
      <div class="health-facts"><span>${publication}</span><span>${esc(quotes)}</span><span>Run ${esc(meta.run_id || "unknown")}</span><span>Generation ${esc(meta.publication?.generation || "unknown")}</span>${meta.model_version ? `<span>Model ${esc(meta.model_version)}</span>` : ""}
        <span>${health.names.length ? `${health.names.length - health.red.length}/${health.names.length} books reporting` : "Book coverage unknown"}</span>
        ${(meta.unresolved_names || []).length ? `<span>${meta.unresolved_names.length} unresolved names: ${esc(meta.unresolved_names.join(", "))}</span>` : ""}</div>
      ${health.names.length ? `<details id="board-health-books" class="health-books" ${booksExpanded ? "open" : ""}><summary id="board-health-book-summary">Book coverage${health.red.length ? ` · ${health.red.length} unavailable` : ""}${health.amber.length ? ` · ${health.amber.length} thin` : ""}</summary><div class="execution-scroll">${bookCountsHtml(meta.books, {})}</div></details>` : ""}
      ${diagnostics || '<p class="sub">No reported pipeline diagnostics. Publication and quote verification are shown above.</p>'}</div></details>`;
  HEALTH_RENDER.set(el, signature);
  const nextBookScroll = el.querySelector?.(".health-books .execution-scroll");
  if (scroll && nextBookScroll) { nextBookScroll.scrollTop = scroll.top; nextBookScroll.scrollLeft = scroll.left; }
  if (["board-health-summary", "board-health-book-summary"].includes(focusId)) el.querySelector?.(`#${focusId}`)?.focus();
}

// ── Status tab ────────────────────────────────────────────────────────────
// /data/status.json (json_out.py): { run_id, last_updated, git_sha, model_version,
//   runs: [{run_id, sport, season, week, scope, status, started_at, finished_at, duration_s,
//           n_games, n_lines, n_alerts, stage_timings {stage: seconds}, counts {book: n}, degradations []}],
//   stage_timings {stage: seconds}, books {book: {count, baseline, status, last_ok}},
//   degradations [{component, reason, severity, run_id, ts}], unresolved_names [..],
//   heartbeat {ts, cron?, dispatched?} }
// Fallback: /api/status (Worker: D1 runs + R2 cf_heartbeat.json + meta.json).

const STATUS = { data: null, loaded: false };

function statusRunsOf(j) {
  if (!j || typeof j !== "object") return [];
  return Array.isArray(j.runs) ? j.runs : Array.isArray(j.rows) ? j.rows : [];
}
function parseMaybeJson(v) {
  if (v == null) return null;
  if (typeof v !== "string") return v;
  try { return JSON.parse(v); } catch (_) { return null; }
}
function heartbeatTs(hb) {
  if (!hb || typeof hb !== "object") return null;
  for (const k of ["ts", "last_tick", "updated_at", "last_updated", "at", "time"]) if (hb[k]) return hb[k];
  return null;
}
function ageLabel(ts) {
  const d = parseTs(ts);
  if (!d) return "never";
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
  if (mins < 60) return `${mins} min ago`;
  const h = mins / 60;
  if (h < 48) return `${h.toFixed(1)} h ago`;
  return `${(h / 24).toFixed(1)} d ago`;
}
function ageHours(ts) {
  const d = parseTs(ts);
  return d ? (Date.now() - d.getTime()) / 3600000 : null;
}
const fmtSecs = (v) => (isNum(v) ? (Number(v) >= 100 ? `${Math.round(Number(v))}s` : `${Number(v).toFixed(1)}s`) : "—");

async function loadStatus(force = false) {
  if (STATUS.data && !force) return STATUS.data;
  let data = null;
  try { data = await fetchJson("data/status.json?t=" + Date.now()); } catch (_) { data = null; }
  {
    try {
      const j = await fetchJson("api/status");
      if (j && j.ok) {
        const m = j.meta || {};
        data = { ...(data || {}), run_id: m.run_id, last_updated: m.last_updated, git_sha: m.git_sha, next_run_eta: m.next_run_eta,
          degradations: m.degradations || [], books: m.books || {}, runs: j.runs || [], heartbeat: j.heartbeat || null,
          dispatch: j.dispatch || null, refresh: j.refresh || null, source: "api" };
      }
    } catch (_) { /* neither */ }
  }
  STATUS.data = data || {};
  const paperData = await Promise.allSettled([
    fetchJson("data/paper_health.json?t=" + Date.now()),
    fetchJson("data/paper.json?t=" + Date.now()),
  ]);
  STATUS.data.paperHealth = paperData[0].status === "fulfilled" ? paperData[0].value : null;
  STATUS.data.paperReport = paperData[1].status === "fulfilled" ? paperData[1].value : null;
  STATUS.loaded = true;
  return STATUS.data;
}

function stageTimingsHtml(timings) {
  const tm = parseMaybeJson(timings);
  if (!tm || typeof tm !== "object") return `<span class="muted">—</span>`;
  const secs = Object.entries(tm)
    .map(([k, v]) => [k, isNum(v) ? Number(v) : (v && isNum(v.seconds) ? Number(v.seconds) : null)])
    .filter(([, v]) => v != null);
  if (!secs.length) return `<span class="muted">—</span>`;
  const max = Math.max(...secs.map(([, v]) => v), 0.001);
  return `<div class="stages">${secs.map(([k, v]) =>
    `<div class="stage"><span class="stage-k">${esc(k)}</span><span class="stage-bar"><i style="width:${Math.max(2, (v / max) * 100).toFixed(0)}%"></i></span><span class="stage-v">${fmtSecs(v)}</span></div>`).join("")}</div>`;
}

function paperScorecardHtml(health, report, meta) {
  if (!health || !report) return `<div class="card"><h3>Weather paper test</h3><p class="muted">Awaiting a successful capture. Paper status unavailable.</p></div>`;
  const age = ageHours(report.last_capture);
  const late = health.status !== "ok" || age == null || age > 12
    || (meta.run_id && health.last_run_id !== meta.run_id);
  const gradeAge = ageHours(report.last_grade);
  const pct = v => isNum(v) ? `${(Number(v) * 100).toFixed(1)}%` : "—";
  const interval = (v, percent = false) => Array.isArray(v) && v.length === 2
    ? `${percent ? pct(v[0]) : fmtNum(v[0], 2)} to ${percent ? pct(v[1]) : fmtNum(v[1], 2)}` : "needs 8 weeks";
  const groups = Object.entries(report.groups || {});
  const table = entries => `<div class="wrap"><table class="kv"><thead><tr><th>Test</th><th>Entries / missed</th><th>Raw CLV (n)</th><th>Excess CLV (n)</th><th>ROI (settled)</th><th>Weeks</th></tr></thead><tbody>${entries.map(([key, stats]) => {
    const [sport, threshold, variant] = key.split("|");
    return `<tr><td>${esc(sport.toUpperCase())} ${esc(threshold)} mph · ${esc(variant)}</td><td>${esc(stats.entries)} / ${esc((report.missed || {})[key] || 0)}</td>`
      + `<td title="95% weekly cluster interval: ${esc(interval(stats.raw_clv_interval))}">${fmtNum(stats.raw_clv, 2)} (${esc(stats.clv_n)})</td>`
      + `<td title="95% weekly cluster interval: ${esc(interval(stats.excess_clv_interval))}">${fmtNum(stats.excess_clv, 2)} (${esc(stats.matched_n)})</td>`
      + `<td title="95% weekly cluster interval: ${esc(interval(stats.roi_interval, true))}">${pct(stats.roi)} (${esc(stats.settled_n)})</td><td>${esc(stats.weeks)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
  const rejected = Object.entries(report.last_capture_rejections || {}).map(([k, n]) => `${k}: ${n}`).join(" · ");
  return `<div class="card"><h3>Weather paper test <span class="pill ${late ? "warn" : "ok"}">${late ? "check capture" : "capturing"}</span></h3>`
    + `<p>Paper only · CFB 12 mph fixed book is primary. 15 mph, NFL and best line are separate secondary tests. No established edge or automatic promotion.</p>`
    + `<p class="sub">Last capture ${esc(report.last_capture ? fmtET(report.last_capture) : "never")} · ${esc(report.capture_runs)} runs · ${esc(report.games)} games. `
    + `Last grade ${esc(report.last_grade ? fmtET(report.last_grade) : "pending first weekly run")}${gradeAge != null && gradeAge > 8 * 24 ? " (overdue)" : ""}. `
    + `${esc(report.missing_closes || 0)} entries missing a fresh close. ${esc((report.capture_gaps || []).length)} known capture gaps. ${esc(health.reason || "")}</p>`
    + table(groups.filter(([k]) => k.endsWith("|fixed")))
    + `<details><summary>Secondary best-line comparisons and capture coverage</summary>${table(groups.filter(([k]) => k.endsWith("|best")))}<p class="sub">${esc(rejected || "No weather-gate rejections in last capture.")}</p><p class="sub">${esc(Object.entries(report.last_quote_rejections || {}).map(([k, n]) => `${k}: ${n}`).join(" · "))}</p>`
    + `<p class="sub">First weather crossing is final; a missing paired quote is a missed opportunity. CLV uses a same-book quote observed within 60 minutes before kickoff. Excess CLV requires every frozen same-time control to have a valid close. Weekly intervals appear after 8 weeks; detailed weekly rows and entry prices are in the scorecard.</p></details>`
    + `<p><a href="data/paper.json" target="_blank" rel="noopener">Open full scorecard JSON</a></p></div>`;
}

function bookCountsHtml(books, runCounts) {
  const names = Object.keys(books || {}).filter((b) => b !== "consensus");
  const rc = parseMaybeJson(runCounts) || {};
  const all = [...new Set([...names, ...Object.keys(rc).filter((k) => typeof rc[k] === "number")])];
  if (!all.length) return `<span class="muted">—</span>`;
  return `<table class="kv"><tr><th>Book</th><th>Lines</th><th>Baseline</th><th>Status</th><th>Last OK</th></tr>${all.map((b) => {
    const bs = (books || {})[b] || {};
    const cnt = bs.count != null ? bs.count : rc[b];
    const st = bs.status || (cnt > 0 ? "green" : "red");
    return `<tr><td>${esc(bookLabel(b))}</td><td>${cnt != null ? esc(cnt) : "—"}</td><td>${bs.baseline != null ? esc(bs.baseline) : "—"}</td>`
      + `<td><span class="chip ${esc(st)}">${esc(st)}</span></td><td>${bs.last_ok ? esc(fmtShortET(bs.last_ok)) : "—"}${bs.reason ? `<span class="sub">${esc(bs.reason)}</span>` : ""}</td></tr>`;
  }).join("")}</table>`;
}

function ecmwfSourceHtml(info) {
  if (!info || typeof info !== "object") return `<span class="muted">Open-Meteo</span>`;
  if (!info.used) {
    const mode = info.mode || "auto";
    return `Open-Meteo <span class="sub">(${esc(mode)}; open data on error, 429, or stale)</span>`;
  }
  const mb = info.bytes ? `${(Number(info.bytes) / 1e6).toFixed(1)} MB` : "";
  const run = info.runs && (info.runs.ifs || info.runs["aifs-single"]);
  return `ECMWF Open Data <span class="sub">CC BY 4.0${mb ? " · " + esc(mb) : ""}${run ? " · " + esc(run) : ""}</span>`;
}

function degradationsHtml(degs) {
  const list = parseMaybeJson(degs);
  if (!Array.isArray(list) || !list.length) return `<div class="banner ok">✓ no degradations</div>`;
  return list.map((d) => {
    const sev = d.severity === "error" || d.severity === "critical" ? "error" : d.severity === "info" ? "info" : "warn";
    return `<div class="banner ${sev}">${sev === "info" ? "ℹ" : "⚠"} <b>${esc(d.component || "pipeline")}</b>: ${esc(d.reason || "")}`
      + `${d.run_id ? ` <span class="sub">${esc(d.run_id)}</span>` : ""}${d.ts ? ` <span class="sub">(${esc(fmtShortET(d.ts))})</span>` : ""}</div>`;
  }).join("");
}

function runsTableHtml(runs) {
  if (!runs.length) return `<div class="empty">no runs recorded yet (D1 runs table empty)</div>`;
  const head = `<tr><th class="left">Run</th><th class="left">Sport</th><th>Wk</th><th class="left">Scope</th><th class="left">Status</th>
    <th class="left">Started (ET)</th><th>Dur</th><th>Games</th><th>Lines</th><th>Alerts</th><th class="left">Degr.</th></tr>`;
  const body = runs.map((r) => {
    const degs = parseMaybeJson(r.degradations_json ?? r.degradations) || [];
    const nDeg = Array.isArray(degs) ? degs.length : 0;
    const ok = (r.status || "ok") === "ok" || r.status === "success";
    return `<tr class="run-row" title="${esc(r.run_id || "")}${r.git_sha ? ` · ${esc(String(r.git_sha).slice(0, 7))}` : ""}">`
      + `<td class="left"><span class="sub">${esc(String(r.run_id || "").slice(0, 16))}</span></td>`
      + `<td class="left">${esc(String(r.sport || "all").toUpperCase())}</td><td>${r.week != null ? esc(r.week) : "—"}</td>`
      + `<td class="left">${esc(r.scope || "—")}</td><td class="left"><span class="pill ${ok ? "ok" : "warn"}">${esc(r.status || "ok")}</span></td>`
      + `<td class="left">${esc(fmtShortET(r.started_at))}</td><td>${fmtSecs(r.duration_s)}</td>`
      + `<td>${r.n_games != null ? esc(r.n_games) : "—"}</td><td>${r.n_lines != null ? esc(r.n_lines) : "—"}</td><td>${r.n_alerts != null ? esc(r.n_alerts) : "—"}</td>`
      + `<td class="left">${nDeg ? `<span class="seg bad">${nDeg}</span>` : `<span class="muted">0</span>`}</td></tr>`;
  }).join("");
  return `<div class="wrap"><table class="runs"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

// meta.book_health = {book: {sport: {rows, last_ok_utc, consecutive_empty, last_error}}}; absent -> null.
function bookHealthFor(meta, book, sport) {
  const bh = meta && meta.book_health;
  const e = bh && typeof bh === "object" && bh[book] && typeof bh[book] === "object" ? bh[book][sport] : null;
  return e && typeof e === "object" ? e : null;
}
function bookHealthHtml(meta) {
  const bh = meta && meta.book_health;
  if (!bh || typeof bh !== "object" || !Object.keys(bh).length) return `<span class="muted">No per-book health reported yet.</span>`;
  const rows = [];
  for (const book of Object.keys(bh).sort()) for (const sport of Object.keys(bh[book] || {}).sort()) {
    const h = bh[book][sport] || {};
    const bad = h.consecutive_empty > 0;
    rows.push(`<tr><td>${esc(bookLabel(book))}</td><td>${esc(String(sport).toUpperCase())}</td><td>${h.rows != null ? esc(h.rows) : "—"}</td>`
      + `<td>${h.last_ok_utc ? esc(fmtShortET(h.last_ok_utc)) : "never"}</td>`
      + `<td>${bad ? `<span class="chip red">${esc(h.consecutive_empty)}</span>` : esc(h.consecutive_empty ?? 0)}</td>`
      + `<td>${h.last_error ? `<span class="sub">${esc(h.last_error)}</span>` : "—"}</td></tr>`);
  }
  if (!rows.length) return `<span class="muted">No per-book health reported yet.</span>`;
  return `<table class="kv"><tr><th>Book</th><th>Sport</th><th>Rows</th><th>Last OK (ET)</th><th>Empty runs</th><th>Last error</th></tr>${rows.join("")}</table>`;
}

async function renderStatus() {
  const host = document.getElementById("statuswrap");
  if (!host) return;
  if (!STATUS.loaded) {
    host.innerHTML = `<div class="empty">loading status…</div>`;
    await loadStatus();
    if (STATE.view !== "status") return;
  }
  const sd = STATUS.data || {};
  const meta = DATA.meta || {};
  const runs = statusRunsOf(sd);
  const latest = runs[0] || {};
  const runId = sd.run_id || meta.run_id || latest.run_id || "—";
  const lastUpdated = sd.last_updated || meta.last_updated || latest.finished_at || null;
  const hb = sd.heartbeat || null;
  const hbTs = heartbeatTs(hb);
  const hbAge = ageHours(hbTs);
  const hbClass = hbAge == null || hbAge > 20 ? "warn" : "ok";
  const dataAge = ageHours(lastUpdated);
  const refresh = sd.refresh || meta.refresh || (QUOTES?.refreshStatus ? QUOTES.refreshStatus(meta, Date.now(), sd.dispatch) : null);
  const refreshLateNow = (refresh && (refresh.overdue || refresh.missed)) || QUOTES?.refreshOverdue(meta);
  const boardMeta = typeof DATA !== "undefined" && DATA && DATA.meta ? DATA.meta : meta;
  const leagueWarnings = leagueWarningList(boardMeta, refresh);
  const dataClass = dataAge == null || dataAge > 20 || refreshLateNow ? "warn" : "ok";
  const timings = sd.stage_timings || latest.stage_timings || latest.stage_timings_json || null;
  const currentMeta = meta.run_id && meta.run_id === (sd.run_id || sd.meta?.run_id);
  const degs = [...((currentMeta ? meta.degradations : sd.degradations)
    || meta.degradations || parseMaybeJson(latest.degradations_json) || [])];
  if (refreshLateNow) degs.push({component: "scheduler", severity: "warn",
    reason: (refresh && refresh.reason) || "Expected refresh is overdue; trigger outcome unknown"});
  for (const warning of leagueWarnings) degs.push({component: "scheduler", severity: "warn",
    reason: warning.reason || `${String(warning.sport || "league").toUpperCase()} board is older than 3h`});
  const unresolved = sd.unresolved_names || meta.unresolved_names || parseMaybeJson(latest.unresolved_json) || [];
  const books = (currentMeta ? meta.books : sd.books) || meta.books || {};
  const unresolvedList = Array.isArray(unresolved) ? unresolved
    : Object.entries(unresolved || {}).flatMap(([bk, names]) => (Array.isArray(names) ? names.map((n) => `${bk}: ${n}`) : []));
  const nextEta = sd.next_run_eta || meta.next_run_eta;

  host.innerHTML = `
    <div class="status-grid">
      <div class="card">
        <h3>Current run</h3>
        ${kv([
          ["Run id", `<span class="sub">${esc(runId)}</span>`],
          ["Published", lastUpdated ? `${esc(fmtET(lastUpdated))} <span class="pill ${dataClass}">${esc(ageLabel(lastUpdated))}</span>` : "—"],
          ["Season / week", `${esc(sd.season ?? meta.season ?? "—")} · wk ${esc(sd.week ?? meta.week ?? "—")}`],
          ["Git", `<span class="sub">${esc(String(sd.git_sha || meta.git_sha || "—").slice(0, 7))}</span>`],
          ["Model", esc(sd.model_version || meta.model_version || "—")],
          ["ECMWF", ecmwfSourceHtml(meta.ecmwf_opendata || sd.ecmwf_opendata || sd.meta?.ecmwf_opendata)],
          ["Next run", nextEta ? esc(fmtET(nextEta)) : "—"],
          ["Refresh", refreshLateNow
            ? `<span class="pill warn">${refresh && refresh.missed ? "Missed" : "Overdue"}</span> <span class="sub">${esc((refresh && refresh.reason) || "Expected refresh is overdue; trigger outcome unknown")}</span>`
            : `<span class="pill ok">On time</span>`],
          ["League boards", leagueWarnings.length
            ? leagueWarnings.map((warning) => `<span class="pill warn">${esc(warning.reason || warning.sport)}</span>`).join(" ")
            : `<span class="pill ok">No league board older than 3h</span>`],
          ["Scope", esc(latest.scope || sd.scope || "—")],
          ["Duration", fmtSecs(latest.duration_s ?? sd.duration_s)],
        ])}
      </div>
      <div class="card">
        <h3>Scheduler heartbeat</h3>
        ${kv([
          ["CF Worker tick", hbTs ? `${esc(fmtET(hbTs))} <span class="pill ${hbClass}">${esc(ageLabel(hbTs))}</span>` : `<span class="pill warn">no heartbeat (cf_heartbeat.json missing)</span>`],
          ["Last cron", esc((hb && (hb.cron || hb.last_cron)) || "—")],
          ["Last dispatch", sd.dispatch ? `${esc(fmtET(sd.dispatch.ts))} · ${esc(sd.dispatch.reason || "unknown")}` : "Unknown (no receipt)"],
          ["Worker plan", esc((sd.dispatch?.plan && `${sd.dispatch.plan.sport || ""}/${sd.dispatch.plan.scope || ""}`) || "—")],
          ["Stale rule", `<span class="sub">refresh overdue after expected run + 90 min; heartbeat stale after 20 h</span>`],
        ])}
        <h3>Stage timings</h3>
        ${stageTimingsHtml(timings)}
      </div>
    </div>
    ${paperScorecardHtml(sd.paperHealth, sd.paperReport, meta)}
    <h3>Degradations</h3>
    <div class="banners static">${degradationsHtml(degs)}</div>
    <div class="status-grid">
      <div class="card"><h3>Books vs baseline</h3>${bookCountsHtml(books, latest.counts_json ?? latest.counts ?? sd.counts)}</div>
      <div class="card"><h3>Book health by sport</h3>${bookHealthHtml(meta)}</div>
      <div class="card"><h3>Unresolved names <span class="sub">(${unresolvedList.length})</span></h3>
        ${unresolvedList.length ? `<div class="names">${unresolvedList.map((n) => `<span class="name">${esc(n)}</span>`).join("")}</div>` : `<span class="muted">none</span>`}
      </div>
    </div>
    <h3>Last ${runs.length || 20} runs${sd.source === "api" ? ` <span class="sub">(via /api/status)</span>` : ""}</h3>
    ${runsTableHtml(runs)}
    <div class="sub status-foot"><button class="controlbtn" id="st-reload" type="button">↻ reload</button></div>`;
  document.getElementById("st-reload").addEventListener("click", async () => { await loadStatus(true); renderStatus(); });
}
