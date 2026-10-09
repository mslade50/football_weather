# Directional wind backtest

`pipeline.directional_backtest` compares closing-total unders under crosswind,
along-field wind (head/tail combined), and oblique wind. Components follow the
live pipeline: absolute wind speed times the sine/cosine of wind direction minus
field orientation. Dominant crosswind/along-field requires at least twice the
other component; other angles remain oblique.

The forecast cohort uses each game's last archived pregame forecast within seven
days. A valid forecast timestamp before kickoff is required. Historical Git rows
use the later of their recorded timestamp and the first commit containing that
content, so postgame backfills cannot become pregame forecasts. The game-time cohort
uses retrospective model estimates (source retained per game), not measurements
inside the stadium. The existing kickoff-hour plus next-two-hourly-samples window
is retained. Forecast and retrospective classifications are never substituted or
pooled. The canonical CFB filter is wind >14 mph, temperature <70°F, absolute
opening spread <=10; NFL is wind >15 mph and temperature <60°F. The broader cohort
uses outdoor games with wind >14 mph, irrespective of temperature/spread.

Every direction group includes one row per game. Unknown/closed roofs, missing
orientation/direction, ungraded games, missing closing totals, and unverifiable
forecast timestamps are counted explicitly as exclusions. Neutral-site venue
identity must be verified rather than inferred from the home team. Historical
single-book opening spreads are labeled with their source and are proxies for
the modern consensus opener.

Historical NFL closing totals come from nflverse `total_line` when available.
Historical CFB uses the last archived pregame quote within six hours of kickoff
as a closing proxy, with quote time/source retained; older quotes are not graded.
Modern games use the existing backtest's retained closing totals.

W-L-P and scoring margin are against that total. ROI assumes -110 and a
one-unit stake, including pushes in the stake denominator. Win rate and its Wilson
95% interval exclude pushes. Opening-minus-closing movement is not alert-entry CLV;
historical NFL can combine the archived opener with nflverse's close, rather than
prices from the same book.
These reports do not simulate the first signal entry price.

The matched comparison weights crosswind-minus-along-field differences within
sport, season, 5-mph wind, 10°F temperature, 10-point absolute opening-spread, and
rain strata (dry, >0–2, >2–6, >6 mm). Only strata containing both directions enter.
Unknown matching covariates are excluded. This remains an observational comparison:
stadium/team effects, multiple comparisons, and correlated games can affect results.
The win-rate intervals do not model stadium dependence; small samples (<30 resolved
games) are flagged. Inspect individual seasons rather than selecting thresholds
that maximize historical ROI.

## Reproduction and publication

Recover the retained 2024–25 Git archives and public ESPN/nflverse results with:

```powershell
git fetch origin backtest/historical-replay:refs/remotes/origin/backtest/historical-replay
python -m scripts.prepare_directional_archive --fetch-actuals --output data/backtest/directional_inputs.parquet --cache-dir data/backtest/directional_archive_cache
```

The preparation script writes a SHA-256 manifest and resumable schedule/weather
caches. Re-running continues missing ERA5 windows; `--offline` uses cached schedules,
and `--network-budget-seconds` bounds the weather fetch. ERA5 requests cover every
verified outdoor graded candidate independently of forecast wind, preserving games
whose strong winds were not forecast. Actuals require three hourly samples and sum
precipitation over the window. Missing data remain missing.

The report accepts JSON game rows or the existing backtest games parquet with
`orientation_deg`, `forecast_at`, `wind_dir_fc`/`wind_dir_deg_fc`, and
`wind_dir_act`/`wind_dir_deg_act` retained:

```powershell
python -m pipeline.directional_backtest --input directional_inputs.parquet --output direction.json
python -m pipeline.backtest --no-network --directional-input directional_inputs.parquet --board-dir data/board --parquet-dir data/backtest
```

The ordinary weekly job reads `backtest/directional_inputs.parquet` from R2 as an
optional historical supplement, adds current-season rows, and writes
`directional_wind` into `board/backtest.json`. It exports `directional_groups.parquet`
and `directional_games.parquet` for auditing. The input dataset is never overwritten
by that job. Transport/authentication failures fail the job; a genuinely absent
historical input produces an explicitly current-season-only report.

No alert rules, impact weights, Telegram messages, or betting thresholds are changed
by this analysis. Manual backtest publication uses `digest=false`.
