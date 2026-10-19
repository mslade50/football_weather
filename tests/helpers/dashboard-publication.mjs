// Receives actual Python write_board + publication_guard output, never patched provenance.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {expirePayload, expireCardQuotes} from '../../site/web/current-quotes.mjs';
import {stadiumHistoryPayload} from '../../site/worker/stadium-history.js';
const input = JSON.parse(readFileSync(0, 'utf8'));
const now = Date.parse('2026-10-15T12:00:06Z');
class Clock extends Date {static now() {return now;}}
const ctx = vm.createContext({Date:Clock, console});
for (const file of ['discovery.js', 'app.js', 'table.js', 'drawer.js', 'backtest.js'])
  vm.runInContext(readFileSync(new URL(`../../site/web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
const run = code => vm.runInContext(code, ctx);
ctx.history = stadiumHistoryPayload();
run("STADIUM_HISTORY.data=history; RAW_META={publication_status:'manifest_verified'}");
for (const sport of ['nfl','cfb']) {
  for (const game of expirePayload(`games_${sport}.json`, input.original[sport], now).games) {
    ctx.game = game;
    assert.equal(game.odds.betcris.total.expired, true);
    assert.equal(game.odds.kalshi.total.line, 46.5);
    assert.ok(game.total_prices.quotes.some(q=>q.book==='kalshi'));
    assert.equal(game.total_prices.quotes.some(q=>q.book==='novig'), false);
    assert.equal(game.total_prices.best_under, null);
    assert.equal(game.total_prices.quotes[0].ev_roi, null);
    assert.equal(run('verifiedOffer(game)'), null);
    if (game.season===2026) {
      const offer = run('exchangeOfferHtml(game)');
      if (run('hardEligible(game)')) assert.match(offer, /reference; \$500 depth unchecked/);
      else assert.match(offer, /Retractable roof not confirmed open|Not eligible/);
    }
    ctx.game=expireCardQuotes(game, now+180000);
    if (game.season===2026) {
      const offer = run('exchangeOfferHtml(game)');
      if (run('hardEligible(game)')) assert.match(offer, /Exchange quotes stale/);
      else assert.match(offer, /Retractable roof not confirmed open|Not eligible/);
    }
    assert.equal(run('verifiedOffer(game)'),null);
  }
  // Server publication guard must retain source evidence even if rehydrating
  // model data fails. Browser cannot confuse these with a missing provider.
  for (const game of input.guarded[sport].games) {
    ctx.game=expireCardQuotes(game, now+180000);
    if (game.season===2026) {
      const offer = run('exchangeOfferHtml(game)');
      if (run('hardEligible(game)')) assert.match(offer, /Exchange quotes stale/);
      else assert.match(offer, /Retractable roof not confirmed open|Not eligible/);
    }
  }
}
for (const id of ['huntington-bank-field','lambeau-field','soldier-field','camp-randall-stadium']) {
  ctx.game=Object.values(input.original).flatMap(p=>p.games).find(g=>g.stadium?.stadium_id===id);
  assert.ok(ctx.game,id);
  assert.equal(run('stadiumSignalHistory(game).available'),true,id);
  assert.match(run('stadiumSignalCompact(game)'),/Open U.*N=/,id);
  assert.match(run('stadiumSignalCompact(game)'),/Forecast does not qualify/);
  assert.match(JSON.stringify(run('stadiumSignalEvidence(game)')),/Historical|hourly|Hourly/);
  assert.match(JSON.stringify(run('stadiumSignalEvidence(game)')),/not.*(validated|forecast)|Not a contemporaneous/);
}
ctx.game=input.original.nfl.games.find(g=>g.stadium.stadium_id==='att-stadium');
assert.match(run('stadiumSignalCompact(game)'),/No qualifying games in coverage/);
ctx.game=input.original.cfb.games.find(g=>g.stadium?.stadium_id==='cotton-bowl');
assert.equal(ctx.game.neutral,true); assert.equal(ctx.game.venue_provenance.source_id,'499');
assert.notEqual(ctx.game.stadium.stadium_id,'gaylord-family-oklahoma-memorial-stadium');
ctx.game=input.original.cfb.games.find(g=>g.venue_provenance.source_id==='3699');
assert.match(run('stadiumSignalHistory(game).reason'),/conflict/);
ctx.game=input.original.cfb.games.find(g=>g.venue_provenance.source_id==='999999');
assert.equal(ctx.game.stadium,null); assert.match(run('stadiumSignalHistory(game).reason'),/crosswalk/);
ctx.game=input.original.cfb.games.find(g=>g.venue_provenance.source.startsWith('espn:'));
assert.match(run('stadiumSignalHistory(game).reason'),/crosswalk/);
// Exact selected line and book filtering never promote a different under.
ctx.game=expireCardQuotes(input.original.nfl.games[0], now);
ctx.game.fresh_odds={selected_line:47.5};
assert.match(run('exchangeOfferHtml(game)'),/selected total/);
run("STATE.book='polymarket_us'");
assert.match(run('exchangeOfferHtml(game)'),/No exchange quote/);
run("STATE.book=''; STADIUM_HISTORY.data=null");
let attempts=0;
ctx.fetch=async () => {attempts++; return attempts===1 ? new Response('failed',{status:503})
  : Response.json(stadiumHistoryPayload());};
await run('loadStadiumHistory()');
assert.match(run('stadiumSignalHistory(game).reason'),/loading failed/);
assert.match(run('stadiumHistoryDetails(game)'),/Retry history/);
assert.equal(run('STADIUM_HISTORY.loading'),null);
await run('loadStadiumHistory(true)');
assert.equal(attempts,2); assert.equal(run('STADIUM_HISTORY.error'),null);
assert.equal(run('stadiumSignalHistory(game).available'),true);
ctx.fetch=async()=>Response.json({schema_version:1,kind:'frozen_aggregate_research',leagues:{}});
await run('loadStadiumHistory(true)');
assert.match(run('STADIUM_HISTORY.error'),/schema/);
assert.equal(run('STADIUM_HISTORY.loading'),null);
console.log('Producer, publication, expiry, venue history, selected totals and retry regression passed');
