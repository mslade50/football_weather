# Historical backtest from the git line/forecast archive — build spec

Target repo: `C:/Users/McKinley Slade/dev/football_weather` (Windows, Git Bash, `python` = 3.10,
no `pip install` without asking, Write/Edit tools for files — no heredocs, type hints /
pathlib / f-strings, `ruff check .` clean, `python -m pytest tests -q -o addopts=""` green).
Read `CLAUDE.md`, `docs/ARCHITECTURE.md` §4–§7 and this file before writing anything.
Do **not** commit or push unless told; never run `pip install`.

## 1. Goal

Grade the **2024 and 2025 seasons** (NFL + CFB) against the rebuilt model so the board's
Backtest tab shows real Wins / Losses / ROI / CLV per bucket **now**, not after the 2026
season accrues. Output lands in the existing `board/backtest.json` as additional per-season
column groups, produced by a new `pipeline.backtest --from-git --seasons 2024,2025` mode.

## 2. What the data is and where it lives

### 2.1 Forecast + line archive = the repo's own git history
Between 2024-09 and 2026-04 the old generator committed `nfl_weather.csv` and
`cfb_weather.xlsx` ~3×/day (≈1,600 commits, message `Update <file> with Timestamp <iso>`).
Each commit is a **snapshot of every upcoming game at that moment**: the forecast as it
stood, the impact numbers, and the lines.

Columns (exact, from `tests/fixtures/legacy/`):

NFL csv: `Game,Date,Time,stadium,avg_wind,wind_vol,orient,wind_impact,weakest_wind_effect,
game_loc,travel_alt,home_temp,away_temp,year_built,wind_dir_1h,wind_dir_2h,temp_fg,wind_fg,
wind_dir_fg,rain_fg,gs_fg,away_fg,Spread_now,Odds_now,Total_now,Under_now,Spread_open,
Odds_open,Total_open,Under_open,Timestamp`
- `Game` = `"away vs home"` lowercase city names; `Date` = `SUN 11/09`; `Time` = `01:00 PM` (ET);
  `gs_fg`/`away_fg` are **fractions** (−0.065 = −6.5 %); lines are BetOnline; `Timestamp` = naive ET ISO of the run.

CFB xlsx (sheet `FBS`; sheet `Other` = FBS-vs-FCS games): `Game,Date,Time,wind_vol,orient,
wind_impact,weakest_wind_effect,travel_alt,home_temp,away_temp,wind_avg,year_built,
wind_dir_1h,wind_dir_2h,temp_fg,wind_fg,wind_dir_fg,rain_fg,gs_fg,away_fg,wind_diff,game_loc,
Fd_open,Odds_o,FD_now,Odds_n,Open,Current,Spread,Total_proj,Move_t,Move_s,My_total,Edge,
My_spread,Edge_s,Timestamp`
- `Game` = `"Away @ Home"` school names; `gs_fg`/`away_fg` are **percent**; `Fd_open/FD_now` =
  FanDuel total open/now with `Odds_o/Odds_n` the under price; `Open/Current` = FanDuel spread
  open/now; `Spread` / `Total_proj` = the generator's consensus reference.

Already-built reader: `scripts/_git_history.py` + `scripts/extract_golden.py` iterate
`git log --format=%H -- <file>` / `git show <sha>:<file>`, dedupe identical blobs by hash, and
emit per-row `commit_date`, `run_month`, `timestamp`, `sha`, `game`, `date`. **Reuse these**;
do not re-implement the git walk. Cache parsed snapshots as parquet under `data/backtest/git/`
(gitignored) so re-runs are instant.

Coverage reality: NFL 2024 wk 3 → SB, NFL 2025 full, CFB 2024 from late Sep, CFB 2025 full.
Preseason/early weeks of 2024 are absent — report coverage per season/week; do not fabricate.

