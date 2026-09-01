// node --test "site/worker/test/*.test.mjs"
import { test } from "node:test";
import assert from "node:assert/strict";

import worker, {
  boardIdentity,
  constantTimeEqual,
  sanitizeDataName,
  sportForMonth,
  etParts,
  resolveCron,
  CRON_PLAN,
  PAID_CRONS,
  HEARTBEAT_CRON,
  MIDDAY_CRON,
  dispatchBoard,
  handleFetch,
  handleScheduled,
} from "../index.js";

const basic = (user, pass) => ({ Authorization: `Basic ${Buffer.from(`${user}:${pass}`).toString("base64")}` });
const req = (path, init = {}) => new Request(`https://football-board.example.workers.dev${path}`, init);

function fakeEnv(overrides = {}) {
  const r2 = new Map();
  const d1calls = [];
  return {
    BOARD_PASSWORD: "viewer-pw",
    BOARD_ADMIN_USERNAME: "mslade",
    BOARD_ADMIN_PASSWORD: "admin-pw",
    ODDS: {
      async get(key) {
        if (!r2.has(key)) return null;
        const body = r2.get(key);
        return { body, json: async () => JSON.parse(body) };
      },
      async put(key, body) { r2.set(key, body); },
      _store: r2,
    },
    DB: {
      prepare(sql) {
        const call = { sql, binds: [] };
        d1calls.push(call);
        const stmt = {
          bind(...b) { call.binds = b; return stmt; },
          async all() { return { results: [{ sql_seen: true }] }; },
          async first() { return null; },
        };
        return stmt;
      },
      _calls: d1calls,
    },
    ASSETS: { fetch: async (r) => new Response(`asset:${new URL(r.url).pathname}`) },
    ...overrides,
  };
}

// ── auth ─────────────────────────────────────────────────────────────────────
test("boardIdentity: viewer password with any username -> viewer", () => {
  const env = fakeEnv();
  const id = boardIdentity(req("/", { headers: basic("anyone", "viewer-pw") }), env);
  assert.equal(id.authenticated, true);
  assert.equal(id.role, "viewer");
});

test("boardIdentity: admin requires exact username + distinct admin password", () => {
  const env = fakeEnv();
  assert.equal(boardIdentity(req("/", { headers: basic("mslade", "admin-pw") }), env).role, "admin");
  assert.equal(boardIdentity(req("/", { headers: basic("other", "admin-pw") }), env).authenticated, false);
  assert.equal(boardIdentity(req("/", { headers: basic("mslade", "viewer-pw") }), env).role, "viewer");
  // admin password equal to viewer password never grants admin
  const same = fakeEnv({ BOARD_ADMIN_PASSWORD: "viewer-pw" });
  assert.equal(boardIdentity(req("/", { headers: basic("mslade", "viewer-pw") }), same).role, "viewer");
});

test("boardIdentity: missing/malformed credentials fail closed; open when unconfigured", () => {
  const env = fakeEnv();
  assert.equal(boardIdentity(req("/"), env).authenticated, false);
  assert.equal(boardIdentity(req("/", { headers: { Authorization: "Basic !!!notbase64" } }), env).authenticated, false);
  assert.equal(boardIdentity(req("/", { headers: basic("x", "wrong") }), env).authenticated, false);
  const open = boardIdentity(req("/"), fakeEnv({ BOARD_PASSWORD: "", BOARD_ADMIN_PASSWORD: "" }));
  assert.equal(open.authenticated, true);
  assert.equal(open.role, "viewer");
});

test("constantTimeEqual", () => {
  assert.equal(constantTimeEqual("abc", "abc"), true);
  assert.equal(constantTimeEqual("abc", "abd"), false);
  assert.equal(constantTimeEqual("abc", "abcd"), false);
  assert.equal(constantTimeEqual("", ""), true);
});

test("fetch: 401 with football-board realm when unauthenticated", async () => {
  const res = await worker.fetch(req("/"), fakeEnv());
  assert.equal(res.status, 401);
  assert.equal(res.headers.get("WWW-Authenticate"), 'Basic realm="football-board"');
});

