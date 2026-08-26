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
