"use strict";
// Records an already placed bet. No quote, price check or silence submits this form.
const CONFIRMATION_DRAFTS = new Map();
const CONFIRMATION_STORAGE_KEY = 'football-placed-bet-drafts-v1';

class PlacedBetConfirmation {
  constructor(gameId, {fetchImpl = (...args) => fetch(...args), storage = null,
    createId = () => crypto.randomUUID(), changed = () => {}} = {}) {
    this.gameId = gameId; this.fetchImpl = fetchImpl; this.storage = storage; this.createId = createId; this.changed = changed;
    this.phase = 'editing'; this.draft = {book: '', line: '', stake: ''}; this.pending = null;
    this.bets = []; this.message = ''; this.storageAvailable = !!storage;
    try {
      this.storage ||= globalThis.sessionStorage;
      this.storageAvailable = !!this.storage;
      const saved = JSON.parse(this.storage?.getItem(CONFIRMATION_STORAGE_KEY) || '{}')[gameId];
      if (saved?.draft) this.draft = saved.draft;
      if (saved?.pending?.game_id === gameId) { this.pending = saved.pending; this.phase = 'uncertain'; }
      else if (saved?.bet?.game_id === gameId) {
        this.pending = {...saved.bet, confirmed: true, acknowledgement: 'I placed this bet'};
        this.phase = 'uncertain'; // Reconcile the saved receipt before offering another bet.
      }
    } catch { this.storageAvailable = false; }
  }
  save() {
    try {
      const all = JSON.parse(this.storage?.getItem(CONFIRMATION_STORAGE_KEY) || '{}');
      all[this.gameId] = {draft: this.draft, pending: this.pending, bet: this.bet || null};
      this.storage?.setItem(CONFIRMATION_STORAGE_KEY, JSON.stringify(all));
    } catch { this.storageAvailable = false; }
  }
  update(field, value) {
    if (this.phase !== 'editing' || !['book', 'line', 'stake'].includes(field)) return;
    this.draft[field] = value; this.save();
  }
  valid() {
    return ['kalshi', 'polymarket_us', 'novig'].includes(this.draft.book)
      && Number(this.draft.line) >= 10 && Number(this.draft.line) <= 150
      && Number(this.draft.stake) > 0 && Number(this.draft.stake) <= 100000;
  }
  matches(bet, details = this.pending) {
    return details && bet?.confirmed === true && bet.source === 'explicit_user_confirmation'
      && ['bet_id', 'game_id', 'book', 'side', 'line', 'stake'].every(key => bet[key] === details[key]);
  }
  async read() {
    const response = await this.fetchImpl('/api/bet-confirmations', {method: 'GET', cache: 'no-store', signal: AbortSignal.timeout(10000)});
    const ledger = await response.json();
    if (!response.ok || ledger.schema_version !== 1 || !ledger.bets || typeof ledger.bets !== 'object' || Array.isArray(ledger.bets)) throw new Error('Confirmation ledger unavailable');
    this.bets = Object.values(ledger.bets).filter(b => b?.game_id === this.gameId && b.confirmed === true && b.source === 'explicit_user_confirmation');
    if (this.bet && !this.bets.some(b => b.bet_id === this.bet.bet_id)) this.bets.push(this.bet);
    if (this.pending) {
      const known = ledger.bets[this.pending.bet_id];
      if (known && !this.matches(known)) { this.phase = 'conflict'; this.message = 'This confirmation ID has different saved details. Contact the board owner.'; }
      else if (this.matches(known)) this.accept(known);
    }
    this.changed();
    return ledger;
  }
  accept(bet) {
    this.bet = bet; this.pending = null; this.phase = 'confirmed'; this.save();
    if (!this.bets.some(b => b.bet_id === bet.bet_id)) this.bets.push(bet);
    this.message = `Recorded UNDER ${bet.line} at ${bet.book}: $${bet.stake} cash stake. Alerts now allow CLEAR invalidation only.`;
  }
  async submit(acknowledged) {
    if (acknowledged !== true || ['saving', 'confirmed', 'conflict'].includes(this.phase)) return false;
    if (!this.pending) {
      if (!this.valid()) { this.message = 'Enter the venue, placed under line and actual positive cash stake.'; this.changed(); return false; }
      this.pending = {bet_id: this.createId(), game_id: this.gameId, book: this.draft.book,
        side: 'under', line: Number(this.draft.line), stake: Number(this.draft.stake), confirmed: true,
        acknowledgement: 'I placed this bet'};
      this.save(); // Retain the exact ID/details before an uncertain network outcome.
    }
    const details = this.pending;
    this.phase = 'saving'; this.message = 'Recording your placed bet…'; this.changed();
    try {
      const response = await this.fetchImpl('/api/bet-confirmations', {method: 'POST', cache: 'no-store',
        headers: {'content-type': 'application/json'}, body: JSON.stringify(details), signal: AbortSignal.timeout(10000)});
      const result = await response.json();
      if (!response.ok || result.ok !== true || result.recorded !== true || !this.matches(result.bet, details)) {
        if (response.status === 409 && /different immutable details/i.test(result.error || '')) {
          this.phase = 'conflict'; this.message = 'Saved details conflict. Recheck the ledger before recording another bet.'; this.changed(); return false;
        }
        throw new Error('Recording was not confirmed');
      }
      this.accept(result.bet); this.changed(); return true;
    } catch {
      if (this.phase === 'confirmed' && this.matches(this.bet, details)) return true;
      this.phase = 'uncertain'; this.message = 'Recording is unconfirmed. Recheck or retry these same details; alerts may not have changed.';
      this.changed(); return false;
    }
  }
  another() {
    if (this.phase !== 'confirmed') return;
    this.phase = 'editing'; this.draft = {book: this.bet.book, line: String(this.bet.line), stake: ''};
    this.bet = null; this.message = ''; this.save(); this.changed();
  }
}

