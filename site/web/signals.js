"use strict";
// Signals view: preset filters replacing the old combined_signals.py page (ARCH §7.4 / §11).
// Presets render the published process.flags decision.

const PRESETS = {
  cfb_wind: { id: "cfb_wind", label: "CFB Wind", flag: "CFB Wind", sports: ["cfb"],
    desc: "Published college wind flag. Screening limits are recorded on each game", sort: (g) => wxNum(g, "wind_fg") },
  nfl_wind: { id: "nfl_wind", label: "NFL Wind", flag: "NFL Wind", sports: ["nfl"],
    desc: "Published NFL wind flag. Screening limits are recorded on each game", sort: (g) => wxNum(g, "wind_fg") },
  heat: { id: "heat", label: "Heat", flag: "Heat", sports: ["nfl", "cfb"],
    desc: "Published heat flag", sort: (g) => wxNum(g, "temp_fg") },
  alt_heat: { id: "alt_heat", label: "Alt+Heat", flag: "Alt+Heat", sports: ["cfb"],
    desc: "Published altitude and heat flag", sort: (g) => (isNum(g.travel_alt) ? Number(g.travel_alt) : -Infinity) },
};
const PRESET_ORDER = ["cfb_wind", "nfl_wind", "heat", "alt_heat"];
const wxNum = (g, k) => (g.weather && isNum(g.weather[k]) ? Number(g.weather[k]) : -Infinity);

function activePreset() {
  return (STATE.preset && PRESETS[STATE.preset]) || null;
}

function gameFlags(g) {
  const record = publishedDecision(g);
  return Array.isArray(record?.flags) ? record.flags : [];
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
      <span class="sub" title="Signal severity tiers are additional context">CFB Wind eligibility: published by the pipeline. Open a game for its applied thresholds. Severity tiers are extra.</span>
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
