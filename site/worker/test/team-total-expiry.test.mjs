import test from "node:test";
import assert from "node:assert/strict";
import { expireCardQuotes, teamBlendedTotal } from "../../web/current-quotes.mjs";

const now = Date.parse("2026-10-24T18:00:00Z");
const quote = (line, expires) => ({ total: { line, under: -110, over: -110, expires_at: expires ?? null } });

test("browser expiry of a pricing book withholds the blended v2 fair (fail closed)", () => {
  // ours (slate-relative) 50, weight 0.35; Pinnacle priced 60 and has expired; the rest sit at 50.
  const card = {
    sport: "cfb", game_id: "g0",
    team_total: { total: 60, total_rel: 50, slate_offset: 10, weight: 0.35 },
    impact: { model_version: "v2", v1: { gs_fg_pct: 0, away_fg_pct: 0 }, v2: { gs_fg_pct: 0, away_fg_pct: 0 } },
    consensus: { total_now: 55, total_open: 50 },
    fair: { fair_total: 55, fair_total_v2: 53.25, edges: [] },
    odds: { pinnacle: quote(60, "2026-10-24T17:59:59Z"), fanduel: quote(50), betonline: quote(50) },
  };
  const out = expireCardQuotes(card, now);
  assert.equal(out.consensus.total_now, null);
  assert.equal(out.fair.fair_total_v2, null);        // never shows a blend priced off the expired quote
});

test("teamBlendedTotal applies weather shade and blend; null without a blend", () => {
  const card = { team_total: { total_rel: 62, weight: 0.35 }, impact: { v2: { gs_fg_pct: -4 } } };
  assert.equal(teamBlendedTotal(card, 56), 56 * 0.96 + 0.35 * 6);
  assert.equal(teamBlendedTotal({ impact: { v2: { gs_fg_pct: -4 } } }, 56), null);
});
