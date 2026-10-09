import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

function harness() {
  const nodes = new Map(); let assignments = 0;
  const document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {value: '', style: {}, classList: {toggle() {}}, hidden: true,
        handlers: {}, addEventListener(type, callback) {this.handlers[type] = callback;}});
      return nodes.get(id);
    }};
  const input = document.getElementById('search'); let raw = '';
  Object.defineProperty(input, 'value', {get() {return raw;}, set(value) {
    assignments++; raw = value; this.selectionStart = this.selectionEnd = value.length;
    this.selectionDirection = 'none';
  }});
  const ctx = vm.createContext({document, URLSearchParams, clearTimeout() {}, setTimeout() {},
    location: {hash: ''}, history: {replaceState() {}}});
  for (const file of ['discovery.js', 'app.js']) {
    vm.runInContext(readFileSync(new URL(`../../web/${file}`, import.meta.url), 'utf8').replace(/\nboot\(\);\s*$/, ''), ctx);
  }
  vm.runInContext('renderTable = () => {}; setupSearch();', ctx);
  document.activeElement = input;
  return {input, ctx, document, run: expression => vm.runInContext(expression, ctx),
    assignments: () => assignments,
    type(value, start = value.length, end = start, direction = 'none') {
      // Native typing changes the value and caret without going through the script value setter.
      raw = value; input.selectionStart = start; input.selectionEnd = end; input.selectionDirection = direction;
      input.handlers.input({target: input});
    }};
}

test('Incremental multi-word typing preserves space, case and the complete query on every input render', () => {
  const h = harness();
  for (const text of ['N', 'Ne', 'New', 'New ', 'New Y', 'New Yo', 'New Yor', 'New York']) {
    h.type(text);
    assert.equal(h.input.value, text);
    assert.equal(h.run('STATE.q'), text);
    assert.equal(h.input.selectionStart, text.length);
  }
  assert.equal(h.assignments(), 0, 'render never rewrites the focused field');
  h.ctx.game = {game_id: 'ny', sport: 'nfl', kickoff_utc: new Date(Date.now() + 86400000).toISOString(),
    home: {name: 'New York Giants'}, away: {name: 'Away'}, stadium: {roof_state: 'open'},
    weather: {wind_fg: 18, temp_fg: 55}};
  h.run('DATA.games.nfl = [game]');
  assert.equal(h.run('currentGames().length'), 1);
  h.type('  NEW   YORK  ');
  assert.equal(h.run('currentGames().length'), 1, 'normalization belongs to comparison, not the input');
  assert.equal(h.input.value, '  NEW   YORK  ');
});

test('Editing in the middle and selection survive input and background renders', () => {
  const h = harness(); h.type('New York', 4);
  assert.equal(h.input.selectionStart, 4);
  h.run('render()');
  assert.equal(h.input.selectionStart, 4);
  h.type('New York', 4, 8, 'backward');
  h.run('render()');
  assert.equal(h.input.selectionStart, 4); assert.equal(h.input.selectionEnd, 8);
  assert.equal(h.input.selectionDirection, 'backward');
  assert.equal(h.assignments(), 0);
});

test('An unfocused search synchronizes restored hash state without rewriting an unchanged value', () => {
  const h = harness(); h.type('New York'); h.document.activeElement = null;
  h.run('render()'); assert.equal(h.assignments(), 0);
  h.run('STATE.q = "Las Vegas"; render()');
  assert.equal(h.input.value, 'Las Vegas'); assert.equal(h.assignments(), 1);
  h.run('render()'); assert.equal(h.assignments(), 1);
});