### 2.2 Actual game-time weather
Open-Meteo ERA5 archive, hourly, per stadium — the fetcher and a resume-safe json cache
already exist in `pipeline/stadiums/climatology.py` (`--cache-dir`), and the last full pull is in
the session scratch dir `.../scratchpad/wx/era5_cache` (10-year hourly for all 173 stadiums;
copy it into `data/backtest/era5/` before starting — if it is missing, re-fetch with a 1.5 s
throttle; the archive quota 429s after ~45 stadiums/hour, so the loop must be resumable).
Actual = mean over the kickoff window `[kick, kick+3h]` (same as `pipeline/backtest.py::_window`).

### 2.3 Results
- NFL: nflverse `games.csv` (`result`, `total`, `home_score`, `away_score`) — parser exists:
  `pipeline/backtest.py::parse_nflverse_scores`.
- CFB: CFBD `/games?year=&seasonType=both&classification=fbs` (`CFBD_API_KEY` is in `.env` and
  loaded by `utils.env.load_repo_dotenv()`), fields `homePoints/awayPoints`; parser exists:
  `parse_cfbd_scores`. ESPN fallback: `parse_espn_scores`.

### 2.4 Model
`pipeline/model/impact.py::compute_impact_v1(sport, month, temp_fg, wind_fg, rain_fg, travel_alt,
home_temp, away_temp, home_elev_m=, era_date=)` reproduces the legacy numbers at 99.6 %
(golden test). `pipeline/model/signals.py::nfl_signal / cfb_signal` give the legacy tiers
(CFB needs `weekday` = ET weekday of the **run**, `open_spread`). v2 lives beside it
(`compute_impact_v2`). Use the archived `temp_fg/wind_fg/rain_fg/travel_alt/home_temp/away_temp`
as inputs — that is exactly what the generator saw at that lead.

## 3. Deliverable — `pipeline/backtest.py --from-git`

Add a mode (new module `pipeline/backtest_git.py` orchestrated from `pipeline/backtest.py`) that
produces `GameRow`s (the existing dataclass) from the git archive instead of `snapshots/`, then
flows through the **existing** grading (`grade_under`, `bucket_inputs`, `first_match`, grid
aggregation, `stadium_results`, `alerts_clv`) unchanged wherever possible.

### 3.1 Steps
1. **Snapshot extraction** (`extract_git_snapshots(sport, seasons) -> DataFrame`): every row of
   every distinct blob for the two files; columns = legacy columns + `commit_date` (UTC ISO of
   the commit), `run_ts` (Timestamp parsed as naive ET → UTC), `sha`, `sheet`.
2. **Game identity**: build `game_id = f"{sport}:{season}:{week}:{away}@{home}"` exactly as the
   pipeline does (ARCH §4.1): resolve team names through `pipeline/odds/teams.py` /
   `data/aliases/*.json` (NFL city names like `n.y. giants`, CFB school names); week from the
   schedule sources (nflverse `games.csv` for NFL; CFBD `/games?year=` for CFB) by matching
   home/away/date (±1 day). Season = kickoff year, except NFL Jan/Feb → previous year.
   Unresolvable names → count and list them in the output `meta.unresolved`; never drop silently.
3. **Kickoff time**: schedule kickoff (UTC) from the same sources, not the snapshot `Time`.
4. **Per-game series**: group snapshots by `game_id`, sort by `run_ts`. Derive for each snapshot
   `lead_hours = (kick − run_ts)/1h`. Keep only `lead_hours ≥ 0.5`.
5. **Model replay per snapshot**: recompute `gs_fg/away_fg` with `compute_impact_v1(..., era_date=commit_date,
   month=run_month)`; keep the archived values too (`gs_fg_archived`) and log the mismatch rate
   (expect ≥ 99 %). Compute the legacy signal tier per snapshot (NFL: `nfl_signal`; CFB:
   `cfb_signal` with `open_spread` = `Open` (FanDuel open) and `weekday` from `run_ts` in ET).
   Also compute v2 impact where inputs allow (`home_elev_m` from `data/stadiums.csv`).
