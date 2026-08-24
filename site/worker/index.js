// Private football weather/odds board Worker (adapted from golf_scraping/board/worker).
// - /data/<name>.json  -> proxied from R2 (board/<name>.json), no-store
// - /api/*             -> read-only D1 queries (history, wx, alerts, runs, status)
// - /refresh           -> admin-only workflow_dispatch of pipeline.yml
// - /auth/me           -> {role}
// - everything else    -> static assets (site/web) after the Basic Auth gate
// scheduled(): CF cron -> heartbeat to R2, then cron -> {sport, scope} via
// CRON_PLAN with America/New_York trimming, then dispatch pipeline.yml.

const DATA_PREFIX = "/data/";
const API_PREFIX = "/api/";
const AUTH_REALM = "football-board";
const SPORTS = new Set(["nfl", "cfb", "all"]);
const SCOPES = new Set(["weather", "light", "full"]);
const HISTORY_ROW_CAP = 2000;
const RUNS_DEFAULT_LIMIT = 20;
const RUNS_MAX_LIMIT = 100;

// ── cron plan ────────────────────────────────────────────────────────────────
// Every expression here must appear verbatim in wrangler.toml `crons` (or be one
// of the paid-plan candidates). A plan entry receives the ET parts of the fire
// time and returns {sport, scope} to dispatch, or null to trim (no dispatch).
// Quartz DOW in the cron strings is 1=Sun..7=Sat; `p.weekday` here is the ET
// short weekday name so DST / UTC-day spillover never leaks into the decision.
export const HEARTBEAT_CRON = "*/30 * * * *";
export const MIDDAY_CRON = "15 17 * * *";

// Football season by ET month (1-12). CFB: late Aug .. mid Jan (bowls/CFP);
// NFL: Aug (preseason) .. Feb (Super Bowl). pipeline.gate_check does the fine
// (kickoff within 10 days) gating, so month-level trimming is enough here.
export function sportForMonth(month) {
  const m = Number(month);
  if (m === 2) return "nfl";
  if (m >= 8 || m === 1) return "all";
  return null;
}

// Paid-plan expressions (ARCH §9.1) with in-handler ET trimming. Free-plan
// entries first; expand wrangler.toml with PAID_CRONS on Workers Paid.
export const CRON_PLAN = {
  [HEARTBEAT_CRON]: () => null,
  [MIDDAY_CRON]: (p) => {
    const sport = sportForMonth(p.month);
    return sport ? { sport, scope: "full" } : null;
  },
  // Tue/Wed 10:00 + 16:00 ET-ish (openers) -> light
  "0 14,20 * * 3,4": (p) => {
    if (!["Tue", "Wed"].includes(p.weekday)) return null;
    const sport = sportForMonth(p.month);
    return sport ? { sport, scope: "light" } : null;
  },
  // Thu/Fri every 2h 08:00-22:00 ET -> light, full at 12 and 18 ET
  "0 0,2,12,14,16,18,20,22 * * 5,6,7": (p) => {
    if (!["Thu", "Fri"].includes(p.weekday)) return null;
    if (p.hour < 8 || p.hour > 22 || p.hour % 2 !== 0) return null;
    const sport = sportForMonth(p.month);
    if (!sport) return null;
    return { sport, scope: p.hour === 12 || p.hour === 18 ? "full" : "light" };
  },
  // Sat hourly 06:00-21:00 ET (CFB) -> light, full at 10 and 14 ET
  "0 0,1,2,10,11,12,13,14,15,16,17,18,19,20,21,22,23 * * 7,1": (p) => {
    if (p.weekday !== "Sat" || p.hour < 6 || p.hour > 21) return null;
    const sport = sportForMonth(p.month);
    if (!sport || sport === "nfl") return null;
    return { sport: "cfb", scope: p.hour === 10 || p.hour === 14 ? "full" : "light" };
  },
  // Sun hourly 06:00-17:00 ET (NFL) -> light
  "0 10,11,12,13,14,15,16,17,18,19,20,21 * * 1": (p) => {
    if (p.weekday !== "Sun" || p.hour < 6 || p.hour > 17) return null;
    return sportForMonth(p.month) ? { sport: "nfl", scope: "light" } : null;
  },
  // Sun pre-kickoff 12:30 / 15:30 / 19:30 ET -> full
  "30 16,19,23 * * 1": (p) => {
    if (p.weekday !== "Sun" || ![12, 15, 19].includes(p.hour) || p.minute !== 30) return null;
    return sportForMonth(p.month) ? { sport: "nfl", scope: "full" } : null;
  },
  // Mon/Thu night 18:00 + 19:00 ET -> light
  "0 22,23 * * 2,5": (p) => {
    if (!["Mon", "Thu"].includes(p.weekday) || ![18, 19].includes(p.hour)) return null;
    const sport = sportForMonth(p.month);
    return sport ? { sport, scope: "light" } : null;
  },
};
export const PAID_CRONS = Object.keys(CRON_PLAN).filter((c) => c !== HEARTBEAT_CRON);

