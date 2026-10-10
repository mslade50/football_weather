import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {expireQuoteMeta} from '../../web/current-quotes.mjs';

function fixture() {
  const details = {open: false}, books = {open: false};
  const summary = {id: 'board-health-summary', focus() {ctx.document.activeElement = this;}}, bookSummary = {id: 'board-health-book-summary', focus() {ctx.document.activeElement = this;}};
  let html = '', writes = 0;
  const scroll = {scrollTop: 0, scrollLeft: 0};
  const el = {querySelector(selector) {return {'#board-health-details': details, '#board-health-books': books,
    '#board-health-summary': summary, '#board-health-book-summary': bookSummary, '.health-books .execution-scroll': scroll}[selector];},
    contains(node) {return node === summary || node === bookSummary;}};
  Object.defineProperty(el, 'innerHTML', {get() {return html;}, set(value) {html = value; writes++; scroll.scrollTop = 0; scroll.scrollLeft = 0;
    details.open = /id="board-health-details" open/.test(value); books.open = /class="health-books" open/.test(value);}});
  const banners = {innerHTML: 'legacy repeated banners'};
  const ctx = vm.createContext({document: {activeElement: null, getElementById: id => id === 'statusbar' ? el : banners},
    esc: v => String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    fmtShortET: String, bookLabel: String, isNum: v => typeof v === 'number' && Number.isFinite(v),
    QUOTES: {refreshOverdue: () => false}, LOAD_ERRORS: {}});
  vm.runInContext(readFileSync(new URL('../../web/status.js', import.meta.url), 'utf8'), ctx);
  ctx.meta = {run_id: 'run-a', publication_status: 'manifest_verified', publication: {generation: 'gen-a'},
    resident: {status: 'fresh', fresh_games: 2}, books: {kalshi: {status: 'green', count: 3}}, degradations: []};
  return {ctx, el, banners, details, books, summary, bookSummary, scroll, writes: () => writes,
    run: expression => vm.runInContext(expression, ctx)};
}

test('Repeated diagnostics collapse into one detail item with report count, while distinct failures remain visible', () => {
  const f = fixture();
  f.ctx.meta.degradations = [{component: 'weather', severity: 'warn', reason: 'Retained ensemble; verification failed', ts: 't1'},
    {component: 'weather', severity: 'warn', reason: 'Retained ensemble;  verification failed', ts: 't2'},
    {component: 'weather', severity: 'warn', reason: 'Different source unavailable'},
    {component: 'publication', severity: 'critical', reason: 'Manifest mismatch <bad>'},
    {component: 'screen', severity: 'info', reason: 'Expected screening outcome'}];
  f.run('renderBanners(meta); renderStatusbar(meta)');
  assert.equal(f.banners.innerHTML, ''); assert.equal(f.banners.hidden, true);
  assert.equal((f.el.innerHTML.match(/Retained ensemble; verification failed/g) || []).length, 1);
  assert.match(f.el.innerHTML, /2 reports/); assert.match(f.el.innerHTML, /t1, t2/);
  assert.match(f.el.innerHTML, /Different source unavailable/);
  assert.match(f.el.innerHTML, /1 failure/); assert.match(f.el.innerHTML, /Manifest mismatch &lt;bad&gt;/);
  assert.doesNotMatch(f.el.innerHTML, /Expected screening outcome/);
});
test('Collapsed status cannot imply healthy publication, prices or loads without verification', () => {
  const f = fixture(); f.ctx.meta.publication_status = 'generation_mismatch'; f.ctx.meta.resident.status = 'stale';
  f.ctx.LOAD_ERRORS.cfb = 'Mixed generation';
  f.run('renderStatusbar(meta)');
  const overview = f.el.innerHTML.split('<details')[0];
  assert.match(overview, /Board unavailable/); assert.match(overview, /Publication unverified/);
  assert.match(overview, /Resident quotes stale/); assert.doesNotMatch(overview, />OK</);
  assert.match(f.el.innerHTML, /Mixed generation/);
});
test('Repeated updates retain expanded panels and focus; changing diagnostics preserves both expansion levels', () => {
  const f = fixture(); f.run('renderStatusbar(meta)');
  f.details.open = true; f.books.open = true; f.ctx.document.activeElement = f.bookSummary;
  const writes = f.writes();
  for (let i = 0; i < 5; i++) f.run('renderStatusbar(meta)');
  assert.equal(f.writes(), writes, 'identical ticks do not replace the DOM');
  assert.equal(f.books.open, true); assert.equal(f.ctx.document.activeElement, f.bookSummary);
  f.ctx.meta.degradations.push({component: 'weather', reason: 'Partial ensemble', severity: 'warn'});
  f.run('renderStatusbar(meta)');
  assert.equal(f.details.open, true); assert.equal(f.books.open, true);
  assert.equal(f.ctx.document.activeElement, f.bookSummary);
});
test('Book failures and provider reasons remain accessible; OK still requires the existing evidence', () => {
  const f = fixture(); f.run('renderStatusbar(meta)'); assert.match(f.el.innerHTML, />OK</);
  f.ctx.meta.books.kalshi = {status: 'red', reason: 'Provider rate limited', count: 0};
  f.run('renderStatusbar(meta)'); assert.match(f.el.innerHTML, /Degraded/);
  assert.match(f.el.innerHTML, /1 unavailable/); assert.match(f.el.innerHTML, /Provider rate limited/);
  assert.match(f.el.innerHTML, /gen-a/);
});
test('Unqualified rows keep an explicit unusable state with no repeated global instruction paragraph', () => {
  const f = fixture();
  vm.runInContext(readFileSync(new URL('../../web/discovery.js', import.meta.url), 'utf8'), f.ctx);
  vm.runInContext('verifiedOffer = () => null;', f.ctx);
  f.ctx.game = {};
  const html = f.run('exchangeOfferHtml(game)');
  assert.match(html, /Liquidity unverified/); assert.match(html, /No usable stake quote/);
  assert.doesNotMatch(html, /Fresh depth, fees and matching rules required|Tap game/);
});


