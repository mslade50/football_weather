"use strict";

function executionGames() {
  return (DATA.games[STATE.sport] || []).filter(g => Date.parse(g.kickoff_utc) > Date.now()
    && !/final|cancel|postpon|suspend|live|progress/i.test(g.status || ""))
    .sort((a, b) => Date.parse(a.kickoff_utc) - Date.parse(b.kickoff_utc));
}

function renderExecution() {
  const host = document.getElementById("executionwrap");
  if (!IS_ADMIN) { host.innerHTML = ""; return; }
  const games = executionGames();
  const game = games.find(g => g.game_id === STATE.executionGame) || games[0];
  STATE.executionGame = game?.game_id || null;
  writeHash();
  host.innerHTML = `<h2>Execution</h2>
    <p class="sub">Preview only · No orders sent. Choose a game to compare available exchange prices and size.</p>
    <div class="execution-controls execution-picker">
      <label>Sport <select id="execution-sport"><option value="nfl">NFL</option><option value="cfb">CFB</option></select></label>
      <label>Game <select id="execution-game" class="execution-game-select" ${games.length ? "" : "disabled"}>
        ${games.length ? games.map(g => `<option value="${esc(g.game_id)}" ${g === game ? "selected" : ""}>${esc(gameLabel(g))} · ${esc(kickoffLabel(g))}</option>`).join("")
          : '<option value="">No upcoming games</option>'}
      </select></label>
    </div>
    ${game ? `<p>${esc(gameLabel(game))} · ${esc(kickoffLabel(game))}</p>${executionPreviewPanel(game)}`
      : '<p class="sub">No upcoming games are available for this sport. Select another sport or refresh lines.</p>'}`;
  const sport = document.getElementById("execution-sport");
  sport.value = STATE.sport;
  sport.addEventListener("change", () => { STATE.executionGame = null; setSport(sport.value); });
  document.getElementById("execution-game").addEventListener("change", event => {
    STATE.executionGame = event.target.value;
    renderExecution();
  });
  if (game) setupExecutionPreview(game);
}

function executionPreviewPanel(g) {
  const lines = [...new Set((g.execution_markets || []).map(r => r.line)
    .concat(Object.values(g.odds || {}).map(b => b.total?.line)))]
    .filter(n => typeof n === "number" && n > 0 && n % 1 === .5).sort((a, b) => a - b);
  const preferred = g.odds?.novig?.total?.line ?? g.consensus?.total_now;
  const selected = lines.includes(preferred) ? preferred : lines[Math.floor(lines.length / 2)];
  const closed = !(Date.parse(g.kickoff_utc) > Date.now());
  return `<section class="execution-preview" aria-label="Execution preview">
    <h3>Execution preview <span class="sub">Preview only · no orders sent</span></h3>
    <p class="sub">Compare live exchange depth for one exact under. Budget includes estimated taker fees.</p>
    <form id="execution-form" class="execution-controls">
      <label>Total <select name="line" aria-label="Under total" ${!lines.length ? "disabled" : ""}>
        ${lines.map(n => `<option value="${n}" ${n === selected ? "selected" : ""}>Under ${n}</option>`).join("")}
      </select></label>
      <label>Budget ($) <input name="budget" aria-label="Budget including fees" type="number" min="1" max="10000" step="0.01" value="500" required></label>
      <label>Max all-in price (¢) <input name="max_price" aria-label="Maximum all-in price in cents" type="number" min="1" max="99" step="0.1" value="99" required></label>
      <button class="controlbtn" type="submit" ${closed || !lines.length ? "disabled" : ""}>Preview live prices</button>
    </form>
    <div id="execution-result" aria-live="polite" class="execution-result">${closed ? "Pre-game previews only." : !lines.length
      ? "No half-point exchange totals available yet." : "Select a total and budget to fetch available size. Balances are not connected."}</div>
  </section>`;
}