6. **Alert simulation** (mirrors `pipeline/alerts.py` after the signal-tier change): the first
   snapshot whose tier != "No Impact" **and** `lead_hours ≤ 240` is the *alert snapshot*;
   record `alert_lead_h`, `alert_tier`, `alert_total`, `alert_under_odds`
   (NFL: `Total_now`/`Under_now`; CFB: `FD_now`/`Odds_n`), `alert_spread`. Track subsequent
   tier changes (`tier_at_kick` = tier of the last snapshot) so we can score
   "signal persistence" (did a 5-day-out High survive to kickoff?).
7. **Closing line**: last snapshot with `lead_hours ≤ 6` (else last snapshot; flag
   `close_lead_h`). `close_total`, `close_spread`.
8. **Actual weather**: ERA5 window mean at the stadium (`data/stadiums.csv` lat/lon via the
   stadium book; game_loc from the row as fallback): `wind_act, gust_act, temp_act, rain_act_mm`.
   Forecast error per lead: `|wind_fg − wind_act|` for the alert snapshot and for leads
   {24, 48, 72, 120, 168} (nearest snapshot within ±6 h of each lead).
9. **Result**: `home_score, away_score, actual_total, actual_margin` from §2.3.
10. **Grading** (reuse `grade_under`): bet = UNDER `alert_total` at `alert_under_odds` (default
    −110 when odds absent); `win/loss/push`; `roi` from the price; `clv_pts = alert_total −
    close_total` (positive = line moved our way); `clv_win = clv_pts > 0`. Also grade the
    **closing-line under** (bet at close) as a second column group `close_*` so we can see
    how much edge is timing vs. weather.
11. **Buckets**: `first_match` on the 118 legacy definitions using `bucket_on ∈ {forecast(at
    alert), actual}` — produce both. Aggregate per bucket per season: Wins/Losses/Push/Sample/
    Margin/ROI/+CLV/CLV % (same keys as today), keyed in the JSON as
    `by_season: {"2024": {...}, "2025": {...}}` on each grid row, plus `all_hist` (2024+2025).
12. **Tier scorecard** (new, small): per sport × tier × lead band (≤48h, 48–120h, >120h):
    n, win %, ROI, CLV %, mean |wind error|, persistence % (tier at kick ≥ tier at alert).
13. **Stadium results**: per stadium per season, same columns as the legacy Stadiums sheet.

### 3.2 CLI
```
python -m pipeline.backtest --from-git --seasons 2024,2025 [--sport nfl|cfb] \
    --era5-cache data/backtest/era5 --git-cache data/backtest/git \
    --board-dir data/board --parquet-dir data/backtest [--no-network] [--refresh-git]
```
`--no-network` must work end-to-end from the caches (tests use it). Print a coverage table
(season × week: snapshots, games, graded) and the tier scorecard to stdout.

### 3.3 Output — `board/backtest.json` (extend, don't break)
- every grid row gains `by_season: {"2024": {8 stats}, "2025": {8 stats}, "all_hist": {8 stats}}`
  (this-season `Wins…` keys unchanged; `legacy {...}` unchanged);
- `stadium_results` gains rows with a `season` field for 2024/2025;
- new top-level `tier_scorecard: [...]` (§3.1 step 12) and `hist_games: [...]` (one slim row per
  graded historical game: game_id, kickoff, tier at alert/kick, alert lead, alert total/odds,
  close total, actual total, result, clv_pts, wind_fg@alert, wind_act, bucket id);
- `meta.hist = {seasons, n_snapshots, n_games, n_graded, unresolved: [...], coverage: {...},
  model_match_rate}`.
Write parquet copies under `data/backtest/hist_*.parquet`.

### 3.4 Board (`site/web/backtest.js`)
Add a season selector on the Backtest tab: `2026 (this season) | 2025 | 2024 | 2024–25 | Legacy
sheet`; the grid and stadium tables render the chosen group; a "Tier scorecard" card above the
grid; `hist_games` behind a "graded games" expander (sortable, 200-row cap, link to the drawer
is not needed — the games are past). `backtestHover(g)` prefers this-season, then `all_hist`,
then legacy, and says which.

