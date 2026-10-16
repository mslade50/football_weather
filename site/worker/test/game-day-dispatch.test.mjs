// node --test "site/worker/test/*.test.mjs"
// Hourly game-day dispatch rides the existing */30 heartbeat cron (:00 ticks).
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { shouldDispatch, resolveCron, handleScheduled, HEARTBEAT_CRON, MIDDAY_CRON } from "../index.js";

const sd = (iso) => shouldDispatch(new Date(iso));
const full = (sport) => ({ sport, scope: "full" });

function fakeEnv(extra = {}) {
  const store = new Map();
  return { ODDS: { _store: store, async put(k, v) { store.set(k, v); } }, ...extra };
}

test("CFB Saturday window 15Z..04Z Sunday, :00 ticks only", () => {
  assert.equal(sd("2026-10-10T14:00:00Z"), null);
  assert.deepEqual(sd("2026-10-10T15:00:00Z"), full("cfb"));
  assert.deepEqual(sd("2026-10-10T23:00:00Z"), full("cfb"));
  assert.deepEqual(sd("2026-10-11T04:00:00Z"), full("cfb"));
  assert.equal(sd("2026-10-11T05:00:00Z"), null);
  assert.equal(sd("2026-10-10T16:30:00Z"), null);
});

test("NFL Sunday 15Z..01Z Monday plus Thu/Mon night windows", () => {
  assert.equal(sd("2026-10-11T14:00:00Z"), null);
  assert.deepEqual(sd("2026-10-11T15:00:00Z"), full("nfl"));
  assert.deepEqual(sd("2026-10-12T01:00:00Z"), full("nfl"));
  assert.equal(sd("2026-10-12T03:00:00Z"), null);
  assert.equal(sd("2026-10-15T21:00:00Z"), null);
  assert.deepEqual(sd("2026-10-15T22:00:00Z"), full("nfl"));
  assert.deepEqual(sd("2026-10-16T02:00:00Z"), full("nfl"));
  assert.deepEqual(sd("2026-10-12T22:00:00Z"), full("nfl"));
  assert.deepEqual(sd("2026-10-13T02:00:00Z"), full("nfl"));
  assert.equal(sd("2026-10-14T17:00:00Z"), null);
});

test("season edges, DST and offseason", () => {
  assert.equal(sd("2026-06-06T16:00:00Z"), null);
  assert.equal(sd("2026-07-12T16:00:00Z"), null);
  assert.deepEqual(sd("2026-12-12T16:00:00Z"), full("all"));
  assert.deepEqual(sd("2027-01-09T16:00:00Z"), full("all"));
  assert.equal(sd("2027-02-06T16:00:00Z"), null);
  assert.deepEqual(sd("2027-02-07T16:00:00Z"), full("nfl"));
  assert.equal(sd("2027-03-07T16:00:00Z"), null);
  // Fixed UTC windows: the 2026-11-01 DST change does not shift them.
  assert.deepEqual(sd("2026-10-31T15:00:00Z"), full("cfb"));
  assert.deepEqual(sd("2026-11-01T15:00:00Z"), full("nfl"));
  assert.equal(shouldDispatch(new Date("nope")), null);
});

test("17:15 mid-day cron is skipped when the 17:00 game-day tick already dispatched", () => {
  assert.equal(resolveCron(MIDDAY_CRON, new Date("2026-10-10T17:15:00Z")), null);
  assert.equal(resolveCron(MIDDAY_CRON, new Date("2026-10-11T17:15:00Z")), null);
  assert.deepEqual(resolveCron(MIDDAY_CRON, new Date("2026-10-14T17:15:00Z")), full("all"));
});

test("scheduled :00 tick dispatches via ctx.waitUntil; :30 does not; failure is logged and recorded", async () => {
  const origFetch = globalThis.fetch;
  const origLog = console.log;
  const calls = [];
  try {
    globalThis.fetch = async (url, init) => { calls.push({ url: String(url), body: JSON.parse(init.body) }); return new Response(null, { status: 204 }); };
    const env = fakeEnv({ GH_DISPATCH_TOKEN: "tok" });
    let waited = null;
    worker.scheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-10T18:00:00Z") }, env, { waitUntil: (p) => { waited = p; } });
    assert.ok(waited instanceof Promise);
    await waited;
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /actions\/workflows\/pipeline\.yml\/dispatches$/);
    assert.equal(calls[0].body.inputs.sport, "cfb");
    assert.equal(calls[0].body.inputs.scope, "full");

    calls.length = 0;
    await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-10T18:30:00Z") }, env);
    assert.equal(calls.length, 0);

    globalThis.fetch = async () => new Response("denied", { status: 500 });
    const logs = [];
    console.log = (m) => logs.push(String(m));
    const failed = await handleScheduled({ cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-10T19:00:00Z") }, env);
    console.log = origLog;
    assert.equal(failed.dispatched, false);
    assert.ok(logs.some((l) => /dispatch .*-> 500 denied/.test(l)), logs.join("|"));
    assert.equal(JSON.parse(env.ODDS._store.get("board/cf_dispatch.json")).status, 500);
  } finally {
    globalThis.fetch = origFetch;
    console.log = origLog;
  }
});