// ET calendar parts of a Date: {weekday:'Sun'.., month:1-12, day, hour:0-23, minute}.
export function etParts(date) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map((part) => [part.type, part.value]));
  return {
    weekday: parts.weekday,
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour) % 24,
    minute: Number(parts.minute),
  };
}

// cron string + fire time -> {sport, scope} | null (trimmed / unknown cron).
export function resolveCron(cron, date) {
  const plan = CRON_PLAN[cron];
  if (!plan) return null;
  return plan(etParts(date)) || null;
}

// ── request helpers ──────────────────────────────────────────────────────────
// /data/<name>.json -> R2 key name; empty string when the name is unsafe.
export function sanitizeDataName(pathname) {
  if (!pathname.startsWith(DATA_PREFIX)) return "";
  const name = pathname.slice(DATA_PREFIX.length).replace(/[^a-zA-Z0-9._-]/g, "");
  if (!name.endsWith(".json") || name === ".json" || name.includes("..")) return "";
  return name;
}

export function boardIdentity(request, env) {
  const viewerSecret = String(env.BOARD_PASSWORD || "");
  const adminSecret = String(env.BOARD_ADMIN_PASSWORD || "");
  const adminUsername = String(env.BOARD_ADMIN_USERNAME || "mslade");
  const auth = request.headers.get("Authorization") || "";
  let username = "", password = "";
  if (auth.startsWith("Basic ")) {
    try {
      const decoded = atob(auth.slice(6));
      const split = decoded.indexOf(":");
      if (split >= 0) {
        username = decoded.slice(0, split);
        password = decoded.slice(split + 1);
      }
    } catch {
      // Malformed credentials fail closed below.
    }
  }
  const admin = !!adminSecret && adminSecret !== viewerSecret
    && constantTimeEqual(username, adminUsername)
    && constantTimeEqual(password, adminSecret);
  const viewer = !!viewerSecret && constantTimeEqual(password, viewerSecret);
  // Local/dev: neither secret configured -> open, but never admin.
  const openDev = !viewerSecret && !adminSecret;
  return {
    authenticated: admin || viewer || openDev,
    username: username || (openDev ? "local" : ""),
    role: admin ? "admin" : "viewer",
  };
}

export function constantTimeEqual(a, b) {
  const aa = new TextEncoder().encode(String(a));
  const bb = new TextEncoder().encode(String(b));
  let diff = aa.length ^ bb.length;
  const n = Math.max(aa.length, bb.length);
  for (let i = 0; i < n; i++) diff |= (aa[i % (aa.length || 1)] || 0) ^ (bb[i % (bb.length || 1)] || 0);
  return diff === 0;
}

export function jsonResponse(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extraHeaders },
  });
}

function unauthorized() {
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": `Basic realm="${AUTH_REALM}"` },
  });
}

function methodNotAllowed(allow) {
  return jsonResponse({ ok: false, error: "method not allowed" }, 405, { Allow: allow });
}

// Bounded, character-restricted query params (ids are `nfl:2026:3:sea@ne`,
// keys are `[a-z0-9_]`). Returns "" when absent/invalid so callers can 400.
function param(url, name, re, max = 80) {
  const v = (url.searchParams.get(name) || "").trim();
  if (!v || v.length > max || !re.test(v)) return "";
  return v;
}
const RE_GAME_ID = /^[a-z]{3}:\d{4}:\d{1,2}:[a-z0-9_.-]+@[a-z0-9_.-]+$/i;
const RE_TOKEN = /^[a-z0-9_]+$/i;
const RE_INT = /^\d{1,6}$/;

