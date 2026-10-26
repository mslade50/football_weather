import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {handleFetch} from '../../site/worker/index.js';
const input=JSON.parse(readFileSync(0,'utf8'));
const env={ODDS:{get:async key=>input.objects[key] ? new Response(input.objects[key]) : null,
  put:()=>assert.fail('Read-only serving must not write')}};
const nodes=new Map();
const document={activeElement:null,querySelector(selector){if(!nodes.has(selector))
  nodes.set(selector,{innerHTML:'',querySelectorAll:()=>[]});return nodes.get(selector);}};
const ctx=vm.createContext({console,document});
for(const file of ['discovery.js','app.js','table.js'])
  vm.runInContext(readFileSync(new URL(`../../site/web/${file}`,import.meta.url),'utf8').replace(/\nboot\(\);\s*$/,''),ctx);
const run=code=>vm.runInContext(code,ctx);
for(const sport of ['nfl','cfb']) {
  const response=await handleFetch(new Request(`https://fixture.invalid/data/games_${sport}.json`),env);
  assert.equal(response.status,200);
  const data=await response.json(),game=data.games[0];
  assert.equal(game.odds.pinnacle.total.line,46.5);
  assert.ok(game.odds.pinnacle.total.expires_at);
  assert.equal(game.odds.kalshi.total.line,46.5);
  for(const book of ['fanduel','betonline','betcris','polymarket_us']) assert.ok(game.odds[book].total.line==null);
  assert.equal(game.odds.fanduel.total.last_observed.status,'stale');
  assert.equal(game.odds.betcris.total.last_observed.status,'expired');
  assert.equal(game.odds.polymarket_us.total.last_observed.status,'unavailable');
  ctx.game=game;ctx.games=data.games;
  run(`STATE.sport='${sport}'; STATE.sort=null; BOOK_SPREADS=false; DATA.games.${sport}=games; BOOKS=['pinnacle','fanduel','betcris','betonline','kalshi','polymarket_us'];`);
  assert.equal(run('currentGames().length'),1); // Default candidate board, both leagues.
  run('renderTable(currentGames())');
  const head=nodes.get('#table thead').innerHTML,body=nodes.get('#table tbody').innerHTML;
  assert.doesNotMatch(head,/Exchange under/);assert.doesNotMatch(body,/best-price|exchange-detail/);
  assert.equal((head.match(/<th /g)||[]).length,18);
  assert.equal((body.match(/<td[ >]/g)||[]).length,18);
  for (const book of ['pinnacle','fanduel','betcris','polymarket_us']) {
    const cell = run(`bookTotalCell(game,'${book}')`);
    assert.match(cell,/<b>46.5<\/b>/);
    assert.match(cell,/<span class="sub">-110<\/span>/);
  }
  assert.match(run("bookTotalCell(game,'never_observed')"),/>—<\/td>/);
  assert.equal(run('tableColumns(BOOKS,false)[3][2](game)'),55); // Temp moved with header/cells.
  run('BOOK_SPREADS=true; STATE.sort=3; renderTable(currentGames())');
  assert.equal((nodes.get('#table tbody').innerHTML.match(/<td[ >]/g)||[]).length,24);
  assert.equal(run('typeof exchangeOfferHtml'),'function'); // Drawer/map data retained.
  const originalNow=Date.now;
  try {
    Date.now=()=>Date.parse(game.odds.pinnacle.total.expires_at)+1;
    const later=await (await handleFetch(new Request(`https://fixture.invalid/data/games_${sport}.json`),env)).json();
    assert.equal(later.games[0].odds.pinnacle.total.line,undefined);
    assert.equal(later.games[0].odds.pinnacle.total.last_observed.status,'expired');
    assert.equal(later.games[0].odds.pinnacle.total.last_observed.updated_at,game.odds.pinnacle.total.updated_at);
  } finally {Date.now=originalNow;}
}
assert.doesNotMatch(readFileSync(new URL('../../site/web/index.html',import.meta.url),'utf8'),/Verified all-in price/);
assert.match(readFileSync(new URL('../../site/web/styles.css',import.meta.url),'utf8'),/td\.stadium-cell \{ grid-column: 1 \/ -1/);
console.log('Partial producer -> sealed publication -> Worker GET -> default NFL/CFB board passed');
