# Team style × weather: does run/pass identity change weather-under results?

Study date 2026-10-24. Scratch code + caches: `tmp/rushpass/` (gitignored; re-runnable).

## Question
Run-dominant teams should be less affected by wind/rain, so weather unders involving them
should do worse (and pass-heavy matchups better). Tested in NFL and CFB, betting the under at
the closing total, using only information available before kickoff for team style.

## Data
| | NFL | CFB |
|---|---|---|
| Seasons | 1999–2025 | 2017–2025 (FBS vs FBS with a total) |
| Games (outdoor/open, line + weather) | 4,468 | 5,895 |
| Closing total | nflverse `games.csv` `total_line` | CFBD `/lines` (consensus ≤2022, DraftKings 2023+) |
| Weather | nflverse game-day reported wind/temp | Open-Meteo ERA5, mean of kickoff→+3h (wind runs ~30–40 % below stadium reports; bands are relative) |
| Style | nflverse play-by-play: neutral-situation pass rate (1st/2nd down, WP 20–80 %, not last 2 min of half), rush vs dropback EPA | CFBD advanced game stats: run share with game script removed (residual on final margin), rush vs pass PPA |

CFBD weather (`/games/weather`) needs a Patreon tier, hence ERA5. The study cost ~50 CFBD
calls (the key had ~310 left for the month at the end).

## Findings

### 1. No reliable style × weather effect in either league
Unders at the closing total when windy, by pre-game style of the matchup (avg of both teams,
season-relative terciles):

| | pass-lean | run-lean | p |
|---|---|---|---|
| NFL wind ≥ 12 mph (n≈430 each) | 55.9 % | 54.4 % | 0.65 |
| NFL wind ≥ 12, 2013+ | 57.8 % | 55.2 % | 0.60 |
| CFB ERA5 wind ≥ 12 (n≈210 each) | 53.5 % | 59.4 % | 0.22 |
| CFB 2017–20 / 2021–25 | 53.6 / 53.4 % | 66.7 / 51.5 % | 0.04 / 0.79 |

- Continuous regressions (total − close ~ wind above threshold × style) give interaction
  p = 0.24–0.94 in every specification, both leagues, for run share and for efficiency gaps.
- **Hindsight test:** using each team's actual full-season style (leave-one-out) instead of
  the pre-game estimate also shows nothing (NFL p = 0.51–0.91, CFB p = 0.29–0.37). Perfect
  knowledge of style would not have helped, so better estimators cannot rescue it.
- Rain × style in CFB: run teams are slightly less hurt (+1.0 pt per SD), p = 0.36. Not usable.
- Where splits look strong (CFB 2017–20, NFL "both pass-lean" in wind), they don't hold in the
  other half of the sample, and in CFB the direction is the *opposite* of the hypothesis.

### 1b. Spread version: does the more run-leaning side cover in weather?
Games with a clear style mismatch (top half of |run-share difference|), pushes excluded.
- **NFL:** no. In wind ≥ 12 the run-leaning side covers 49.3 % (n = 637). Since 2013 it's 43.9 %
  (n = 271, p = 0.04), the *opposite* direction. Regression interaction p = 0.50.
- **CFB: the one lead.** The run-leaning side covers 49.5 % (<8 mph ERA5), 50.8 % (8–12), 54.3 %
  (12+, n = 302, p ≈ 0.15). It holds in both halves (2017–20 55.2 %, 2021–25 53.2 %) and is
  stronger when the run team is the underdog (56.2 %, n = 144). Not significant, and the
  continuous regression is flat (p = 0.96), so it isn't proven. It's worth tracking live
  rather than building on now.

