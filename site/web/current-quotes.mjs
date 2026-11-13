// Scheduler health; published prices are immutable.
export function refreshOverdue(meta, now = Date.now()) {
  const eta = Date.parse(meta?.next_run_eta), updated = Date.parse(meta?.last_updated);
  return Number.isFinite(eta) && Number.isFinite(updated) && updated < eta && now > eta + 90 * 60000;
}

function refreshIso(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

// Live scheduler health for /api/status and the status pill. `overdue` is the
// published ETA plus 90 minutes with no newer board. `missed` is a dispatch
// that failed, or was accepted, and still has not published.
export function refreshStatus(meta, now = Date.now(), dispatch = null) {
  const eta = Date.parse(meta?.next_run_eta ?? "");
  const updated = Date.parse(meta?.last_updated ?? "");
  const overdue = refreshOverdue(meta || {}, now);
  const dispatchTs = Date.parse(dispatch?.ts ?? "");
  const afterPublish = Number.isFinite(dispatchTs) && (!Number.isFinite(updated) || dispatchTs >= updated);
  const failed = !!(dispatch && dispatch.dispatched === false && dispatch.trimmed !== true);
  const acceptedStale = !!(dispatch && dispatch.dispatched === true && afterPublish
    && Number.isFinite(updated) && now > dispatchTs + 30 * 60000);
  const missed = (!!failed && afterPublish && now > dispatchTs + 20 * 60000) || acceptedStale;
  let reason = null;
  if (failed && afterPublish && now > dispatchTs + 20 * 60000) {
    reason = `Scheduled refresh failed: ${dispatch.reason || "dispatch failed"}`;
  } else if (acceptedStale) {
    reason = "Refresh was dispatched but the board was not published";
  } else if (overdue) {
    reason = "Expected refresh is overdue; trigger outcome unknown";
  }
  const leagues = leagueBoardWarnings(meta, now);
  return {
    state: missed ? "missed" : overdue ? "overdue" : "ok",
    overdue: !!overdue,
    missed: !!missed,
    expected_at: Number.isFinite(eta) ? refreshIso(eta) : null,
    last_updated: Number.isFinite(updated) ? refreshIso(updated) : null,
    grace_minutes: 90,
    reason,
    leagues,
  };
}

// A league board older than this during 7 AM–11 PM ET is a status warning.
// The 2h slot plus about an hour of slack.
export const LEAGUE_BOARD_STALE_MS = 3 * 60 * 60 * 1000;

function etCalendar(date) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", month: "numeric", hour: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map((part) => [part.type, part.value]));
  return { month: Number(parts.month), hour: Number(parts.hour) % 24 };
}

function retainedRunTime(runId) {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(String(runId || ""));
  if (!match) return null;
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}Z`;
}

function formatBoardAge(ms) {
  const hours = ms / 3600000;
  if (hours < 2) return `${Math.max(0, Math.round(ms / 60000))} min`;
  const rounded = Math.round(hours * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}h` : `${rounded.toFixed(1)}h`;
}

// Per-league freshness for the status tab. Active hours are 7 AM–11 PM ET,
// in season. A carried sport uses its own refreshed_at. A sport rebuilt in
// the latest run uses meta.last_updated. Off hours and empty boards stay quiet.
export function leagueBoardWarnings(meta, now = Date.now()) {
  const when = new Date(now);
  if (Number.isNaN(when.getTime())) return [];
  const parts = etCalendar(when);
  if (parts.hour < 7 || parts.hour > 23) return [];
  if (!(parts.month >= 8 || parts.month <= 2)) return [];
  const leagues = parts.month === 2 ? ["nfl"] : ["nfl", "cfb"];
  const retained = meta && meta.retained_sports && typeof meta.retained_sports === "object" ? meta.retained_sports : {};
  const counts = meta && meta.sport_counts && typeof meta.sport_counts === "object" ? meta.sport_counts : {};
  const warnings = [];
  for (const sport of leagues) {
    const info = retained[sport];
    const carried = !!(info && typeof info === "object");
    const games = Number(carried ? info.games : counts[sport]);
    if (!Number.isFinite(games) || games <= 0) continue;
    const raw = carried ? (info.refreshed_at || retainedRunTime(info.from_run_id)) : meta.last_updated;
    const stamp = Date.parse(raw || "");
    if (!Number.isFinite(stamp)) continue;
    const age = now - stamp;
    if (age <= LEAGUE_BOARD_STALE_MS) continue;
    warnings.push({
      sport,
      refreshed_at: refreshIso(stamp),
      age_minutes: Math.round(age / 60000),
      reason: `${sport.toUpperCase()} board is ${formatBoardAge(age)} old during active hours (older than 3h)`,
    });
  }
  return warnings;
}
