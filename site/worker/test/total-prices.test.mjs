import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function preview() {
  const ctx = vm.createContext({
    STATE: { book: '' }, HK: 0, HOVER: {},
    isNum: (v) => typeof v === 'number' && Number.isFinite(v),
    parseTs: (v) => v ? new Date(v) : null,
    fmtTotal: String, fmtOdds: String, fmtShortET: String, bookLabel: String,
    esc: (v) => String(v).replaceAll('<', '&lt;'), gameLabel: () => 'Away @ Home',
  });
  vm.runInContext(readFileSync(new URL('../../web/table.js', import.meta.url), 'utf8'), ctx);
  const quote = (book, side, ev_roi, minutes = 0) => ({
    book, side, ev_roi, line: 46.5, odds: 108, cost_prob: .48, fair_cost: .50,
    win_prob: .50, push_prob: 0, updated_at: new Date(Date.now() - minutes * 60000).toISOString(),
  });
  ctx.game = { kickoff_utc: new Date(Date.now() + 3600000).toISOString(), total_prices: { quotes: [
    quote('cheap', 'under', .04), quote('higher', 'under', .01),
    quote('over-book', 'over', .10), quote('stale', 'under', .50, 61),
  ] } };
  return ctx;
}

test('Best price ranks fresh unders by ROI and follows the book filter', () => {
  const ctx = preview();
  assert.equal(vm.runInContext('totalPriceQuotes(game, "under")[0].book', ctx), 'cheap');
  assert.equal(vm.runInContext('totalPriceQuotes(game)[0].book', ctx), 'over-book');
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /Est\. EV \+4\.0%/);
  ctx.STATE.book = 'higher';
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /Est\. EV \+1\.0%/);
});

test('Missing, expired and started games do not show a best price; negative EV stays visible', () => {
  const ctx = preview();
  ctx.game.total_prices.quotes = [ctx.game.total_prices.quotes[0]];
  ctx.game.total_prices.quotes[0].ev_roi = -.02;
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /-2\.0% · no \+EV/);
  ctx.game.kickoff_utc = new Date(Date.now() - 1000).toISOString();
  assert.match(vm.runInContext('bestPriceCell(game)', ctx), /No fresh price/);
  ctx.game = { kickoff_utc: new Date(Date.now() + 3600000).toISOString() };
  assert.equal(vm.runInContext('totalPriceQuotes(game).length', ctx), 0);
});
