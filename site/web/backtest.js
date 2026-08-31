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
//     games: [ GameRow.to_dict(): game_id, sport, season, week, kickoff_utc, home_id, away_id, home_name, away_name,
//              stadium_name, roof_state, temp_fc, wind_fc, gust_fc, rain_fc, lead_fc, gs_fg_v1, gs_fg_v2,
//              temp_act, wind_act, rain_act, total_open, total_close, spread_open, spread_close, clv_status,
//              actual_total, under_result "W"|"L"|"P", margin, Signal, Sample, Margin, ROI ],
//     alerts_clv: { n, by_tier [..], by_league [..], by_book [..], by_model [ {key "v1"|"v2", n, avg_clv, pos, pos_frac} ],
//                   alerts [ {alert_key, season, week, ..., closing_line, clv_pts} ] } }
//   Snake-case spellings (wind_lo/wind_hi/.., wins/losses/.., team/stadium/record/pct, a `clv` block with a
//   dict-shaped by_model) are accepted too.
//
// Historical seasons (pipeline/backtest_git.py, docs/HISTORICAL_BACKTEST_SPEC.md) ride along in the
// same payload and are picked with the #bt-season selector:
//   grid[].by_season / grid[].by_season_close: { "2024": {8 stats}, "2025": {..}, all_hist: {..} } — the
//     UNDER graded at the alert total, and the same bet placed at the closing total (#bt-bet);
//   stadium_results[].season = 2024 | 2025 on the historical rows;
//   tier_scorecard: [ {sport, tier (the PEAK tier the game reached), lead_band, n, win_pct, roi, clv_pct,
//                      avg_clv, wind_err, persistence, evaporated, n_actual} ];
//   hist_games: [ {game_id, kickoff_utc, alert_tier, tier_at_kick, alert_lead_h, alert_total, close_total,
//                  actual_total, under_result, close_result, clv_pts, wind_fc, wind_alert, wind_act, Signal} ];
//   meta.hist: { seasons, n_games, n_graded, n_alerted, model_match_rate, coverage [..], unresolved [..] };
//   stadium_wx: [ {stadium_id, stadium, team, sport, seasons "2015-2024", wind_p75, mean_wind, thin,
//                  all_* / wind10_* / wind12_* / wind15_* / top25_* / early_* / late_*
//                  each {n, record "W-L-P", win_pct, roi}} ] — pipeline/stadium_wx.py, ERA5 wind at
//                  kickoff over ~10 seasons; the venue record the old site showed, both sports.
//                  DESCRIPTIVE ONLY: per-venue ROI spread is indistinguishable from sampling noise
//                  and a venue's early half does not predict its late half (meta venue_noise).
//   stadium_wx_bands: [ {sport "nfl"|"cfb"|"all", band, wind_min, n, record, win_pct, roi} ] — the
//                  under record by ABSOLUTE ERA5 wind, which is the split that does hold up.
//
// First-match semantics (pages/cfb_weather.py get_backtesting_data): walk the grid in id
// order; wind_hi null -> 100, spread_lo null -> 0, temp_lo null -> 0; a null spread_hi never
// matches an NCAAF row (the legacy quirk) but NFL rows carry no spread bands, so both spread
// bounds null on an NFL row means "any spread"; the row's CLV must equal the game's CLV status
// (the aggregate null-CLV row is used only when no status can be computed). CLV status
// = "Positive" when the consensus total dropped from open (open > now), else "Negative".
//
// The grid shows two column groups: the primary one picked by #bt-season ("2026 (this season)"
// recomputed from graded games, an archive season, 2024–25, or the sheet) and "Legacy sheet"
// (row.legacy = the xlsx numbers) behind the #bt-legacy checkbox (on by default; both the
// season and the checkbox are remembered in localStorage). Until meta.n_graded > 0 a banner
// says the legacy numbers are what is being shown; the stadium table falls back to the sheet
// too. backtestHover prefers this season, then the 2024–25 replay, then the sheet.

const BT_LEGACY_KEY = "fw.btLegacy";
const BT_SEASON_KEY = "fw.btSeason";
function loadBtLegacy() {
  try { return localStorage.getItem(BT_LEGACY_KEY) !== "0"; } catch (_) { return true; }
}
function saveBtLegacy(on) {
  try { localStorage.setItem(BT_LEGACY_KEY, on ? "1" : "0"); } catch (_) { /* private mode / blocked storage */ }
}
function loadBtSeason() {
  try { return localStorage.getItem(BT_SEASON_KEY) || ""; } catch (_) { return ""; }
}
function saveBtSeason(v) {
  try { localStorage.setItem(BT_SEASON_KEY, v || ""); } catch (_) { /* private mode / blocked storage */ }
}
const BT_NO_GRADED_BANNER = "No graded 2026 games yet (first grading after Week 0 settles) — showing legacy sheet results.";
// #bt-season values: "" = this season, a year or ALL_HIST = the git-archive replay, "legacy" = the xlsx
const BT_THIS_SEASON = "2026 (this season)";
const BT_ALL_HIST = "all_hist";
const BT_SEASON_LEGACY = "legacy";

