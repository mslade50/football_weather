"use strict";
// Backtest tab (PLAN Phase 6): the bucket grid that replaced cfb_weather_backtest.xlsx
// "Backtesting", the per-stadium under records ("Stadiums" sheet) and the matched games
// list (the old bottom table of pages/cfb_weather.py). Also exports the first-match
// Record / ROI lookup the hover cards use (backtestHover / backtestMatch).
//
// /data/backtest.json (pipeline/backtest.py BacktestResult.payload -> R2 board/backtest.json):
//   { meta {run_id, generated_at, last_updated, bucket_on, n_games, n_graded, sources,
//           legacy {source "cfb_weather_backtest.xlsx", seasons "pre-2026", n_buckets 118}},
//     grid: [ { id, sport "NCAAF"|"NFL", Wind Above, Wind Below, Temp Above, Temp Below, Spread_l, Spread_h,
//               CLV from Open null|"Positive"|"Negative", Signal, Wins, Losses, Push, Sample, Margin, ROI,
//               "+ CLV", "CLV %", n_games, legacy {Wins, Losses, Push, Sample, Margin, ROI, "+ CLV", "CLV %"} } ],
//     stadium_results: [ { stadium_id, sport, season, Team, Stadium, Record "W-L-P", Percentage, under_w, under_l, under_p, roi, n } ],
//     stadium_results_legacy: [ { Team, Stadium, Record "W-L-P", Percentage, sport "cfb" } ]   (the xlsx Stadiums sheet),
//     postmortem: { methodology, latest {window_start, window_end, summary, bets},
//                   season {summary, by_week, by_sport, by_tier, by_book, by_model, bets} },
//     games: [ GameRow.to_dict(): game_id, sport, season, week, kickoff_utc, home_id, away_id, home_name, away_name,
//              stadium_name, roof_state, temp_fc, wind_fc, gust_fc, rain_fc, lead_fc, gs_fg_v1, gs_fg_v2,
//              temp_act, wind_act, rain_act, total_open, total_close, spread_open, spread_close, clv_status,
//              actual_total, under_result "W"|"L"|"P", margin, Signal, Sample, Margin, ROI ],
//     alerts_clv: { n, by_tier [..], by_league [..], by_book [..], by_model [ {key "v1"|"v2", n, avg_clv, pos, pos_frac} ],
//                   alerts [ {alert_key, season, week, ..., closing_line, clv_pts} ] } }
//   Snake-case spellings (wind_lo/wind_hi/.., wins/losses/.., team/stadium/record/pct, a `clv` block with a
//   dict-shaped by_model) are accepted too.
//
// First-match semantics (pages/cfb_weather.py get_backtesting_data): walk the grid in id
// order; wind_hi null -> 100, spread_lo null -> 0, temp_lo null -> 0; a null spread_hi never
// matches an NCAAF row (the legacy quirk) but NFL rows carry no spread bands, so both spread
// bounds null on an NFL row means "any spread"; the row's CLV must equal the game's CLV status
// (the aggregate null-CLV row is used only when no status can be computed). CLV status
// = "Positive" when the consensus total dropped from open (open > now), else "Negative".
//
// The grid shows two column groups: "2026 (this season)" (recomputed from graded games) and
// "Legacy sheet" (row.legacy = the xlsx numbers), the latter behind the #bt-legacy checkbox
// (on by default, remembered in localStorage). Until meta.n_graded > 0 a banner says the
// legacy numbers are what is being shown; the stadium table falls back to the sheet too.

const BT_LEGACY_KEY = "fw.btLegacy";
function loadBtLegacy() {
  try { return localStorage.getItem(BT_LEGACY_KEY) !== "0"; } catch (_) { return true; }
}
function saveBtLegacy(on) {
  try { localStorage.setItem(BT_LEGACY_KEY, on ? "1" : "0"); } catch (_) { /* private mode / blocked storage */ }
}
const BT_NO_GRADED_BANNER = "No graded 2026 games yet (first grading after Week 0 settles) — showing legacy sheet results.";

const BT = { data: null, loaded: false, loading: null, sport: "", section: "postmortem", q: "", sort: null, dir: -1, legacy: loadBtLegacy() };

const BT_SPORT = { nfl: "NFL", cfb: "NCAAF" };
const btNum = (...vals) => { for (const v of vals) if (isNum(v)) return Number(v); return null; };
const btStr = (...vals) => { for (const v of vals) if (v !== null && v !== undefined && v !== "") return String(v); return null; };