### 3.5 Workflow
`.github/workflows/backtest.yml`: add a manual `workflow_dispatch` input `from_git: true` that
runs the `--from-git` mode (needs `fetch-depth: 0` on checkout, `CFBD_API_KEY`, and the ERA5
cache restored from R2 `backtest/era5/` — upload it there once from local via
`wrangler r2 object put`; document the command in `site/worker/SETUP.md`). Weekly runs keep the
historical groups by **merging** the previous `backtest.json` (`by_season`, `tier_scorecard`,
`hist_games`) if the from-git step is not re-run, so the tab never loses them.

## 4. Tests (all offline)
- `tests/fixtures/git_archive/`: 6–8 real blobs per file (scrubbed to ≤ 15 games) spanning one
  NFL week and one CFB week of 2025 at leads ~7 d / 3 d / 1 d / 4 h, plus a tiny nflverse csv,
  CFBD games json, and ERA5 window json for those stadiums.
- `tests/test_backtest_git.py`: game_id resolution (incl. `n.y. giants`, `Miami (OH)`), season
  rollover (NFL Jan), lead computation across DST, alert-snapshot selection (≤ 240 h, first
  tier), tier persistence, closing selection (≤ 6 h), grading (win/loss/push, ROI at −110 and at
  +100, CLV sign), bucket assignment reproduces the legacy `Signal` id for a hand-checked row,
  by_season aggregation sums, `--no-network` end-to-end producing `backtest.json` with the
  new keys, and model replay match rate ≥ 0.99 on the fixture.
- `tests/test_site_contract.py`: backtest.js references `by_season`, `tier_scorecard`,
  `hist_games`, the season selector id.
- Existing tests must stay green (745+).

## 5. Acceptance
1. `python -m pipeline.backtest --from-git --seasons 2024,2025` runs locally in < 10 min from
   caches; coverage table shows NFL 2025 ≥ 250 graded games, CFB 2025 ≥ 600, and 2024 whatever
   the archive holds (report it); `meta.hist.unresolved` ≤ 2 % of archive games.
2. Backtest tab shows 2024 / 2025 / 2024–25 columns with non-zero samples, the tier scorecard,
   and the graded-games list; 2026 columns stay as they are.
3. Sanity: bucket 1 (NCAAF wind 8–15, temp 75–100, spread 10–20) historical numbers are in the
   same ballpark as the legacy sheet's 333-sample line (it was built from overlapping seasons);
   explain any large divergence in the report rather than tuning to match.
4. `ruff check .`, full pytest, `node --check site/web/backtest.js` clean.

## 6. Pitfalls (known)
- `Timestamp` in the archive is **naive ET**; `Date` has no year — take the year from the commit
  date, rolling back for Jan/Feb NFL games.
- Blob dedupe: many commits are identical (`Update … Timestamp` with no line change) — hash the
  blob before parsing (extract_golden already does).
- CFB `Other` sheet rows (FBS-vs-FCS) are gradeable and belong to CFB; keep them.
- The 2024 CFB weeks before the archive starts have no snapshots — report as "not covered".
- Lines are one book per sport (BetOnline NFL, FanDuel CFB), not the 3-book consensus the live
  board tracks now; label them as such in the JSON (`line_book`).
