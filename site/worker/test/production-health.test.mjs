import {test} from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFileSync} from "node:fs";
import {expireCardQuotes, expireQuoteMeta, expirePayload, quoteExpired, refreshOverdue} from "../../web/current-quotes.mjs";
import {handleScheduled, HEARTBEAT_CRON, MIDDAY_CRON, handleFetch} from "../index.js";

const expiry = "2026-10-05T02:28:02Z", now = Date.parse("2026-10-05T02:39:00Z");
test("consumer expiry preserves openers and source clocks, invalidates derived prices", () => {
  const original = {odds: {betcris: {total: {line: 48, under: -110, open_line: 47, expires_at: expiry,
    source_updated_at: "2026-10-05T02:23:02Z"}}, pinnacle: {total: {line: 49, under: -111}}},
    consensus: {total_now: 48, total_open: 47}, fair: {edges: [{book: "betcris"}], fair_total: 45},
    total_prices: {best: "betcris"}, weather: {rain_fg: null}};
  assert.equal(quoteExpired(expiry, Date.parse(expiry)), false);
  assert.equal(quoteExpired("invalid", now), true);
  const c = expireCardQuotes(original, now);
  assert.equal(c.odds.betcris.total.line, undefined);
  assert.equal(c.odds.betcris.total.open_line, 47);
  assert.equal(c.odds.betcris.total.source_updated_at, original.odds.betcris.total.source_updated_at);
  assert.equal(c.odds.pinnacle.total.line, 49);
  assert.equal(c.consensus.total_now, null);
  assert.equal(c.consensus.total_open, 47);
  assert.equal(c.consensus.spread_n_books, 0);
  assert.equal(c.consensus.spread_thin, true);
  assert.equal(c.consensus.total_n_books, 1);
  assert.equal(c.consensus.total_thin, true);
  assert.deepEqual(c.fair.edges, []);
  assert.equal(c.weather.rain_fg, null);
  assert.equal(original.odds.betcris.total.line, 48);
});
test("consumer preserves server-recomputed comparisons for quotes already marked expired", () => {
  const original = {odds: {betcris: {total: {open_line: 47, expires_at: expiry, expired: true}},
      pinnacle: {total: {line: 51, under: -110, expires_at: "2026-10-05T03:39:00Z"}}},
    consensus: {total_open: 47, total_now: 51, n_books: 1, thin: true,
      spread_n_books: 1, spread_thin: true, total_n_books: 1, total_thin: true},
    fair: {fair_total: 45.9, edges: [{book: "pinnacle"}]}, total_prices: {best: "pinnacle"}};
  const c = expireCardQuotes(original, now);
  assert.deepEqual(c.expired_markets, undefined);
  assert.equal(c.consensus.total_now, 51);
  assert.equal(c.consensus.total_n_books, 1);
  assert.equal(c.fair.fair_total, 45.9);
  assert.deepEqual(c.fair.edges, [{book: "pinnacle"}]);
  assert.equal(c.odds.betcris.total.open_line, 47);
});
test("consumer expiry reports fresh book coverage independently per market", () => {
  const original = {odds: {
    betcris: {spread: {home_line: -2.5, expires_at: expiry}, total: {line: 48, open_line: 47, expires_at: expiry}},
    pinnacle: {spread: {home_line: -3}, total: {line: 50}},
    fanduel: {total: {line: 49}}, draftkings: {total: {line: 51}},
  }, consensus: {spread_now: -2.7, total_now: 49, spread_n_books: 2, total_n_books: 4}};
  const c = expireCardQuotes(original, now);
  assert.equal(c.consensus.spread_n_books, 1);
  assert.equal(c.consensus.spread_thin, true);
  assert.equal(c.consensus.total_n_books, 3);
  assert.equal(c.consensus.total_thin, false);
  assert.equal(c.consensus.n_books, 0); // legacy current-comparison invalidation stays conservative
});
test("runtime metadata shows expired quotes rather than green counts", () => {
  const m = {books: {betcris: {count: 2, status: "green"}}, counts: {betcris: {nfl: 2}},
    quote_expiries: [{book: "betcris", sport: "nfl", market: "total", count: 2, expires_at: expiry}]};
  const after = expireQuoteMeta(m, now);
  assert.equal(after.books.betcris.status, "red");
  assert.equal(after.counts.betcris.nfl, 0);
  assert.deepEqual(expireQuoteMeta(after, now), after);
});
test("compact board consumer clears expired comparisons while preserving openers and weather", () => {
  const row = {quote_expires_at: expiry, total_now: 48, total_open: 47, rain_fg: null, fair_total: 45, n_books: 2,
    spread_n_books: 1, spread_thin: true, total_n_books: 2, total_thin: false};
  const result = expirePayload("board.json", {rows: [row]}, now).rows[0];
  assert.equal(result.total_now, null);
  assert.equal(result.fair_total, null);
  assert.equal(result.n_books, 0);
  assert.equal(result.spread_n_books, null);
  assert.equal(result.spread_thin, null);
  assert.equal(result.total_n_books, null);
  assert.equal(result.total_thin, null);
  assert.equal(result.total_open, 47);
  assert.equal(result.rain_fg, null);
  assert.equal(row.total_now, 48);
});
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
test("an open drawer stops displaying expired consensus in its title", () => {
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
test("authenticated data route enforces expiry while historical series remain untouched", async () => {
  const env = environment();
  env.BOARD_PASSWORD = "private";
  const value = {games: [{odds: {betcris: {total: {line: 48, expires_at: "2000-01-01T00:00:00Z", open_line: 47}}}, fair: {edges: []}}]};
  await env.ODDS.put("board/games_nfl.json", JSON.stringify(value));
  const url = "https://board.example/data/games_nfl.json";
  assert.equal((await handleFetch(new Request(url), env)).status, 401);
  const r = await handleFetch(new Request(url, {headers: {Authorization: `Basic ${btoa("viewer:private")}`}}), env);
  assert.equal((await r.json()).games[0].odds.betcris.total.line, undefined);
});
