import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const now = Date.parse('2026-10-09T18:00:00Z');
class Clock extends Date { static now() { return now; } }
function ui() {
  const ctx = vm.createContext({Date: Clock, URLSearchParams, Map, Set, console,
    document: {}, navigator: {clipboard: {writeText: async value => {ctx.copied = value;}}}});
  for (const file of ['discovery.js', 'app.js', 'signals.js', 'table.js', 'map.js', 'drawer.js']) {
    const source = readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, '');
    vm.runInContext(source, ctx);
  }
  vm.runInContext("RAW_META = {run_id: 'fixture', publication_status: 'manifest_verified', publication: {generation: 'fixture-generation'}}", ctx);
  ctx.game = {game_id: 'game', run_id: 'fixture', sport: 'cfb', week: 6, kickoff_utc: '2026-10-11T18:00:00Z',
    home: {name: 'Home'}, away: {name: 'Away'}, stadium: {lat: 51.603333, lon: -.066389, roof_state: 'open'},
    consensus: {spread_open: -3, total_now: 46.5}, weather: {wind_fg: 18, temp_fg: 58, rain_fg: 0},
    signal: {label: 'Low (Wind)'}, total_prices: {quotes: []}};
  return ctx;
}
const run = (ctx, expression) => vm.runInContext(expression, ctx);