// the 8 result columns of a grid row (this season) or of its `legacy` block (the sheet)
const BT_STAT_KEYS = ["wins", "losses", "push", "sample", "margin", "roi", "pos_clv", "clv_pct"];
function normalizeStats(r) {
  return {
    wins: btNum(r.wins, r.Wins), losses: btNum(r.losses, r.Losses), push: btNum(r.push, r.Push),
    sample: btNum(r.sample, r.Sample), margin: btNum(r.margin, r.Margin), roi: btNum(r.roi, r.ROI),
    pos_clv: btNum(r.pos_clv, r.plus_clv, r["+ CLV"]), clv_pct: btNum(r.clv_pct, r["CLV %"]),
  };
}
function normalizeGridRow(raw, idx) {
  const r = raw || {};
  const clv = btStr(r.clv, r.clv_from_open, r["CLV from Open"]);
  const legacy = r.legacy && typeof r.legacy === "object" ? normalizeStats(r.legacy) : normalizeStats({});
  // sport code ("cfb"/"nfl") or label ("NCAAF"/"NFL") -> label; bucketMatch / the sport filter compare labels
  const sp = (btStr(r.Sport, r.sport) || "").toUpperCase();
  return {
    legacy,
    id: btNum(r.id, r.signal, r.Signal) ?? idx + 1,
    sport: BT_SPORT[sp.toLowerCase()] || sp,
    wind_lo: btNum(r.wind_lo, r.wind_above, r["Wind Above"]),
    wind_hi: btNum(r.wind_hi, r.wind_below, r["Wind Below"]),
    temp_lo: btNum(r.temp_lo, r.temp_above, r["Temp Above"]),
    temp_hi: btNum(r.temp_hi, r.temp_below, r["Temp Below"]),
    spread_lo: btNum(r.spread_lo, r.spread_l, r.Spread_l),
    spread_hi: btNum(r.spread_hi, r.spread_h, r.Spread_h),
    clv: clv && /^(pos|neg)/i.test(clv) ? (/^pos/i.test(clv) ? "Positive" : "Negative") : null,
    ...normalizeStats(r),
  };
}
function normalizeStadiumRow(raw, legacy = false) {
  const r = raw || {};
  let wins = btNum(r.wins), losses = btNum(r.losses), push = btNum(r.push);
  const rec = btStr(r.record, r.Record);
  if (rec && (wins === null || losses === null)) {
    const m = rec.match(/^(\d+)-(\d+)(?:-(\d+))?$/);
    if (m) { wins = Number(m[1]); losses = Number(m[2]); push = m[3] ? Number(m[3]) : 0; }
  }
  return {
    team: btStr(r.team, r.Team) || "", stadium: btStr(r.stadium, r.Stadium) || "",
    wins, losses, push: push ?? 0,
    record: rec || (wins !== null && losses !== null ? `${wins}-${losses}-${push ?? 0}` : "—"),
    pct: btNum(r.pct, r.percentage, r.Percentage),
    legacy,
  };
}
function normalizeBtGame(raw) {
  const r = raw || {};
  const gid = btStr(r.game_id);
  const ur = btStr(r.under_result);
  return {
    game_id: gid,
    sport: (btStr(r.sport) || (gid ? gid.split(":")[0] : "") || "").toLowerCase(),
    season: btNum(r.season), week: btNum(r.week),
    kickoff_utc: btStr(r.kickoff_utc, r.kickoff), date_label: btStr(r.date_label, r.date),
    away: btStr(r.away_name, r.away && r.away.short, r.away && r.away.name, typeof r.away === "string" ? r.away : null, r.away_id) || "?",
    home: btStr(r.home_name, r.home && r.home.short, r.home && r.home.name, typeof r.home === "string" ? r.home : null, r.home_id) || "?",
    stadium: btStr(r.stadium_name, r.stadium && r.stadium.name, typeof r.stadium === "string" ? r.stadium : null) || "",
    wind_fg: btNum(r.wind_fc, r.wind_fg), temp_fg: btNum(r.temp_fc, r.temp_fg), rain_fg: btNum(r.rain_fc, r.rain_fg),
    wind_actual: btNum(r.wind_act, r.wind_actual), temp_actual: btNum(r.temp_act, r.temp_actual), rain_actual: btNum(r.rain_act, r.rain_actual),
    lead_hours: btNum(r.lead_fc, r.lead_hours),
    spread_open: btNum(r.spread_open), spread_close: btNum(r.spread_close),
    total_open: btNum(r.total_open), total_close: btNum(r.total_close), total_actual: btNum(r.actual_total, r.total_actual, r.total_pts),
    under_hit: ur ? (ur === "W" ? true : ur === "L" ? false : null)
      : (r.under_hit === true || r.under_hit === 1 ? true : (r.under_hit === false || r.under_hit === 0 ? false : null)),
    margin: btNum(r.margin),
    clv_pts: btNum(r.clv_pts), clv_status: btStr(r.clv_status),
    bucket_id: btNum(r.Signal, r.bucket_id, r.signal),
    bucket_sample: btNum(r.Sample), bucket_roi: btNum(r.ROI),
    gs_v1: btNum(r.gs_fg_v1, r.gs_fg_pct_v1, r.gs_v1), gs_v2: btNum(r.gs_fg_v2, r.gs_fg_pct_v2, r.gs_v2),
  };
}
// alerts_clv.by_model (list of {key, n, avg_clv, pos_frac}) or clv.by_model (dict) -> {model: {n, avg, pos}}
function normalizeClv(d) {
  const clv = d.alerts_clv && typeof d.alerts_clv === "object" ? d.alerts_clv : (d.clv && typeof d.clv === "object" ? d.clv : null);
  if (!clv) return null;
  const models = {};
  const raw = clv.by_model;
  const add = (k, m) => { models[k] = { n: btNum(m.n), avg: btNum(m.avg_clv, m.avg), pos: btNum(m.pos_frac, m.pos_rate, m.pos_pct) }; };
  if (Array.isArray(raw)) raw.forEach((m) => { if (m && m.key != null) add(String(m.key), m); });
  else if (raw && typeof raw === "object") Object.entries(raw).forEach(([k, m]) => { if (m && typeof m === "object") add(k, m); });
  let weeks = btNum(clv.weeks);
  if (weeks === null && Array.isArray(clv.alerts)) weeks = new Set(clv.alerts.filter((a) => a && a.week != null).map((a) => `${a.season}-${a.week}`)).size;
  return { n: btNum(clv.n), weeks, by_model: models, alerts: Array.isArray(clv.alerts) ? clv.alerts : [] };
}
function normalizePmStats(raw) {
  const stats = raw || {};
  return { bets: btNum(stats.bets) ?? 0, graded: btNum(stats.graded) ?? 0, pending: btNum(stats.pending) ?? 0,
    wins: btNum(stats.wins) ?? 0, losses: btNum(stats.losses) ?? 0, pushes: btNum(stats.pushes) ?? 0,
    units: btNum(stats.units), roi: btNum(stats.roi), priced_bets: btNum(stats.priced_bets),
    clv_bets: btNum(stats.clv_bets), avg_clv: btNum(stats.avg_clv), beat_close: btNum(stats.beat_close),
    tied_close: btNum(stats.tied_close), beat_close_rate: btNum(stats.beat_close_rate), avg_first_edge: btNum(stats.avg_first_edge),
    avg_lead_hours: btNum(stats.avg_lead_hours), weather_checked: btNum(stats.weather_checked),
    actual_windy_games: btNum(stats.actual_windy_games),
    wind_thesis_games: btNum(stats.wind_thesis_games), wind_thesis_checked: btNum(stats.wind_thesis_checked),
    wind_thesis_pending: btNum(stats.wind_thesis_pending), wind_materialized: btNum(stats.wind_materialized),
    wind_materialization_rate: btNum(stats.wind_materialization_rate), first_wind_mae_mph: btNum(stats.first_wind_mae_mph),
    first_wind_bias_mph: btNum(stats.first_wind_bias_mph), final_wind_mae_mph: btNum(stats.final_wind_mae_mph),
    final_wind_bias_mph: btNum(stats.final_wind_bias_mph), first_temp_mae_f: btNum(stats.first_temp_mae_f),
    final_temp_mae_f: btNum(stats.final_temp_mae_f), first_rain_mae_mm: btNum(stats.first_rain_mae_mm),
    final_rain_mae_mm: btNum(stats.final_rain_mae_mm) };
}
function normalizePmBet(raw) {
  const b = raw || {}, fw = b.first_weather || {}, lf = b.final_forecast || {}, aw = b.actual_weather || {};
  return { ...b, sport: String(b.sport || "").toLowerCase(), away: btStr(b.away) || "?", home: btStr(b.home) || "?",
    stadium: btStr(b.stadium) || "", first_line: btNum(b.first_line), first_odds: btNum(b.first_odds),
    first_fair: btNum(b.first_fair), first_edge: btNum(b.first_edge), closing_line: btNum(b.closing_line),
    clv_pts: btNum(b.clv_pts), hours_before_kickoff: btNum(b.hours_before_kickoff), unit_profit: btNum(b.unit_profit),
    actual_total: btNum(b.actual_total), home_score: btNum(b.home_score), away_score: btNum(b.away_score),
    first_weather: { temp_f: btNum(fw.temp_f), wind_mph: btNum(fw.wind_mph), gust_mph: btNum(fw.gust_mph), rain_mm: btNum(fw.rain_mm) },
    final_forecast: { temp_f: btNum(lf.temp_f), wind_mph: btNum(lf.wind_mph), gust_mph: btNum(lf.gust_mph), rain_mm: btNum(lf.rain_mm) },
    actual_weather: { temp_f: btNum(aw.temp_f), wind_mph: btNum(aw.wind_mph), gust_mph: btNum(aw.gust_mph), rain_mm: btNum(aw.rain_mm) } };
}
function normalizePostmortem(d) {
  const pm = d.postmortem && typeof d.postmortem === "object" ? d.postmortem : {};
  const latest = pm.latest && typeof pm.latest === "object" ? pm.latest : {};
  const season = pm.season && typeof pm.season === "object" ? pm.season : {};
  const normRollup = (rows) => (Array.isArray(rows) ? rows : []).map((r) => ({ ...r, ...normalizePmStats(r) }));
  return { methodology: pm.methodology || {},
    latest: { window_start: btStr(latest.window_start), window_end: btStr(latest.window_end),
      summary: normalizePmStats(latest.summary), bets: (Array.isArray(latest.bets) ? latest.bets : []).map(normalizePmBet) },
    season: { summary: normalizePmStats(season.summary), bets: (Array.isArray(season.bets) ? season.bets : []).map(normalizePmBet),
      by_week: normRollup(season.by_week), by_sport: normRollup(season.by_sport), by_tier: normRollup(season.by_tier),
      by_book: normRollup(season.by_book), by_model: normRollup(season.by_model) } };
}
function normalizeBacktest(payload) {
  const d = payload && typeof payload === "object" ? payload : {};
  const meta = d.meta && typeof d.meta === "object" ? d.meta : {};
  const grid = (Array.isArray(d.grid) ? d.grid : Array.isArray(d.buckets) ? d.buckets : [])
    .map(normalizeGridRow).filter((row) => row.sport).sort((x, y) => x.id - y.id);
  const stadiums = (Array.isArray(d.stadium_results) ? d.stadium_results : Array.isArray(d.stadiums) ? d.stadiums : [])
    .map((row) => normalizeStadiumRow(row, false)).filter((row) => row.stadium || row.team);
  const stadiums_legacy = (Array.isArray(d.stadium_results_legacy) ? d.stadium_results_legacy : [])
    .map((row) => normalizeStadiumRow(row, true)).filter((row) => row.stadium || row.team);
  const games = (Array.isArray(d.games) ? d.games : Array.isArray(d.matched_games) ? d.matched_games : []).map(normalizeBtGame);
  const clv = normalizeClv(d);
  const postmortem = normalizePostmortem(d);
  const lg = meta.legacy && typeof meta.legacy === "object" ? meta.legacy : {};
  const legacy = { source: btStr(lg.source), seasons: btStr(lg.seasons), n_buckets: btNum(lg.n_buckets) };
  return { run_id: meta.run_id || d.run_id || null, generated_at: meta.generated_at || meta.last_updated || d.generated_at || d.last_updated || null,
    bucket_on: meta.bucket_on || null, n_graded: btNum(meta.n_graded), weeks: btNum(d.weeks, clv && clv.weeks),
    grid, stadiums, stadiums_legacy, games, clv, postmortem, legacy };
}