const executionMoney = n => `$${Number(n).toFixed(2)}`;
const executionCents = n => n == null ? "—" : `${(Number(n) * 100).toFixed(2)}¢`;
const executionBookLabel = book => book === "4cx" ? "4CX" : bookLabel(book);
function executionResultHtml(result) {
  const p = result.average_price;
  const odds = p == null ? "—" : fmtOdds(Math.round(p >= .5 ? -100 * p / (1 - p) : 100 * (1 - p) / p));
  const rows = result.allocations.map(a => `<tr><td>${esc(bookLabel(a.book))}</td><td>${a.quantity}</td>
    <td>${executionCents(a.ask)}</td><td>${executionMoney(a.fees)}</td><td>${executionMoney(a.spend)}</td>
    <td>${executionCents(a.all_in_price)}</td></tr>`).join("");
  return `<p><b>${result.allocations.length ? `Under ${result.line} · ${executionMoney(result.spend)} estimated spend · ${odds} effective odds`
    : "No liquidity available within this price limit"}</b></p>
    <p>${executionMoney(result.fees)} fees · ${executionMoney(result.unspent)} unspent · ${executionMoney(result.payout_if_win)} payout if it wins
      (${executionMoney(result.profit_if_win)} profit)</p>
    <p>Average all-in ${executionCents(p)} · worst all-in ${executionCents(result.worst_price)} · limit ${executionCents(result.max_price)}</p>
    ${rows ? `<div class="execution-scroll"><table class="kv"><thead><tr><th>Exchange</th><th>Contracts</th><th>Ask</th><th>Fees</th><th>Spend</th><th>All-in</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
    <p class="execution-age">Fetched ${esc(fmtET(result.fetched_at))}. Snapshot expires in 15 seconds; refresh before relying on it.</p>
    <ul class="execution-venues">${result.venues.map(v => `<li><b>${esc(executionBookLabel(v.book))}</b>: ${v.status === "available"
      ? `${v.depth_levels} live price levels` : esc(v.reason || "No available size")}</li>`).join("")}</ul>
    ${result.notes.map(n => `<p class="sub">${esc(n)}</p>`).join("")}
    <details><summary>Settlement rules for included exchanges</summary>${result.venues.filter(v => v.rules)
      .map(v => `<p><b>${esc(bookLabel(v.book))}</b><br>${esc(v.rules)}</p>`).join("") || "No markets included."}</details>`;
}

function setupExecutionPreview(g) {
  const form = document.getElementById("execution-form"), host = document.getElementById("execution-result");
  if (!form || !host) return;
  let revision = 0, expiryTimer;
  function invalidate() {
    revision += 1;
    clearTimeout(expiryTimer);
    host.textContent = "Inputs changed. Preview again for an updated allocation.";
  }
  form.addEventListener("input", invalidate);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const current = ++revision, button = form.querySelector("button");
    clearTimeout(expiryTimer);
    const values = new FormData(form);
    const params = new URLSearchParams({ game_id: g.game_id, line: values.get("line"),
      budget: values.get("budget"), max_price: String(Number(values.get("max_price")) / 100) });
    button.disabled = true;
    host.textContent = "Fetching live order books and fees…";
    try {
      const response = await fetch(`/api/execution-preview?${params}`, { cache: "no-store", signal: AbortSignal.timeout(12000) });
      const result = await response.json();
      if (current !== revision || !host.isConnected) return;
      if (!response.ok || !result.ok) throw new Error(result.error || "Preview unavailable");
      host.innerHTML = executionResultHtml(result);
      const expire = () => {
        if (current !== revision || !host.isConnected) return;
        const note = host.querySelector(".execution-age");
        if (note) { note.textContent = "Snapshot expired — preview again for current prices and size."; note.classList.add("execution-expired"); }
      };
      expiryTimer = setTimeout(expire, Math.max(0, Date.parse(result.expires_at) - Date.now()));
    } catch (error) {
      if (current === revision && host.isConnected) host.textContent = `Preview unavailable: ${error.message}`;
    } finally { button.disabled = false; }
  });
}
