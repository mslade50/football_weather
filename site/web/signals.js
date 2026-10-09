"use strict";
// Signals view: preset filters replacing the old combined_signals.py page (ARCH §7.4 / §11).
// CFB Wind is the authoritative CFB wind eligibility rule for this board and Telegram;
// impact tiers are severity labels shown in addition to matching signal filters.
// A preset picks a combined flag (CFB Wind / NFL Wind / Heat / Alt+Heat), the sport(s) it applies
// to and a sort key. While a preset is active it filters + sorts the Table and the maps too (fill
// color switches to the flag palette). Flags come from GameCard.signal.flags; when the pipeline
// did not emit them the same §7.4 rules are evaluated client-side so the view still works.

const PRESETS = {
  cfb_wind: { id: "cfb_wind", label: "CFB Wind", flag: "CFB Wind", sports: ["cfb"],
    desc: "CFB wind filter (board + Telegram): spread open within ±10 · temp < 70 °F · wind > 14 mph", sort: (g) => wxNum(g, "wind_fg") },
  nfl_wind: { id: "nfl_wind", label: "NFL Wind", flag: "NFL Wind", sports: ["nfl"],
    desc: "wind > 15 mph · temp < 60 °F", sort: (g) => wxNum(g, "wind_fg") },
  heat: { id: "heat", label: "Heat", flag: "Heat", sports: ["nfl", "cfb"],
    desc: "temp > 80 °F · both teams' home avg temp < 57 °F · CFB spread within ±10", sort: (g) => wxNum(g, "temp_fg") },
  alt_heat: { id: "alt_heat", label: "Alt+Heat", flag: "Alt+Heat", sports: ["cfb"],
    desc: "travel altitude > 800 m · spread within ±10 · temp > 75 °F", sort: (g) => (isNum(g.travel_alt) ? Number(g.travel_alt) : -Infinity) },
};
const PRESET_ORDER = ["cfb_wind", "nfl_wind", "heat", "alt_heat"];
const CFB_OPEN_SPREAD_MAX = 10;

const wxNum = (g, k) => (g.weather && isNum(g.weather[k]) ? Number(g.weather[k]) : -Infinity);

function activePreset() {
  return (STATE.preset && PRESETS[STATE.preset]) || null;
}

// §7.4 combined flags, evaluated locally when signal.flags is missing
function computeFlags(g) {
  const wx = g.weather || {}, c = g.consensus || {};
  const wind = isNum(wx.wind_fg) ? Number(wx.wind_fg) : null;
  const temp = isNum(wx.temp_fg) ? Number(wx.temp_fg) : null;
  const open = isNum(c.spread_open) ? Number(c.spread_open) : null;
  const ht = isNum(g.home_temp) ? Number(g.home_temp) : null, at = isNum(g.away_temp) ? Number(g.away_temp) : null;
  const alt = isNum(g.travel_alt) ? Number(g.travel_alt) : null;
  const out = [];
  if (isDome(g) || wind == null || temp == null) return out;
  if (g.sport === "cfb" && (open == null || Math.abs(open) > CFB_OPEN_SPREAD_MAX)) return out;
  if (g.sport === "cfb" && temp < 70 && wind > 14) out.push("CFB Wind");
  if (g.sport === "nfl" && wind > 15 && temp < 60) out.push("NFL Wind");
  if (ht != null && at != null && ht < 57 && at < 57 && temp > 80) out.push("Heat");
  if (g.sport === "cfb" && alt != null && alt > 800 && temp > 75) out.push("Alt+Heat");
  return out;
}
function gameFlags(g) {
  // Recompute CFB flags from the raw forecast and opener. Never preserve a stale
  // CFB Wind flag when the canonical board rule no longer matches.
  if (g.sport === "cfb") return computeFlags(g);
  const f = g.signal && g.signal.flags;
  if (Array.isArray(f)) return f;
  return computeFlags(g);
}
function hasFlag(g, flag) {
  return gameFlags(g).includes(flag);
}

// games (all weeks of the selected week filter) matching a preset across its sports
function presetGames(preset) {
  return filterDiscovery(DATA.games[STATE.sport] || [], STATE, preset);
}
function presetSort(rows, preset) {
  return rows.slice().sort((a, b) => preset.sort(b) - preset.sort(a));
}

function setPreset(id) {
  const preset = PRESETS[id] || null;
  STATE.preset = preset ? id : null;
  if (preset && !preset.sports.includes(STATE.sport)) {
    STATE.sport = preset.sports[0];
    STATE.week = null;
    populateWeeks();
  }
  STATE.sort = null;
  render();
}

function presetChip(pr, count, active) {
  const color = FLAG_COLORS[pr.flag] || "#8b949e";
  return `<button type="button" class="preset${active ? " active" : ""}" data-preset="${esc(pr.id)}" title="${esc(pr.desc)}"
    style="--pc:${color}"><span class="dot" style="background:${color}"></span>${esc(pr.label)}<span class="cnt">${count}</span></button>`;
}

function renderPresetBar(host) {
  const preset = activePreset();
  const counts = {};
  for (const id of PRESET_ORDER) counts[id] = presetGames(PRESETS[id]).length;
  host.innerHTML = `<div class="presetbar">
      <span class="sub" title="Signal severity tiers are additional context">CFB Wind eligibility: opening spread within ±10, temp &lt; 70 °F, wind &gt; 14 mph. Severity tiers are extra.</span>
      ${PRESET_ORDER.map((id) => presetChip(PRESETS[id], counts[id], preset && preset.id === id)).join("")}
      ${preset ? `<button type="button" class="controlbtn" id="preset-clear" title="Clear preset">✕ clear</button>
        <span class="sub">${esc(preset.desc)} · sorted by ${preset.id === "heat" ? "temp" : preset.id === "alt_heat" ? "travel altitude" : "wind"}</span>
        <button type="button" class="controlbtn" id="preset-map" title="Show these games on the ${esc(STATE.sport.toUpperCase())} map">Map →</button>` : ""}
    </div>`;
  host.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => setPreset(b.dataset.preset === STATE.preset ? null : b.dataset.preset)));
  const clear = host.querySelector("#preset-clear");
  if (clear) clear.addEventListener("click", () => setPreset(null));
  const toMap = host.querySelector("#preset-map");
  if (toMap) toMap.addEventListener("click", () => switchView("map", STATE.sport));
}

// Signals tab: preset bar + the shared table filtered/sorted by the preset. With no preset the
// tab lists every flagged game (any flag) for the current sport so the page is never empty.
function renderSignals() {
  const bar = document.getElementById("signalsbar");
  renderPresetBar(bar);
  const preset = activePreset();
  let rows;
  if (preset) rows = presetSort(presetGames(preset), preset);
  else {
    rows = currentGames();
    rows.sort((a, b) => wxNum(b, "wind_fg") - wxNum(a, "wind_fg"));
  }
  document.getElementById("rowcount").textContent = rows.length
    ? `${rows.length} ${preset ? preset.label : STATE.focus === "all" ? "inspected" : "signal / near-signal"} game${rows.length === 1 ? "" : "s"}`
    : (preset ? `no ${preset.label} games this week` : "no signal / near-signal candidates");
  renderTable(rows, { keepOrder: STATE.sort == null });
}