test('Hard roof/spread eligibility applies even in inspect mode and presets', () => {
  const ctx = ui();
  run(ctx, 'STATE.sport = "cfb"; STATE.focus = "all"; DATA.games.cfb = [game];');
  assert.equal(run(ctx, 'currentGames().length'), 1);
  ctx.game.stadium.roof_state = 'closed';
  assert.equal(run(ctx, 'currentGames().length'), 0);
  assert.equal(run(ctx, 'presetGames(PRESETS.cfb_wind).length'), 0);
  ctx.game.stadium.roof_state = 'open'; ctx.game.consensus.spread_open = 10.5;
  assert.equal(run(ctx, 'currentGames().length'), 0);
});
test('Signal, near signal, quiet and unknown are distinct; no prices never hides a game', () => {
  const ctx = ui();
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'signal');
  ctx.game.weather.wind_fg = 12;
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'near');
  ctx.game.weather.wind_fg = 3;
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'quiet');
  run(ctx, 'STATE.sport = "cfb"; DATA.games.cfb = [game];');
  assert.equal(run(ctx, 'currentGames().length'), 0);
  run(ctx, 'STATE.focus = "all"');
  assert.equal(run(ctx, 'currentGames().length'), 1);
  ctx.game.weather.wind_fg = null;
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'unknown');
  ctx.game.weather.wind_fg = 18;
  run(ctx, 'STATE.focus = "candidates"; STATE.book = "kalshi"; STATE.minEdge = 999;');
  assert.equal(run(ctx, 'currentGames().length'), 1);
});
test('Impact confidence and rain probability are never treated as signal likelihood', () => {
  const ctx = ui(); ctx.game.weather.precip_prob = .01; ctx.game.impact = {v2: {conf: .01}};
  assert.equal(run(ctx, 'discoveryState(game).probability'), null);
  ctx.game.discovery = {signal_probability: .01};
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'quiet');
});
test('Preset results apply the same search, severity, week and sport controls', () => {
  const ctx = ui();
  run(ctx, 'STATE.sport = "cfb"; DATA.games.cfb = [game]; STATE.q = "nonmatching";');
  assert.equal(run(ctx, 'currentGames().length'), 0);
  assert.equal(run(ctx, 'presetGames(PRESETS.cfb_wind).length'), 0);
  run(ctx, 'STATE.q = ""; STATE.signal = "Very High";');
  assert.equal(run(ctx, 'presetGames(PRESETS.cfb_wind).length'), 0);
  run(ctx, 'STATE.signal = ""; STATE.week = 7;');
  assert.equal(run(ctx, 'presetGames(PRESETS.cfb_wind).length'), 0);
});
test('Exact stadium coordinate copy preserves latitude-longitude order, precision and failure feedback', async () => {
  const ctx = ui(), status = {}, button = {dataset: {coordinates: run(ctx, 'stadiumCoordinates(game)')}, parentElement: {querySelector: () => status}};
  ctx.button = button;
  await run(ctx, 'copyCoordinates(button)');
  assert.equal(ctx.copied, '51.603333, -0.066389'); assert.match(status.textContent, /Copied/);
  ctx.navigator.clipboard.writeText = async () => {throw new Error('denied');};
  await run(ctx, 'copyCoordinates(button)'); assert.match(status.textContent, /select the coordinates/);
  assert.match(run(ctx, 'popupHtml(game)'), /Copy lat, lon/);
  assert.match(run(ctx, 'gameInfoTable(game)'), /51\.603333/);
});
function offer(ctx) {
  ctx.game.execution_preview = {ok: true, game_id: 'game', board_run_id: 'fixture', publication_generation: 'fixture-generation', side: 'under', line: 46.5, stake_mode: 'principal',
    settlement_verified: true, rules_key: 'full-game-ot', principal: 500, fees: 5, payout_if_win: 1000, average_price: .505,
    fetched_at: '2026-10-09T17:59:55Z', depth_fetched_at: '2026-10-09T17:59:55Z', expires_at: '2026-10-09T18:00:10Z',
    allocations: [{book: 'kalshi', line: 46.5, side: 'under', rules_key: 'full-game-ot', principal: 500, fees: 5}]};
}
test('Only a fresh verified exchange allocation with $500 principal and matching rules qualifies', () => {
  const ctx = ui(); offer(ctx);
  assert.ok(run(ctx, 'verifiedOffer(game)'));
  assert.match(run(ctx, 'exchangeOfferHtml(game)'), /500\.00 stake \+ \$5\.00 fees/);
  for (const [key, value] of [['principal', 499.99], ['fees', null], ['average_price', .001], ['settlement_verified', false],
    ['stake_mode', 'fee_inclusive_budget'], ['depth_fetched_at', null], ['fetched_at', '2026-10-09T17:59:00Z']]) {
    offer(ctx); ctx.game.execution_preview[key] = value;
    assert.equal(run(ctx, 'verifiedOffer(game)'), null, key);
  }
  offer(ctx); ctx.game.execution_preview.allocations[0].book = 'draftkings';
  assert.equal(run(ctx, 'verifiedOffer(game)'), null);
  offer(ctx); ctx.game.execution_preview.allocations[0].line = 47.5;
  assert.equal(run(ctx, 'verifiedOffer(game)'), null);
  offer(ctx); ctx.game.execution_preview.allocations[0].rules_key = 'regulation-only';
  assert.equal(run(ctx, 'verifiedOffer(game)'), null);
});
test('Invalid JSON shapes and mixed generations fail as unavailable, not empty games', () => {
  const ctx = ui();
  assert.throws(() => run(ctx, 'normalizeGames({error: "unauthorized"})'), /Malformed/);
  ctx.game.run_id = 'old';
  assert.throws(() => run(ctx, 'acceptGames([game], {run_id: "new"})'), /generation mismatch/);
  assert.equal(run(ctx, 'acceptGames([], {run_id: "new"}).length'), 0);
});
test('Unrelated publications and another sport/scope cannot complete a refresh', () => {
  const ctx = ui(); ctx.request = {id: 'requested', sport: 'cfb', scope: 'exchanges', started: now};
  ctx.receipt = {request_id: 'requested', sport: 'cfb', scope: 'exchanges', completed_at: '2026-10-09T18:00:01Z'};
  assert.equal(run(ctx, 'refreshMatches(receipt, request)'), true);
  for (const [key, value] of [['request_id', 'other'], ['sport', 'nfl'], ['scope', 'full'], ['completed_at', '2026-10-09T17:00:00Z']]) {
    const saved = ctx.receipt[key]; ctx.receipt[key] = value;
    assert.equal(run(ctx, 'refreshMatches(receipt, request)'), false); ctx.receipt[key] = saved;
  }
});

test('Exchange refresh deadline produces an explicit unavailable result without altering prior data', () => {
  const ctx = ui(); let done = false;
  ctx.msg = {}; ctx.done = () => {done = true;};
  ctx.fetch = () => {throw new Error('No fetch should occur after deadline');};
  ctx.request = {id: 'old', scope: 'exchanges', sport: 'cfb', started: now - 31000};
  run(ctx, 'DATA.games.cfb = [game]; pollForNewData(request, done, msg)');
  assert.ok(done); assert.match(ctx.msg.textContent, /timed out.*unchanged/);
  assert.equal(run(ctx, 'DATA.games.cfb[0] === game'), true);
});
