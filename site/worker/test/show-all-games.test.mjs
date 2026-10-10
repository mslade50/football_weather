import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const now=Date.parse('2026-10-10T18:00:00Z');
class Clock extends Date { static now() {return now;} }
function harness() {
  const nodes=new Map();
  const document={activeElement:null,querySelectorAll:()=>[],querySelector(selector){return selector.startsWith('#table ')?this.getElementById(selector):null;},getElementById(id){
    if(!nodes.has(id)) nodes.set(id,{value:'',checked:false,style:{},parentElement:{hidden:false},classList:{toggle(){}},handlers:{},
      querySelectorAll:()=>[],addEventListener(type,fn){this.handlers[type]=fn;}});return nodes.get(id);}};
  const ctx=vm.createContext({Date:Clock,URLSearchParams,document,clearTimeout(){},setTimeout(){},
    location:{hash:''},history:{replaceState(_a,_b,hash){ctx.location.hash=hash;}}});
  for(const file of ['discovery.js','app.js','signals.js','table.js','map.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`,import.meta.url),'utf8').replace(/\nboot\(\);\s*$/,''),ctx);
  ctx.tableIds=[];
  vm.runInContext(`renderTableOriginal=renderTable; renderTable=rows=>{tableIds=rows.map(g=>g.game_id)}; renderLegend=()=>{}; placeMarkers=()=>{};
    ensureMap=()=>({resize(){},jumpTo(){},fitBounds(){}}); setupShowAllGames();`,ctx);
  const game=(sport,id,wind,patch={})=>({game_id:`${sport}-${id}`,run_id:'run',sport,week:6,status:'scheduled',
    kickoff_utc:'2026-10-11T18:00:00Z',home:{name:`Home ${id}`},away:{name:'Away'},
    stadium:{roof_state:'open',lat:40,lon:-80},consensus:{spread_open:-3,total_now:46.5},weather:{temp_fg:58,wind_fg:wind,rain_fg:0},...patch});
  for(const sport of ['nfl','cfb']){
    const threshold=sport==='nfl'?8:14;
    ctx[sport]=[game(sport,'signal',threshold+4),game(sport,'near',threshold-2),game(sport,'quiet',1),
      game(sport,'unknown',null),game(sport,'closed',threshold+4,{stadium:{roof_state:'closed',lat:40,lon:-80}}),
      ...(sport==='cfb'?[game(sport,'spread',18,{consensus:{spread_open:20,total_now:46.5}})]:[]),
      game(sport,'past',18,{kickoff_utc:'2026-10-09T18:00:00Z'}),game(sport,'live',18,{status:'in_progress'}),
      game(sport,'final',18,{status:'final'}),game(sport,'cancel',18,{status:'cancelled'}),
      game(sport,'postponed',18,{status:'postponed'}),game(sport,'other-week',18,{week:7})];
    vm.runInContext(`DATA.games.${sport}=${sport}; RAW_GAMES.${sport}=${sport};`,ctx);
  }
  const run=expression=>vm.runInContext(expression,ctx);
  return {ctx,nodes,run,toggle(value){const node=nodes.get('showallgames');node.checked=value;node.handlers.change({target:node});}};
}

test('Repeated NFL and CFB toggles show the same selected upcoming slate in map and table',()=>{
  const h=harness();
  for(const sport of ['nfl','cfb']){
    h.run(`STATE.sport='${sport}'; STATE.week='6'; STATE.view='table'; STATE.showAllGames=false; render()`);
    assert.deepEqual(Array.from(h.ctx.tableIds),[`${sport}-signal`,`${sport}-near`]);
    for(let i=0;i<3;i++){
      h.toggle(true);
      const expected=['signal','near','quiet','unknown','closed',...(sport==='cfb'?['spread']:[])].map(id=>`${sport}-${id}`);
      assert.deepEqual(Array.from(h.ctx.tableIds),expected);
      h.run('renderTableOriginal(currentGames())');
      const html=h.nodes.get('#table tbody').innerHTML;
      assert.equal((html.match(/class="game-detail"/g)||[]).length,expected.length);
      assert.match(html,/Closed roof/);
      if(sport==='cfb') assert.match(html,/Opening spread outside ±10/);
      h.run("STATE.view='map'; render()");
      assert.deepEqual(Array.from(h.run('MAP.rows.map(g=>g.game_id)')),expected);
      h.toggle(false);
      assert.deepEqual(Array.from(h.run('MAP.rows.map(g=>g.game_id)')),[`${sport}-signal`,`${sport}-near`]);
      h.run("STATE.view='table'; render()");
      assert.deepEqual(Array.from(h.ctx.tableIds),[`${sport}-signal`,`${sport}-near`]);
    }
  }
});

test('Toggle preserves week and raw search while clearing signal-only filters; URL round-trips visibility',()=>{
  const h=harness();h.run(`STATE.sport='cfb'; STATE.week='6'; STATE.q='  HOME   quiet  '; STATE.signal='High'; STATE.preset='cfb_wind';`);
  h.toggle(true);
  assert.equal(h.run('STATE.week'),'6');assert.equal(h.run('STATE.q'),'  HOME   quiet  ');
  assert.deepEqual(Array.from(h.ctx.tableIds),['cfb-quiet']);
  assert.equal(h.run('STATE.signal'),'');assert.equal(h.run('STATE.preset'),null);
  assert.match(h.ctx.location.hash,/all_games=1/);h.run('STATE.showAllGames=false; readHash()');assert.equal(h.run('STATE.showAllGames'),true);
  h.toggle(false);assert.equal(h.ctx.tableIds.length,0);assert.doesNotMatch(h.ctx.location.hash,/all_games=1/);
  h.run("STATE.q=''; STATE.week='7'; STATE.showAllGames=true; render()");
  assert.deepEqual(Array.from(h.ctx.tableIds),['cfb-other-week']);
});

test('Expanded visibility has no effect on Signals presets or historical games and hard gates stay strict',()=>{
  const h=harness();h.run("STATE.sport='cfb'; STATE.week=6; STATE.showAllGames=true; STATE.view='signals';");
  assert.deepEqual(Array.from(h.run('currentGames().map(g=>g.game_id)')),['cfb-signal','cfb-near']);
  assert.equal(h.run('presetGames(PRESETS.cfb_wind).some(g=>!hardEligible(g))'),false);
  h.run("STATE.view='table'; STATE.tableMode='history'");assert.equal(h.run('showAllBoardGames(STATE)'),false);
  h.ctx.game=h.ctx.cfb.find(g=>g.game_id==='cfb-closed');assert.equal(h.run('hardEligible(game)'),false);
  assert.match(h.run('discoveryHtml(game)'),/Closed roof/);assert.match(h.run('exchangeOfferHtml(game)'),/Not eligible/);
  h.ctx.game=h.ctx.cfb.find(g=>g.game_id==='cfb-spread');assert.match(h.run('discoveryHtml(game)'),/Opening spread outside ±10/);
  assert.equal(h.run('verifiedOffer(game)'),null);
});

function partial(h){
  const game=h.ctx.cfb[0];h.ctx.game=game;
  h.run("RAW_META={publication_status:'manifest_verified',publication:{generation:'gen'}}");
  game.execution_preview={ok:true,game_id:game.game_id,board_run_id:'run',publication_generation:'gen',side:'under',line:46.5,
    stake_mode:'principal',cash_liquidity_verified:false,settlement_verified:true,rules_key:'rules',principal:100,fees:1,
    payout_if_win:200,average_price:.505,max_price:.99,worst_price:.505,fetched_at:'2026-10-10T17:59:55Z',depth_fetched_at:'2026-10-10T17:59:53Z',
    expires_at:'2026-10-10T18:00:10Z',allocations:[{book:'kalshi',side:'under',line:46.5,rules_key:'rules',principal:100,fees:1,payout_if_win:200,all_in_price:.505}],
    venues:[{book:'kalshi',status:'available',settlement_verified:true,rules_key:'rules',depth_fetched_at:'2026-10-10T17:59:53Z'}]};
  return game.execution_preview;
}
test('Fresh compatible partial principal is scoped to checked depth and never qualifies the $500 offer',()=>{
  const h=harness();partial(h);
  assert.match(h.run('exchangeOfferHtml(game)'),/Checked depth below \$500/);
  assert.match(h.run('exchangeOfferHtml(game)'),/\$100\.00 principal/);assert.equal(h.run('verifiedOffer(game)'),null);
});
test('Missing, stale, mismatched and failed evidence says liquidity unverified rather than insufficient capacity',()=>{
  const h=harness();h.ctx.game=h.ctx.cfb[0];assert.match(h.run('exchangeOfferHtml(game)'),/Liquidity unverified/);
  for(const [key,value] of [['fetched_at','2026-10-10T17:59:00Z'],['depth_fetched_at',null],['fees',null],['settlement_verified',false],
    ['line',47.5],['principal',null],['cash_liquidity_verified',null],['venues',[]],['allocations',{}],['publication_generation','old'],['max_price',.5]]){
    const r=partial(h);r[key]=value;assert.match(h.run('exchangeOfferHtml(game)'),/Liquidity unverified/,key);
    assert.doesNotMatch(h.run('exchangeOfferHtml(game)'),/below \$500/,key);
  }
  partial(h);h.run("LOAD_ERRORS.cfb='Interrupted read'");assert.match(h.run('exchangeOfferHtml(game)'),/Liquidity unverified/);
});


test('Partial checked depth exposes actual line, limit and detailed fees, clocks and failures',()=>{
  const h=harness(),r=partial(h);r.venues.push({book:'novig',status:'unavailable',reason:'Depth timeout'});
  const compact=h.run('exchangeOfferHtml(game)'),detail=h.run('exchangeOfferHtml(game,true)');
  assert.match(compact,/U 46.5/);assert.match(compact,/limit 99.0¢/);assert.match(compact,/\$1.00 fees/);
  assert.ok(detail.includes(new Date(r.fetched_at).toISOString()));assert.ok(detail.includes(new Date(r.depth_fetched_at).toISOString()));assert.match(detail,/Matching settlement verified/);
  assert.match(detail,/kalshi|Kalshi/);assert.match(detail,/Depth timeout/);
});
test('Missing, inconsistent or over-limit individual price evidence cannot prove a depth shortfall',()=>{
  const h=harness();
  for(const mutate of [r=>{r.max_price=.51;r.worst_price=.8;r.allocations[0].all_in_price=.8;},
    r=>{delete r.worst_price;},r=>{delete r.allocations[0].all_in_price;},
    r=>{r.allocations[0].payout_if_win=100;},r=>{r.worst_price=.6;}]){
    const r=partial(h);mutate(r);assert.match(h.run('exchangeOfferHtml(game)'),/Liquidity unverified/);
  }
});


test('Malformed venue entries fail closed in both compact and detailed partial rendering',()=>{
  const h=harness();
  for(const malformed of [null,42,'kalshi',{}, {status:'available'}, {book:'kalshi',status:null}]){
    const r=partial(h);r.venues.push(malformed);
    assert.equal(h.run('verifiedPrincipalShortfall(game)'),null);
    assert.match(h.run('exchangeOfferHtml(game,true)'),/Liquidity unverified/);
    assert.match(h.run('exchangeOfferHtml(game)'),/Liquidity unverified/);
  }
});
