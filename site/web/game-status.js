"use strict";
// The only legacy-card status fallback in the browser runtime.
function gameStatusCode(game) {
  if (game.status_code != null) return game.status_code;
  const raw = String(game.status || "");
  const match = [[/final/i, "final"], [/live|progress|in_play|half/i, "live"],
    [/cancel/i, "cancelled"], [/postpon/i, "postponed"], [/suspend/i, "suspended"],
    [/tbd/i, "tbd"], [/unknown/i, "unknown"]].find(([pattern]) => pattern.test(raw));
  return match ? match[1] : raw.toLowerCase() === "scheduled" ? "scheduled" : "unspecified";
}
function bettableBeforeKickoff(game, now = Date.now()) {
  return Date.parse(game.kickoff_utc) > now
    && ["scheduled", "tbd", "unknown", "unspecified"].includes(gameStatusCode(game));
}
