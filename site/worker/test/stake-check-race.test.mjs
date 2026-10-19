import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

function harness() {
  let clock = Date.parse('2026-10-09T18:00:00Z'), timerId = 0;
  const timers = new Map(), requests = [], detached = [], nodes = new Map();
  class Clock extends Date { static now() { return clock; } }
  function node(id) {
    const value = {id, disabled: false, hidden: false, isConnected: true, handlers: {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      click() { return this.handlers.click?.(); }, focus() { ctx.document.activeElement = this; }};
    Object.defineProperty(value, 'innerHTML', {set(html) {
      this.html = html;
      if (id === 'drawer-prices' || id === 'drawer-body') replaceControls();
    }, get() { return this.html || ''; }});
    return value;
  }
  function replaceControls() {
    for (const id of ['verify-exchange-stake', 'cancel-exchange-stake', 'verify-exchange-result']) {
      const previous = nodes.get(id);
      if (previous) { previous.isConnected = false; detached.push(previous); }
      nodes.set(id, node(id));
    }
  }
  const ctx = vm.createContext({Date: Clock, URLSearchParams, AbortController,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, at: clock + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    document: {activeElement: {focus() {}}, getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node(id));
      return nodes.get(id);
    }},
    fetch(url, options) {
      // Deliberately ignore abort here: identity guards must reject even noncooperative late responses.
      return new Promise(resolve => requests.push({url, options, resolve}));
    }});
  for (const file of ['discovery.js', 'app.js', 'table.js', 'signals.js', 'drawer.js']) {
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  }
  vm.runInContext(`writeHash = () => {}; render = () => {};
    renderDrawerTitle = () => {}; weatherTable = () => ''; oddsTable = () => '';
    gameInfoTable = () => ''; compassCard = () => ''; hourlyStrip = () => '';
    renderHourlyChart = () => {}; renderDriftChart = () => {}; loadHistory = () => {}; renderDrawerAlerts = () => {};`, ctx);
  vm.runInContext("RAW_META = {run_id: 'fixture', publication_status: 'manifest_verified', publication: {generation: 'fixture-generation'}}", ctx);
  const game = id => ({game_id: id, run_id: 'fixture', sport: 'cfb', kickoff_utc: '2026-10-11T18:00:00Z',
    consensus: {total_now: 46.5, spread_open: -3}, stadium: {roof_state: 'open'}, total_prices: {quotes: []}});
  ctx.games = [game('a'), game('b')];
  vm.runInContext('DATA.games.cfb = games; openDrawer("a")', ctx);
  function advance(ms) {
    clock += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= clock) { timers.delete(id); timer.callback(); }
  }
  const result = gameId => ({ok: true, game_id: gameId, board_run_id: 'fixture', publication_generation: 'fixture-generation', side: 'under', line: 46.5, stake_mode: 'principal',
    settlement_verified: true, rules_key: 'full-game', principal: 500, fees: 5, payout_if_win: 1000, average_price: .505,
    fetched_at: new Date(clock).toISOString(), depth_fetched_at: new Date(clock).toISOString(),
    expires_at: new Date(clock + 15000).toISOString(),
    allocations: [{book: 'kalshi', side: 'under', line: 46.5, rules_key: 'full-game', principal: 500, fees: 5}]});
  async function respond(index, gameId, response = result(gameId)) {
    requests[index].resolve({ok: true, json: async () => response});
    // Settle fetch, json, completion and finally deterministically, with no wall-clock sleep.
    for (let i = 0; i < 6; i++) await Promise.resolve();
  }
  return {ctx, nodes, requests, detached, advance, respond, result,
    run: expression => vm.runInContext(expression, ctx),
    tick() { vm.runInContext('render(); refreshDrawerQuotes()', ctx); }};
}