- Never use the ESPN scoreboard for historical results by default (it's a current slate); CFBD
  with the key, nflverse for NFL.
- `TELEGRAM_DISABLED=1` is set in `.env`; the backtest never sends anyway. Do not touch
  `tests/conftest.py`.

## 7. Implementation notes (built 2026-08-26)

`pipeline/backtest_git.py` (orchestrated from `pipeline/backtest.py --from-git`), board wiring in
`site/web/backtest.js`, tests in `tests/test_backtest_git.py` off `tests/fixtures/git_archive/`
(rebuild with `python scripts/make_backtest_git_fixtures.py`). Design summary: ARCH §7.6.

### 7.1 What the archive actually holds

`python -m pipeline.backtest --from-git --seasons 2024,2025 --no-network` — 9 s from the caches.

| | games | priced | alerted | graded (close) | alert bets | ERA5 actuals | weeks |
|---|---|---|---|---|---|---|---|
| CFB 2024 | 625 | 597 | 176 | 596 | 175 | 603 | 4–16 |
| CFB 2025 | 770 | 674 | 172 | 674 | 172 | 747 | 2–16 |
| NFL 2024 | 176 | 173 | 82 | 173 | 82 | 176 | 3–19 |
| NFL 2025 | 211 | 171 | 91 | 171 | 91 | 211 | 1–22 |

53,312 snapshot rows from 1,427 distinct blobs → 1,782 games, 1,614 graded, 521 alerted.
v1 replay matches the archived `gs_fg` on **99.73 %** of snapshots (NFL 100 %). Unresolved team
names: **0**. FBS/NFL rows that found no schedule game: 5 of 32,955 (0.02 %).

Two acceptance numbers in §5.1 are not reachable from this archive and are reported rather than
tuned to: **NFL 2025 has 211 games in the archive, not ≥250** (the generator listed only the games
inside its forecast horizon, so a slate appears partially), and the 2024 seasons start at CFB week
4 / NFL week 3 (the archive begins 2024-09-17). CFB 2025 clears the ≥600 bar (674).

### 7.2 Deviations from §3, and why

- **The workbook's `Other` sheet is FCS-vs-FCS, not FBS-vs-FCS, and carries no lines** (no
  `Fd_open`/`FD_now`/`Odds_*`, and no `Timestamp` — its run time falls back to the commit). The
  1,795 rows that do match an FBS-classification schedule game are kept and replayed; the other
  18,562 have no schedule row and no total, so they can never be graded. They are counted per
  sheet in `meta.hist.rows_by_sheet` instead of being reported as failures.
- **`Date` resolves to the nearest MM/DD, not the next one.** The generator kept a game listed for
  a day or two after kickoff; a strictly forward rule pushed those rows a full year forward (it
  cost ~350 NFL rows and 15 phantom matchups before the fix).
- **Bucket inputs are the closing forecast for every game, not the alert forecast.** §3.1.11 says
  "forecast (at alert)", but unalerted games have no alert forecast, so that basis would mix two
  different measurements inside the close-bet column group and make it incomparable with the
  alert-bet one. The closing forecast is also what `row_from_snapshots` feeds the 2026 columns.
  The alert-time forecast rides along as `wind_alert` / `temp_alert` and is what the tier
  scorecard's wind error and `wind_err_alert` are computed from.
- **The two graded bets are two sibling maps, not one prefixed block**: `by_season` (UNDER at the
  alert total) and `by_season_close` (UNDER at the closing total), each `{"2024", "2025",
  "all_hist"} → the 8 legacy keys`. The board renders one of them as the primary column group
  (`#bt-season` picks the season, `#bt-bet` picks the bet). Per-lead forecast error is a long
  table (`data/backtest/hist_leads.parquet`) rather than 10 more `GameRow` columns.
- **Per-stadium records grade the closing bet** (`stadium_results` rows with a `season`), which is
  what the legacy Stadiums sheet measures; grading them at the alert would shrink them to the
  alerted subset.
- **CI restores `backtest/era5/windows.parquet`, not `backtest/era5/`.** The hourly ERA5 cache is
  ~670 MB across 437 files; `fill_actuals` reduces it to one mean per kickoff window (1,082 rows,
  29 KB) and reads that first, so the workflow only needs the reduction. Upload command in
  `site/worker/SETUP.md` §10. `wind_dir_act` stays null: the climatology cache does not fetch
  `wind_direction_10m`.
- `--era5-max-fetch N` bounds the archive pull per run (windows are half-years per stadium, named
  exactly like the climatology cache so a partial pull resumes). The 147 windows missing for
  2025/26 fetched cleanly at the 1.5 s throttle with no 429.

### 7.3 Sanity (§5.3)

Bucket 1 (NCAAF wind 8–15, temp 75–100, spread 10–20), UNDER at the close: **10-14-0, ROI −0.205
on n=24** against the sheet's 165-162-6, ROI −0.036 on n=333. Same sign, and the gap is inside one
standard error at n=24 (±0.20). The sheet is not restricted to 2024–25 — 333 samples in one narrow
bucket implies roughly 6–10 seasons — so the two lines are not expected to agree in magnitude.