async function loadBacktest(force = false) {
  if (BT.data && !force) return BT.data;
  if (BT.loading && !force) return BT.loading;
  BT.loading = (async () => {
    let payload = null;
    try { payload = await fetchJson("data/backtest.json?t=" + Date.now()); } catch (_) { payload = null; }
    BT.data = normalizeBacktest(payload);
    BT.loaded = true;
    BT.loading = null;
    return BT.data;
  })();
  return BT.loading;
}

// ── first-match lookup (hover Record / ROI) ───────────────────────────────
function clvStatusOf(open, now) {
  if (!isNum(open) || !isNum(now)) return null;
  return Number(open) > Number(now) ? "Positive" : "Negative";
}
function bucketMatch(grid, sportLabel, wind, temp, spreadAbs, clvStatus) {
  if (!grid || !isNum(wind) || !isNum(temp)) return null;
  for (const row of grid) {
    if (row.sport !== sportLabel) continue;
    const wlo = row.wind_lo ?? 0, whi = row.wind_hi ?? 100;
    const tlo = row.temp_lo ?? 0, thi = row.temp_hi ?? 200;
    if (!(wlo <= wind && wind <= whi && tlo <= temp && temp <= thi)) continue;
    // legacy: `CLV from Open == status` — the aggregate (NaN) row never matches when a status is known
    if (clvStatus ? row.clv !== clvStatus : row.clv !== null) continue;
    if (row.spread_hi === null && row.spread_lo === null) {
      if (sportLabel !== "NFL") continue;             // legacy: NaN Spread_h never matches an NCAAF row
    } else {
      if (row.spread_hi === null || !isNum(spreadAbs)) continue;
      if (!((row.spread_lo ?? 0) <= spreadAbs && spreadAbs <= row.spread_hi)) continue;
    }
    return row;
  }
  return null;
}
function backtestMatch(g) {
  if (!g || !BT.data || isDome(g)) return null;
  const wx = g.weather || {}, c = g.consensus || {};
  const spread = isNum(c.spread_open) ? Math.abs(Number(c.spread_open)) : (isNum(c.spread_now) ? Math.abs(Number(c.spread_now)) : null);
  return bucketMatch(BT.data.grid, BT_SPORT[g.sport] || String(g.sport || "").toUpperCase(), wx.wind_fg, wx.temp_fg, spread,
    clvStatusOf(c.total_open, c.total_now));
}
function fmtRoi(v) { return isNum(v) ? `${Number(v) >= 0 ? "+" : ""}${(Number(v) * 100).toFixed(1)}%` : "—"; }
const fmtInt = (v) => (isNum(v) ? String(Math.round(Number(v))) : "—");
const fmtPct = (v) => (isNum(v) ? `${(Number(v) * 100).toFixed(1)}%` : "—");
function fmtRecord(stats) {
  if (!stats) return "—";
  return `${fmtInt(stats.wins ?? 0)}-${fmtInt(stats.losses ?? 0)}-${fmtInt(stats.push ?? 0)}`;
}
// this-season numbers when the bucket has graded games, else the sheet's; `.src` says which
function bucketStats(row) {
  if (!row) return null;
  if (isNum(row.sample) && Number(row.sample) > 0) return { ...normalizeStats(row), src: "2026" };
  const lg = row.legacy || normalizeStats({});
  return { ...lg, src: "legacy sheet" };
}
// [[label, value], ...] for hover cards / drawer (empty when no bucket matched or no data)
function backtestHover(g) {
  const row = backtestMatch(g);
  if (!row) return [];
  const stats = bucketStats(row);
  return [
    ["Record (under)", `${fmtRecord(stats)} · n=${fmtInt(stats.sample)} · ${stats.src}`],
    ["ROI", `${fmtRoi(stats.roi)}${isNum(stats.margin) ? ` · margin ${Number(stats.margin) >= 0 ? "+" : ""}${Number(stats.margin).toFixed(2)}` : ""}`],
    ["Bucket", `#${row.id} ${bucketLabel(row)}`],
  ];
}
// this-season stadium rows when any exist, else the legacy sheet (row.legacy = true)
function stadiumRows() {
  const d = BT.data || { stadiums: [], stadiums_legacy: [] };
  return d.stadiums.length ? d.stadiums : (d.stadiums_legacy || []);
}
function stadiumResultFor(g) {
  if (!g || !BT.data) return null;
  const name = ((g.stadium && g.stadium.name) || "").toLowerCase();
  const home = ((g.home && (g.home.name || g.home.short)) || "").toLowerCase();
  return stadiumRows().find((row) => (name && row.stadium.toLowerCase() === name) || (home && row.team.toLowerCase() === home)) || null;
}

