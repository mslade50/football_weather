"use strict";
// Header: sport/week label, "Updated HH:MM ET (viewer tz) · next run ~HH:MM", book chips
// from meta.books, degradation banners, run-health statusbar.

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
    const local = fmtLocal(meta.last_updated);
    upd.textContent = `Updated ${et}` + (local && !et.includes(local) ? ` (${local})` : "");
    upd.title = `run_id ${meta.run_id || "?"}` + (meta.git_sha ? ` · ${String(meta.git_sha).slice(0, 7)}` : "")
      + (meta.model_version ? ` · model ${meta.model_version}` : "");
  } else {
    upd.textContent = "";
  }
  const nr = document.getElementById("nextrun");
  nr.textContent = meta.next_run_eta ? `· next run ~${fmtET(meta.next_run_eta)}` : "";

  renderBookChips(meta.books || {});
}

function renderBookChips(books) {
  const el = document.getElementById("bookchips");
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

function renderBanners(meta) {
  const el = document.getElementById("banners");
  const degs = (meta.degradations || []).filter((d) => d && (d.severity || "warn") !== "info");
  el.innerHTML = degs.map((d) => {
    const sev = d.severity === "error" || d.severity === "critical" ? "error" : "warn";
    return `<div class="banner ${sev}">⚠ <b>${esc(d.component || "pipeline")}</b>: ${esc(d.reason || "")}${d.ts ? ` <span class="sub">(${esc(fmtShortET(d.ts))})</span>` : ""}</div>`;
  }).join("");
}

// Run health: books reporting vs expected, counts vs baseline, unresolved names.
function renderStatusbar(meta) {
  const el = document.getElementById("statusbar");
  if (!el) return;
  const books = meta.books || {};
  const names = Object.keys(books).filter((b) => b !== "consensus");
  if (!names.length && !(meta.degradations || []).length) { el.innerHTML = ""; return; }
  const red = names.filter((b) => (books[b].status || "green") === "red");
  const amber = names.filter((b) => books[b].status === "amber");
  const ok = !red.length && !(meta.degradations || []).some((d) => d.severity === "error" || d.severity === "critical");
  const pill = ok ? '<span class="pill ok">✓ OK</span>' : '<span class="pill warn">⚠ Degraded</span>';
  const segs = [];
  if (names.length) segs.push(`<span class="seg">Books <b>${names.length - red.length}/${names.length}</b> reporting</span>`);
  if (red.length) segs.push(`<span class="seg bad">Dark: <b>${red.map(bookLabel).join(", ")}</b></span>`);
  if (amber.length) segs.push(`<span class="seg">Thin: <b>${amber.map(bookLabel).join(", ")}</b></span>`);
  const unresolved = meta.unresolved_names || [];
  if (unresolved.length) segs.push(`<span class="seg" title="${esc(unresolved.join(", "))}">Unresolved names <b>${unresolved.length}</b></span>`);
  if (meta.model_version) segs.push(`<span class="seg">Model <b>${esc(meta.model_version)}</b></span>`);
  el.innerHTML = pill + segs.join('<span class="sep">·</span>');
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
  if (!data || (!statusRunsOf(data).length && !data.run_id)) {
    try {
      const j = await fetchJson("api/status");
      if (j && j.ok) {
        const m = j.meta || {};
        data = { ...(data || {}), run_id: m.run_id, last_updated: m.last_updated, git_sha: m.git_sha, next_run_eta: m.next_run_eta,
          degradations: m.degradations || [], books: m.books || {}, runs: j.runs || [], heartbeat: j.heartbeat || null, source: "api" };
      }
    } catch (_) { /* neither */ }
  }
  STATUS.data = data || {};
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
      + `<td><span class="chip ${esc(st)}">${esc(st)}</span></td><td>${bs.last_ok ? esc(fmtShortET(bs.last_ok)) : "—"}</td></tr>`;
  }).join("")}</table>`;
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
  const dataClass = dataAge == null || dataAge > 20 ? "warn" : "ok";
  const timings = sd.stage_timings || latest.stage_timings || latest.stage_timings_json || null;
  const degs = (Array.isArray(sd.degradations) && sd.degradations.length ? sd.degradations : null)
    || meta.degradations || parseMaybeJson(latest.degradations_json) || [];
  const unresolved = sd.unresolved_names || meta.unresolved_names || parseMaybeJson(latest.unresolved_json) || [];
  const books = (sd.books && Object.keys(sd.books).length ? sd.books : null) || meta.books || {};
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
          ["Next run", nextEta ? esc(fmtET(nextEta)) : "—"],
          ["Scope", esc(latest.scope || sd.scope || "—")],
          ["Duration", fmtSecs(latest.duration_s ?? sd.duration_s)],
        ])}
      </div>
      <div class="card">
        <h3>Scheduler heartbeat</h3>
        ${kv([
          ["CF Worker tick", hbTs ? `${esc(fmtET(hbTs))} <span class="pill ${hbClass}">${esc(ageLabel(hbTs))}</span>` : `<span class="pill warn">no heartbeat (cf_heartbeat.json missing)</span>`],
          ["Last cron", esc((hb && (hb.cron || hb.last_cron)) || "—")],
          ["Last dispatch", hb && hb.dispatched != null ? esc(String(hb.dispatched)) : "—"],
          ["Worker plan", esc((hb && hb.plan && `${hb.plan.sport || ""}/${hb.plan.scope || ""}`) || "—")],
          ["Stale rule", `<span class="sub">alert when meta or heartbeat > 20 h</span>`],
        ])}
        <h3>Stage timings</h3>
        ${stageTimingsHtml(timings)}
      </div>
    </div>
    <h3>Degradations</h3>
    <div class="banners static">${degradationsHtml(degs)}</div>
    <div class="status-grid">
      <div class="card"><h3>Books vs baseline</h3>${bookCountsHtml(books, latest.counts_json ?? latest.counts ?? sd.counts)}</div>
      <div class="card"><h3>Unresolved names <span class="sub">(${unresolvedList.length})</span></h3>
        ${unresolvedList.length ? `<div class="names">${unresolvedList.map((n) => `<span class="name">${esc(n)}</span>`).join("")}</div>` : `<span class="muted">none</span>`}
      </div>
    </div>
    <h3>Last ${runs.length || 20} runs${sd.source === "api" ? ` <span class="sub">(via /api/status)</span>` : ""}</h3>
    ${runsTableHtml(runs)}
    <div class="sub status-foot"><button class="controlbtn" id="st-reload" type="button">↻ reload</button></div>`;
  document.getElementById("st-reload").addEventListener("click", async () => { await loadStatus(true); renderStatus(); });
}
