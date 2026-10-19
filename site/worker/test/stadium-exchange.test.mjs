import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const now = Date.parse('2026-10-14T12:00:00Z');
class Clock extends Date { static now() { return now; } }
function fixture() {
  const ctx = vm.createContext({Date: Clock, console});
  for (const file of ['discovery.js', 'app.js', 'table.js', 'drawer.js', 'backtest.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  vm.runInContext(`RAW_META = {publication_status:'manifest_verified', publication:{generation:'gen'}}; BT.loaded=true;`, ctx);
  ctx.game = {game_id:'nfl:2026:6:a@h', run_id:'run', sport:'nfl', kickoff_utc:'2026-10-18T17:00:00Z', status:'scheduled',
    stadium:{stadium_id:'venue-1',name:'Fixture stadium',roof_state:'open',orient_deg:0},
    consensus:{total_now:46.5}, weather:{wind_fg:18,temp_fg:55,wind_dir_deg:0,wind_dir_fg:'N'}, odds:{}, total_prices:{quotes:[]}};
  return {ctx, run: code => vm.runInContext(code, ctx)};
}

test('Exchange distinguishes no quote, stale sources, unchecked fresh reference, expired check and unavailable publication', () => {
  const f=fixture();
  assert.match(f.run('exchangeOfferHtml(game)'),/No exchange quote/);
  f.ctx.game.total_prices.quotes=[{book:'kalshi',side:'under',line:46.5,odds:-110,fetched_at:'2026-10-13T23:45:00Z'}];
  assert.match(f.run('exchangeOfferHtml(game)'),/Exchange quotes stale/);
  f.ctx.game.total_prices.quotes[0].fetched_at='2026-10-14T11:59:55Z';
  assert.match(f.run('exchangeOfferHtml(game)'),/U 46.5.*reference; \$500 depth unchecked/);
  assert.equal(f.run('verifiedOffer(game)'),null);
  f.ctx.game.execution_preview={expires_at:'2026-10-14T11:59:59Z'};
  assert.match(f.run('exchangeOfferHtml(game)'),/Stake check expired/);
  f.run("LOAD_ERRORS.nfl='failed'"); assert.match(f.run('exchangeOfferHtml(game)'),/Board unavailable/);
});

test('An old successful allocation cannot survive exact-total or started-status changes', () => {
  const f=fixture();
  f.ctx.game.execution_preview={ok:true,game_id:f.ctx.game.game_id,board_run_id:'run',publication_generation:'gen',stake_mode:'principal',
    settlement_verified:true,side:'under',line:46.5,rules_key:'rules',principal:500,fees:5,payout_if_win:1000,average_price:.505,
    fetched_at:'2026-10-14T11:59:55Z',depth_fetched_at:'2026-10-14T11:59:55Z',expires_at:'2026-10-14T12:00:10Z',
    allocations:[{book:'kalshi',side:'under',line:46.5,rules_key:'rules',principal:500,fees:5}]};
  assert.ok(f.run('verifiedOffer(game)'));
  f.ctx.game.consensus.total_now=47.5; assert.equal(f.run('verifiedOffer(game)'),null);
  assert.match(f.run('exchangeOfferHtml(game)'),/Under line changed/);
  f.ctx.game.fresh_odds={selected_line:46.5}; assert.ok(f.run('verifiedOffer(game)'));
  f.ctx.game.status='in progress'; assert.equal(f.run('verifiedOffer(game)'),null);
});

test('Compact compass reuses known bearings and omits invented axis / direction', () => {
  const f=fixture(); assert.match(f.run('tableStadiumHtml(game)'),/field axis 0 degrees; wind from 0 degrees/);
  f.ctx.game.stadium.orient_deg=null; f.ctx.game.weather.wind_dir_deg=null;
  const html=f.run('tableStadiumHtml(game)');
  assert.match(html,/Axis unknown/); assert.match(html,/Direction unknown/);
  assert.doesNotMatch(f.run('compassSvg(game)'),/class="field"|class="wind"/);
});

test('Book totals keep current under / price compact and evidence reachable in touch details', () => {
  const f=fixture();
  f.ctx.game.odds.kalshi={total:{line:46.5,under:-110,over:-105,open_line:48.5,open_under:-108,open_kind:'true_open',
    open_ts:'2026-10-10T12:00:00Z',updated_at:'2026-10-14T11:59:55Z',expires_at:'2026-10-14T12:00:10Z'}};
  f.run("BOOKS=['kalshi']");
  const cell=f.run("bookTotalCell(game,'kalshi')");
  assert.match(cell,/>U 46.5</); assert.match(cell,/>-110</); assert.match(cell,/true opener/);
  assert.doesNotMatch(cell,/>48.5</);
  const detail=f.run('oddsTable(game)');
  assert.match(detail,/Updated:/); assert.match(detail,/2026-10-14T11:59:55/); assert.match(detail,/Expires:/); assert.match(detail,/true opener/);
});