const BT = {
  data: null, loaded: false, loading: null, sport: "", section: "grid", q: "", sort: null, dir: -1,
  legacy: loadBtLegacy(), season: loadBtSeason(), bet: "alert", histSort: "kickoff_utc", histDir: -1,
  scoreOpen: true, histOpen: false,
};
const BT_HIST_MAX = 200;   // rows rendered in the graded-games expander

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
// { "2024": {8 stats}, "2025": .., all_hist: .. } -> the same map with normalized stat keys
function normalizeSeasonMap(raw) {
  const out = {};
  if (raw && typeof raw === "object") {
    Object.entries(raw).forEach(([season, stats]) => {
      if (stats && typeof stats === "object") out[String(season)] = normalizeStats(stats);
    });
  }
  return out;
}
function normalizeGridRow(raw, idx) {
  const r = raw || {};
  const clv = btStr(r.clv, r.clv_from_open, r["CLV from Open"]);
  const legacy = r.legacy && typeof r.legacy === "object" ? normalizeStats(r.legacy) : normalizeStats({});
  const by_season = normalizeSeasonMap(r.by_season);
  const by_season_close = normalizeSeasonMap(r.by_season_close);
  // sport code ("cfb"/"nfl") or label ("NCAAF"/"NFL") -> label; bucketMatch / the sport filter compare labels
  const sp = (btStr(r.Sport, r.sport) || "").toUpperCase();
  return {
    legacy, by_season, by_season_close,
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
    season: btNum(r.season), sport: (btStr(r.sport) || "").toLowerCase(),
    legacy,
  };
}
function normalizeScorecardRow(raw) {
  const r = raw || {};
  return {
    sport: (btStr(r.sport) || "").toLowerCase(), tier: btStr(r.tier) || "—", lead_band: btStr(r.lead_band) || "—",
    n: btNum(r.n), wins: btNum(r.wins), losses: btNum(r.losses), push: btNum(r.push),
    win_pct: btNum(r.win_pct), roi: btNum(r.roi), clv_pct: btNum(r.clv_pct), avg_clv: btNum(r.avg_clv),
    wind_err: btNum(r.wind_err), persistence: btNum(r.persistence),
    evaporated: btNum(r.evaporated), n_actual: btNum(r.n_actual),
  };
}
function normalizeWindBandRow(raw) {
  const r = raw || {};
  return {
    sport: (btStr(r.sport) || "").toLowerCase(), band: btStr(r.band) || "", wind_min: btNum(r.wind_min),
    n: btNum(r.n), record: btStr(r.record) || "—", win_pct: btNum(r.win_pct), roi: btNum(r.roi),
  };
}
function normalizeStadiumWxRow(raw) {
  const r = raw || {};
  const out = {
    stadium_id: btStr(r.stadium_id) || "", stadium: btStr(r.stadium) || "", team: btStr(r.team) || "",
    sport: (btStr(r.sport) || "").toLowerCase(), seasons: btStr(r.seasons) || "",
    wind_p75: btNum(r.wind_p75), mean_wind: btNum(r.mean_wind), thin: r.thin === true || r.thin === 1,
  };
  // all_*, wind10_*, wind12_*, wind15_*, top25_*, early_*, late_* groups
  ["all", "wind10", "wind12", "wind15", "top25", "early", "late"].forEach((g) => {
    out[`${g}_n`] = btNum(r[`${g}_n`]);
    out[`${g}_record`] = btStr(r[`${g}_record`]) || "—";
    out[`${g}_roi`] = btNum(r[`${g}_roi`]);
    out[`${g}_win_pct`] = btNum(r[`${g}_win_pct`]);
  });
  return out;
}
function normalizeHistGame(raw) {
  const r = raw || {};
  const g = normalizeBtGame(r);
  return {
    ...g,
    alert_tier: btStr(r.alert_tier), tier_at_kick: btStr(r.tier_at_kick), alert_lead_h: btNum(r.alert_lead_h),
    alert_total: btNum(r.alert_total), alert_odds: btNum(r.alert_under_odds), close_lead_h: btNum(r.close_lead_h),
    total_close: btNum(r.close_total, r.total_close), roi_alert: btNum(r.roi_alert),
    close_hit: btStr(r.close_result) === "W" ? true : (btStr(r.close_result) === "L" ? false : null),
    wind_alert: btNum(r.wind_alert), wind_err: btNum(r.wind_err_alert), line_book: btStr(r.line_book),
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
  const lg = meta.legacy && typeof meta.legacy === "object" ? meta.legacy : {};
  const legacy = { source: btStr(lg.source), seasons: btStr(lg.seasons), n_buckets: btNum(lg.n_buckets) };
  const scorecard = (Array.isArray(d.tier_scorecard) ? d.tier_scorecard : []).map(normalizeScorecardRow);
  const histGames = (Array.isArray(d.hist_games) ? d.hist_games : []).map(normalizeHistGame);
  const stadiumWx = (Array.isArray(d.stadium_wx) ? d.stadium_wx : []).map(normalizeStadiumWxRow)
    .filter((r) => r.stadium || r.stadium_id);
  const windBands = (Array.isArray(d.stadium_wx_bands) ? d.stadium_wx_bands : []).map(normalizeWindBandRow);
  const hist = meta.hist && typeof meta.hist === "object" ? meta.hist : {};
  // seasons offered by the selector: every key the grid carries except the all-seasons rollup
  const seasons = [...new Set(grid.flatMap((row) => Object.keys(row.by_season)))]
    .filter((s) => s !== BT_ALL_HIST).sort().reverse();
  return { run_id: meta.run_id || d.run_id || null, generated_at: meta.generated_at || meta.last_updated || d.generated_at || d.last_updated || null,
    bucket_on: meta.bucket_on || null, n_graded: btNum(meta.n_graded), weeks: btNum(d.weeks, clv && clv.weeks),
    grid, stadiums, stadiums_legacy, games, clv, legacy, scorecard, histGames, stadiumWx, windBands, hist, seasons };
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
// ── season selection ──────────────────────────────────────────────────────
// "" = this season (the row's own 8 columns), a year / all_hist = the git-archive replay,
// "legacy" = the xlsx sheet. `bet` is "alert" (the tier alert's total) or "close".
function seasonLabel(key) {
  if (!key) return BT_THIS_SEASON;
  if (key === BT_SEASON_LEGACY) return "Legacy sheet";
  if (key === BT_ALL_HIST) return "2024–25";
  return String(key);
}
function seasonKeys(d) {
  const hist = d && d.seasons && d.seasons.length ? [...d.seasons, BT_ALL_HIST] : [];
  return ["", ...hist, BT_SEASON_LEGACY];
}
function statsFor(row, season = BT.season, bet = BT.bet) {
  if (!row) return normalizeStats({});
  if (!season) return normalizeStats(row);
  if (season === BT_SEASON_LEGACY) return row.legacy || normalizeStats({});
  const group = bet === "close" ? row.by_season_close : row.by_season;
  return (group && group[season]) || normalizeStats({});
}
// hover / drawer preference: this season if it graded anything, else the 2024–25 replay, else the sheet
function bucketStats(row) {
  if (!row) return null;
  if (isNum(row.sample) && Number(row.sample) > 0) return { ...normalizeStats(row), src: "2026" };
  const all = (row.by_season || {})[BT_ALL_HIST];
  if (all && isNum(all.sample) && Number(all.sample) > 0) return { ...all, src: "2024–25" };
  const lg = row.legacy || normalizeStats({});
  return { ...lg, src: "legacy sheet" };
}
// The signal tier's own 2024–25 record, pooled across lead bands (a single band is too thin to
// read). `evaporated` is the share whose ERA5 actuals would not have fired any tier — the bet was
// on weather that never showed up. Low tiers evaporate ~75% of the time, so this rides next to
// every signal rather than living in a doc.
function tierRecord(sport, tierLabel) {
  const d = BT.data;
  if (!d || !tierLabel || tierLabel === "No Impact") return null;
  const rows = (d.scorecard || []).filter((r) => r.sport === sport && r.tier === tierLabel);
  if (!rows.length) return null;
  const acc = { wins: 0, losses: 0, push: 0, n: 0, roiN: 0, roiSum: 0, evapN: 0, evapSum: 0 };
  rows.forEach((r) => {
    acc.wins += r.wins ?? 0; acc.losses += r.losses ?? 0; acc.push += r.push ?? 0; acc.n += r.n ?? 0;
    if (isNum(r.roi) && isNum(r.n)) { acc.roiSum += r.roi * r.n; acc.roiN += r.n; }
    if (isNum(r.evaporated) && isNum(r.n_actual)) { acc.evapSum += r.evaporated * r.n_actual; acc.evapN += r.n_actual; }
  });
  if (!acc.n) return null;
  return {
    wins: acc.wins, losses: acc.losses, push: acc.push, n: acc.n,
    roi: acc.roiN ? acc.roiSum / acc.roiN : null,
    evaporated: acc.evapN ? acc.evapSum / acc.evapN : null,
  };
}
// [[label, value], ...] for hover cards / drawer (empty when no bucket matched or no data)
function backtestHover(g) {
  const row = backtestMatch(g);
  const tier = tierRecord(g && g.sport, ((g || {}).signal || {}).label);
  const out = [];
  if (row) {
    const stats = bucketStats(row);
    out.push(["Record (under)", `${fmtRecord(stats)} · n=${fmtInt(stats.sample)} · ${stats.src}`]);
    out.push(["ROI", `${fmtRoi(stats.roi)}${isNum(stats.margin) ? ` · margin ${Number(stats.margin) >= 0 ? "+" : ""}${Number(stats.margin).toFixed(2)}` : ""}`]);
    out.push(["Bucket", `#${row.id} ${bucketLabel(row)}`]);
  }
  if (tier) {
    out.push(["Tier 2024–25", `${fmtRecord(tier)} · n=${fmtInt(tier.n)} · ROI ${fmtRoi(tier.roi)}`]);
    if (isNum(tier.evaporated)) {
      out.push(["Forecast held", `${fmtPct(1 - tier.evaporated)} of these tiers still fired on the actual weather`]);
    }
  }
  const band = windBandRow(g);
  if (band) out.push(band);
  const wx = stadiumWxRow(g);
  if (wx) out.push(wx);
  return out;
}
// stadium rows for the selected season: this season's (else the sheet), one archive season,
// both archive seasons merged, or the sheet itself
function stadiumRows(season = BT.season) {
  const d = BT.data || { stadiums: [], stadiums_legacy: [], seasons: [] };
  const legacyRows = d.stadiums_legacy || [];
  if (season === BT_SEASON_LEGACY) return legacyRows;
  const hist = new Set((d.seasons || []).map(Number));
  if (!season) {
    const own = (d.stadiums || []).filter((row) => !hist.has(Number(row.season)));
    return own.length ? own : legacyRows;
  }
  const want = season === BT_ALL_HIST ? hist : new Set([Number(season)]);
  const rows = (d.stadiums || []).filter((row) => want.has(Number(row.season)));
  return season === BT_ALL_HIST ? mergeStadiumRows(rows) : rows;
}
// one row per stadium across the archive seasons (records added, pct = ROI weighted by n)
function mergeStadiumRows(rows) {
  const by = new Map();
  rows.forEach((row) => {
    const key = `${row.team}|${row.stadium}`;
    const cur = by.get(key);
    if (!cur) { by.set(key, { ...row, season: null, _roi: (row.pct ?? 0) * ((row.wins ?? 0) + (row.losses ?? 0) + (row.push ?? 0)) }); return; }
    cur.wins = (cur.wins ?? 0) + (row.wins ?? 0);
    cur.losses = (cur.losses ?? 0) + (row.losses ?? 0);
    cur.push = (cur.push ?? 0) + (row.push ?? 0);
    cur._roi += (row.pct ?? 0) * ((row.wins ?? 0) + (row.losses ?? 0) + (row.push ?? 0));
  });
  return [...by.values()].map(({ _roi, ...row }) => {
    const n = (row.wins ?? 0) + (row.losses ?? 0) + (row.push ?? 0);
    return { ...row, record: `${row.wins ?? 0}-${row.losses ?? 0}-${row.push ?? 0}`, pct: n ? _roi / n : null };
  });
}
// ── stadium weather records (pipeline/stadium_wx.py) ─────────────────────
// ~10 seasons of under records per venue, keyed on ERA5 wind at kickoff rather than forecast
// wind. `top25_*` is the venue's own windiest quarter — self-normalising, and always populated,
// unlike the absolute bands (true ERA5 >= 15 mph is only ~3% of games).
function stadiumWxFor(g) {
  const rows = (BT.data || {}).stadiumWx || [];
  if (!g || !rows.length) return null;
  const sid = (g.stadium && g.stadium.stadium_id) || null;
  const name = ((g.stadium && g.stadium.name) || "").toLowerCase();
  const sport = String(g.sport || "").toLowerCase();
  return rows.find((r) => r.sport === sport && ((sid && r.stadium_id === sid) || (name && r.stadium.toLowerCase() === name))) || null;
}
// [label, value] for the hover: the venue's all-games under record and its windiest quarter
function stadiumWxRow(g) {
  const r = stadiumWxFor(g);
  if (!r || !isNum(r.all_n) || !r.all_n) return null;
  let val = `${r.all_record} · ROI ${fmtRoi(r.all_roi)} · n=${fmtInt(r.all_n)}`;
  if (isNum(r.top25_n) && r.top25_n >= 5) {
    val += ` · windiest 25% (≥${fmtNum(r.wind_p75, 0)} mph): ${r.top25_record} · ${fmtRoi(r.top25_roi)}`;
  }
  return [`Stadium ${r.seasons || ""}`.trim(), `${val} · descriptive only`];
}
// The split that holds up: the under record by ABSOLUTE ERA5 wind, pooled over ~10 seasons.
// Conditional on the wind actually showing up — pair it with the "Forecast held" row above.
function windBandRow(g) {
  const rows = (BT.data || {}).windBands || [];
  const wind = ((g || {}).weather || {}).wind_fg;
  const sport = String((g || {}).sport || "").toLowerCase();
  if (!rows.length || !isNum(wind)) return null;
  const mine = rows.filter((r) => r.sport === sport && isNum(r.wind_min) && wind >= r.wind_min && r.n >= 100);
  if (!mine.length) return null;
  const best = mine.reduce((a, b) => (b.wind_min > a.wind_min ? b : a));   // the tightest band it clears
  return [`Wind ≥${fmtNum(best.wind_min, 0)} actual`,
    `${best.record} · ${fmtRoi(best.roi)} · n=${fmtInt(best.n)} (2015–24, if the wind shows up)`];
}
// the drawer / hover card always follow the bucketStats preference (this season → 2024–25 →
// the sheet), never whatever season the Backtest tab's selector happens to be showing
function hoverStadiumRows() {
  const own = stadiumRows("");
  if (own.length && !own[0].legacy) return own;
  const hist = stadiumRows(BT_ALL_HIST);
  return hist.length ? hist : own;
}
function stadiumResultFor(g) {
  if (!g || !BT.data) return null;
  const name = ((g.stadium && g.stadium.name) || "").toLowerCase();
  const home = ((g.home && (g.home.name || g.home.short)) || "").toLowerCase();
  return hoverStadiumRows().find((row) => (name && row.stadium.toLowerCase() === name) || (home && row.team.toLowerCase() === home)) || null;
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

// grid sort keys: "wins".."clv_pct" (the selected season) or "l:wins".."l:clv_pct" (legacy sheet);
// null = sheet order (id)
function gridSortValue(row, key) {
  if (!key) return row.id;
  const [grp, k] = key.startsWith("l:") ? ["legacy", key.slice(2)] : ["", key];
  const v = grp ? (row.legacy || {})[k] : statsFor(row)[k];
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
function primaryGroupLabel() {
  if (!BT.season) return BT_THIS_SEASON;
  if (BT.season === BT_SEASON_LEGACY) return "Legacy sheet";
  return `${seasonLabel(BT.season)} · at ${BT.bet === "close" ? "close" : "alert"}`;
}
function primaryGroupTitle() {
  if (!BT.season) return "recomputed from this season's graded games (under at the closing total)";
  if (BT.season === BT_SEASON_LEGACY) return "the legacy cfb_weather_backtest.xlsx numbers";
  return BT.bet === "close"
    ? "git-archive replay: the UNDER bet at the closing total"
    : "git-archive replay: the UNDER bet at the total when the signal tier first fired";
}
function gridSectionHtml(rows) {
  // the sheet's own group is redundant when it is already the primary one
  const withLegacy = BT.legacy && BT.season !== BT_SEASON_LEGACY;
  const ncols = 6 + 8 + (withLegacy ? 8 : 0);
  const legacyTitle = BT.data && BT.data.legacy && BT.data.legacy.source
    ? `${BT.data.legacy.source}${BT.data.legacy.seasons ? ` · ${BT.data.legacy.seasons}` : ""}` : "cfb_weather_backtest.xlsx";
  const groups = `<tr class="bt-grp"><th colspan="6"></th><th colspan="8" class="grp" title="${esc(primaryGroupTitle())}">${esc(primaryGroupLabel())}</th>`
    + (withLegacy ? `<th colspan="8" class="grp lg" title="${esc(legacyTitle)}">Legacy sheet</th>` : "") + `</tr>`;
  const head = `<tr class="bt-cols"><th>#</th><th class="left">Sport</th><th>Wind</th><th>Temp</th><th>Spread</th><th class="left">CLV</th>`
    + statHeadHtml("") + (withLegacy ? statHeadHtml("l:") : "") + `</tr>`;
  const body = sortGridRows(rows).map((row) => `<tr class="bt-row${row.clv ? " sub" : ""}${BT.hl === row.id ? " hl" : ""}" data-id="${row.id}">`
    + `<td class="left">${row.id}</td><td class="left">${esc(row.sport)}</td>`
    + `<td>${esc(band(row.wind_lo, row.wind_hi))}</td><td>${esc(band(row.temp_lo, row.temp_hi, "°"))}</td>`
    + `<td>${row.spread_lo === null && row.spread_hi === null ? "—" : esc(band(row.spread_lo, row.spread_hi))}</td>`
    + `<td class="left">${row.clv ? `<span class="mv ${row.clv === "Positive" ? "up" : "dn"}">${esc(row.clv)}</span>` : "all"}</td>`
    + statCellsHtml(statsFor(row)) + (withLegacy ? statCellsHtml(row.legacy || {}, "lg") : "") + `</tr>`).join("");
  return `<div class="wrap bt-wrap"><table class="bt bt-grid"><thead>${groups}${head}</thead><tbody>${body || `<tr><td colspan="${ncols}" class="empty">no grid rows</td></tr>`}</tbody></table></div>`;
}

// ── tier scorecard + graded games (git-archive replay) ────────────────────
function scorecardSectionHtml(rows) {
  if (!rows.length) return "";
  // keyed on the PEAK tier (the worst the game reached), grading the bet at that escalation
  const head = `<tr><th class="left">Sport</th><th class="left" title="the worst tier the game reached">Signal tier</th><th class="left">Lead</th><th>n</th>
    <th title="wins / (wins + losses) of the under at the alert total">Win %</th><th title="(sum of per-bet ROI) / n">ROI</th>
    <th title="alerts whose total closed below the alert total">CLV %</th><th>Avg CLV</th>
    <th title="mean |forecast wind at the alert − ERA5 wind at kickoff|">Wind err</th>
    <th title="alerts whose tier at kickoff was still at least the tier that fired">Persist</th>
    <th title="share whose ERA5 actuals would not have fired any tier — the weather never showed up">Evaporated</th></tr>`;
  const body = rows.map((row) => `<tr><td class="left">${esc(row.sport.toUpperCase())}</td><td class="left">${esc(row.tier)}</td>`
    + `<td class="left">${esc(row.lead_band)}</td><td>${fmtInt(row.n)}</td><td>${fmtPct(row.win_pct)}</td>`
    + `<td class="${roiClass(row.roi)}"><b>${fmtRoi(row.roi)}</b></td><td>${fmtPct(row.clv_pct)}</td>`
    + `<td class="${roiClass(row.avg_clv)}">${isNum(row.avg_clv) ? (row.avg_clv >= 0 ? "+" : "") + Number(row.avg_clv).toFixed(2) : "—"}</td>`
    + `<td>${fmtNum(row.wind_err, 1)}</td><td>${fmtPct(row.persistence)}</td>`
    + `<td class="${isNum(row.evaporated) && row.evaporated > 0.6 ? "dn" : ""}">${fmtPct(row.evaporated)}</td></tr>`).join("");
  return `<details class="bt-card bt-score-card"${BT.scoreOpen ? " open" : ""}><summary>Tier scorecard · ${rows.length} rows</summary>`
    + `<div class="wrap bt-wrap"><table class="bt bt-score"><thead>${head}</thead><tbody>${body}</tbody></table></div></details>`;
}
const BT_HIST_HEAD = [
  ["kickoff_utc", "Date", ""], ["sport", "Sport", ""], ["home", "Game", ""], ["alert_tier", "Tier", "tier at the alert → tier at kickoff"],
  ["alert_lead_h", "Lead", "hours before kickoff the tier first fired"], ["alert_total", "Alert total", "the bet"],
  ["total_close", "Close", ""], ["total_actual", "Actual", ""], ["under_hit", "U/O", "under at the alert total"],
  ["clv_pts", "CLV", "alert total − closing total"], ["wind_alert", "Wind fc → act", ""], ["bucket_id", "Bucket", ""],
];
function sortHistGames(rows) {
  const key = BT.histSort;
  return rows.slice().sort((x, y) => {
    const a = x[key], b = y[key];
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    const cmp = typeof a === "string" ? a.localeCompare(String(b)) : Number(a) - Number(b);
    return cmp === 0 ? String(x.game_id).localeCompare(String(y.game_id)) : cmp * BT.histDir;
  });
}
function histGamesHtml(rows) {
  if (!rows.length) return "";
  const shown = sortHistGames(rows).slice(0, BT_HIST_MAX);
  const head = `<tr>${BT_HIST_HEAD.map(([k, label, title]) => `<th class="sortable${BT.histSort === k ? " sorted" : ""}" data-hsort="${k}"`
    + `${title ? ` title="${esc(title)}"` : ""}>${esc(label)}${BT.histSort === k ? (BT.histDir < 0 ? " ▼" : " ▲") : ""}</th>`).join("")}</tr>`;
  const body = shown.map((row) => {
    const res = row.under_hit === true ? '<span class="mv up">U</span>' : row.under_hit === false ? '<span class="mv dn">O</span>' : "—";
    const clv = isNum(row.clv_pts) ? `<span class="mv ${row.clv_pts >= 0 ? "up" : "dn"}">${row.clv_pts >= 0 ? "+" : ""}${Number(row.clv_pts).toFixed(1)}</span>` : "—";
    const tier = `${esc(row.alert_tier || "—")}${row.tier_at_kick && row.tier_at_kick !== row.alert_tier ? ` <span class="sub">→ ${esc(row.tier_at_kick)}</span>` : ""}`;
    return `<tr class="bt-game${row.game_id ? " link" : ""}" data-game="${esc(row.game_id || "")}" data-bucket="${row.bucket_id ?? ""}">`
      + `<td class="left">${esc(row.kickoff_utc ? fmtShortET(row.kickoff_utc) : "—")}${row.week != null ? ` <span class="sub">wk ${esc(row.week)}</span>` : ""}</td>`
      + `<td class="left">${esc(row.sport.toUpperCase())}</td><td class="left game">${esc(row.away)} @ ${esc(row.home)}</td>`
      + `<td class="left">${tier}</td><td>${isNum(row.alert_lead_h) ? `${Math.round(row.alert_lead_h)}h` : "—"}</td>`
      + `<td>${fmtTotal(row.alert_total)}</td><td>${fmtTotal(row.total_close)}</td><td>${fmtTotal(row.total_actual)}</td>`
      + `<td>${res}</td><td>${clv}</td><td>${fmtNum(row.wind_alert, 1)} → ${fmtNum(row.wind_actual, 1)}</td>`
      + `<td>${row.bucket_id != null ? `#${row.bucket_id}` : "—"}</td></tr>`;
  }).join("");
  const cap = rows.length > BT_HIST_MAX ? ` <span class="sub">(showing the first ${BT_HIST_MAX})</span>` : "";
  return `<details class="bt-card bt-hist"${BT.histOpen ? " open" : ""}><summary>graded games · ${rows.length}${cap}</summary>`
    + `<div class="wrap bt-wrap"><table class="bt bt-histgames"><thead>${head}</thead><tbody>${body}</tbody></table></div></details>`;
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

function filteredBacktest() {
  const d = BT.data || { grid: [], stadiums: [], stadiums_legacy: [], games: [], scorecard: [], histGames: [], seasons: [] };
  const sportLabel = BT.sport ? BT_SPORT[BT.sport] : "";
  const histSeasons = new Set((d.seasons || []).map(String));
  let grid = d.grid, stadiums = stadiumRows(), games = d.games;
  let scorecard = d.scorecard || [], histGames = d.histGames || [];
  if (sportLabel) {
    grid = grid.filter((row) => row.sport === sportLabel);
    games = games.filter((row) => row.sport === BT.sport);
    scorecard = scorecard.filter((row) => row.sport === BT.sport);
    histGames = histGames.filter((row) => row.sport === BT.sport);
  }
  // the legacy sheet's stadium rows are CFB-only; the replay's carry their own sport
  if (BT.sport === "nfl" && (BT.season === BT_SEASON_LEGACY || !stadiums.some((row) => row.sport))) stadiums = [];
  else if (BT.sport) stadiums = stadiums.filter((row) => !row.sport || row.sport === BT.sport);
  if (BT.season && BT.season !== BT_SEASON_LEGACY) {
    const want = BT.season === BT_ALL_HIST ? histSeasons : new Set([String(BT.season)]);
    histGames = histGames.filter((row) => want.has(String(row.season)));
  }
  if (BT.q) {
    const q = BT.q;
    const hit = (row) => `${row.away} ${row.home} ${row.stadium} ${row.game_id || ""}`.toLowerCase().includes(q);
    stadiums = stadiums.filter((row) => `${row.team} ${row.stadium}`.toLowerCase().includes(q));
    games = games.filter(hit);
    histGames = histGames.filter(hit);
  }
  return { grid, stadiums, games: games.slice(0, 1000), scorecard, histGames };
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
  const hist = d.hist || {};
  const seasonOpts = seasonKeys(d).map((k) => `<option value="${esc(k)}">${esc(k ? seasonLabel(k) : BT_THIS_SEASON)}</option>`).join("");
  const showBet = !!BT.season && BT.season !== BT_SEASON_LEGACY;
  const controls = `<div class="controls btctl">
    <select id="bt-sport"><option value="">Sport: all</option><option value="cfb">CFB (NCAAF)</option><option value="nfl">NFL</option></select>
    <select id="bt-season" title="Which column group the grid and the stadium table show">${seasonOpts}</select>
    <select id="bt-bet" title="Grade the under at the total when the tier first fired, or at the closing total"${showBet ? "" : " hidden"}>
      <option value="alert">bet at alert</option><option value="close">bet at close</option></select>
    <select id="bt-section"><option value="grid">Bucket grid</option><option value="stadiums">Stadium results</option><option value="games">Matched games</option></select>
    <input id="bt-q" placeholder="Filter team / stadium…" />
    <label class="chk" title="Show the legacy sheet's Wins/Losses/Push/Sample/Margin/ROI/+CLV/CLV % next to this season's"><input type="checkbox" id="bt-legacy" /> legacy</label>
    <span class="sub" id="bt-count"></span>
    <span class="sub">${meta}${d.run_id ? ` · run ${esc(d.run_id)}` : ""}</span>
    <button class="controlbtn" id="bt-reload" type="button" title="Re-fetch backtest.json">↻</button>
  </div>`;
  const banner = d.n_graded === 0 && d.grid.length ? `<div class="banner warn bt-banner">${esc(BT_NO_GRADED_BANNER)}</div>` : "";
  const histNote = BT.season && BT.season !== BT_SEASON_LEGACY && isNum(hist.n_graded)
    ? `<div class="sub bt-note">${esc(seasonLabel(BT.season))} replayed from the git archive: ${esc(hist.n_games)} games, `
      + `${esc(hist.n_graded)} graded, ${esc(hist.n_alerted)} in a signal tier`
      + (isNum(hist.model_match_rate) ? ` · v1 replay matches the archived numbers ${(Number(hist.model_match_rate) * 100).toFixed(1)}%` : "")
      + `</div>` : "";
  let section = "";
  if (!d.grid.length && !d.games.length && !d.stadiums.length && !d.stadiums_legacy.length) {
    section = `<div class="empty">no backtest published yet (backtest.yml writes board/backtest.json every Tuesday)</div>`;
  } else if (BT.section === "stadiums") {
    section = stadiumSectionHtml(flt.stadiums);
  } else if (BT.section === "games") {
    section = gamesSectionHtml(flt.games);
  } else {
    section = scorecardSectionHtml(flt.scorecard) + gridSectionHtml(flt.grid) + histGamesHtml(flt.histGames);
  }
  host.innerHTML = controls + banner + clvSummaryHtml(d.clv) + histNote + section;
  const counts = { grid: `${flt.grid.length} / ${d.grid.length} buckets`, stadiums: `${flt.stadiums.length} stadiums`, games: `${flt.games.length} / ${d.games.length} games` };
  document.getElementById("bt-count").textContent = counts[BT.section] || "";
  document.getElementById("bt-sport").value = BT.sport;
  document.getElementById("bt-season").value = BT.season;
  document.getElementById("bt-bet").value = BT.bet;
  document.getElementById("bt-section").value = BT.section;
  document.getElementById("bt-q").value = BT.q;
  document.getElementById("bt-legacy").checked = BT.legacy;
  document.getElementById("bt-season").addEventListener("change", (ev) => {
    BT.season = ev.target.value;
    saveBtSeason(BT.season);
    renderBacktest();
  });
  document.getElementById("bt-bet").addEventListener("change", (ev) => { BT.bet = ev.target.value; renderBacktest(); });
  document.getElementById("bt-sport").addEventListener("change", (ev) => { BT.sport = ev.target.value; renderBacktest(); });
  document.getElementById("bt-section").addEventListener("change", (ev) => { BT.section = ev.target.value; renderBacktest(); });
  document.getElementById("bt-q").addEventListener("input", (ev) => { BT.q = ev.target.value.toLowerCase().trim(); renderBacktest(); });
  document.getElementById("bt-legacy").addEventListener("change", (ev) => {
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
  host.querySelectorAll("details.bt-card").forEach((el) => el.addEventListener("toggle", () => {
    if (el.classList.contains("bt-hist")) BT.histOpen = el.open; else BT.scoreOpen = el.open;
  }));
  host.querySelectorAll("table.bt-histgames th.sortable").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.hsort;
    if (BT.histSort === key) BT.histDir = -BT.histDir;
    else { BT.histSort = key; BT.histDir = -1; }
    renderBacktest();
  }));
  host.querySelectorAll("tr.bt-game.link").forEach((tr) => tr.addEventListener("click", () => {
    const gid = tr.dataset.game;
    if (gid && findGame(gid)) { STATE.view = "table"; openAlertGame({ game: gid }); return; }
    const bid = Number(tr.dataset.bucket);
    if (Number.isFinite(bid)) { BT.hl = bid; BT.section = "grid"; renderBacktest(); }
  }));
}