### 7.4 Follow-up pass (2026-08-31): what the replay changed about the live system

Reading the replay produced four changes to the shipped pipeline, plus one decision to leave a
rule alone. Numbers below are 2024–25, under the rules as they now stand.

**Betting week gate.** `utils.timeutil.bet_week_open` / `in_bet_week`: no EDGE alert before Monday
00:00 ET of the game's own week (`alerts.py::edge_candidates` takes the run clock;
`collect_candidates` always passes it). The weather window still runs 10 days so the board carries
next week's forecast — only the alert waits. Per GAME, not per (season, week): CFBD files the
whole postseason as one week, which would make a January bowl bettable in mid-December. The replay
applies the same gate, so the board's history is the rule that is actually bet. Cost: 28 of 521
alerts; every bucket improved slightly (any tier −2.3% → −0.7%, ≥Mid +23.2% → +26.9%).

**NFL Low names its cause.** `nfl_signal` now returns `Low (Rain)` / `Low (Wind)` like CFB
(`level` unchanged, so keys/tiers/`signal_slug` are untouched). The bare label hid a real split:
NFL wind −15.6% (n=103) and NFL rain −18.7% (n=28) are 79/21 of a bucket that read as one thing.

**The scorecard keys on the peak tier.** `alert_tier` is the first tier of *any* kind, so a game
that opened Low and became Very High was filed under Low. Rows now carry `peak_tier` and the
escalation bet taken at that snapshot, and `tier_scorecard` groups on it. The undercount was
roughly 4x: Very High 4 → 16, High 6 → 25.

**`evaporated` rides with every signal.** `tier_for()` re-scores a game through the same signal
functions with the ERA5 actuals substituted for wind/temp/rain, so "would this have fired if we
had known the weather?" is one call. `tier_scorecard` carries the share that would not have fired
(65.8% overall; CFB `Low (Wind)` at 48–120 h is 86%), and `backtestHover` puts the tier's own
record and `1 − evaporated` on every hover card and drawer. The caveat travels with the signal
instead of living in this document.

**Low stays on, deliberately.** The evidence does not support removing it: CFB Low is a push
(wind +1.7% ± 6.8%, rain +0.8% ± 11.2%), not a loser, and two seasons cannot separate "no edge"
from "small edge". Conditional on the weather being real, Low is +4.5% ± 9.3%. Betting it later in
the week does not help — evaporation falls 66% → 44% between Monday and the day before, and ROI
does not improve, because the market sharpens at the same rate the forecast does. Raising the NFL
wind floor does not help either (8 → 13 moves ROI −16.2% → −9.3%, every value inside every other's
error bar; the worst single band is 10–11 mph, *above* the obvious cut). 2026 is the first season
with real `lead_hours` and ensemble spread rather than the legacy day-of-week proxy, so the
question gets a better answer next year than this data can give.

**Not adopted:** gating alerts to ≥ Mid. It is the only cut with a signal (+26.9% ± 10.6%, n=74,
positive in both seasons, both sports and every lead cap), but it costs 84% of the volume — ~1.7
bets a week — and the threshold was chosen by testing against this same data. Recorded here as a
hypothesis to track forward, not a validated edge.

## 8. Stadium weather records (`pipeline/stadium_wx.py`, built 2026-08-31)

Rebuild of the hand-built CFB "Stadiums" sheet for both sports, over ten seasons, out of data the
repo already holds. `python -m pipeline.stadium_wx --seasons 2015-2024` → 11,667 graded outdoor
games (1,980 NFL + 9,687 CFB), 9,452 with an ERA5 kickoff wind, 183 venue rows.

| input | source | note |
|---|---|---|
| weather | ERA5 hourly 2015–2024, 173 stadiums | already local (`data/backtest/era5`); no fetching |
| NFL lines + results | nflverse `total_line` / `under_odds` / `total` | complete back to 1999 |
| CFB lines + results | CFBD `/games` (venue) joined by id to `/lines` (totals) | one request per season, cached |

