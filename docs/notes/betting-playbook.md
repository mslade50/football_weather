# Weather-totals betting playbook (deep dive, 2026-10-25)

Plain-language synthesis of five Codex-reviewed studies (scratch work and full numbers in
`tmp/deep/w{1..5}_*/FINDINGS.md`, gitignored). Data: 2026 live board (980 games, 7 books, every forecast
run and alert), the 2024–25 git archive (what the board showed at each run), and closing lines with
observed weather back to 1999 (NFL) / 2017 (CFB).

## 1. What the system actually does today

| Piece | What it is | What it controls |
|---|---|---|
| **Process screen** (since Oct 18) | Open roof + forecast sustained wind ≥ 15 mph (or ≥ 23 mm rain, ECMWF + one model agreeing) + CFB opening spread within ±10 + price hasn't moved > 4 pp against the under since each book's opener | **Whether a game is a candidate / alert** |
| **v1** | Frozen legacy staircase: 12/15/17/25 mph → 2/3.5/6.5/10 % of the total, cold/heat/rain extras | Board fair total, edge chips, and **which book/price the message recommends** (ranked by "est. EV") |
| **v2** | Smooth version of the same thing (gust, wind direction, rain probability), CFB × 0.4 | Board only |
| **Tier** (Low/Mid/High) | Wind + temperature label | Nothing about value |
| **Delivery** | Telegram retired Oct 19; a ChatGPT monitor reads the board | Its entry/stake rules were never written down — §4 is meant to be that rule set |

Every 2026 alert in the database was sent under **older rules** (8–15 mph thresholds, no price gate); the
current screen has no live track record yet.

## 2. Answers to the questions

**Does v1 make sense?** Partly. Its *direction* is right and its size is about right for the NFL *final
score* in long history, but as a betting tool it has two real flaws:
1. **It shows the whole weather effect as "edge" no matter how much the market already moved.**
   Fair = current line × (1 − shade), so with opener 47, 18 mph and the line already 44, it still shows
   ~+2.9 pts. The real expected value left is a fraction of the shade that shrinks with time (§3).
2. **Beyond 48 h it reads a forecast pulled toward the stadium's normal wind**, while the alert reads raw
   ECMWF. A raw 17 mph at 5–7 days is worth 0–2 % in v1, yet the alert fires. Meanwhile the market moves
   on the raw forecasts: CFB windy games start dropping a median ~110 h before kickoff, our forecast
   first shows 15 mph at a median ~55 h.

**Do we just need the weather and the opener?** No — tested directly, "opener adjusted for weather minus
how far the line has moved" predicts the rest of the line move *worse* than the current line × shade
(both leagues, every out-of-sample split). Lines move ~1.5 pts for non-weather reasons; that noise swamps
the weather part, so "how far it moved" is not "how much weather is priced". Betting lines that sit above
an opener-adjusted number *without* a real weather shade loses (44–47 %).

**What is v2 and why isn't it better?** Same input (forecast wind) reshaped; it correlates 0.97 with v1
on observed weather. Gusts, direction and rain probability add nothing measurable. Its extra small CFB
flags at 8–12 mph win 50 %. Weather explains ~0.5 % of NFL and ~0 % of CFB variance in the final score
against the close, so no reshaping can show up in results. Don't promote it.

## 3. Where the edge actually is (and isn't)

