import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

function harness(browserZone = 'Asia/Tokyo') {
  const nodes = new Map();
  const document = {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {innerHTML:'', querySelectorAll:()=>[]});
    return nodes.get(selector);
  }};
  // Default-zone Intl calls deliberately use a non-US viewer zone.
  const ViewerIntl = {DateTimeFormat: function(locale, opts = {}) {
    return new Intl.DateTimeFormat(locale, {timeZone:browserZone, ...opts});
  }};
  const ctx = vm.createContext({Intl:ViewerIntl, document});
  for (const file of ['app.js', 'discovery.js', 'signals.js', 'table.js'])
    vm.runInContext(readFileSync(new URL(`../../web/${file}`,import.meta.url),'utf8').replace(/\nboot\(\);\s*$/,''),ctx);
  return {html:game=>ctx.tableKickoffHtml(game), ctx, nodes};
}
const game = (utc, zone, extra = {}) => ({kickoff_utc:utc, stadium:{timezone:zone}, ...extra});

test('Eastern primary is explicit, viewer-independent, and same-offset local times are suppressed', () => {
  for (const viewer of ['Asia/Tokyo', 'America/Los_Angeles', 'UTC']) {
    const {html} = harness(viewer);
    for (const zone of ['America/New_York', 'America/Detroit', 'America/Toronto']) {
      assert.match(html(game('2026-10-18T17:00:00Z',zone)), /Sun, 10\/18, 1:00 PM EDT/);
      assert.doesNotMatch(html(game('2026-10-18T17:00:00Z',zone)), /Local:/);
      assert.match(html(game('2027-01-10T18:00:00Z',zone)), /Sun, 1\/10, 1:00 PM EST/);
    }
  }
});

test('Pacific, Mountain and Central venue clocks use date-specific daylight and standard labels', () => {
  const {html} = harness();
  for (const [zone, summer, winter] of [
    ['America/Los_Angeles', '10:00 AM PDT', '10:00 AM PST'],
    ['America/Denver', '11:00 AM MDT', '11:00 AM MST'],
    ['America/Chicago', '12:00 PM CDT', '12:00 PM CST'],
  ]) {
    assert.match(html(game('2026-10-18T17:00:00Z',zone)), new RegExp(`Local: ${summer}`));
    assert.match(html(game('2027-01-10T18:00:00Z',zone)), new RegExp(`Local: ${winter}`));
  }
});

test('Arizona stays MST while Eastern changes across DST', () => {
  const {html} = harness();
  assert.match(html(game('2026-10-18T17:00:00Z','America/Phoenix')), /1:00 PM EDT.*Local: 10:00 AM MST/s);
  assert.match(html(game('2027-01-10T18:00:00Z','America/Phoenix')), /1:00 PM EST.*Local: 11:00 AM MST/s);
});

test('neutral London venue wins over game/home-team zone and uses BST or GMT', () => {
  const {html} = harness();
  const neutral = {neutral:true, tz:'America/New_York', home:{timezone:'America/Chicago'}};
  assert.match(html(game('2026-10-18T13:30:00Z','Europe/London',neutral)), /9:30 AM EDT.*Local: 2:30 PM BST/s);
  assert.match(html(game('2026-10-25T13:30:00Z','Europe/London',neutral)), /9:30 AM EDT.*Local: 1:30 PM GMT/s);
  assert.match(html(game('2026-11-01T14:30:00Z','Europe/London',neutral)), /9:30 AM EST.*Local: 2:30 PM GMT/s);
});

test('Eastern and London labels change at the actual DST transition instant', () => {
  const {html} = harness();
  for (const [utc, eastern] of [
    ['2026-03-08T06:59:00Z','1:59 AM EST'], ['2026-03-08T07:00:00Z','3:00 AM EDT'],
    ['2026-11-01T05:59:00Z','1:59 AM EDT'], ['2026-11-01T06:00:00Z','1:00 AM EST'],
  ]) assert.ok(html(game(utc,'America/New_York')).includes(eastern));
  assert.match(html(game('2026-03-29T00:59:00Z','Europe/London')), /Local: Sun, 3\/29, 12:59 AM GMT/);
  assert.match(html(game('2026-03-29T01:00:00Z','Europe/London')), /Local: Sun, 3\/29, 2:00 AM BST/);
  assert.match(html(game('2026-10-25T00:59:00Z','Europe/London')), /Local: Sun, 10\/25, 1:59 AM BST/);
  assert.match(html(game('2026-10-25T01:00:00Z','Europe/London')), /Local: Sun, 10\/25, 1:00 AM GMT/);
});

test('local date is included only when it differs from Eastern, in either direction', () => {
  const {html} = harness();
  assert.match(html(game('2026-10-19T04:30:00Z','America/Los_Angeles')), /Mon, 10\/19, 12:30 AM EDT.*Local: Sun, 10\/18, 9:30 PM PDT/s);
  assert.match(html(game('2026-10-19T02:30:00Z','Europe/London')), /Sun, 10\/18, 10:30 PM EDT.*Local: Mon, 10\/19, 3:30 AM BST/s);
  assert.match(html(game('2027-01-01T04:30:00Z','Europe/London')), /Thu, 12\/31, 11:30 PM EST.*Local: Fri, 1\/1, 4:30 AM GMT/s);
});

test('TBD and unknown schedules never display placeholder or invented clocks', () => {
  const {html} = harness();
  for (const g of [game('2026-10-18T17:00:00Z','America/Chicago',{status:'tbd'}), game('TBD','Europe/London')]) {
    assert.match(html(g), /Kickoff TBD/); assert.doesNotMatch(html(g), /AM|PM|Local:/);
  }
  for (const utc of [null, '', 'bad date', '2026-10-18']) {
    assert.match(html(game(utc,'America/Chicago')), /Kickoff unknown/);
    assert.doesNotMatch(html(game(utc,'America/Chicago')), /AM|PM|Local:/);
  }
});

test('missing or invalid stadium zone stays unknown instead of using fallback game zone', () => {
  const {html} = harness();
  for (const stadium of [undefined, {}, {timezone:''}, {timezone:'Not/AZone'}, {timezone:'EST'}]) {
    const value = html({kickoff_utc:'2026-10-18T17:00:00Z', stadium, tz:'America/New_York'});
    assert.match(value, /1:00 PM EDT/); assert.match(value, /Local time zone unknown/);
    assert.doesNotMatch(value, /Local:.*(?:AM|PM)/);
  }
});

test('actual NFL and CFB rows render Eastern followed by venue local time', () => {
  const {ctx,nodes} = harness();
  vm.runInContext(`STATE.showAllGames=true; STATE.view='table'; BOOKS=[];`,ctx);
  for (const sport of ['nfl','cfb']) {
    ctx.row = {...game('2027-01-10T18:00:00Z','America/Chicago'), sport, game_id:sport+'-neutral', neutral:true,
      home:{name:'Home'}, away:{name:'Away'}, status:'scheduled', weather:{wind_fg:18,temp_fg:58,rain_fg:0},
      consensus:{spread_open:-3}, signal:{label:'Low (Wind)'}, stadium:{timezone:'America/Chicago',roof_state:'open'}};
    vm.runInContext(`STATE.sport=row.sport; renderTable([row]);`,ctx);
    const value = nodes.get('#table tbody').innerHTML;
    assert.match(value, /1:00 PM EST.*Local: 12:00 PM CST/s);
    assert.match(value, /class="sub kickoff-eastern"/); assert.match(value, /class="sub kickoff-local"/);
    assert.match(value, /class="game-detail"/); assert.match(value, /\(N\)/);
  }
});
