// All-week 2h ET cadence rides the existing */30 heartbeat. No third cron.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  handleScheduled, handleFetch, HEARTBEAT_CRON, MIDDAY_CRON,
  refreshSlotPlan, isInRefreshWindow, REFRESH_SLOT_HOURS,
} from "../index.js";
import { refreshStatus } from "../../web/current-quotes.mjs";

function fakeEnv(extra = {}) {
  const store = new Map();
  return {
    ODDS: {
      _store: store,
      async get(key) {
        if (!store.has(key)) return null;
        const body = store.get(key);
        return { body, json: async () => JSON.parse(body) };
      },
      async put(key, value) { store.set(key, value); },
    },
    DB: { prepare() {
      const stmt = { bind() { return stmt; }, async all() { return { results: [] }; } };
      return stmt;
    } },
    ...extra,
  };
}

function receipt(store, iso, dispatched = true) {
  store.set("board/cf_dispatch.json", JSON.stringify({
    schema_version: 1, ts: iso, dispatched, trimmed: false, cron: HEARTBEAT_CRON,
    reason: dispatched ? "GitHub accepted dispatch; workflow completion unverified" : "GitHub dispatch failed",
  }));
}

test("ET slots are the odd hours from 7 through 23, including across DST", () => {
  assert.deepEqual(REFRESH_SLOT_HOURS, [7, 9, 11, 13, 15, 17, 19, 21, 23]);
  assert.deepEqual(refreshSlotPlan(new Date("2026-10-19T11:00:00Z")), { sport: "all", scope: "full" }); // Mon 7:00 EDT
  assert.equal(refreshSlotPlan(new Date("2026-10-19T12:00:00Z")), null); // 8:00 EDT
  assert.equal(refreshSlotPlan(new Date("2026-10-19T10:00:00Z")), null); // 6:00 EDT, before the window
  assert.equal(isInRefreshWindow(new Date("2026-10-19T03:30:00Z")), true); // 11:30 PM EDT
  assert.equal(isInRefreshWindow(new Date("2026-10-19T04:30:00Z")), false); // 12:30 AM EDT
  assert.deepEqual(refreshSlotPlan(new Date("2026-11-02T12:00:00Z")), { sport: "all", scope: "full" }); // Mon 7:00 EST
  assert.equal(refreshSlotPlan(new Date("2026-06-15T11:00:00Z")), null); // off-season
  assert.deepEqual(refreshSlotPlan(new Date("2027-02-02T12:00:00Z")), { sport: "nfl", scope: "full" }); // 7:00 EST February
});

test("Monday daytime heartbeat dispatches; a recent receipt and an active run do not", async () => {
  const orig = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      if (String(url).includes("/dispatches")) return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ workflow_runs: [] }), { status: 200 });
    };
    const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
    const early = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T10:00:00Z") }, env);
    assert.equal(early.trimmed, true);
    assert.equal(calls.length, 0);

    const slot = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T11:00:00Z") }, env);
    assert.equal(slot.dispatched, true);
    assert.deepEqual(slot.plan, { sport: "all", scope: "full" });
    assert.ok(calls.some((url) => url.endsWith("/dispatches")));

    calls.length = 0;
    receipt(env.ODDS._store, "2026-10-19T12:00:00Z");
    const blocked = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T13:00:00Z") }, env);
    assert.equal(blocked.trimmed, true);
    assert.equal(calls.length, 0, "90 min dedupe suppresses a slot inside the gap");

    receipt(env.ODDS._store, "2026-10-19T11:00:00Z");
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      if (String(url).includes("/dispatches")) return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ workflow_runs: [{ status: "in_progress", conclusion: null }] }), { status: 200 });
    };
    const active = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T15:00:00Z") }, env);
    assert.equal(active.trimmed, true);
    assert.equal(active.reason, "already running");
    assert.equal(calls.filter((url) => url.endsWith("/dispatches")).length, 0);
  } finally {
    globalThis.fetch = orig;
  }
});

test("a missed slot is caught up on the next heartbeat, and a fresh failure waits 90 min", async () => {
  const orig = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      if (String(url).includes("/dispatches")) return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ workflow_runs: [] }), { status: 200 });
    };
    const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
    receipt(env.ODDS._store, "2026-10-19T11:00:00Z");
    const soon = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T12:30:00Z") }, env);
    assert.equal(soon.trimmed, true);
    assert.equal(calls.length, 0);

    const caught = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T13:30:00Z") }, env);
    assert.equal(caught.dispatched, true);
    assert.equal(calls.filter((url) => url.endsWith("/dispatches")).length, 1);

    calls.length = 0;
    receipt(env.ODDS._store, "2026-10-19T15:00:00Z", false);
    const retrySoon = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T15:30:00Z") }, env);
    assert.equal(retrySoon.trimmed, true);
    const retry = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-19T16:30:00Z") }, env);
    assert.equal(retry.dispatched, true);
  } finally {
    globalThis.fetch = orig;
  }
});

test("mid-day cron still fires without a receipt and skips when a slot just dispatched", async () => {
  const orig = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async () => { calls.push("dispatch"); return new Response(null, { status: 204 }); };
    const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
    const fired = await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-14T17:15:00Z") }, env);
    assert.equal(fired.dispatched, true);
    assert.equal(calls.length, 1);

    const skipped = await handleScheduled({ cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-14T17:15:00Z") }, env);
    assert.equal(skipped.trimmed, true);
    assert.equal(skipped.reason, "recent dispatch");
    assert.equal(calls.length, 1);
    assert.equal(JSON.parse(env.ODDS._store.get("board/cf_dispatch.json")).dispatched, true);
  } finally {
    globalThis.fetch = orig;
  }
});

test("status refresh distinguishes overdue from a failed dispatch", () => {
  const meta = { last_updated: "2026-10-19T04:41:00Z", next_run_eta: "2026-10-19T11:00:00Z" };
  const before = refreshStatus(meta, Date.parse("2026-10-19T12:29:00Z"));
  assert.equal(before.state, "ok");
  const overdue = refreshStatus(meta, Date.parse("2026-10-19T12:30:01Z"));
  assert.equal(overdue.state, "overdue");
  assert.equal(overdue.missed, false);
  assert.match(overdue.reason, /overdue/);
  const failed = refreshStatus(meta, Date.parse("2026-10-19T11:30:00Z"), {
    dispatched: false, trimmed: false, ts: "2026-10-19T11:00:00Z", reason: "GitHub dispatch failed",
  });
  assert.equal(failed.state, "missed");
  assert.match(failed.reason, /GitHub dispatch failed/);
});

test("api/status includes the refresh verdict", async () => {
  const env = fakeEnv({ BOARD_PASSWORD: "viewer-pw" });
  env.ODDS._store.set("board/meta.json", JSON.stringify({
    run_id: "r", last_updated: "2020-01-01T00:00:00Z", next_run_eta: "2020-01-01T02:00:00Z",
  }));
  env.ODDS._store.set("board/cf_dispatch.json", JSON.stringify({
    dispatched: false, trimmed: false, ts: "2020-01-01T02:05:00Z", reason: "GitHub dispatch failed",
  }));
  const res = await handleFetch(new Request("https://board.example/api/status", {
    headers: { Authorization: `Basic ${Buffer.from("a:viewer-pw").toString("base64")}` },
  }), env);
  const body = await res.json();
  assert.equal(body.refresh.state, "missed");
  assert.equal(body.refresh.overdue, true);
  assert.match(body.refresh.reason, /GitHub dispatch failed/);
});
