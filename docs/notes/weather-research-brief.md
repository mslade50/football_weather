# Weather betting research and paper-test handoff

October 26, 2026. Research conclusions through October 25; implementation and
initial production verification on October 26.

**Outcome:** no new profitable betting strategy was established. We deployed an
automated prospective paper test of the most promising wind-under hypothesis.
The live betting model, screens, stakes and alert rules were not changed.

## Belief and work performed

The original question was whether rushing-oriented teams are less affected by
bad weather than passing-oriented teams. The broader belief was that weather's
effect on scoring might depend on team style, stadium exposure and forecast
timing, and that bookmakers might not fully price those differences. We also
tested the reverse idea: calm, dry weather at usually weather-prone outdoor
venues might create an overs opportunity.

We examined team style in earlier exploratory work, then expanded into stadium
design and field orientation, longer opening-to-closing histories, rain/snow/
temperature, benign-weather overs, and timestamped forecast vintages with
sportsbook prices. Historical outcome coverage reached back to 1999 for NFL
and 2007 for college; genuine forecast-and-price evidence was concentrated in
2024–26. More observed-weather history does not supply missing pregame forecasts.

## What failed or remained unsupported

| Idea | Disposition |
|---|---|
| Rushing versus passing modifier | Earlier work found no reliable totals interaction warranting a weather adjustment. Team-style features were **not rebuilt on the expanded dataset**, so this question remains incompletely tested. |
| Stadium and orientation adjustments | Venue slopes did not predict later slopes reliably. All 26 registered orientation/venue development tests failed the multiple-testing screen. College crosswind was suggestive later, but could not rescue that failed gate; NFL direction reversed across samples. |
| Optimal betting time | Claims that 72 hours was optimal, openers should be avoided, or a specific NFL closing mispricing was identified were withdrawn. No net-of-drift timing test survived its league's multiple-testing correction. |
| Rain, snow and temperature rules | Hindsight scoring associations did not establish an executable forecast-time edge. Rain did not clear replication gates; snow was unstable. The existing 23 mm rain trigger fired zero times in 2,230 eligible forecast-games in the archive. No profitable replacement cutoff was established. |
| Benign-weather overs | None of 12 primary real-forecast tests survived the multiple-testing screen. The former precise expected-ROI estimate and claims of demonstrated market fairness were withdrawn. Absence of evidence is not proof of zero edge. |

The audit also exposed sample mismatches, forecast-availability mistakes,
uncertainty calculations that missed shared calibration dependence, and
unsupported conversions from line movement or scoring residuals into profit.
Corrections and explicit withdrawals narrowed the conclusions. Weather affecting
football, forecasts predicting weather, and a wager beating its price are three
different claims.

## What potentially worked

The strongest candidate was an under at the first usable ECMWF forecast crossing
12 mph. The post-hoc 2024–25 college sample had **184 bets across 26 season-weeks**,
approximately **+0.93 points of raw closing-line value (CLV)** and **+0.52 points
over a matched baseline**. Its saved-price record was 101–83, about +4.66% realized
ROI. Those are selected tuning-sample results, not an independently validated
profit forecast. The subsequently inspected 2026 sample was also involved in
policy development and is not clean confirmation. NFL evidence was thinner.

This justified collecting new evidence. It did not justify a live rule, a stake
increase, or treating CLV as guaranteed profit.

## What now runs automatically

[PR #51](https://github.com/mslade50/football_weather/pull/51) deployed permanent
captures, frozen paper opportunities, weekly grading, a scorecard and health
reporting through the existing jobs and R2 storage. Find **Weather paper test**
on the board's [Status tab](https://football-board.mckinleyslade.workers.dev/#view=status).

The primary test is CFB, known exposed roof, four complete IFS wind hours,
12+ mph, 24–144 hours before kickoff, using a fresh paired FanDuel total/odds.
15 mph, NFL/BetOnline and best-line comparisons are secondary. It freezes the
first qualifying observation, including a missing-price opportunity, and saves
all observed games and rejection reasons. Grading uses actual recorded odds;
fresh same-book closes and frozen controls support separate CLV comparisons.

**This is a new first-observed polling policy, not an exact replay of the
historical daily-vintage rule.** Its historical returns cannot be assigned to
the deployed policy. The authoritative definitions and recovery instructions
are in [PAPER_POLICY.md](../PAPER_POLICY.md).

Launch verification recorded two captures covering 76 games and a successful
grading job. There were no qualifying entries: the first capture's 27 eligible
games had valid FanDuel quotes, but winds topped out near 8 mph. The implementation
passed 1,722 Python tests, 197 browser/Worker tests, Ruff and independent Claude
review. That review cleared the implementation, not every historical research
claim; the earlier research-phase Claude review had been blocked by a usage limit.

## What to watch and what happens next

- **Weekly collection health:** recent capture and grade timestamps, gaps,
  unavailable/stale quotes and missing fresh closes. A successful capture can
  still have poor research coverage. The existing polling cadence can miss the
  final pregame hour needed for a close.
- **Forecast and control coverage:** the existing NWS-first pipeline selectively
  fetches IFS. Quiet games may lack complete IFS windows and cannot serve as
  controls. Comparisons are conditional on available data; they are not a
  representative causal benchmark for the entire market.
- **Honest uncertainty:** keep CFB/NFL and primary/secondary tests separate.
  Weekly clustered intervals start after eight informative weeks; that is a
  display threshold, not a promotion threshold. Do not tune cutoffs to early wins.
- **A future decision:** set a review endpoint and confirmation requirements
  before interpreting accumulated performance. Require adequate independent
  weeks, usable prices, coverage and returns after vig alongside baseline-adjusted
  CLV. No automatic promotion or stake recommendation is implemented.
- **Operations:** inspect checkpoint size and job duration at season end. No new
  provider, paid service or fetch cadence was added. To stop the paper process,
  set `FOOTBALL_PAPER_ENABLED=0`; retain its history. Paper failures are isolated
  from live board publication and are visible through health reporting.

The research source was the corrected local report
`docs/notes/weather-research-2026-10-25.html` in the original checkout and its
`tmp/inst/final_review/` audit notes. Those local research artifacts are not
included in this commit. This brief preserves the decision-relevant conclusions;
the deployed policy document defines the ongoing experiment.
