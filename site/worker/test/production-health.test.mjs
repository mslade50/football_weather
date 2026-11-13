import {test} from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFileSync} from "node:fs";
import {refreshOverdue} from "../../web/current-quotes.mjs";
import {handleScheduled, HEARTBEAT_CRON, MIDDAY_CRON, handleFetch} from "../index.js";

const now = Date.parse("2026-10-05T02:39:00Z");
test("overdue refresh uses expected ETA plus bounded grace, rather than 20 hours", () => {
  const meta = {last_updated: "2026-10-05T02:39:00Z", next_run_eta: "2026-10-05T09:17:00Z"};
  assert.equal(refreshOverdue(meta, Date.parse("2026-10-05T10:47:00Z")), false);
  assert.equal(refreshOverdue(meta, Date.parse("2026-10-05T10:47:01Z")), true);
  assert.equal(refreshOverdue({last_updated: meta.last_updated}, now), false);
});
test("health UI labels warning-only weather and thin books as degraded", () => {
  const el = {innerHTML: ""};
  const context = vm.createContext({document: {getElementById: () => el}, esc: String, bookLabel: String,
    QUOTES: {refreshOverdue: () => false}});
  vm.runInContext(readFileSync(new URL("../../web/status.js", import.meta.url), "utf8"), context);
  context.meta = {books: {pinnacle: {status: "green"}}, degradations: [{component: "weather", severity: "warn"}]};
  vm.runInContext("renderStatusbar(meta)", context);
  assert.match(el.innerHTML, /Degraded/);
  context.meta.degradations = [{severity: "info"}];
  context.meta.resident = {status: 'fresh', fresh_games: 1};
  context.meta.publication_status = 'manifest_verified';
  vm.runInContext("renderStatusbar(meta)", context);
  assert.match(el.innerHTML, /OK/);
  context.meta.books.pinnacle.status = "amber";
  vm.runInContext("renderStatusbar(meta)", context);
  assert.match(el.innerHTML, /Degraded/);
});
test("an open drawer updates its title when a new snapshot has no consensus", () => {
  const title = {innerHTML: ""};
  const context = vm.createContext({document: {getElementById: () => title}, esc: String,
    gameLabel: () => "Game", signalPill: () => "", kickoffLabel: () => "Kickoff",
    isNum: v => v != null && Number.isFinite(v), fmtLine: String, fmtTotal: String});
  vm.runInContext(readFileSync(new URL("../../web/drawer.js", import.meta.url), "utf8"), context);
  context.game = {sport: "nfl", week: 5, consensus: {spread_now: 3, total_now: 48}};
  vm.runInContext("renderDrawerTitle(game)", context);
  assert.match(title.innerHTML, /spread 3.*total 48/);
  context.game.consensus = {spread_now: null, total_now: null};
  vm.runInContext("renderDrawerTitle(game)", context);
  assert.doesNotMatch(title.innerHTML, /spread 3|total 48/);
});
function environment() {
  const data = new Map();
  return {ODDS: {async put(key, value) {data.set(key, value);}, async get(key) {
    return data.has(key) ? {body: data.get(key), json: async () => JSON.parse(data.get(key))} : null;}, data}};
}
test("dispatch receipts survive heartbeat-only ticks and distinguish missing token", async () => {
  const env = environment();
  const result = await handleScheduled({cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-05T17:15:00Z")}, env);
  assert.equal(result.dispatched, false);
  const receipt = JSON.parse(env.ODDS.data.get("board/cf_dispatch.json"));
  assert.equal(receipt.reason, "dispatch token missing");
  await handleScheduled({cron: HEARTBEAT_CRON, scheduledTime: Date.parse("2026-10-05T17:30:00Z")}, env);
  assert.deepEqual(JSON.parse(env.ODDS.data.get("board/cf_dispatch.json")), receipt);
});
test("accepted and failed dispatch receipts record result without claiming build success", async () => {
  const previous = globalThis.fetch;
  try {
    for (const status of [204, 503]) {
      const env = {...environment(), GH_DISPATCH_TOKEN: "fixture"};
      globalThis.fetch = async () => new Response(status === 204 ? null : "upstream failure", {status});
      const result = await handleScheduled({cron: MIDDAY_CRON, scheduledTime: Date.parse("2026-10-05T17:15:00Z")}, env);
      const receipt = JSON.parse(env.ODDS.data.get("board/cf_dispatch.json"));
      assert.equal(receipt.status, status);
      assert.equal(result.dispatched, status === 204);
      assert.match(receipt.reason, status === 204 ? /completion unverified/ : /dispatch failed/);
    }
  } finally { globalThis.fetch = previous; }
});
test("authenticated data route preserves published quotes and derived values", async () => {
  const env = environment();
  env.BOARD_PASSWORD = "private";
  const value = {games: [{odds: {
    betcris: {total: {line: 48, expires_at: "2000-01-01T00:00:00Z", open_line: 47}},
    pinnacle: {total: {line: 47, expires_at: "2000-01-01T00:00:00Z"}}},
    fair: {priced_at: "2000-01-01T00:00:00Z", fair_total: 45.6, edges: [{book: "pinnacle", edge_pts: 1.4}]},
    consensus: {total_now: 47}, total_prices: {best_under: {book: "pinnacle", line: 47}}}]};
  await env.ODDS.put("board/games_nfl.json", JSON.stringify(value));
  const url = "https://board.example/data/games_nfl.json";
  assert.equal((await handleFetch(new Request(url), env)).status, 401);
  const r = await handleFetch(new Request(url, {headers: {Authorization: `Basic ${btoa("viewer:private")}`}}), env);
  assert.deepEqual(await r.json(), value);
});