function confirmationControls(g) {
  if (!IS_ADMIN) return '';
  return `<section class="placed-bet-confirmation"><h3>Record a bet you placed</h3>
    <p class="sub">Enter your actual cash stake, excluding fees. Recording does not place a bet. After confirmation, this game gets CLEAR invalidation alerts only.</p>
    <form id="placed-bet-form">
      <label>Venue <select id="placed-bet-book" required><option value="">Choose venue</option><option value="kalshi">Kalshi</option><option value="polymarket_us">Polymarket US</option><option value="novig">NoVig</option></select></label>
      <label>Placed UNDER line <input id="placed-bet-line" type="number" inputmode="decimal" min="10" max="150" step="0.5" required></label>
      <label>Cash stake ($) <input id="placed-bet-stake" type="number" inputmode="decimal" min="0.01" max="100000" step="0.01" required></label>
      <label class="placed-bet-ack"><input id="placed-bet-ack" type="checkbox"> I placed this bet</label>
      <button class="controlbtn" id="placed-bet-submit" type="submit">Record placed bet</button>
      <button class="controlbtn" id="placed-bet-recheck" type="button">Recheck saved bets</button>
      <button class="controlbtn" id="placed-bet-another" type="button" hidden>Record another bet</button>
    </form><p id="placed-bet-status" role="status" aria-live="polite"></p><div id="placed-bet-saved" class="sub"></div></section>`;
}

function setupConfirmationControls(g) {
  const form = document.getElementById('placed-bet-form');
  if (!form || !IS_ADMIN) return;
  let controller = CONFIRMATION_DRAFTS.get(g.game_id);
  if (!controller) {
    controller = new PlacedBetConfirmation(g.game_id);
    CONFIRMATION_DRAFTS.set(g.game_id, controller);
  }
  const book = document.getElementById('placed-bet-book'), line = document.getElementById('placed-bet-line');
  const stake = document.getElementById('placed-bet-stake'), ack = document.getElementById('placed-bet-ack');
  book.value = controller.draft.book; line.value = controller.draft.line; stake.value = controller.draft.stake;
  ack.checked = false; // A saved draft never repeats acknowledgement automatically.
  const paint = () => {
    if (DRAWER.game?.game_id !== g.game_id || document.getElementById('placed-bet-form') !== form) return;
    const editing = controller.phase === 'editing';
    for (const field of [book, line, stake]) field.disabled = !editing;
    const submit = document.getElementById('placed-bet-submit');
    submit.disabled = !ack.checked || ['saving', 'confirmed', 'conflict'].includes(controller.phase);
    submit.textContent = controller.phase === 'uncertain' ? 'Retry same confirmation' : 'Record placed bet';
    document.getElementById('placed-bet-another').hidden = controller.phase !== 'confirmed';
    document.getElementById('placed-bet-status').textContent = controller.message
      + (!controller.storageAvailable ? ' Browser draft storage unavailable; keep this page open while rechecking.' : '');
    document.getElementById('placed-bet-saved').innerHTML = controller.bets.map(b =>
      `<div>${esc(bookLabel(b.book))} UNDER ${esc(String(b.line))} · $${esc(String(b.stake))} cash · confirmed ${esc(b.confirmed_at || 'timestamp unavailable')}</div>`).join('');
  };
  controller.changed = paint;
  for (const [field, node] of [['book', book], ['line', line], ['stake', stake]]) node.addEventListener('input', () => controller.update(field, node.value));
  ack.addEventListener('change', paint);
  form.addEventListener('submit', async event => { event.preventDefault(); await controller.submit(ack.checked); ack.checked = false; paint(); });
  document.getElementById('placed-bet-recheck').addEventListener('click', async () => {
    try { await controller.read(); } catch { controller.message = 'Saved bets unavailable; confirmation state is unchanged.'; paint(); }
  });
  document.getElementById('placed-bet-another').addEventListener('click', () => {
    controller.another(); book.value = controller.draft.book; line.value = controller.draft.line; stake.value = ''; ack.checked = false; paint();
  });
  paint();
  controller.read().catch(() => { controller.message ||= 'Saved bets unavailable; nothing has been recorded by opening this game.'; paint(); });
}