function clampInt(raw, dflt, max) {
  if (!RE_INT.test(raw || "")) return dflt;
  return Math.max(1, Math.min(max, Number(raw)));
}

// ── D1 routes ────────────────────────────────────────────────────────────────
async function apiRoute(url, request, env, identity) {
  if (request.method !== "GET") return methodNotAllowed("GET");
  if (!env.DB) return jsonResponse({ ok: false, error: "D1 not configured" }, 503);
  const path = url.pathname.slice(API_PREFIX.length);
  try {
    if (path === "history") {
      const gameId = param(url, "game_id", RE_GAME_ID);
      if (!gameId) return jsonResponse({ ok: false, error: "game_id required" }, 400);
      const market = param(url, "market", RE_TOKEN, 16);
      const book = param(url, "book", RE_TOKEN, 32);
      let sql = `SELECT scraped_at, game_id, book, market, side, line, odds, prob, fair_line,
                        fair_prob, edge_pts, edge_prob, is_main, run_id
                 FROM odds_history WHERE game_id = ?1`;
      const binds = [gameId];
      if (market) { binds.push(market); sql += ` AND market = ?${binds.length}`; }
      if (book) { binds.push(book); sql += ` AND book = ?${binds.length}`; }
      sql += ` ORDER BY scraped_at ASC LIMIT ${HISTORY_ROW_CAP}`;
      const res = await env.DB.prepare(sql).bind(...binds).all();
      return jsonResponse({ ok: true, game_id: gameId, rows: res.results || [] });
    }
    if (path === "wx") {
      const gameId = param(url, "game_id", RE_GAME_ID);
      if (!gameId) return jsonResponse({ ok: false, error: "game_id required" }, 400);
      const res = await env.DB.prepare(
        `SELECT * FROM weather_history WHERE game_id = ?1 ORDER BY fetched_at ASC LIMIT ${HISTORY_ROW_CAP}`,
      ).bind(gameId).all();
      return jsonResponse({ ok: true, game_id: gameId, rows: res.results || [] });
    }
    if (path === "alerts") {
      const sport = param(url, "sport", /^(nfl|cfb)$/);
      const season = param(url, "season", /^\d{4}$/);
      const week = param(url, "week", /^\d{1,2}$/);
      const where = [];
      const binds = [];
      if (sport) { binds.push(sport); where.push(`sport = ?${binds.length}`); }
      if (season) { binds.push(Number(season)); where.push(`season = ?${binds.length}`); }
      if (week) { binds.push(Number(week)); where.push(`week = ?${binds.length}`); }
      const sql = `SELECT * FROM alerts${where.length ? ` WHERE ${where.join(" AND ")}` : ""}
                   ORDER BY last_sent_at DESC LIMIT 500`;
      const res = await env.DB.prepare(sql).bind(...binds).all();
      return jsonResponse({ ok: true, rows: res.results || [] });
    }
    if (path === "runs") {
      const limit = clampInt(url.searchParams.get("limit"), RUNS_DEFAULT_LIMIT, RUNS_MAX_LIMIT);
      const res = await env.DB.prepare(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?1",
      ).bind(limit).all();
      return jsonResponse({ ok: true, rows: res.results || [] });
    }
    if (path === "status") {
      const runs = await env.DB.prepare(
        `SELECT run_id, sport, season, week, scope, status, started_at, finished_at, duration_s,
                n_games, n_lines, n_alerts, degradations_json
         FROM runs ORDER BY started_at DESC LIMIT ${RUNS_DEFAULT_LIMIT}`,
      ).all();
      const heartbeat = await readR2Json(env, "board/cf_heartbeat.json");
      const meta = await readR2Json(env, "board/meta.json");
      return jsonResponse({
        ok: true,
        role: identity.role,
        heartbeat,
        meta: meta ? {
          run_id: meta.run_id, last_updated: meta.last_updated, season: meta.season, week: meta.week,
          git_sha: meta.git_sha, next_run_eta: meta.next_run_eta,
          degradations: meta.degradations || [], books: meta.books || {},
        } : null,
        runs: runs.results || [],
      });
    }
    return jsonResponse({ ok: false, error: "not found" }, 404);
  } catch (err) {
    console.log(`api/${path} query failed: ${err}`);
    return jsonResponse({ ok: false, error: "query failed" }, 502);
  }
}