### 2. Why the first quick look seemed to support it
The first pass used raw rush rate (carries ÷ plays over the last 10 games). That gave NFL
wind ≥ 10 under 58.6 % (pass-heavy) vs 51.4 % (run-heavy), p = 0.016. Taking raw rush rate
apart: the part that predicts is **game script** (teams that run because they're leading),
p ≈ 0.07, not play-calling. Neutral-situation pass rate, the real "identity" measure, has zero
effect (p = 0.98). It's a borderline result found among many cuts, so treat it as noise.

### 3. The market already prices style, just not with weather
Closing totals are lower for run-heavy matchups (NFL −1.6 pts per SD of neutral pass rate;
CFB −2.0 pts per SD of run share) and drop for wind (NFL −0.12, CFB −0.30 pts per mph), but
the wind drop is the same size regardless of style (interaction ≈ 0, p ≈ 0.7–0.8). Results
show that's correct: there is no extra drop to capture.

### 4. Teams barely change behaviour in wind (NFL)
Neutral pass rate vs the team's own pre-game baseline: −0.4 pp at 10–14 mph, −1.6 pp at
15–19, −2 to −5 pp at 20+. The scoring drop in wind comes from execution (kicking, deep
passing), not a shift to the run, which is why identity doesn't moderate it.

### 5. What does carry the signal: wind itself
NFL unders at close: 48.8 % (0–9 mph), 53.8 % (10–14), 57.1 % (15–19), 53.4 % (20+).
CFB (ERA5): 50.6 % (<8), 51.1 % (8–12), 53.9 % (12–16), 57.7 % (16+). That is the existing
model's job; style adds nothing on top of it.

## Addendum: is the board's fair total calibrated? (`tmp/rushpass/fair_total.py`)
The board already computes a fair total, `consensus × (1 + gs_fg%/100)` with v1 tiers, and
edges in points (`pipeline/model/fair.py`). The alert, the tier and the backtest grading are
binary (under or not). Nobody has checked the fair *number* against outcomes. First pass:
apply v1 to game-time weather and compare its shade to what the closing total missed.

| | slope of (total − close) on v1 shade | read |
|---|---|---|
| NFL 1999–2025 (n = 5,120) | **1.11 ± 0.22** | the close missed roughly the full v1 shade |
| NFL 2013+ | 0.92 ± 0.33 | same |
| CFB, ERA5 wind as-is | 0.43 ± 0.31 | the close already has most of it |
| CFB, ERA5 × 1.35 | 0.12 ± 0.18 | same |

Data-driven NFL residual curve: −0.29 pts per mph above 8 (p < 0.001); the extra 15+ mph term
and cold are not significant. Out-of-sample (leave-one-season-out) it ranks games correctly
but over-predicts the extreme bucket (predicted −2.7, realised −1.9 vs the no-shade baseline).
Residual SD around the close: NFL 13.6, CFB 16.4 pts (this is the σ for edge → win probability).

**Two big caveats, so this is direction, not a number to ship:**
1. It uses *actual* game weather. The market set the close on forecasts, so part of the "miss"
   is forecast error nobody could have known. That overstates what's capturable. The real
   test needs the forecast as it stood at bet time, i.e. the git-archive backtest.
2. ERA5 measurement error biases the CFB slope toward 0, so "CFB is priced" may partly be
   noisy wind data.

What it suggests building: a calibrated fair total, `fair = line + β(lead, sport)·shade(forecast)`
with β fitted on archive snapshots by lead time, plus σ to turn the gap into P(under) and EV.
Grade alerts on realised points against the fair number (and CLV), not just W/L.

## Alert-time results from the git-archive backtest (2024–25)
Built `pipeline/backtest_git.py` (reviewed, receipt `20261024-170736-54760e`):
`python -m pipeline.backtest_git` → `data/backtest/hist_{snapshots,games}_{nfl,cfb}.parquet`.
NFL 344 games / 5,161 snapshots; CFB 1,270 games / 32,192 snapshots. Model replay matches the
archived numbers on 100 % (NFL) and 99.6 % (CFB) of snapshots; 1 CFB game unresolved.
Lines are one book (BetOnline NFL, FanDuel CFB). Analysis: `tmp/hist/analysis.py`.

**Market movement is the solid finding.** Share of the v1 shade the line moves *after* a
snapshot (by the close), clustered by game:

| lead | NFL | CFB |
|---|---|---|
| 72–168 h | **0.41 ± 0.09** | **0.21 ± 0.04** |
| 24–72 h | 0.28 ± 0.06 | 0.14 ± 0.03 |
| 0–24 h | 0.08 ± 0.03 | 0.03 ± 0.02 |

So v1's shade is real information the market prices in later, but only 20–40 % of it shows up
as line movement, and almost none of it is left on game day. Outcome-based coefficients
(actual − line) are ≈ 1 but ± 1, so two seasons can't calibrate against results; calibrate
the fair total to **CLV**, then confirm on results over time.

**Alerts (under at the alert line):**
- NFL High 10–1–1 (+8.2 u), Mid 17–16–1 (−0.5 u), Low 6–13 (−7.6 u). Alerts 5–10 days
  out did best (14–8–1, +1.0 pt CLV); ≤ 48 h lost.
- CFB: 115 of 126 alerts are Low, 57–58 (−6.3 u) despite +0.67 pt average CLV. Early (120–240 h)
  CFB alerts were 28–32.
- Every under at the close, all games: NFL 156–185, CFB 633–637. The baseline is negative.
- σ(actual − close): NFL 12.9, CFB 15.9. Forecast wind error vs ERA5: ~1.8 mph at 1–2 days,
  2.0 at 3 days, 2.5 at 5 days, 3.4–5.4 at 7 days.

**CFB spread lead at bet time** (run-leaning side covers, clear style mismatch only):
calm 47–50 %; 10–15 mph forecast 60 % (72 h, n = 79) and 74 % (close forecast, n = 77);
15+ mph too few games (15–17). Same direction as the 2017–25 closing-line study, small sample.

## Look-forward style estimates (built and validated; reusable)
Even though the betting effect is null, this is the right way to estimate team style before
a game if it's ever used (e.g. as a spread/side input, or for a re-test on live data).

**Estimator:** `pre = (k·shrink·prior_season + n·season_to_date) / (k + n)`, all centred on
the league mean (prior season's league mean for NFL, so no leakage).

| | NFL neutral pass rate | CFB script-adj. run share |
|---|---|---|
| Year-over-year corr (all / same coach / new coach) | 0.45 / 0.49 / 0.29 | 0.68 / 0.75 / 0.38 |
| Same coach + same QB (NFL) | 0.57 | n/a |
| Low / high returning production (CFB) | n/a | 0.59 / 0.76 |
| Best k (prior weight in games) | 6 | 3 |
| Prior shrink: same / new coach | 0.4 / 0.2 | (0.6 + 0.3·returning PPA) × (1 / 0.7) |
| Corr. with true full-season style, wk 1–3 / mid / late | 0.50 / 0.75 / 0.93 | 0.75 / 0.90 / 0.97 |

- **Early season:** CFB identity is knowable from day one (option teams, Air Force, etc.);
  NFL identity is only half-known until ~week 4 and much less after a coaching change.
- **Efficiency gaps (rush EPA − pass EPA) are not predictable pre-game** in either league:
  YoY corr 0.22 (CFB) to 0.37 (NFL), and in CFB the blend loses to predicting zero
  out-of-sample. Don't build on efficiency; tendency is the only stable style signal.
- CFB estimator tuned on 2017–21, tested on 2022–25 (MSE 0.0091 vs 0.0101 to-date-only,
  0.0121 prior-only). NFL tuned in-sample over 2000–25 (small parameter grid).

## Recommendation for the process
1. **Don't add a style term to v1 or v2 impact, and don't use it to filter alerts.** The
   hindsight test rules it out; any weight fitted to it would be fitting noise.
2. If you want it on the board as context, show "run share (pre-game)" in the drawer, but
   label it informational. Cheap to compute: NFL from nflverse pbp weekly, CFB from one CFBD
   `/stats/game/advanced?year=` call per week, plus `/player/returning` and `/coaches` once
   preseason.
3. Re-test only if new evidence appears: once the git-archive historical backtest
   (`docs/HISTORICAL_BACKTEST_SPEC.md`) exists, rerun the style split at the *alert*
   snapshot (forecast wind, earlier line) rather than at close. This study can't rule out a
   timing-specific effect, since everything here is at the close.
4. Better leverage for "process" work: forecast-error-by-lead and the wind curve itself
   (§5), plus line timing (alert vs close), which this study shows is where the edge lives.

## Reproduce
```
cd tmp/rushpass
python nfl_build.py && python nfl_analysis.py && python nfl_decomp.py && python nfl_oracle.py
python cfbd_fetch.py   # needs .cfbd_key; ~4 calls/season, cached
python cfb_weather.py  # ERA5 per game, resume-safe, ~80 games/min (hourly quota)
python cfb_build.py && python cfb_analysis.py
```
