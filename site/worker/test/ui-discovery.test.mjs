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
    const source = readFileSync(new URL('../../web/game-status.js', import.meta.url), 'utf8') + '\n' + readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, '');
    vm.runInContext(source, ctx);
  }
  vm.runInContext("RAW_META = {run_id: 'fixture', publication_status: 'manifest_verified', publication: {generation: 'fixture-generation'}}", ctx);
  ctx.game = {game_id: 'game', run_id: 'fixture', sport: 'cfb', week: 6, kickoff_utc: '2026-10-11T18:00:00Z',
    home: {name: 'Home'}, away: {name: 'Away'}, stadium: {lat: 51.603333, lon: -.066389, roof_state: 'open'},
    consensus: {spread_open: -3, total_now: 46.5}, weather: {wind_fg: 18, temp_fg: 58, rain_fg: 0},
    process: {decision_version:'decision-v1', eligible:true, ineligible_reasons:[], discovery_state:'signal',
      discovery_reasons:['wind_signal'], wind_band:'flag', rain_tier:'ignore',
      temp_f:58, wind_mph:18, rain_mm:0, severity_tier:'Low', severity_rank:1, severity_size:15,
      display_label:'Low (Wind)', display_color:'blue', flags:['CFB Wind'],
      thresholds:{wind_flag_mph:15,wind_watch_mph:12,rain_ignore_mm:5,rain_watch_mm:15,rain_signal_mm:23,cfb_open_spread_max:10,temp_max_f:65}},
    signal: {label: 'Low (Wind)'}, total_prices: {quotes: []}};
  return ctx;
}
const run = (ctx, expression) => vm.runInContext(expression, ctx);

test('Retained weather keeps its numbers with original time, visible age and stale styling', () => {
  const ctx = ui();
  const head = {querySelectorAll: () => []}, body = {querySelectorAll: () => []};
  ctx.document.querySelector = selector => selector === '#table thead' ? head : body;
  ctx.document.getElementById = () => null;
  run(ctx, "STATE.book = 'pinnacle'; STATE.sort = null;");
  ctx.game.weather = {temp_fg: 58, wind_fg: 18, gust_fg: 25, rain_fg: 2, point_aged: true, retained: true,
    fetched_at: '2026-10-09T14:05:00Z', retained_from: '2026-10-09T14:05:00Z', retained_reason: 'source_unavailable'};
  Object.assign(ctx.game.process, {discovery_state: 'unknown', discovery_reasons: ['forecast_retained'],
    display_label: 'Forecast retained', display_muted: true, display_color: '#8b949e', flags: []});
  run(ctx, 'renderTable([game])');
  assert.match(body.innerHTML, /Forecast from .*10\/9.*10:05.*235 min ago.*source unavailable/);
  assert.match(body.innerHTML, /class="pricing-stale muted"[^>]*>58<\/td>/);
  assert.match(body.innerHTML, /class="pricing-stale muted"[^>]*>18\.0/);
  assert.match(body.innerHTML, /class="pricing-stale muted"[^>]*>2\.0/);
  assert.doesNotMatch(body.innerHTML, /Forecast incomplete/);
  assert.match(body.innerHTML, /Forecast retained; fresh evidence unavailable/);
  assert.match(run(ctx, 'weatherTable(game)'), /Retained forecast.*235 min ago.*source unavailable/);
  ctx.game.weather.retained_reason = 'incomplete_point';
  assert.match(run(ctx, 'weatherCoverageHtml(game)'), /incomplete point/);
  // JavaScript never expires retention: only the published marker controls it.
  assert.match(run(ctx, 'retainedForecastText(game.weather, Date.now() + 86400000)'), /1675 min ago/);
});

test('Old cards without retained fields preserve ordinary weather rendering', () => {
  const ctx = ui();
  assert.equal(run(ctx, 'weatherCoverageHtml(game)'), '');
  assert.equal(run(ctx, 'retainedWeatherAttrs(game.weather)'), '');
  assert.equal(run(ctx, 'retainedForecastText(game.weather)'), '');
  const html = run(ctx, 'weatherTable(game)');
  assert.match(html, /18\.0 mph/);
  assert.doesNotMatch(html, /Retained forecast|pricing-stale/);
  ctx.game.weather.wind_fg = null;
  assert.equal(run(ctx, 'weatherCoverageHtml(game)'), '');
  ctx.game.process.wind_mph = null;
  assert.match(run(ctx, 'weatherCoverageHtml(game)'), /Forecast incomplete/);
});