async function readR2Json(env, key) {
  try {
    const obj = await env.ODDS.get(key);
    return obj ? await obj.json() : null;
  } catch {
    return null;
  }
}

// ── refresh ──────────────────────────────────────────────────────────────────
async function refreshRoute(url, request, env, identity) {
  if (request.method !== "POST") return methodNotAllowed("POST");
  if (identity.role !== "admin") {
    return jsonResponse({ ok: false, error: "refresh is restricted to the admin login" }, 403);
  }
  if (!env.GH_DISPATCH_TOKEN) {
    return jsonResponse({ ok: false, error: "refresh not configured (GH_DISPATCH_TOKEN unset)" }, 503);
  }
  // CSRF guard: browsers replay cached Basic Auth on cross-site form POSTs, so
  // only accept JSON bodies (a non-simple content type forces a CORS preflight
  // this Worker never answers). app.js always posts application/json.
  const ct = (request.headers.get("content-type") || "").toLowerCase();
  if (!ct.startsWith("application/json")) {
    return jsonResponse({ ok: false, error: "refresh requires a JSON body (content-type: application/json)" }, 415);
  }
  let body = {};
  try {
    const raw = await request.text();
    body = raw.trim() ? (JSON.parse(raw) || {}) : {};
    if (typeof body !== "object" || Array.isArray(body)) body = {};
  } catch {
    return jsonResponse({ ok: false, error: "invalid JSON body" }, 400);
  }
  const sport = String(body.sport || url.searchParams.get("sport") || "all");
  const scope = String(body.scope || url.searchParams.get("scope") || "light");
  const force = body.force === true || url.searchParams.get("force") === "1";
  if (!SPORTS.has(sport) || !SCOPES.has(scope)) {
    return jsonResponse({ ok: false, error: "sport must be nfl|cfb|all and scope weather|light|full" }, 400);
  }
  // Forced refreshes skip the dedup (an active run is usually a ~20s gate-skip);
  // pipeline.yml's concurrency group queues rather than races.
  if (!force && (await boardRunActive(env))) {
    return jsonResponse({ ok: true, already_running: true, sport, scope });
  }
  const r = await dispatchBoard(env, { sport, scope, force });
  return r.ok
    ? jsonResponse({ ok: true, sport, scope, force }, 202)
    : jsonResponse({ ok: false, status: r.status, error: r.detail || "dispatch failed" }, 502);
}

// ── GitHub dispatch (never throws) ──────────────────────────────────────────
function ghRepo(env) { return String(env.GH_REPO || "mslade50/football_weather"); }
function ghWorkflow(env) { return String(env.GH_WORKFLOW || "pipeline.yml"); }

export async function dispatchBoard(env, { sport = "all", scope = "light", force = false } = {}, fetchImpl = fetch) {
  let status = 0;
  let detail = "";
  try {
    // The dispatch API takes inputs as strings (same as `gh workflow run -f`).
    const body = { ref: String(env.GH_REF || "main"), inputs: { sport, scope } };
    if (force) body.inputs.force = "true";
    const resp = await fetchImpl(
      `https://api.github.com/repos/${ghRepo(env)}/actions/workflows/${ghWorkflow(env)}/dispatches`,
      { method: "POST", headers: ghHeaders(env), body: JSON.stringify(body) },
    );
    status = resp.status;
    if (!resp.ok) detail = (await resp.text()).slice(0, 300);
  } catch (err) {
    detail = String(err);
  }
  return { ok: status >= 200 && status < 300, status, detail };
}

// Fails OPEN (false) if the check errors — a missed dedup is cheaper than
// dropping a refresh the user explicitly asked for.
export async function boardRunActive(env, fetchImpl = fetch) {
  try {
    const resp = await fetchImpl(
      `https://api.github.com/repos/${ghRepo(env)}/actions/workflows/${ghWorkflow(env)}/runs?per_page=8`,
      { headers: ghHeaders(env) },
    );
    if (!resp.ok) return false;
    const data = await resp.json();
    const ACTIVE = new Set(["queued", "in_progress", "requested", "waiting", "pending"]);
    return (data.workflow_runs || []).some((r) => ACTIVE.has(r.status));
  } catch {
    return false;
  }
}