// ── rendering ─────────────────────────────────────────────────────────────
const band = (lo, hi, unit = "") => {
  if (lo === null && hi === null) return "any";
  if (lo === null) return `≤ ${hi}${unit}`;
  if (hi === null) return `≥ ${lo}${unit}`;
  return `${lo}–${hi}${unit}`;
};
function bucketLabel(row) {
  return `wind ${band(row.wind_lo, row.wind_hi)} · temp ${band(row.temp_lo, row.temp_hi, "°")}`
    + (row.spread_lo !== null || row.spread_hi !== null ? ` · spread ${band(row.spread_lo, row.spread_hi)}` : "")
    + (row.clv ? ` · CLV ${row.clv.toLowerCase()}` : "");
}
const roiClass = (v) => (isNum(v) ? (Number(v) > 0 ? "up" : Number(v) < 0 ? "dn" : "") : "");

// grid sort keys: "wins".."clv_pct" (this season) or "l:wins".."l:clv_pct" (legacy sheet); null = sheet order (id)
function gridSortValue(row, key) {
  if (!key) return row.id;
  const [grp, k] = key.startsWith("l:") ? ["legacy", key.slice(2)] : ["", key];
  const v = grp ? (row.legacy || {})[k] : row[k];
  return isNum(v) ? Number(v) : null;
}
function sortGridRows(rows) {
  if (!BT.sort) return rows.slice().sort((x, y) => x.id - y.id);
  return rows.slice().sort((x, y) => {
    const a = gridSortValue(x, BT.sort), b = gridSortValue(y, BT.sort);
    if (a === null && b === null) return x.id - y.id;
    if (a === null) return 1;                     // nulls last regardless of direction
    if (b === null) return -1;
    return a === b ? x.id - y.id : (a - b) * BT.dir;
  });
}
const BT_STAT_HEAD = [
  ["wins", "W", ""], ["losses", "L", ""], ["push", "P", ""], ["sample", "n", "graded games (W+L+P)"],
  ["margin", "Margin", "avg margin (pts) of the under: close total − actual"], ["roi", "ROI", "(W·100/110 − L) / n"],
  ["pos_clv", "+CLV", "games whose total closed below the open"], ["clv_pct", "CLV %", "+CLV / n"],
];
function statHeadHtml(prefix) {
  return BT_STAT_HEAD.map(([k, label, title]) => {
    const key = `${prefix}${k}`;
    const on = BT.sort === key;
    return `<th class="sortable${on ? " sorted" : ""}" data-sort="${key}"${title ? ` title="${esc(title)}"` : ""}>${esc(label)}${on ? (BT.dir < 0 ? " ▼" : " ▲") : ""}</th>`;
  }).join("");
}
function statCellsHtml(stats, cls = "") {
  const td = (inner, extra = "") => `<td class="${cls}${extra ? ` ${extra}` : ""}">${inner}</td>`;
  return td(fmtInt(stats.wins)) + td(fmtInt(stats.losses)) + td(fmtInt(stats.push)) + td(fmtInt(stats.sample))
    + td(isNum(stats.margin) ? fmtNum(stats.margin, 2) : "—")
    + td(`<b>${fmtRoi(stats.roi)}</b>`, roiClass(stats.roi))
    + td(fmtInt(stats.pos_clv)) + td(fmtPct(stats.clv_pct));
}
function gridSectionHtml(rows) {
  const withLegacy = BT.legacy;
  const ncols = 6 + 8 + (withLegacy ? 8 : 0);
  const legacyTitle = BT.data && BT.data.legacy && BT.data.legacy.source
    ? `${BT.data.legacy.source}${BT.data.legacy.seasons ? ` · ${BT.data.legacy.seasons}` : ""}` : "cfb_weather_backtest.xlsx";
  const groups = `<tr class="bt-grp"><th colspan="6"></th><th colspan="8" class="grp" title="recomputed from this season's graded games (under at the closing total)">2026 (this season)</th>`
    + (withLegacy ? `<th colspan="8" class="grp lg" title="${esc(legacyTitle)}">Legacy sheet</th>` : "") + `</tr>`;
  const head = `<tr class="bt-cols"><th>#</th><th class="left">Sport</th><th>Wind</th><th>Temp</th><th>Spread</th><th class="left">CLV</th>`
    + statHeadHtml("") + (withLegacy ? statHeadHtml("l:") : "") + `</tr>`;
  const body = sortGridRows(rows).map((row) => `<tr class="bt-row${row.clv ? " sub" : ""}${BT.hl === row.id ? " hl" : ""}" data-id="${row.id}">`
    + `<td class="left">${row.id}</td><td class="left">${esc(row.sport)}</td>`
    + `<td>${esc(band(row.wind_lo, row.wind_hi))}</td><td>${esc(band(row.temp_lo, row.temp_hi, "°"))}</td>`
    + `<td>${row.spread_lo === null && row.spread_hi === null ? "—" : esc(band(row.spread_lo, row.spread_hi))}</td>`
    + `<td class="left">${row.clv ? `<span class="mv ${row.clv === "Positive" ? "up" : "dn"}">${esc(row.clv)}</span>` : "all"}</td>`
    + statCellsHtml(row) + (withLegacy ? statCellsHtml(row.legacy || {}, "lg") : "") + `</tr>`).join("");
  return `<div class="wrap bt-wrap"><table class="bt bt-grid"><thead>${groups}${head}</thead><tbody>${body || `<tr><td colspan="${ncols}" class="empty">no grid rows</td></tr>`}</tbody></table></div>`;
}
function stadiumSectionHtml(rows) {
  const legacy = rows.length > 0 && rows.every((row) => row.legacy);
  const sorted = rows.slice().sort((x, y) => (BT.sort === "record" ? (y.wins ?? 0) - (x.wins ?? 0) : (y.pct ?? -9) - (x.pct ?? -9)));
  const head = `<tr><th class="left">Team</th><th class="left">Stadium</th><th>Record (under)</th><th>n</th><th title="under hit rate minus 0.5238 (−110 breakeven)">Pct</th></tr>`;
  const body = sorted.map((row) => `<tr><td class="left">${esc(row.team || "—")}</td><td class="left">${esc(row.stadium)}</td>`
    + `<td>${esc(row.record)}</td><td>${(row.wins ?? 0) + (row.losses ?? 0) + (row.push ?? 0) || "—"}</td>`
    + `<td class="${roiClass(row.pct)}">${isNum(row.pct) ? fmtNum(row.pct, 3) : "—"}</td></tr>`).join("");
  const label = legacy ? `<div class="sub bt-note">Stadium under records (legacy sheet) — no 2026 stadium results graded yet.</div>` : "";
  return `${label}<div class="wrap bt-wrap"><table class="bt"><thead>${head}</thead><tbody>${body || `<tr><td colspan="5" class="empty">no stadium results</td></tr>`}</tbody></table></div>`;
}
function gamesSectionHtml(rows) {
  if (!rows.length && !(BT.data && BT.data.games.length)) return `<div class="empty">none graded yet</div>`;
  const head = `<tr><th class="left">Date</th><th class="left">Sport</th><th class="left">Game</th><th class="left">Stadium</th>
    <th title="forecast at alert lead → actual at kickoff">Wind fc → act</th><th>Temp fc → act</th><th>Rain</th>
    <th>Spread open → close</th><th>Total open → close</th><th title="final total points; U = under hit">Result</th>
    <th>CLV</th><th title="first-match bucket id">Bucket</th><th>gs v1 / v2</th></tr>`;
  const body = rows.map((row) => {
    const res = isNum(row.total_actual) ? `${fmtTotal(row.total_actual)}${row.under_hit === true ? ' <span class="mv up">U</span>' : row.under_hit === false ? ' <span class="mv dn">O</span>' : ""}` : "—";
    const clv = isNum(row.clv_pts) ? `<span class="mv ${row.clv_pts >= 0 ? "up" : "dn"}">${row.clv_pts >= 0 ? "+" : ""}${Number(row.clv_pts).toFixed(1)}</span>` : "—";
    return `<tr class="bt-game${row.game_id ? " link" : ""}" data-game="${esc(row.game_id || "")}" data-bucket="${row.bucket_id ?? ""}">`
      + `<td class="left">${esc(row.date_label || (row.kickoff_utc ? fmtShortET(row.kickoff_utc) : "—"))}${row.week != null ? ` <span class="sub">wk ${esc(row.week)}</span>` : ""}</td>`
      + `<td class="left">${esc(row.sport.toUpperCase())}</td><td class="left game">${esc(row.away)} @ ${esc(row.home)}</td><td class="left">${esc(row.stadium)}</td>`
      + `<td>${fmtNum(row.wind_fg, 1)} → ${fmtNum(row.wind_actual, 1)}</td><td>${fmtNum(row.temp_fg, 0)} → ${fmtNum(row.temp_actual, 0)}</td>`
      + `<td>${fmtNum(row.rain_fg, 1)}${isNum(row.rain_actual) ? ` → ${fmtNum(row.rain_actual, 1)}` : ""}</td>`
      + `<td>${fmtLine(row.spread_open)} → ${fmtLine(row.spread_close)}</td><td>${fmtTotal(row.total_open)} → ${fmtTotal(row.total_close)}</td>`
      + `<td>${res}</td><td>${clv}</td><td>${row.bucket_id != null ? `#${row.bucket_id}` : "—"}</td>`
      + `<td>${fmtNum(row.gs_v1, 1)} / ${fmtNum(row.gs_v2, 1)}</td></tr>`;
  }).join("");
  return `<div class="wrap bt-wrap"><table class="bt"><thead>${head}</thead><tbody>${body || `<tr><td colspan="13" class="empty">no matched games</td></tr>`}</tbody></table></div>`;
}
function clvSummaryHtml(clv) {
  if (!clv || !Object.keys(clv.by_model || {}).length) return "";
  const cell = (k) => {
    const m = clv.by_model[k] || {};
    const avg = m.avg, pos = m.pos;
    return `<b>${esc(k)}</b> n=${m.n ?? "?"} avg ${isNum(avg) ? (avg >= 0 ? "+" : "") + Number(avg).toFixed(2) : "—"}${isNum(pos) ? ` · +CLV ${Math.round(pos * 100)}%` : ""}`;
  };
  const models = Object.keys(clv.by_model).sort();
  const v1 = (clv.by_model.v1 || {}).avg, v2 = (clv.by_model.v2 || {}).avg;
  const weeks = clv.weeks;
  let gate = "";
  if (isNum(v1) && isNum(v2)) {
    const ok = v2 >= v1 && isNum(weeks) && weeks >= 4;
    gate = `<span class="pill ${ok ? "ok" : "warn"}" title="promotion rule: ALERT_MODEL=v2 only when v2 CLV ≥ v1 over ≥ 4 weeks">${ok ? "v2 promotion eligible" : "keep v1"}</span>`;
  }
  return `<div class="statusbar bt-clv">${models.map((k) => `<span class="seg">${cell(k)}</span>`).join('<span class="sep">|</span>')}${isNum(weeks) ? `<span class="sep">|</span><span class="seg">${weeks} wk</span>` : ""}${gate}</div>`;
}