| Claim | Evidence | Confidence |
|---|---|---|
| The line keeps moving toward the weather after a forecast appears | Remaining move to close per point of v1 shade: **NFL 0.54 / 0.34 / 0.23**, **CFB 0.32 / 0.20 / 0.10** at 96–168 / 48–72 / 12–36 h (fitted mostly on the 2024–25 legacy board's shade) | Direction medium-high; exact per-lead sizes low-medium |
| Flagged CFB totals fall ~1.6 pts first sighting → close (calm ~0.1) | 64 games 2026 | Medium-high |
| Among the 34 of those that ended ≥ 1 pt lower, 62 % of the final drop was in by 72 h and 85 % by 24 h | outcome-selected hindsight: describes the shape of a move that happened, not a forecast | Descriptive |
| CFB weather is priced by the close | Close-miss slope 0.36 ± 0.28 (2017–25) | Medium |
| NFL windy games finish ~2.5–3 pts lower than calm ones vs the close (observed game wind) | 1999–2025; 15–19 mph 57 % under, calm 49 % | High (relative) |
| …but much of that is **calm NFL games going over** the close (+1 to +1.5 pts every era) | same | High |
| An NFL forecast-wind under edge *at bet time* | 13–17 games per band in 2024–25 | **Unproven** |
| 2026 alerts: +1.0 pt CLV | 51 bets, t = 3.9 | High |
| …but betting the best number on *any* game at the same lead gets +0.75; alerts beat that by only +0.03 to +0.31 (CI spans 0) | matched baseline, n = 32–46 | Medium |
| Weather-driven alerts beat that baseline by +1.3 to +1.9 pts | n = 9–14, one of several subgroups | Low (hypothesis) |
| Record 29-25-2, +1.7 u | CI 40–67 % win rate | = noise |
| 2026 is an over year: every under at the close 47 % | 591 games | Medium-high |

**Bottom line (a hypothesis, not demonstrated profit):** if there is money in weather unders, it is in
*getting ahead of the line move*, which decays fast. That is CLV, not a proven score edge you can wait for.
College weather is largely priced by the close. NFL wind has the stronger long-run footprint, but the
at-bet-time sample is still tiny. No combination of rules tested here has a demonstrated positive return.

## 4. The process (what to do, when, where)

**What to look for — a candidate needs all of:**
1. Open roof, and for CFB the spread within ±10.
2. Forecast sustained wind ≥ 15 mph with ECMWF and at least one other model agreeing, or agreed heavy rain.
   Gusts, direction, cold and light rain don't matter.
3. **A meaningful expected remaining move** at the best available number:
   expected move ≈ total × v1 shade % × coefficient(lead). Illustrative values at a 45 NFL / 55 CFB total:

   | Wind (v1 shade) | NFL 4–7 days | NFL 2–3 days | NFL ≤ 1.5 days | CFB 4–7 days | CFB 2–3 days | CFB ≤ 1.5 days |
   |---|---|---|---|---|---|---|
   | 15–16 mph (3.5 %) | 0.9 | 0.5 | 0.4 | 0.6 | 0.4 | 0.2 |
   | 17–24 mph (6.5 %) | **1.6** | 1.0 | 0.7 | **1.1** | 0.7 | 0.4 |
   | 25+ mph (10 %) | **2.4** | **1.5** | **1.0** | **1.8** | **1.1** | 0.6 |

   Bold marks an illustrative estimate above one point, **not a validated betting signal**. The coefficients
   were fitted mostly on the 2024–25 legacy board's shade (that board did not yet pull long-range forecasts
   toward stadium-normal wind; the rebuild added that on 2026-08-25). Today's board shows a smaller shade for
   the same raw forecast beyond 48 h (raw 17 mph at 5 days → 12.6 mph → 2 % → ~0.5 pt). Refit and validate
   the coefficients on today's inputs before using the table for entries; how forecast busts interact with
   the substituted input is not established.
4. A real price: within 5 pts of the consensus (rejects the exchange "ladder" bug), quote not stale.

**When:**
- **CFB (Saturday):** Sun–Mon openers — only very strong forecasts (≥ 20 mph). Tue–Wed is when most flagged
  lines moved in 2026 (72–120 h). Thu: watch. Fri–Sat: forecasts are steadier, but less of the move is left. (In the 2024–25
  archive, ≥ 15 mph forecasts were still ≥ 15 in the final forecast 66 % of the time at 24 h (n = 41) vs 13 % at
  120 h (n = 60); live 2026 was weaker, 40 % (n = 25) and 14 % (n = 7). These measure forecast persistence, not
  observed-wind accuracy.)
- **NFL (Sunday):** strong (17+) forecasts Tue–Thu; Fri–Sat still has some move left and steadier forecasts (archive NFL ≥ 15 mph persistence 65 % at 24 h, n = 17).
- **Early vs late is not settled.** Earlier entry captured more CLV, but better profitability than waiting
  was not established (CFB early 52.9 %, n = 153 vs confirmed-at-24 h 61.2 %, n = 49; neither significant).
  The timing study's suggestion: small early stakes on strong, model-agreed flags; the main stake Friday on
  games still flagged, at the best number.