Keyed on **ERA5 wind at kickoff**, not forecast wind, so the forecast error measured in §7 stays
out of the venue numbers. `stadium_wx.fill_actuals` indexes each hourly file by hour first —
`backtest.window_stats` rescans every timestamp per game, which is fine for the 1.8 k games of the
git replay and ~10⁹ parses here.

### 8.1 The result that holds up: absolute wind

Under at the closing total, by ERA5 wind over the kickoff window:

| sport | band | record | n | win % | ROI |
|---|---|---|---|---|---|
| **NFL** | < 10 mph | 649-670-17 | 1336 | 49.2 % | −4.3 % |
| **NFL** | ≥ 10 | 335-227-3 | 565 | **59.6 %** | **+15.7 % ± 4.0** |
| **NFL** | ≥ 12 | 193-134-2 | 329 | 59.0 % | +14.5 % |
| **NFL** | ≥ 15 | 81-58-1 | 140 | 58.3 % | +13.2 % |
| **CFB** | < 10 | 2958-3001-63 | 6022 | 49.6 % | −5.2 % |
| **CFB** | ≥ 10 | 808-704-17 | 1529 | 53.4 % | +2.0 % |
| **CFB** | ≥ 15 | 163-132-6 | 301 | 55.2 % | +5.4 % |
| **all** | ≥ 10 | 1143-931-20 | 2094 | 55.1 % | +5.7 % ± 2.1 |

Monotonic in wind, ~4 SE on the NFL side, unchanged by restricting to `roof == outdoors`
(+14.9 %), and positive in **8 of 10 seasons** (2016 −13 % and 2024 −21 % the exceptions, n≈45
each). The market does not fully price wind into totals, and it under-prices it most in the NFL.

This is a **measurement, not a strategy**: nobody can bet ERA5 actuals. It establishes the physics
the signal is trying to catch, and it reconciles §7 — the effect is real and large, the forecast is
the bottleneck (65 % of firings evaporate), which is why a stricter forecast bar (≥ Mid) showed an
edge and Low did not.

### 8.2 The result that does not: per-venue splits

The venue table was the thing asked for, and it does not survive its own test.

* Per-venue top-quartile ROI across the 146 venues with a sample ≥ 8: **sd 0.244** against
  **0.247** expected from pure coin flips at the same sample sizes — a ratio of **0.99**. There is
  no detectable venue-to-venue variation beyond sampling noise. Lincoln Financial's 18-4 is the top
  of 146 noisy draws, not evidence about Lincoln Financial.
* A venue's first half does not predict its second: **corr(early ROI, late ROI) = +0.05** over the
  151 venues with ≥ 10 graded games in each half.

`venue_noise_check` computes both and puts the verdict in `meta.venue_noise`; the board labels the
venue row **"descriptive only"** so it cannot read as an edge. The pooled band (§8.1) is what the
hover shows as actionable, tagged *"if the wind shows up"* next to the tier's evaporation rate.

The honest summary: **wind matters, venues don't** — or at least, ten seasons cannot show that they
do. Any venue effect is smaller than the noise floor of ~60 games per stadium, so a venue prior
would need either far more history or a hierarchical model that shrinks each venue toward the
pooled band rather than reporting its raw record.

### 8.3 Caveats

* ERA5 10 m wind is a 3-hour grid-cell mean and reads *below* a stadium anemometer; the bands are
  in that vocabulary, not the signal's forecast vocabulary (`actual ≈ 0.824 × forecast + 0.74`).
* 2,215 of 11,667 games have no ERA5: venues outside the 173-stadium cache (FCS, neutral sites) and
  January games past the 2024-12-31 window. Fetch those before extending the seasons.
* CFB totals are the median across whatever providers CFBD carries that year (1 book pre-2022, 2–3
  after), so the CFB closing number is noisier than the NFL one.
* Retractable roofs: only `dome` / `closed` are excluded. A roof that was closed but filed as
  `retractable` stays in, which dilutes rather than inflates the wind effect.