test("fetch: /auth/me reports role", async () => {
  const res = await handleFetch(req("/auth/me", { headers: basic("mslade", "admin-pw") }), fakeEnv());
  const body = await res.json();
  assert.equal(res.status, 200);
  assert.equal(body.role, "admin");
  assert.equal(body.can_refresh, true);
});

// ── /data sanitize ───────────────────────────────────────────────────────────
test("sanitizeDataName strips unsafe chars and requires .json", () => {
  assert.equal(sanitizeDataName("/data/games_nfl.json"), "games_nfl.json");
  assert.equal(sanitizeDataName("/data/meta.json1"), "");
  assert.equal(sanitizeDataName("/data/games_nfl.json"), "games_nfl.json");
  assert.equal(sanitizeDataName("/data/../secret.json"), "");
  assert.equal(sanitizeDataName("/data/board.csv"), "");
  assert.equal(sanitizeDataName("/data/.json"), "");
  assert.equal(sanitizeDataName("/data/a/b.json"), "ab.json");
});

test("fetch: /data proxies R2 board/<name> with no-store; 404 otherwise", async () => {
  const env = fakeEnv();
  env.ODDS._store.set("board/meta.json", '{"run_id":"r1"}');
  const ok = await handleFetch(req("/data/meta.json?bust=1", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("cache-control"), "no-store");
  assert.equal(await ok.text(), '{"run_id":"r1"}');
  const miss = await handleFetch(req("/data/nope.json", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(miss.status, 404);
  const bad = await handleFetch(req("/data/meta.txt", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(bad.status, 404);
});

test("fetch: / rewrites to /index.html after auth; other paths pass to ASSETS", async () => {
  const env = fakeEnv();
  const home = await handleFetch(req("/", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(await home.text(), "asset:/index.html");
  const js = await handleFetch(req("/app.js", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(await js.text(), "asset:/app.js");
});

// ── /api ─────────────────────────────────────────────────────────────────────
test("api/history validates game_id and binds market/book filters", async () => {
  const env = fakeEnv();
  const bad = await handleFetch(req("/api/history?game_id=DROP%20TABLE", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(bad.status, 400);
  const res = await handleFetch(
    req("/api/history?game_id=nfl:2026:3:sea@ne&market=total&book=betonline", { headers: basic("a", "viewer-pw") }), env,
  );
  assert.equal(res.status, 200);
  const call = env.DB._calls.at(-1);
  assert.match(call.sql, /FROM odds_history WHERE game_id = \?1/);
  assert.deepEqual(call.binds, ["nfl:2026:3:sea@ne", "total", "betonline"]);
  assert.match(call.sql, /LIMIT 2000/);
});

test("api/runs clamps limit; api/alerts filters; api/status includes heartbeat", async () => {
  const env = fakeEnv();
  env.ODDS._store.set("board/cf_heartbeat.json", '{"ts":"2026-09-01 12:00:00 UTC","cron":"*/30 * * * *"}');
  const runs = await handleFetch(req("/api/runs?limit=9999", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(runs.status, 200);
  assert.deepEqual(env.DB._calls.at(-1).binds, [100]);
  const alerts = await handleFetch(req("/api/alerts?sport=nfl&season=2026&week=3", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(alerts.status, 200);
  assert.deepEqual(env.DB._calls.at(-1).binds, ["nfl", 2026, 3]);
  const status = await handleFetch(req("/api/status", { headers: basic("a", "viewer-pw") }), env);
  const body = await status.json();
  assert.equal(body.heartbeat.cron, "*/30 * * * *");
  assert.equal(body.meta, null);
  const nf = await handleFetch(req("/api/nope", { headers: basic("a", "viewer-pw") }), env);
  assert.equal(nf.status, 404);
});

// ── /refresh ─────────────────────────────────────────────────────────────────
test("refresh: admin only, validates sport/scope, dispatches with string inputs", async () => {
  const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
  const viewer = await handleFetch(req("/refresh", { method: "POST", headers: basic("a", "viewer-pw") }), env);
  assert.equal(viewer.status, 403);
  const badScope = await handleFetch(req("/refresh", {
    method: "POST", headers: { ...basic("mslade", "admin-pw"), "content-type": "application/json" },
    body: JSON.stringify({ sport: "nfl", scope: "bogus" }),
  }), env);
  assert.equal(badScope.status, 400);
  const unconfigured = await handleFetch(req("/refresh", { method: "POST", headers: basic("mslade", "admin-pw") }), fakeEnv());
  assert.equal(unconfigured.status, 503);
  // CSRF: a cross-site form POST (simple content type, params in the query) is rejected.
  const form = await handleFetch(req("/refresh?sport=all&scope=full&force=1", {
    method: "POST", headers: { ...basic("mslade", "admin-pw"), "content-type": "application/x-www-form-urlencoded" },
    body: "sport=all",
  }), env);
  assert.equal(form.status, 415);
  // JSON body with an empty payload still dispatches (defaults all/light).
  const seen = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => { seen.push({ url, init }); return new Response(null, { status: url.includes("/runs") ? 200 : 204, headers: { "content-type": "application/json" } }); };
  try {
    const okRes = await handleFetch(req("/refresh", {
      method: "POST", headers: { ...basic("mslade", "admin-pw"), "content-type": "application/json" }, body: "{}",
    }), env);
    assert.equal(okRes.status, 202);
    assert.deepEqual(await okRes.json(), { ok: true, sport: "all", scope: "light", force: false });
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("dispatchBoard posts to pipeline.yml on main with sport/scope/force inputs", async () => {
  const seen = [];
  const fakeFetch = async (url, init) => { seen.push({ url, init }); return new Response(null, { status: 204 }); };
  const r = await dispatchBoard({ GH_DISPATCH_TOKEN: "tok" }, { sport: "cfb", scope: "full", force: true }, fakeFetch);
  assert.equal(r.ok, true);
  assert.equal(seen[0].url, "https://api.github.com/repos/mslade50/football_weather/actions/workflows/pipeline.yml/dispatches");
  assert.deepEqual(JSON.parse(seen[0].init.body), { ref: "main", inputs: { sport: "cfb", scope: "full", force: "true" } });
  assert.equal(seen[0].init.headers.Authorization, "Bearer tok");
  const fail = await dispatchBoard({ GH_DISPATCH_TOKEN: "tok" }, {}, async () => { throw new Error("boom"); });
  assert.equal(fail.ok, false);
  assert.match(fail.detail, /boom/);
});

test("scheduled failure notices use concise SYSTEM language", async () => {
  const origFetch = globalThis.fetch;
  const sent = [];
  try {
    globalThis.fetch = async (url, init) => {
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      return new Response(null, { status: 200 });
    };
    const blockedEnv = fakeEnv({ TELEGRAM_BOT_TOKEN: "bot", TELEGRAM_CHAT_ID: "chat" });
    await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-10T17:15:00Z") }, blockedEnv);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].body.chat_id, "chat");
    assert.match(sent[0].body.text,
      /^🚨 SYSTEM · Scheduled refresh blocked\nCause: GitHub dispatch token is missing\.\nAction:/);

    sent.length = 0;
    globalThis.fetch = async (url, init) => {
      if (String(url).includes("api.github.com")) return new Response("denied", { status: 500 });
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      return new Response(null, { status: 200 });
    };
    const failedEnv = fakeEnv({ GH_DISPATCH_TOKEN: "tok", TELEGRAM_BOT_TOKEN: "bot", TELEGRAM_CHAT_ID: "chat" });
    await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-10T17:15:00Z") }, failedEnv);
    assert.equal(sent.length, 1);
    assert.match(sent[0].body.text,
      /^🚨 SYSTEM · Scheduled refresh failed\nRequest: all\/full\nResult: 500 · denied$/);
  } finally {
    globalThis.fetch = origFetch;
  }
});

// ── cron plan / ET trimming / Quartz DOW ─────────────────────────────────────
test("sportForMonth season windows", () => {
  assert.equal(sportForMonth(9), "all");
  assert.equal(sportForMonth(12), "all");
  assert.equal(sportForMonth(1), "all");
  assert.equal(sportForMonth(2), "nfl");
  assert.equal(sportForMonth(5), null);
  assert.equal(sportForMonth(7), null);
});

test("etParts handles EDT and EST", () => {
  // 2026-09-13 17:15Z is a Sunday, 13:15 EDT
  const edt = etParts(new Date("2026-09-13T17:15:00Z"));
  assert.deepEqual(edt, { weekday: "Sun", month: 9, day: 13, hour: 13, minute: 15 });
  // 2026-12-06 17:15Z is a Sunday, 12:15 EST
  const est = etParts(new Date("2026-12-06T17:15:00Z"));
  assert.deepEqual(est, { weekday: "Sun", month: 12, day: 6, hour: 12, minute: 15 });
  // UTC-day spillover: 2026-10-04 01:00Z is Sunday UTC but Saturday 21:00 ET
  assert.equal(etParts(new Date("2026-10-04T01:00:00Z")).weekday, "Sat");
});

test("heartbeat cron never dispatches; mid-day cron dispatches in season only", () => {
  assert.equal(resolveCron(HEARTBEAT_CRON, new Date("2026-10-10T15:00:00Z")), null);
  assert.deepEqual(resolveCron(MIDDAY_CRON, new Date("2026-10-10T17:15:00Z")), { sport: "all", scope: "full" });
  assert.deepEqual(resolveCron(MIDDAY_CRON, new Date("2027-02-03T17:15:00Z")), { sport: "nfl", scope: "full" });
  assert.equal(resolveCron(MIDDAY_CRON, new Date("2026-06-03T17:15:00Z")), null);
  assert.equal(resolveCron("unknown cron", new Date()), null);
});

test("every wrangler.toml cron has a CRON_PLAN entry", async () => {
  const fs = await import("node:fs");
  const toml = fs.readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
  const block = toml.slice(toml.indexOf("crons = ["), toml.indexOf("]", toml.indexOf("crons = [")));
  const crons = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  assert.equal(crons.length, 2, "free plan: exactly 2 cron triggers");
  for (const c of crons) assert.ok(c in CRON_PLAN, `missing CRON_PLAN entry for ${c}`);
  assert.ok(PAID_CRONS.includes(MIDDAY_CRON));
  assert.ok(!PAID_CRONS.includes(HEARTBEAT_CRON));
});

test("Quartz DOW: cron strings use 1=Sun..7=Sat and never 0", () => {
  for (const cron of Object.keys(CRON_PLAN)) {
    const dow = cron.trim().split(/\s+/)[4];
    if (dow === "*") continue;
    for (const d of dow.split(",")) {
      assert.match(d, /^[1-7]$/, `${cron}: DOW field must be Quartz 1-7 (got ${d})`);
    }
  }
  // Saturday CFB cron lists Quartz 7 (Sat) and 1 (Sun UTC spillover), not 6/0.
  const sat = Object.keys(CRON_PLAN).find((c) => c.endsWith("* * 7,1"));
  assert.ok(sat);
  assert.deepEqual(resolveCron(sat, new Date("2026-10-03T14:00:00Z")), { sport: "cfb", scope: "full" }); // Sat 10:00 EDT
  assert.deepEqual(resolveCron(sat, new Date("2026-10-04T01:00:00Z")), { sport: "cfb", scope: "light" }); // Sat 21:00 EDT (Sun UTC)
  assert.equal(resolveCron(sat, new Date("2026-10-04T02:00:00Z")), null); // Sat 22:00 EDT -> trimmed
  assert.equal(resolveCron(sat, new Date("2026-10-04T14:00:00Z")), null); // Sunday ET -> trimmed
  assert.equal(resolveCron(sat, new Date("2027-02-06T14:00:00Z")), null); // Feb: no CFB
});

test("paid-plan trimming: Tue/Wed openers, Thu/Fri cadence, Sunday NFL", () => {
  const tueWed = "0 14,20 * * 3,4";
  assert.deepEqual(resolveCron(tueWed, new Date("2026-09-15T14:00:00Z")), { sport: "all", scope: "light" }); // Tue 10 EDT
  assert.equal(resolveCron(tueWed, new Date("2026-09-14T14:00:00Z")), null); // Mon (should never fire, trimmed anyway)
  const thuFri = "0 0,2,12,14,16,18,20,22 * * 5,6,7";
  assert.deepEqual(resolveCron(thuFri, new Date("2026-09-17T16:00:00Z")), { sport: "all", scope: "full" }); // Thu 12 EDT
  assert.deepEqual(resolveCron(thuFri, new Date("2026-09-17T14:00:00Z")), { sport: "all", scope: "light" }); // Thu 10 EDT
  assert.deepEqual(resolveCron(thuFri, new Date("2026-09-19T02:00:00Z")), { sport: "all", scope: "light" }); // Fri 22 EDT (Sat UTC)
  assert.equal(resolveCron(thuFri, new Date("2026-09-17T10:00:00Z")), null); // Thu 06 EDT -> trimmed
  assert.deepEqual(resolveCron(thuFri, new Date("2026-09-19T00:00:00Z")), { sport: "all", scope: "light" }); // Fri 20 EDT
  const sunHourly = "0 10,11,12,13,14,15,16,17,18,19,20,21 * * 1";
  assert.deepEqual(resolveCron(sunHourly, new Date("2026-09-20T10:00:00Z")), { sport: "nfl", scope: "light" }); // Sun 06 EDT
  assert.equal(resolveCron(sunHourly, new Date("2026-12-06T10:00:00Z")), null); // Sun 05 EST -> trimmed
  const sunPre = "30 16,19,23 * * 1";
  assert.deepEqual(resolveCron(sunPre, new Date("2026-09-20T16:30:00Z")), { sport: "nfl", scope: "full" }); // 12:30 EDT
  assert.equal(resolveCron(sunPre, new Date("2026-12-06T16:30:00Z")), null); // 11:30 EST -> trimmed
  const nights = "0 22,23 * * 2,5";
  assert.deepEqual(resolveCron(nights, new Date("2026-09-14T22:00:00Z")), { sport: "all", scope: "light" }); // Mon 18 EDT
  assert.equal(resolveCron(nights, new Date("2026-09-15T22:00:00Z")), null); // Tue -> trimmed
});

// ── scheduled() ──────────────────────────────────────────────────────────────
test("scheduled: heartbeat written first, then trimmed or dispatched", async () => {
  const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
  const trimmed = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-10T15:00:00Z") }, env);
  assert.equal(trimmed.trimmed, true);
  const hb = JSON.parse(env.ODDS._store.get("board/cf_heartbeat.json"));
  assert.equal(hb.cron, HEARTBEAT_CRON);
  assert.equal(hb.ts, "2026-10-10 15:00:00 UTC");
  assert.equal(hb.schema_version, 1);

  // Off-season mid-day fire: heartbeat updated, no dispatch.
  const off = await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-06-03T17:15:00Z") }, env);
  assert.equal(off.trimmed, true);
  assert.equal(JSON.parse(env.ODDS._store.get("board/cf_heartbeat.json")).cron, MIDDAY_CRON);

  // In-season without a token: no dispatch, plan reported.
  const noTok = await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-10T17:15:00Z") }, fakeEnv());
  assert.equal(noTok.dispatched, false);
  assert.deepEqual(noTok.plan, { sport: "all", scope: "full" });
});

test("default export wires fetch and scheduled with ctx.waitUntil", async () => {
  assert.equal(typeof worker.fetch, "function");
  assert.equal(typeof worker.scheduled, "function");
  const env = fakeEnv();
  let waited = null;
  worker.scheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.now() }, env, { waitUntil: (p) => { waited = p; } });
  assert.ok(waited instanceof Promise);
  await waited;
  assert.ok(env.ODDS._store.has("board/cf_heartbeat.json"));
});
