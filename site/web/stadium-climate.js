"use strict";
// Longer outdoor game-window climate, beside the frozen W-L signal history.
// Every displayed cell shows N. Rows stay in season order and are not ranked.

const STADIUM_CLIMATE = {data: null, loading: null, error: null};
const CLIMATE_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

async function loadStadiumClimate(force = false) {
  if (STADIUM_CLIMATE.loading) return STADIUM_CLIMATE.loading;
  if (STADIUM_CLIMATE.data && !force) return STADIUM_CLIMATE.data;
  STADIUM_CLIMATE.error = null;
  STADIUM_CLIMATE.loading = fetchJson("/api/stadium-climate", {cache: "no-store"}).then(payload => {
    if (payload?.schema_version !== 1 || payload.kind !== "stadium_game_window_climate") throw new Error("Invalid climate schema");
    if (!Array.isArray(payload.windows) || !Array.isArray(payload.window_labels) || !Array.isArray(payload.cell_columns)
      || !payload.cell_columns.includes("n") || !payload.venues) throw new Error("Invalid climate schema");
    STADIUM_CLIMATE.data = payload;
  }).catch(error => {
    STADIUM_CLIMATE.data = null;
    STADIUM_CLIMATE.error = error.message || "Climate request failed";
  }).finally(() => { STADIUM_CLIMATE.loading = null; });
  return STADIUM_CLIMATE.loading;
}

function climateLocalParts(iso, timeZone) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  if (typeof timeZone !== "string" || (timeZone !== "UTC" && !timeZone.includes("/"))) return null;
  let parts;
  try {
    parts = new Intl.DateTimeFormat("en-US", {
      timeZone, month: "numeric", hour: "numeric", hourCycle: "h23",
    }).formatToParts(date);
  } catch (_) {
    return null;
  }
  const value = type => parts.find(part => part.type === type)?.value;
  let hour = Number(value("hour"));
  const month = Number(value("month"));
  if (hour === 24) hour = 0;
  if (!Number.isInteger(hour) || hour < 0 || hour > 23 || !Number.isInteger(month) || month < 1 || month > 12) return null;
  return {month, hour};
}

function climateWindowIndex(hour, bounds) {
  const rows = Array.isArray(bounds) && bounds.length ? bounds : [[0, 15], [15, 18], [18, 21], [21, 24]];
  for (let i = 0; i < rows.length; i++) {
    if (hour >= rows[i][0] && hour < rows[i][1]) return i;
  }
  return null;
}

function climateSeasonMonth(month) {
  return month >= 8 ? month : month + 12;
}

function climateYearLabel(years) {
  if (!Array.isArray(years) || !years.length) return "no seasons";
  const runs = [];
  let start = years[0], prev = years[0];
  for (const year of years.slice(1)) {
    if (year === prev + 1) { prev = year; continue; }
    runs.push(start === prev ? String(start) : `${start}–${prev}`);
    start = prev = year;
  }
  runs.push(start === prev ? String(start) : `${start}–${prev}`);
  return runs.join(", ");
}

function climateCells(payload, venue) {
  const columns = payload.cell_columns;
  return (venue.rows || []).map(row => Object.fromEntries(columns.map((key, index) => [key, row[index]])))
    .sort((a, b) => climateSeasonMonth(a.month) - climateSeasonMonth(b.month) || a.window - b.window);
}

function climateCount(label, count, n) {
  return `${label} ${count} of ${n}`;
}

function climateCellText(cell, payload) {
  const thin = cell.n < (payload.small_sample_n || 8);
  return `N=${cell.n}; wind p10/p50/p90 ${cell.wind_p10}/${cell.wind_p50}/${cell.wind_p90} mph; `
    + `temp p10/p50/p90 ${cell.temp_p10}/${cell.temp_p50}/${cell.temp_p90} °F; `
    + `rain p50/p90 ${cell.precip_p50}/${cell.precip_p90} mm; `
    + `${climateCount("wind ≥12 mph", cell.wind_ge12, cell.n)}; `
    + `${climateCount("wind ≥15 mph", cell.wind_ge15, cell.n)}; `
    + `${climateCount("rain ≥5 mm", cell.rain_ge5, cell.n)}; `
    + `${climateCount("rain ≥23 mm", cell.rain_ge23, cell.n)}`
    + (thin ? "; small sample; not for ranking venues" : "");
}

function climateWindowName(payload, index) {
  return payload.window_labels?.[index] || payload.windows?.[index] || "window";
}

