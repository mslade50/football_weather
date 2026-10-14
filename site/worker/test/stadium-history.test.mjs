import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import vm from 'node:vm';
import {handleFetch} from '../index.js';
import {NFL_VENUES, stadiumHistoryPayload} from '../stadium-history.js';

test('Immutable NFL aggregate bytes, WLP, standardized ROI and league reconciliation agree', () => {
  const bytes=readFileSync(new URL('../../../data/raw/nfl_stadium_signal_history.json', import.meta.url));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), '99053e1daddcf3138685534ddbea31a55f518cd469a320efe1c4db377a958950');
  const data=stadiumHistoryPayload().leagues.nfl;
  const rows=data.record_rows.map(row=>Object.fromEntries(data.record_columns.map((key,i)=>[key,row[i]])));
  assert.equal(data.coverage_rows.reduce((n,row)=>n+row[2],0),2094);
  for(const row of rows) for(const basis of ['opening','closing']) {
    assert.equal(row[`${basis}_wins`]+row[`${basis}_losses`]+row[`${basis}_pushes`],row.n);
    const roi=(row[`${basis}_wins`]/1.1-row[`${basis}_losses`])/row.n;
    assert.ok(Math.abs(roi-row[`${basis}_roi_standardized_minus110`])<1e-8);
    if(row.n<10 || row[`${basis}_wins`]<5) assert.equal(row[`${basis}_roi_actual_source_odds`],null);
  }
  for(const check of data.audit.checks) assert.equal(rows.filter(row=>row.rule===check.rule&&row.period===check.period).reduce((n,row)=>n+row.n,0),check.n);
  const csv=readFileSync(new URL('../../../data/stadiums.csv',import.meta.url),'utf8').split(/\r?\n/);
  for(const [canonical,source] of Object.entries(NFL_VENUES)) {
    assert.ok(csv.find(line=>line.startsWith(canonical+',')&&line.includes(','+source+',')),canonical);
  }
});

test('History endpoint is authenticated GET-only, aggregate-only and does not access R2/D1', async () => {
  const env={BOARD_PASSWORD:'fixture',ODDS:{get(){throw new Error('No board reads allowed');}}};
  const request=(method='GET',auth=true)=>new Request('https://fixture.invalid/api/stadium-history',{
    method,headers:auth?{Authorization:'Basic '+Buffer.from('reader:fixture').toString('base64')}:{} });
  assert.equal((await handleFetch(request('GET',false),env)).status,401);
  assert.equal((await handleFetch(request('POST'),env)).status,405);
  const response=await handleFetch(request(),env); assert.equal(response.status,200);
  const body=await response.json(); assert.equal(body.kind,'frozen_aggregate_research');
  for(const forbidden of ['game_id','final_score','opening_odds','closing_odds','total_open','total_close'])
    assert.equal(body.leagues.nfl.record_columns.includes(forbidden),false);
});

function ui() {
  const ctx=vm.createContext({console});
  for(const name of ['discovery.js','app.js','backtest.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${name}`,import.meta.url),'utf8').replace(/\nboot\(\);\s*$/,''),ctx);
  ctx.data=stadiumHistoryPayload();
  vm.runInContext('STADIUM_HISTORY.data=data',ctx);
  ctx.game={sport:'nfl',season:2026,stadium:{stadium_id:'metlife-stadium',roof_state:'open'},
    venue_provenance:{source_id:'NYC01',resolved_id:'metlife-stadium'},weather:{wind_fg:18,temp_fg:55}};
  return {ctx,run:code=>vm.runInContext(code,ctx)};
}
test('Exact stadium and league provide full/recent paired records with explicit actual-weather caveats',()=>{
  const f=ui(),result=f.run('stadiumSignalHistory(game)');
  assert.equal(result.periods[0].n,14); assert.equal(result.periods[0].opening,'6–8–0'); assert.equal(result.periods[0].closing,'7–7–0');
  assert.equal(result.periods[1].n,5);
  const compact=f.run('stadiumSignalCompact(game)');
  assert.match(compact,/N=14.*small sample/); assert.match(compact,/Retrospective actual weather/);
  const detail=JSON.stringify(f.run('stadiumSignalEvidence(game)'));
  assert.match(detail,/IEM airport/); assert.match(detail,/unaudited/); assert.match(detail,/not a validated forward policy/);
  f.ctx.game.weather.wind_fg=14;
  assert.equal(f.run('stadiumSignalHistory(game).periods[0].n'),48);
  f.ctx.game.sport='cfb'; assert.equal(f.run('stadiumSignalHistory(game).available'),false);
});
test('Venue renames match only verified IDs; missing coverage never becomes a zero record',()=>{
  const f=ui(); f.ctx.game.stadium.name='Renamed building'; assert.equal(f.run('stadiumSignalHistory(game).available'),true);
  f.ctx.game.venue_provenance.source_id='CLE00'; assert.equal(f.run('stadiumSignalHistory(game).available'),false);
  delete f.ctx.game.venue_provenance; assert.equal(f.run('stadiumSignalHistory(game).available'),false);
  f.ctx.game.venue_provenance={source_id:'DAL00',resolved_id:'att-stadium'}; f.ctx.game.stadium.stadium_id='att-stadium';
  const known=f.run('stadiumSignalHistory(game)'); assert.equal(known.available,true); assert.equal(known.periods[0].n,0);
  assert.match(f.run('stadiumSignalCompact(game)'),/No qualifying games in coverage/);
  f.ctx.game.stadium.stadium_id='replacement-stadium'; assert.match(f.run('stadiumSignalCompact(game)'),/unavailable/);
  f.ctx.game.stadium.stadium_id='highmark-stadium'; f.ctx.game.venue_provenance={source_id:'BUF00',resolved_id:'highmark-stadium'};
  assert.match(f.run('stadiumSignalHistory(game).reason'),/replaced/);
});