test('A delayed stake response survives a 15-second refresh replacing the status and buttons', async () => {
  const h = harness(), firstButton = h.nodes.get('verify-exchange-stake'), firstStatus = h.nodes.get('verify-exchange-result');
  const pending = firstButton.click();
  h.advance(15000); h.tick();
  assert.equal(firstStatus.isConnected, false);
  const replacement = h.nodes.get('verify-exchange-stake');
  assert.notEqual(replacement, firstButton);
  assert.ok(replacement.handlers.click, 'replacement button is always wired');
  assert.equal(replacement.disabled, true);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /Checking exchange depth/);
  assert.equal(h.nodes.get('cancel-exchange-stake').hidden, false);
  await replacement.click();
  assert.equal(h.requests.length, 1, 'refresh does not allow a second pending request');
  await h.respond(0, 'a'); await pending;
  assert.equal(h.run('STAKE_CHECKS.get("a").phase'), 'completed');
  assert.equal(h.run('VERIFIED_OFFERS.get("a").principal'), 500);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /500\.00 stake/);
  assert.equal(replacement.disabled, false);
  h.tick();
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /500\.00 stake/, 'completed result survives later replacement');
  assert.ok(h.nodes.get('verify-exchange-stake').handlers.click);
});

test('Navigating to another game aborts the old request and cannot replace the new result', async () => {
  const h = harness(), old = h.nodes.get('verify-exchange-stake').click();
  h.run('openDrawer("b")');
  assert.equal(h.requests[0].options.signal.aborted, true);
  const current = h.nodes.get('verify-exchange-stake').click();
  await h.respond(1, 'b'); await current;
  const displayed = h.nodes.get('verify-exchange-result').innerHTML;
  await h.respond(0, 'a'); await old;
  assert.equal(h.run('VERIFIED_OFFERS.has("a")'), false);
  assert.equal(h.run('VERIFIED_OFFERS.has("b")'), true);
  assert.equal(h.nodes.get('verify-exchange-result').innerHTML, displayed);
});

test('Cancel and restart on the same game reject a superseded response without enabling the new pending button', async () => {
  const h = harness(), old = h.nodes.get('verify-exchange-stake').click();
  h.tick(); h.nodes.get('cancel-exchange-stake').click();
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /cancelled/);
  const current = h.nodes.get('verify-exchange-stake').click();
  h.tick(); await h.respond(0, 'a'); await old;
  assert.equal(h.run('VERIFIED_OFFERS.has("a")'), false);
  assert.equal(h.nodes.get('verify-exchange-stake').disabled, true);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /Checking exchange depth/);
  await h.respond(1, 'a'); await current;
  assert.equal(h.run('STAKE_CHECKS.get("a").id'), 2);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /500\.00 stake/);
});

test('Closing and reopening the same drawer cannot revive the closed request', async () => {
  const h = harness(), old = h.nodes.get('verify-exchange-stake').click();
  h.run('closeDrawer(); openDrawer("a")');
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.respond(0, 'a'); await old;
  assert.equal(h.run('VERIFIED_OFFERS.has("a")'), false);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /cancelled/);
  assert.equal(h.nodes.get('verify-exchange-stake').disabled, false);
});

test('A line change at the next tick invalidates the pending request and its late response', async () => {
  const h = harness(), old = h.nodes.get('verify-exchange-stake').click();
  h.ctx.games[0].consensus.total_now = 47.5; h.tick();
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.respond(0, 'a'); await old;
  assert.equal(h.run('VERIFIED_OFFERS.has("a")'), false);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /line changed/);
});

test('Timeout re-enables the current replacement control and rejects a later noncooperative response', async () => {
  const h = harness(), old = h.nodes.get('verify-exchange-stake').click();
  h.advance(15000); h.tick(); h.advance(13000);
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.equal(h.nodes.get('verify-exchange-stake').disabled, false);
  assert.match(h.nodes.get('verify-exchange-result').innerHTML, /timed out/);
  await h.respond(0, 'a'); await old;
  assert.equal(h.run('VERIFIED_OFFERS.has("a")'), false);
});