test('Expiry refreshes preserve book scroll and focused disclosure while retaining updated diagnostic clocks', () => {
  const f = fixture(), now = Date.now();
  const raw = {...f.ctx.meta, quote_expiries: [{book: 'kalshi', sport: 'cfb', market: 'total', count: 3,
    expires_at: new Date(now - 1000).toISOString()}]};
  f.ctx.meta = expireQuoteMeta(raw, now); f.run('renderStatusbar(meta)');
  f.details.open = f.books.open = true; f.scroll.scrollTop = 80; f.scroll.scrollLeft = 340;
  f.ctx.document.activeElement = f.bookSummary;
  f.ctx.meta = expireQuoteMeta(raw, now + 15000); f.run('renderStatusbar(meta)');
  assert.equal(f.scroll.scrollTop, 80); assert.equal(f.scroll.scrollLeft, 340);
  assert.equal(f.details.open, true); assert.equal(f.books.open, true);
  assert.equal(f.ctx.document.activeElement, f.bookSummary);
  assert.match(f.el.innerHTML, /3 expired quotes excluded/);
  assert.ok(f.el.innerHTML.includes(new Date(now + 15000).toISOString()));
});


test('Historical and execution views retain the compact health area for pipeline failures without load errors', () => {
  const nodes = new Map();
  const document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null,
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, {style: {}, value: '', classList: {toggle() {}}, hidden: true}); return nodes.get(id); }};
  const ctx = vm.createContext({document, URLSearchParams, clearTimeout() {}, setTimeout() {},
    location: {hash: ''}, history: {replaceState() {}}});
  for (const file of ['discovery.js', 'status.js', 'app.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  vm.runInContext(`IS_ADMIN=true; QUOTES={expireQuoteMeta:m=>m, expireCardQuotes:g=>g, refreshOverdue:()=>true};
    RAW_META={publication_status:'manifest_verified',resident:{status:'fresh'},degradations:[{component:'pipeline',severity:'critical',reason:'Verification failed'}]};
    renderHeader=()=>{}; renderExecution=()=>{}; closeDrawer=()=>{}; renderHistoricalTable=()=>{};`, ctx);
  for (const state of ['STATE.view="execution"', 'STATE.view="table"; STATE.tableMode="history"']) {
    vm.runInContext(`${state}; render()`, ctx);
    assert.equal(nodes.get('statusbar').style.display, '');
    assert.match(nodes.get('statusbar').innerHTML, /Verification failed/);
    assert.match(nodes.get('statusbar').innerHTML, /Expected refresh is overdue/);
    assert.equal(nodes.get('loadnotice').hidden, true, 'shared warnings have one home');
  }
});

test('Compact offers retain fees and separate quote/depth ages; drawer retains exact source clocks', () => {
  const f = fixture(); vm.runInContext(readFileSync(new URL('../../web/discovery.js', import.meta.url), 'utf8'), f.ctx);
  const quote = new Date(Date.now()-5000).toISOString(), depth = new Date(Date.now()-8000).toISOString();
  f.ctx.game = {offer:{line:46.5,average_price:.505,principal:500,fees:5,allocations:[{book:'kalshi'}],fetched_at:quote,depth_fetched_at:depth}};
  f.ctx.fmtTotal=String; f.run('verifiedOffer = g => g.offer;');
  const compact=f.run('exchangeOfferHtml(game)'), detailed=f.run('exchangeOfferHtml(game,true)');
  assert.match(compact, /\$500\.00 stake \+ \$5\.00 fees/); assert.match(compact, /Quote \d+s old.*depth \d+s old/);
  assert.equal(compact.includes(quote),false); assert.equal(compact.includes(depth),false);
  assert.ok(detailed.includes(quote)); assert.ok(detailed.includes(depth));
});

test('Weather coverage is compact and distinguishes missing, aged, partial and unverified forecasts', () => {
  const f = fixture();
  for(const file of ['discovery.js','table.js']) vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8'), f.ctx);
  f.ctx.game={weather:{temp_fg:58,wind_fg:18,rain_fg:0,ensemble_status:'ok'}};
  assert.equal(f.run('weatherCoverageHtml(game)'), '');
  for (const [patch, label] of [[{point_aged:true},'Point forecast aged'],[{ensemble_status:'partial_members_degraded'},'Partial ensemble'],
    [{ensemble_status:'retained_members_degraded'},'Ensemble unverified'],[{ensemble_status:'unavailable_degraded'},'Ensemble unavailable'],
    [{temp_fg:null},'Forecast incomplete']]) {
    f.ctx.game.weather={temp_fg:58,wind_fg:18,rain_fg:0,...patch}; assert.match(f.run('weatherCoverageHtml(game)'),new RegExp(label));
  }
});