function pmStats(bets) {
  const graded = bets.filter((b) => ["W", "L", "P"].includes(b.result));
  const nums = (key) => bets.map((b) => b[key]).filter(isNum).map(Number);
  const clv = nums("clv_pts"), units = graded.map((b) => b.unit_profit).filter(isNum).map(Number);
  const thesis = bets.filter((b) => b.wind_thesis);
  const thesisChecked = thesis.filter((b) => typeof b.wind_materialized === "boolean");
  const firstErr = bets.filter((b) => isNum(b.first_weather.wind_mph) && isNum(b.actual_weather.wind_mph))
    .map((b) => Number(b.first_weather.wind_mph) - Number(b.actual_weather.wind_mph));
  const finalErr = bets.filter((b) => isNum(b.final_forecast.wind_mph) && isNum(b.actual_weather.wind_mph))
    .map((b) => Number(b.final_forecast.wind_mph) - Number(b.actual_weather.wind_mph));
  const firstTempErr = bets.filter((b) => isNum(b.first_weather.temp_f) && isNum(b.actual_weather.temp_f))
    .map((b) => Number(b.first_weather.temp_f) - Number(b.actual_weather.temp_f));
  const finalTempErr = bets.filter((b) => isNum(b.final_forecast.temp_f) && isNum(b.actual_weather.temp_f))
    .map((b) => Number(b.final_forecast.temp_f) - Number(b.actual_weather.temp_f));
  const firstRainErr = bets.filter((b) => isNum(b.first_weather.rain_mm) && isNum(b.actual_weather.rain_mm))
    .map((b) => Number(b.first_weather.rain_mm) - Number(b.actual_weather.rain_mm));
  const finalRainErr = bets.filter((b) => isNum(b.final_forecast.rain_mm) && isNum(b.actual_weather.rain_mm))
    .map((b) => Number(b.final_forecast.rain_mm) - Number(b.actual_weather.rain_mm));
  const avg = (xs) => xs.length ? xs.reduce((a, x) => a + x, 0) / xs.length : null;
  return { bets: bets.length, graded: graded.length, pending: bets.length - graded.length,
    wins: graded.filter((b) => b.result === "W").length, losses: graded.filter((b) => b.result === "L").length,
    pushes: graded.filter((b) => b.result === "P").length, units: units.length ? units.reduce((a, x) => a + x, 0) : null,
    roi: units.length ? units.reduce((a, x) => a + x, 0) / units.length : null, clv_bets: clv.length,
    avg_clv: avg(clv), beat_close: clv.filter((x) => x > 0).length,
    beat_close_rate: clv.length ? clv.filter((x) => x > 0).length / clv.length : null,
    wind_thesis_games: thesis.length, wind_thesis_checked: thesisChecked.length,
    wind_thesis_pending: thesis.length - thesisChecked.length, wind_materialized: thesisChecked.filter((b) => b.wind_materialized).length,
    wind_materialization_rate: thesisChecked.length ? thesisChecked.filter((b) => b.wind_materialized).length / thesisChecked.length : null,
    first_wind_mae_mph: firstErr.length ? avg(firstErr.map(Math.abs)) : null,
    final_wind_mae_mph: finalErr.length ? avg(finalErr.map(Math.abs)) : null,
    first_temp_mae_f: firstTempErr.length ? avg(firstTempErr.map(Math.abs)) : null,
    final_temp_mae_f: finalTempErr.length ? avg(finalTempErr.map(Math.abs)) : null,
    first_rain_mae_mm: firstRainErr.length ? avg(firstRainErr.map(Math.abs)) : null,
    final_rain_mae_mm: finalRainErr.length ? avg(finalRainErr.map(Math.abs)) : null };
}
function pmSummaryHtml(label, summary) {
  const record = `${summary.wins || 0}-${summary.losses || 0}-${summary.pushes || 0}`;
  const close = summary.clv_bets ? `${summary.beat_close}/${summary.clv_bets} beat · ${isNum(summary.avg_clv) ? (summary.avg_clv >= 0 ? "+" : "") + Number(summary.avg_clv).toFixed(2) : "—"} avg` : "pending";
  const wind = summary.wind_thesis_checked ? `${summary.wind_materialized}/${summary.wind_thesis_checked} held${summary.wind_thesis_pending ? ` · ${summary.wind_thesis_pending} pending` : ""}`
    : summary.wind_thesis_games ? `${summary.wind_thesis_games} pending` : "no wind thesis";
  const mae = (first, final, unit) => isNum(first) ? `${Number(first).toFixed(1)}${isNum(final) ? `→${Number(final).toFixed(1)}` : ""}${unit}` : "—";
  return `<div class="bt-pm-card"><span class="sub">${esc(label)}</span><b>${record}</b>`
    + `<span>${isNum(summary.units) ? `${summary.units >= 0 ? "+" : ""}${Number(summary.units).toFixed(2)}u · ${fmtRoi(summary.roi)}` : `${summary.graded || 0} graded`}</span>`
    + `<span>Close: ${close}</span><span>Wind: ${wind}</span>`
    + `<span class="bt-pm-accuracy">Forecast MAE first→final: wind ${mae(summary.first_wind_mae_mph, summary.final_wind_mae_mph, " mph")} · temp ${mae(summary.first_temp_mae_f, summary.final_temp_mae_f, "°F")} · rain ${mae(summary.first_rain_mae_mm, summary.final_rain_mae_mm, " mm")}</span></div>`;
}
function postmortemSectionHtml(pm, bets) {
  const latestStats = pmStats(bets);
  const range = pm.latest.window_start && pm.latest.window_end ? `${fmtShortET(pm.latest.window_start)} – ${fmtShortET(pm.latest.window_end)}` : "Last 7 days";
  const summary = `<div class="bt-pm-summary">${pmSummaryHtml(range, latestStats)}${pmSummaryHtml("Season", pm.season.summary)}</div>`;
  const method = pm.methodology || {}, windThreshold = btNum(method.wind_materialization_mph) ?? 12;
  const note = `<div class="sub bt-note">First = first successfully sent actionable alert. Close = same-book pre-kick line. Actual weather = venue historical-model estimate, kickoff through +2h. Wind “held” = forecast and actual both ≥ ${fmtNum(windThreshold, 0)} mph.</div>`;
  const weeks = (pm.season.by_week || []).map((w) => `<tr><td class="left">${esc(String(w.sport || "").toUpperCase())} ${esc(w.season)} wk ${esc(w.week)}</td>`
    + `<td>${w.wins}-${w.losses}-${w.pushes}</td><td class="${roiClass(w.units)}">${isNum(w.units) ? `${w.units >= 0 ? "+" : ""}${Number(w.units).toFixed(2)}u` : "—"}</td>`
    + `<td>${w.clv_bets ? `${w.beat_close}/${w.clv_bets}` : "—"}</td><td>${isNum(w.avg_clv) ? `${w.avg_clv >= 0 ? "+" : ""}${Number(w.avg_clv).toFixed(2)}` : "—"}</td>`
    + `<td>${w.wind_thesis_checked ? `${w.wind_materialized}/${w.wind_thesis_checked}` : (w.wind_thesis_games ? "pending" : "—")}</td></tr>`).join("");
  const weekTable = weeks ? `<details class="bt-pm-weeks"><summary>Season by week</summary><div class="wrap"><table class="bt"><thead><tr><th class="left">Week</th><th>Record</th><th>Units</th><th>Beat close</th><th>Avg CLV</th><th>Wind held</th></tr></thead><tbody>${weeks}</tbody></table></div></details>` : "";
  const splitRows = [["Sport", pm.season.by_sport], ["Signal", pm.season.by_tier], ["Book", pm.season.by_book], ["Model", pm.season.by_model]]
    .flatMap(([dimension, rows]) => (rows || []).map((row) => `<tr><td class="left">${esc(dimension)}</td><td class="left">${esc(String(row.key || "?").replace("_", " "))}</td>`
      + `<td>${row.wins}-${row.losses}-${row.pushes}</td><td class="${roiClass(row.units)}">${isNum(row.units) ? `${row.units >= 0 ? "+" : ""}${Number(row.units).toFixed(2)}u` : "—"}</td>`
      + `<td>${row.clv_bets ? `${row.beat_close}/${row.clv_bets}` : "—"}</td><td>${isNum(row.avg_clv) ? `${row.avg_clv >= 0 ? "+" : ""}${Number(row.avg_clv).toFixed(2)}` : "—"}</td></tr>`)).join("");
  const splitTable = splitRows ? `<details class="bt-pm-weeks"><summary>Season splits</summary><div class="wrap"><table class="bt"><thead><tr><th class="left">Split</th><th class="left">Group</th><th>Record</th><th>Units</th><th>Beat close</th><th>Avg CLV</th></tr></thead><tbody>${splitRows}</tbody></table></div></details>` : "";
  const head = `<tr><th class="left">Game</th><th class="left">First edge</th><th>Close</th><th>Result</th><th>Wind: first → final fc → actual</th><th>Temp: first → actual</th><th>Rain: first → actual</th></tr>`;
  const body = bets.map((b) => {
    const side = b.market === "total" ? `${b.side === "under" ? "U" : "O"}${fmtTotal(b.first_line)}`
      : `${b.side === "home" ? b.home : b.away} ${fmtLine(b.first_line)}`;
    const first = `${esc(side)} ${isNum(b.first_odds) ? (b.first_odds > 0 ? "+" : "") + Math.round(b.first_odds) : ""}`
      + ` · edge ${isNum(b.first_edge) ? (b.first_edge >= 0 ? "+" : "") + Number(b.first_edge).toFixed(1) : "—"}`
      + `${isNum(b.hours_before_kickoff) ? ` · ${Math.round(b.hours_before_kickoff)}h early` : ""}`;
    const clv = isNum(b.clv_pts) ? `<span class="mv ${b.clv_pts > 0 ? "up" : b.clv_pts < 0 ? "dn" : ""}">${b.clv_pts > 0 ? "+" : ""}${Number(b.clv_pts).toFixed(1)}</span>` : "—";
    const close = `${b.market === "spread" ? fmtLine(b.closing_line) : fmtTotal(b.closing_line)} · ${clv}`;
    const score = isNum(b.away_score) && isNum(b.home_score) ? `${b.away_score}-${b.home_score}` : (isNum(b.actual_total) ? fmtTotal(b.actual_total) : "—");
    const closeGrade = b.result_at_close && b.result_at_close !== b.result ? ` <span class="sub">(close ${esc(b.result_at_close)})</span>` : "";
    const result = b.result ? `<span class="mv ${b.result === "W" ? "up" : b.result === "L" ? "dn" : ""}">${esc(b.result)}</span> · ${score}${closeGrade}` : "pending";
    const windState = b.wind_thesis ? (typeof b.wind_materialized !== "boolean" ? "actual pending" : b.wind_materialized ? '<span class="mv up">held</span>' : '<span class="mv dn">missed</span>')
      : b.wind_materialized ? "emerged (not thesis)" : "not wind-driven";
    return `<tr class="bt-game${b.game_id ? " link" : ""}" data-game="${esc(b.game_id || "")}"><td class="left"><b>${esc(b.away)} @ ${esc(b.home)}</b><br><span class="sub">${b.kickoff_utc ? esc(fmtShortET(b.kickoff_utc)) : ""} · ${esc(String(b.sport || "").toUpperCase())}</span></td>`
      + `<td class="left">${first}<br><span class="sub">${esc(String(b.book || ""))} · ${esc(String(b.tier || "").replace("_", " "))}</span></td>`
      + `<td>${close}</td><td>${result}</td>`
      + `<td>${fmtNum(b.first_weather.wind_mph, 1)} → ${fmtNum(b.final_forecast.wind_mph, 1)} → ${fmtNum(b.actual_weather.wind_mph, 1)} mph<br>${windState}</td>`
      + `<td>${fmtNum(b.first_weather.temp_f, 0)}° → ${fmtNum(b.actual_weather.temp_f, 0)}°</td>`
      + `<td>${fmtNum(b.first_weather.rain_mm, 1)} → ${fmtNum(b.actual_weather.rain_mm, 1)} mm</td></tr>`;
  }).join("");
  const table = `<div class="wrap bt-wrap"><table class="bt bt-postmortem"><thead>${head}</thead><tbody>${body || '<tr><td colspan="7" class="empty">No alerted plays kicked off in the last 7 days.</td></tr>'}</tbody></table></div>`;
  return summary + note + table + weekTable + splitTable;
}