function stadiumClimateView(g) {
  const data = STADIUM_CLIMATE.data;
  const caveat = "N is the number of games in that month and kickoff window. Do not rank venues by thin cells.";
  if (STADIUM_CLIMATE.error) return {status: "error", intro: `Game-window climate unavailable: ${STADIUM_CLIMATE.error}`, rows: []};
  if (!data) return {status: STADIUM_CLIMATE.loading ? "loading" : "missing", intro: "Game-window climate loading", rows: []};
  const stadium = g?.stadium || {};
  const span = Array.isArray(data.seasons) ? `${data.seasons[0]}–${data.seasons[1]}` : "the published seasons";
  const base = `ERA5 kickoff-to-+2h climate, ${span}. ${data.methodology?.weather || "Grid reanalysis, not a field sensor."} ${caveat}`;
  if (!stadium.stadium_id) return {status: "unresolved", intro: `Stadium not resolved. ${caveat}`, rows: []};
  if (stadium.roof_type === "dome") return {status: "dome", intro: `Dome. Outdoor game-window climate is not used. ${caveat}`, rows: []};
  const venue = data.venues?.[stadium.stadium_id];
  if (!venue) {
    const indoor = stadium.roof_type === "retractable"
      ? "Retractable roof. No games tagged open or outdoors are in this history."
      : "No completed outdoor games in this history.";
    return {status: stadium.roof_type === "retractable" ? "retractable-none" : "none", intro: `${indoor} ${caveat}`, rows: []};
  }
  const local = climateLocalParts(g.kickoff_utc, stadium.timezone);
  const windowIndex = local ? climateWindowIndex(local.hour, data.window_hours_local) : null;
  const cells = climateCells(data, venue);
  const rows = cells.map(cell => {
    const current = !!local && cell.month === local.month && cell.window === windowIndex;
    const name = `${CLIMATE_MONTHS[cell.month] || "Month"} · ${climateWindowName(data, cell.window)}`;
    const text = climateCellText(cell, data);
    return {
      label: current ? `This kickoff · ${name}` : name,
      value: text,
      compact: `Climate · ${name} · ${text}`,
      n: cell.n,
      current,
      thin: cell.n < (data.small_sample_n || 8),
      month: cell.month,
      window: cell.window,
    };
  });
  if (local && windowIndex !== null && !rows.some(row => row.current)) {
    const name = `${CLIMATE_MONTHS[local.month]} · ${climateWindowName(data, windowIndex)}`;
    rows.unshift({
      label: `This kickoff · ${name}`,
      value: "N=0; no sampled games in this month and window; not for ranking venues",
      compact: `Climate · ${name} · N=0; no sampled games in this window`,
      n: 0,
      current: true,
      thin: true,
      month: local.month,
      window: windowIndex,
    });
  }
  const roofNote = stadium.roof_type === "retractable"
    ? "Retractable roof: N counts only games tagged open or outdoors. "
    : "Open-air games only. ";
  return {
    status: "ok",
    intro: `${roofNote}NFL and CFB kickoffs at this stadium are combined. ${venue.n} games, seasons ${climateYearLabel(venue.seasons)}. ${base}`,
    rows,
  };
}

function stadiumClimateDetails(g) {
  const view = stadiumClimateView(g);
  const rows = view.rows.map(row => `<tr><td>${esc(row.label)}</td><td>${esc(row.value)}</td></tr>`).join("");
  const retry = STADIUM_CLIMATE.error ? '<button type="button" class="controlbtn retry-stadium-climate">Retry climate</button>' : "";
  return `<details class="stadium-climate-details"><summary>Stadium game-window climate · wind, temperature, rain</summary><p class="sub">${esc(view.intro)}</p>${rows ? `<table class="kv">${rows}</table>` : ""}${retry}</details>`;
}

function stadiumClimateCompact(g) {
  const view = stadiumClimateView(g);
  if (view.status === "ok") {
    const current = view.rows.find(row => row.current) || view.rows[0];
    return `<span class="sub stadium-climate">${esc(current.compact)}</span>`;
  }
  return `<span class="sub stadium-climate">${esc(view.intro)}</span>`;
}

function paintStadiumClimate() {
  if (typeof DRAWER === "undefined" || !DRAWER.game || typeof document === "undefined") return;
  const host = document.getElementById("drawer-stadium-climate");
  if (!host) return;
  const open = !!host.querySelector("details")?.open;
  host.innerHTML = stadiumClimateDetails(DRAWER.game);
  if (open) {
    const details = host.querySelector("details");
    if (details) details.open = true;
  }
}

if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("click", async event => {
    const button = event.target.closest?.(".retry-stadium-climate");
    if (!button) return;
    button.disabled = true;
    await loadStadiumClimate(true);
    if (typeof render === "function" && typeof STATE !== "undefined" && ["table", "map", "signals"].includes(STATE.view)) render();
    paintStadiumClimate();
  });
}