**Where:**
- **Check NoVig first** (lowest typical hold ~2 %, best under price in 58–74 % of snapshots), then Kalshi /
  Polymarket after fees and size (not measured here), then Pinnacle. The best of seven books beats the sharp
  median by ~0.4 pt on average (biased up by noise).
- Break-even depends on the actual under odds, not the venue: −110 needs 52.4 %, −120 needs 54.5 %, +100
  needs 50 % (before fees). Lower average hold improves shopping; it doesn't make any particular quote +EV.
- No book is a clear leader. Other books sometimes lagged BetOnline / FanDuel moves by hours; after Pinnacle
  moves, matching was usually immediate but some books had multi-hour tails (NoVig p90 14 h). Low-medium confidence.

**How much:** expected returns and how many bets these rules produce per season have **not** been established.
Any bankroll cap is your discretion, not study-derived; nothing here supports bigger stakes for 25+ mph NFL.
Keep stakes small and flat until the live scorecard (§5 item 10) shows weather bets beating the matched baseline.

**How to judge it:** never by W-L (telling 55 % from 52.4 % takes ~2,300 bets). Judge by CLV **minus a
matched baseline** (best number on all games at the same lead). A season where weather bets beat the
baseline by ≥ 0.5 pt with a confidence interval above zero is the bar for trusting them more.

## 5. Changes to the system (ranked)

**Bugs (fix now):**
1. Exchange ladder-rung alerts 15–25 pts off the real total (9 alerts Oct 10–17). Reject any quote ≥ 5 pts from consensus.
2. Game records keep updating weather *after kickoff*; the backtest and stored fair totals read those values. Freeze at kickoff.
3. Repo backtest's ESPN request gets 403 → college games never graded unless the paid CFBD key is set (a browser User-Agent fixes it). Also `exposed` is False for every game in the backtest output.
4. Follow-up alerts overwrite the original alert's tier, so first-notice tier is lost.
5. The signal forecast (raw ECMWF numbers) isn't stored per run, so the current screen can't be graded later. Store it.

**Model/board:**
6. Replace the board's "edge" with **expected remaining move** = total × shade % × coefficient(sport, lead), in
   points. Keep the full shade only as an uncalibrated weather adjustment, not a final-score fair or a betting
   edge. (The candidate NFL score correction needs a calm-game over-drift term and is unproven; CFB × 0.4 is
   informational only.)
7. Refit the coefficients on the forecast the market actually reacts to (likely the raw model guidance, not the
   climatology-pulled one beyond 48 h) and validate before switching.
8. Recommended book/price should rank by expected remaining move and the actual quote's odds, not v1 EV.
9. Require a real weather signal rather than generic line shopping (in 2026, alerts with no weather shade matched
   generic shopping). Do not treat a zero climatology-blended board shade as absence of raw forecast wind; validate
   any shade or expected-move filter on current-screen candidates before enabling it. The line-only
   approximation of the 4 pp gate showed no demonstrated value; test the production gate (juice and worst-book
   rules included) against the expected-move threshold before retiring it.

**Measurement:**
10. Live scorecard: freeze line/odds/book/forecast at alert time; report CLV vs matched baseline by sport, lead and wind band.
11. After each season, refit the coefficients with the new archive and report updated confidence intervals;
    how much uncertainty shrinks depends on the number of new qualifying games.

**Open PRs:** #47 (historical backtest) is useful as is. #48 (v2 to final scores) and #49 (team totals) are
board-only and safe, but this study says v2 shouldn't be promoted; #49's CFB team-total evidence wasn't
re-tested here.

## 6. What we still don't know
- Whether NFL forecast-wind unders beat the close at bet time (need ~3 more seasons of archived forecasts).
- Whether the weather-driven CLV premium over a matched shopper (+1.3 to +1.9 pts, n = 9–14) is real.
- Exchange fees and size limits (all exchange edges here are upper bounds).
- Whether the market's early move is reacting to ECMWF, GFS ensembles or something else (forecast model run times aren't stored).
