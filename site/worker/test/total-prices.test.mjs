import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function preview() {
  const ctx = vm.createContext({
    STATE: { book: '' }, HK: 0, HOVER: {},
    isNum: (v) => typeof v === 'number' && Number.isFinite(v),
    parseTs: (v) => v ? new Date(v) : null,
    fmtTotal: String, fmtLine: String, fmtOdds: String, fmtShortET: String, bookLabel: String,
    esc: (v) => String(v).replaceAll('<', '&lt;'), gameLabel: () => 'Away @ Home',
  });
  vm.runInContext(readFileSync(new URL('../../web/discovery.js', import.meta.url), 'utf8'), ctx);
  vm.runInContext(readFileSync(new URL('../../web/table.js', import.meta.url), 'utf8'), ctx);
  const quote = (book, side, ev_roi, minutes = 0) => ({
    book, side, ev_roi, line: 46.5, odds: 108, cost_prob: .48, fair_cost: .50,
    win_prob: .50, push_prob: 0, updated_at: new Date(Date.now() - minutes * 60000).toISOString(),
  });
  ctx.game = { kickoff_utc: new Date(Date.now() + 3600000).toISOString(), total_prices: { quotes: [
    quote('kalshi', 'under', .04), quote('novig', 'under', .01),
    quote('prophetx', 'over', .10), quote('draftkings', 'under', .50, 61),
  ] } };
  return ctx;
}

test('Only exchange references appear; unverified depth cannot become a recommendation', () => {
  const ctx = preview();
  assert.equal(vm.runInContext('totalPriceQuotes(game, "under").length', ctx), 2);
  assert.ok(vm.runInContext('totalPriceQuotes(game).every(q => q.book !== "draftkings")', ctx));
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /Liquidity unverified/);
  ctx.STATE.book = 'novig';
  assert.equal(vm.runInContext('totalPriceQuotes(game)[0].book', ctx), 'novig');
});

test('Missing and started games show no verified recommendation', () => {
  const ctx = preview();
  ctx.game.kickoff_utc = new Date(Date.now() - 1000).toISOString();
  assert.equal(vm.runInContext('totalPriceQuotes(game).length', ctx), 0);
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /Liquidity unverified/);
});

test('Spread coverage is independent of total coverage in both new and older payloads', () => {
  const ctx = preview();
  ctx.game.consensus = {spread_open: -3, spread_now: -3, spread_src: 'pin', n_books: 3, thin: false,
    spread_n_books: 1, spread_thin: true, total_n_books: 3, total_thin: false};
  assert.match(vm.runInContext('consensusSpreadCell(game)', ctx), /1 book/);
  assert.equal(vm.runInContext('marketCoverage(game, "total")', ctx), 3);
  delete ctx.game.consensus.spread_n_books;
  ctx.game.odds = {pinnacle: {spread: {home_line: -3}, total: {line: 47}},
    betcris: {spread: {open_line: -2.5}, total: {line: 47}},
    betonline: {spread: {home_line: -3, expired: true}, total: {line: 46.5}}};
  assert.match(vm.runInContext('consensusSpreadCell(game)', ctx), /1 book/);
});

test('Late first-observed CFB baselines are not labeled as observed at T minus six days', () => {
  const ctx = preview();
  ctx.game.sport = 'cfb';
  ctx.quote = {open_ts: '2026-10-08T19:00:00Z', open_target_ts: '2026-10-04T19:00:00Z'};
  assert.equal(vm.runInContext('totalBaselineLabel(game, quote)', ctx), 'first seen (after T−6d)');
  ctx.quote.open_ts = '2026-10-04T18:00:00Z';
  assert.equal(vm.runInContext('totalBaselineLabel(game, quote)', ctx), 'T−6d');
});

test('Explicit true opener and first-seen provenance stay distinct', () => {
  const ctx = preview(); ctx.game.sport = 'nfl'; ctx.quote = {open_kind: 'true_open'};
  assert.equal(vm.runInContext('totalBaselineLabel(game, quote)', ctx), 'true opener');
  ctx.quote.open_kind = 'first_seen';
  assert.equal(vm.runInContext('totalBaselineLabel(game, quote)', ctx), 'first seen');
});