function ghHeaders(env) {
  return {
    "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "football-board-cron",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

// Best-effort Telegram alert; no-ops if creds aren't set.
export async function notifyTelegram(env, text, fetchImpl = fetch) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  try {
    await fetchImpl(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
    });
  } catch (err) {
    console.log(`notifyTelegram failed: ${err}`);
  }
}

// ── handlers ─────────────────────────────────────────────────────────────────
export async function handleFetch(request, env) {
  const url = new URL(request.url);

  const identity = boardIdentity(request, env);
  if (!identity.authenticated) return unauthorized();

  if (url.pathname === "/auth/me") {
    if (request.method !== "GET") return methodNotAllowed("GET");
    return jsonResponse({
      ok: true,
      username: identity.username,
      role: identity.role,
      can_refresh: identity.role === "admin",
    });
  }

  if (url.pathname === "/refresh") return refreshRoute(url, request, env, identity);

  if (url.pathname.startsWith(API_PREFIX)) return apiRoute(url, request, env, identity);

  if (url.pathname.startsWith(DATA_PREFIX)) {
    if (request.method !== "GET") return methodNotAllowed("GET");
    const name = sanitizeDataName(url.pathname);
    if (!name) return new Response("not found", { status: 404 });
    const obj = await env.ODDS.get(`board/${name}`);
    if (!obj) return new Response("not found", { status: 404 });
    return new Response(obj.body, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  }

  // Static frontend. With assets.html_handling="none", rewrite the bare homepage
  // only after the auth gate above has accepted the request.
  if (url.pathname === "/") {
    const assetUrl = new URL(request.url);
    assetUrl.pathname = "/index.html";
    return env.ASSETS.fetch(new Request(assetUrl.toString(), request));
  }
  return env.ASSETS.fetch(request);
}

export async function handleScheduled(event, env) {
  const fired = new Date(event.scheduledTime || Date.now());
  // Heartbeat FIRST, before any trim/token/dispatch check: pipeline.build reads
  // this from R2 and alerts when it goes >20h stale — the only signal that the
  // CF cron triggers themselves died.
  try {
    await env.ODDS.put("board/cf_heartbeat.json", JSON.stringify({
      schema_version: 1,
      ts: fired.toISOString().replace("T", " ").slice(0, 19) + " UTC",
      cron: event.cron,
    }), { httpMetadata: { contentType: "application/json" } });
  } catch (err) {
    console.log(`heartbeat write failed: ${err}`);
  }

  const plan = resolveCron(event.cron, fired);
  if (!plan) {
    console.log(`scheduled: trimmed ${event.cron} at ${fired.toISOString()} (no dispatch)`);
    return { dispatched: false, trimmed: true };
  }
  if (!env.GH_DISPATCH_TOKEN) {
    console.log("scheduled: GH_DISPATCH_TOKEN not set — skipping pipeline dispatch");
    await notifyTelegram(env,
      "⚠️ Football board cron fired but GH_DISPATCH_TOKEN is unset — no pipeline runs "
      + "are being dispatched. `wrangler secret put GH_DISPATCH_TOKEN` to fix.");
    return { dispatched: false, trimmed: false, plan };
  }
  const { ok, status, detail } = await dispatchBoard(env, plan);
  console.log(`scheduled: pipeline.yml dispatch ${plan.sport}/${plan.scope} -> ${status || "exception"}`
    + (ok ? "" : ` ${detail}`));
  if (!ok) {
    await notifyTelegram(env,
      "⚠️ Football board cron FAILED to dispatch pipeline.yml"
      + `\ncron: ${event.cron} (${plan.sport}/${plan.scope})`
      + `\nstatus: ${status || "exception"}`
      + `\n${detail || "(no detail)"}`);
  }
  return { dispatched: ok, trimmed: false, plan, status };
}

export default {
  fetch: (request, env) => handleFetch(request, env),
  scheduled: (event, env, ctx) => ctx.waitUntil(handleScheduled(event, env)),
};