test('Discovery renders every published state and ordered reason without reading raw weather', () => {
  const ctx = ui();
  for (const [kind, reasons, label] of [
    ['signal', ['wind_signal', 'rain_signal', 'ecmwf_apart', 'opener_unknown'], 'Signal · wind ≥15 mph · heavy rain · ECMWF stands apart · opener line unknown'],
    ['near', ['wind_watch', 'rain_elevated'], 'Watch · 12–15 mph wind · not a flag · rain 15–23 mm'],
    ['quiet', ['below_margin'], 'Below screening margin'],
    ['quiet', ['temp_gate'], 'Temperature gate: needs under 65 °F'],
    ['moved', ['price_moved'], 'Under moved vs opener'],
    ['unknown', ['spread_unknown'], 'Opening spread unknown'],
    ['ineligible', ['closed_roof', 'spread_outside'], 'Closed roof · Opening spread outside ±10'],
  ]) {
    Object.assign(ctx.game.process, {discovery_state: kind, discovery_reasons: reasons});
    ctx.game.weather = {wind_fg: 999, temp_fg: -999, rain_fg: 999};
    assert.equal(run(ctx, 'discoveryState(game).kind'), kind);
    assert.equal(run(ctx, 'discoveryState(game).label'), label);
  }
});
test('Flags, eligibility, pill label and color trust the record even when raw fields disagree', () => {
  const ctx = ui();
  ctx.game.stadium.roof_state = 'closed'; ctx.game.consensus.spread_open = 100;
  ctx.game.process.flags = ['Heat', 'Alt+Heat'];
  ctx.game.process.display_label = 'Rain models disagree'; ctx.game.process.display_color = '#8b949e';
  ctx.game.process.rain_agreement = 'agree'; ctx.game.process.wind_band = 'flag';
  assert.equal(run(ctx, 'decisionEligible(game)'), true);
  assert.deepEqual(Array.from(run(ctx, 'gameFlags(game)')), ['Heat', 'Alt+Heat']);
  assert.match(run(ctx, 'signalPill(game.signal,game)'), /Rain models disagree/);
  assert.match(run(ctx, 'signalPill(game.signal,game)'), /#8b949e/);
  assert.match(run(ctx, 'popupHtml(game)'), /Rain models disagree/);
  ctx.game.process.eligible = false;
  ctx.game.process.ineligible_reasons = ['roof_unconfirmed'];
  assert.equal(run(ctx, 'decisionEligible(game)'), false);
  assert.match(run(ctx, 'ineligibleReason(game)'), /not confirmed open/);
});
test('Older cards preserve the published label and show neutral refresh pending without evaluating', () => {
  const ctx = ui(); delete ctx.game.process;
  ctx.game.signal = {label: 'Low (Rain)', flags: ['CFB Wind']};
  assert.equal(run(ctx, 'discoveryState(game).kind'), 'pending');
  assert.deepEqual(Array.from(run(ctx, 'gameFlags(game)')), []);
  assert.match(run(ctx, 'signalPill(game.signal,game)'), /Low \(Rain\)/);
  assert.match(run(ctx, 'signalPill(game.signal,game)'), /Refresh pending/);
  assert.match(run(ctx, 'discoveryHtml(game)'), /pending/);
  assert.match(run(ctx, 'popupHtml(game)'), /Refresh pending/);
  assert.equal(run(ctx, 'verifiedOffer(game)'), null);
  assert.match(run(ctx, 'exchangeOfferHtml(game)'), /Refresh pending/);
  run(ctx, 'STATE.sport="cfb"; DATA.games.cfb=[game]');
  assert.equal(run(ctx, 'currentGames().length'), 1);
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

test('Tier filter, Signal sort, cluster colour and marker size come from the published record', () => {
  const ctx = ui();
  ctx.game.signal = {label: 'High Impact', size: 40, color: 'purple'};
  // Retained forecast: the raw label says High but the record ranks it as No.
  Object.assign(ctx.game.process, {severity_tier: 'No', severity_rank: 0, severity_size: 7, display_label: 'Forecast retained'});
  ctx.game.weather = {wind_fg: 25, temp_fg: 40, rain_fg: 0, retained: true, point_aged: true,
    retained_from: '2026-10-09T14:05:00Z', retained_reason: 'source_unavailable', lead_hours: 48, source: 'nws'};
  run(ctx, 'STATE.sport = "cfb"; STATE.week = null; STATE.focus = "all"; STATE.showAllGames = true; STATE.signal = "High"; DATA.games.cfb = [game];');
  assert.equal(run(ctx, 'filterDiscovery([game], STATE).length'), 0);
  run(ctx, 'STATE.signal = "No"');
  assert.equal(run(ctx, 'filterDiscovery([game], STATE).length'), 1);
  assert.equal(run(ctx, 'severityRank(game)'), 0);
  assert.equal(run(ctx, 'markerRadius(game)'), 5 + 7 * 0.42);
  ctx.document.createElement = () => ({style: {}, addEventListener() {}, setAttribute() {}});
  ctx.high = {...ctx.game, game_id: 'b', process: {...ctx.game.process, severity_rank: 3, severity_tier: 'High', display_color: 'purple'}};
  assert.equal(run(ctx, 'clusterEl([game, high]).style.cssText.includes(markerFill(high))'), true);
  // A card with no record has no tier: it cannot match a tier filter or rank.
  delete ctx.game.process;
  assert.equal(run(ctx, 'severityTier(game)'), null);
  assert.equal(run(ctx, 'severityRank(game)'), null);
  run(ctx, 'STATE.signal = "High"');
  assert.equal(run(ctx, 'filterDiscovery([game], STATE).length'), 0);
  const column = run(ctx, 'tableColumns([])').find(c => c[4] === 'signal');
  assert.equal(column[2], run(ctx, 'severityRank'));
});

test('Presets sort on the decision forecast, not the stitched wind', () => {
  const ctx = ui();
  ctx.game.weather.wind_fg = 99; ctx.game.process.wind_mph = 16;
  assert.equal(run(ctx, 'PRESETS.cfb_wind.sort(game)'), 16);
  ctx.game.process.temp_f = 41; ctx.game.weather.temp_fg = 80;
  assert.equal(run(ctx, 'PRESETS.heat.sort(game)'), 41);
  ctx.game.process.wind_mph = null;
  assert.equal(run(ctx, 'PRESETS.nfl_wind.sort(game)'), -Infinity);
});

test('Map rain ring and rain tier follow the published tier, never raw rain or retained weather', () => {
  const ctx = ui();
  ctx.game.weather.rain_fg = 40;
  assert.equal(run(ctx, 'markerRing(game)'), 'rgba(255,255,255,.25)');
  ctx.game.process.rain_tier = 'watch';
  assert.equal(run(ctx, 'markerRing(game)'), '#d0d7de');
  ctx.game.weather.retained = true;
  assert.equal(run(ctx, 'markerRing(game)'), 'rgba(255,255,255,.25)');
});

test('Map and drawer mark retained weather: no arrow, no live wind title, no stale lead hours', () => {
  const ctx = ui();
  ctx.game.weather = {temp_fg: 58, wind_fg: 18, gust_fg: 25, rain_fg: 2, wind_dir_deg: 90, wind_dir_fg: 'E', lead_hours: 48, source: 'nws',
    retained: true, point_aged: true, fetched_at: '2026-10-09T14:05:00Z', retained_from: '2026-10-09T14:05:00Z', retained_reason: 'source_unavailable'};
  assert.equal(run(ctx, 'windArrowLen(game)'), 0);
  const popup = run(ctx, 'popupHtml(game)');
  assert.match(popup, /class="pricing-stale muted" title="Forecast from[^"]*source unavailable[^"]*">18\.0 mph/);
  assert.match(popup, /class="pricing-stale muted"[^>]*>2\.0 mm/);
  assert.match(popup, /Source<\/span><span>nws · retained/);
  assert.doesNotMatch(popup, /48h out/);
  const drawer = run(ctx, 'weatherTable(game)');
  assert.match(drawer, /nws · retained/);
  assert.doesNotMatch(drawer, /lead 48h/);
  assert.doesNotMatch(run(ctx, 'compassSvg(game)'), /class="wind"/);
  // A current point keeps arrow, lead hours and plain rows.
  ctx.game.weather.retained = false; ctx.game.weather.point_aged = false;
  assert.ok(run(ctx, 'windArrowLen(game)') > 0);
  assert.match(run(ctx, 'popupHtml(game)'), /48h out/);
  assert.match(run(ctx, 'weatherTable(game)'), /lead 48h/);
});

test('Drawer and popup show nothing, not a JS guess, when the record lacks a value', () => {
  const ctx = ui();
  ctx.game.stadium.orient_deg = 0; ctx.game.weather.wind_dir_deg = 90; ctx.game.weather.cross_mph = 7; ctx.game.weather.head_mph = 3;
  Object.assign(ctx.game.process, {cross_mph: null, head_mph: null, crosswind_factor: null});
  assert.equal(run(ctx, 'crossHeadText(game)'), '—');
  assert.doesNotMatch(run(ctx, 'popupHtml(game)'), /Cross \/ Head/);
  Object.assign(ctx.game.process, {cross_mph: 8, head_mph: 1, crosswind_factor: null});
  assert.doesNotMatch(run(ctx, 'crossHeadText(game)'), /sin/);
  assert.equal(run(ctx, 'tempGateText(game)'), 'Shown only. Optional temperature gate is off.');
  Object.assign(ctx.game.process, {temp_gate: true});
  assert.equal(run(ctx, 'tempGateText(game)'), 'Optional gate on');
  ctx.game.process.temp_gate_max_f = 60;
  assert.match(run(ctx, 'tempGateText(game)'), /under 60°F/);
  delete ctx.game.process;
  assert.equal(run(ctx, 'tempGateText(game)'), '—');
  assert.match(run(ctx, 'weatherTable(game)'), /Missing forecast fields[^]*?—/);
});

test('The discovery cell no longer carries a signal-likelihood note', () => {
  const ctx = ui();
  assert.doesNotMatch(run(ctx, 'discoveryHtml(game)'), /likelihood/i);
  assert.equal(run(ctx, 'Object.keys(discoveryState(game)).includes("probability")'), false);
});