function filteredBacktest() {
  const d = BT.data || { grid: [], stadiums: [], stadiums_legacy: [], games: [], postmortem: { latest: { bets: [] } } };
  const sportLabel = BT.sport ? BT_SPORT[BT.sport] : "";
  let grid = d.grid, stadiums = stadiumRows(), games = d.games, bets = d.postmortem.latest.bets;
  if (sportLabel) { grid = grid.filter((row) => row.sport === sportLabel); games = games.filter((row) => row.sport === BT.sport); bets = bets.filter((row) => row.sport === BT.sport); }
  if (BT.sport === "nfl") stadiums = [];   // stadium sheet is CFB-only (legacy)
  if (BT.q) {
    const q = BT.q;
    stadiums = stadiums.filter((row) => `${row.team} ${row.stadium}`.toLowerCase().includes(q));
    games = games.filter((row) => `${row.away} ${row.home} ${row.stadium} ${row.game_id || ""}`.toLowerCase().includes(q));
    bets = bets.filter((row) => `${row.away} ${row.home} ${row.stadium} ${row.game_id || ""} ${row.book || ""}`.toLowerCase().includes(q));
  }
  return { grid, stadiums, games: games.slice(0, 1000), bets };
}

async function renderBacktest() {
  const host = document.getElementById("backtestwrap");
  if (!host) return;
  if (!BT.loaded) {
    host.innerHTML = `<div class="empty">loading backtest…</div>`;
    await loadBacktest();
    if (STATE.view !== "backtest") return;
  }
  const d = BT.data;
  const flt = filteredBacktest();
  const lg = d.legacy || {};
  const meta = (d.generated_at ? `updated ${esc(fmtShortET(d.generated_at))}` : "")
    + (d.bucket_on ? ` · buckets on ${esc(d.bucket_on)}` : "") + (isNum(d.n_graded) ? ` · ${d.n_graded} graded` : "")
    + (lg.source ? ` · legacy: ${esc(lg.source)}${lg.seasons ? ` (${esc(lg.seasons)})` : ""}` : "");
  const controls = `<div class="controls btctl">
    <select id="bt-sport"><option value="">Sport: all</option><option value="cfb">CFB (NCAAF)</option><option value="nfl">NFL</option></select>
    <select id="bt-section"><option value="postmortem">Weekly post-mortem</option><option value="grid">Bucket grid</option><option value="stadiums">Stadium results</option><option value="games">Matched games</option></select>
    <input id="bt-q" placeholder="Filter team / stadium…" />
    ${BT.section === "grid" ? `<label class="chk" title="Show the legacy sheet's Wins/Losses/Push/Sample/Margin/ROI/+CLV/CLV % next to this season's"><input type="checkbox" id="bt-legacy" /> legacy</label>` : ""}
    <span class="sub" id="bt-count"></span>
    <span class="sub">${meta}${d.run_id ? ` · run ${esc(d.run_id)}` : ""}</span>
    <button class="controlbtn" id="bt-reload" type="button" title="Re-fetch backtest.json">↻</button>
  </div>`;
  const banner = BT.section === "grid" && d.n_graded === 0 && d.grid.length ? `<div class="banner warn bt-banner">${esc(BT_NO_GRADED_BANNER)}</div>` : "";
  let section = "";
  if (!d.grid.length && !d.games.length && !d.stadiums.length && !d.stadiums_legacy.length && !d.postmortem.season.bets.length) {
    section = `<div class="empty">no backtest published yet (backtest.yml writes board/backtest.json every Tuesday)</div>`;
  } else if (BT.section === "postmortem") {
    section = postmortemSectionHtml(d.postmortem, flt.bets);
  } else if (BT.section === "stadiums") {
    section = stadiumSectionHtml(flt.stadiums);
  } else if (BT.section === "games") {
    section = gamesSectionHtml(flt.games);
  } else {
    section = gridSectionHtml(flt.grid);
  }
  host.innerHTML = controls + banner + clvSummaryHtml(d.clv) + section;
  const counts = { postmortem: `${flt.bets.length} plays`, grid: `${flt.grid.length} / ${d.grid.length} buckets`, stadiums: `${flt.stadiums.length} stadiums`, games: `${flt.games.length} / ${d.games.length} games` };
  document.getElementById("bt-count").textContent = counts[BT.section] || "";
  document.getElementById("bt-sport").value = BT.sport;
  document.getElementById("bt-section").value = BT.section;
  document.getElementById("bt-q").value = BT.q;
  if (document.getElementById("bt-legacy")) document.getElementById("bt-legacy").checked = BT.legacy;
  document.getElementById("bt-sport").addEventListener("change", (ev) => { BT.sport = ev.target.value; renderBacktest(); });
  document.getElementById("bt-section").addEventListener("change", (ev) => { BT.section = ev.target.value; renderBacktest(); });
  document.getElementById("bt-q").addEventListener("input", (ev) => { BT.q = ev.target.value.toLowerCase().trim(); renderBacktest(); });
  if (document.getElementById("bt-legacy")) document.getElementById("bt-legacy").addEventListener("change", (ev) => {
      BT.legacy = !!ev.target.checked;
      saveBtLegacy(BT.legacy);
      if (!BT.legacy && BT.sort && BT.sort.startsWith("l:")) BT.sort = null;   // hidden group can't stay the sort key
      renderBacktest();
    });
  document.getElementById("bt-reload").addEventListener("click", async () => { await loadBacktest(true); renderBacktest(); });
  host.querySelectorAll("table.bt-grid th.sortable").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (BT.sort === key) { if (BT.dir < 0) BT.dir = 1; else { BT.sort = null; BT.dir = -1; } }   // desc → asc → sheet order
    else { BT.sort = key; BT.dir = -1; }
    renderBacktest();
  }));
  host.querySelectorAll("tr.bt-game.link").forEach((tr) => tr.addEventListener("click", () => {
    const gid = tr.dataset.game;
    if (gid && findGame(gid)) { STATE.view = "table"; openAlertGame({ game: gid }); return; }
    const bid = Number(tr.dataset.bucket);
    if (Number.isFinite(bid)) { BT.hl = bid; BT.section = "grid"; renderBacktest(); }
  }));
}
