# Process screen

How the board decides which games belong on the under screen. This is a
description of filters and the code that implements them. It is not a pick,
a recommendation, or a profit claim.

The rules are the same for NFL and CFB. College keeps one extra market gate
that was already in the product: the consensus opening spread must be within
±10. A missing CFB opener stays ineligible.

## What is frozen

`pipeline/model/impact.py` `compute_impact_v1` is unchanged. Its rain tiers
stay `>1 / ≥6 / >12` mm (`config.RAIN_TIERS_MM`). Alerts still quote v1 impact
until the existing promotion gate says otherwise (`ALERT_MODEL`, default `v1`).
v2 still runs beside it.

The stitched forecast that v1 reads is also unchanged:

| Lead | Value v1 uses (`wind_fg`, `temp_fg`, `rain_fg`) |
|---|---|
| ≤ 48 h | HRRR, then NBM (existing stitch) |
| 48 h to 7 d | NBM, with the medium-range blend filling gaps |
| > 7 d | Weighted AIFS / IFS / GFS mean |

Changing that stitch would change the weather inputs v1 turns into `gs_fg` on
live cards. The process screen therefore does **not** replace those fields.
Beyond 48 h it reads a separate ECMWF-first forecast (`signal_*` on the
weather block). `PROCESS_ECMWF_FIRST=0` leaves the signal forecast on the
stitch as well. Per-model numbers are always shown.

The published tier (`pipeline/build.py` `build_record`) uses that same
signal forecast. v1 `gs_fg` still uses the stitch. Past 48 h the impact
line and the signal label can disagree; the label is the process screen.

## Rules

### 1. Temperature

Temperature is shown on the card and is **not** a hard gate. 65°F is only
the default threshold of an optional gate that is **off** for both NFL and
CFB. While the gate is off, a warm game can still flag on wind or on agreed
heavy rain.

Turn the gate on with `PROCESS_TEMP_GATE`:

| Value | Effect |
|---|---|
| unset, `0`, `false`, `off` | gate off; temperature is informational |
| `1`, `true`, `on` | gate on; signal-driving temp must be under 65°F |
| a number, such as `60` | gate on; temp must be under that number |

`PROCESS_TEMP_GATE_NFL` and `PROCESS_TEMP_GATE_CFB` override the global
switch for that league when they are set. An unset league variable follows
the global switch. The comparison is strict `<`. A missing temperature
fails the gate only when the gate is on.

The dashboard reads `process.temp_gate` and `process.temp_gate_max_f` when
the card carries them. Cards built with the gate off, and cards that have
no process block yet, keep the default: gate off.

Code: `pipeline/model/config.py` `PROCESS_TEMP_MAX_F`, `PROCESS_TEMP_GATE`.
`pipeline/model/process.py` `temp_gate`, applied in `evaluate_card` and
`pipeline/model/signals.py`. `site/web/discovery.js` `activeTempGate`.

### 2. Wind

Sustained wind only. Gusts and the ensemble P90 are not the gate. The same
cut applies to NFL and CFB.

| Sustained wind | Band |
|---|---|
| under 12 mph | not in the wind screen |
| 12 mph up to 15 mph | watch. Display only. **Not a flag** and not an alert |
| 15 mph and above | flag |

Exactly 12 mph is a watch. Exactly 15 mph is a flag. The watch is labeled
"not a flag" on the dashboard and does not send Telegram.

Direction uses the stadium field orientation in `data/stadiums.csv`
(`orientation_deg`, served as `stadium.orient_deg`):

```
crosswind factor = |sin(wind_dir_deg − orientation_deg)|
cross_mph = sustained wind × that factor
head_mph  = sustained wind × |cos(wind_dir_deg − orientation_deg)|
```

The factor is 1 for a pure crosswind and 0 for a pure headwind or tailwind.
The dashboard shows the factor and both components (drawer, map popup).
There is no crosswind gate. A 2024–25 check found an NFL crosswind
association and no CFB crosswind association; that is context for reading
the number, not a filter.

Code: `process.wind_band`, `process.crosswind_factor`, `process.wind_components`.
The stitched `weather.cross_mph` / `head_mph` from `pipeline/weather/merge.py`
remain the v1-window components. The process block repeats them from the
signal-driving wind.

### 3. Rain

Game-window total is the sum of the three hourly samples from kickoff through
+2 h, in millimetres. Light rain is ignored.

| Total | Tier |
|---|---|
| < 5 mm | ignore |
| 5 mm up to 15 mm | watch (matters with wind or cold, not by itself) |
| 15 mm up to 23 mm | elevated watch (still short of the heavy-rain signal) |
| ≥ 23 mm | heavy-rain amount |

23 mm is the round of 0.30 in/hr sustained for three hours
(0.90 in × 25.4 = 22.86 mm).

A heavy-rain **signal** also needs agreement: the ECMWF total (IFS if that
field is present, otherwise AIFS) and a non-ECMWF partner are both ≥ 23 mm
and within 10 mm of each other. The partner is the closest non-ECMWF model
that is itself at or above 23 mm. A nearer model under 23 mm is not the
partner. IFS and AIFS do not confirm each other.

Example: ECMWF 25 mm, NBM 22 mm, GFS 30 mm. NBM is closer but under 23 mm,
so the partner is GFS. The gap is 5 mm, inside 10 mm, so the rain agrees.
If GFS were also under 23 mm, the status is `disagree` and there is no
partner. If ECMWF is missing, the status is `missing_ecmwf`. If other models
have no rain total, the status is `unverified`. Neither is a signal.
Disagreement is shown and is not a signal.

The comparison total is ECMWF's own window, not the short-range stitch.
Telegram and the board use that same total. When the card already carries
`process.rain_agreement`, the board uses that status. Otherwise it recomputes
from the model rows with the same IFS-then-AIFS rule. A stitch of 25 mm with
ECMWF at 8 mm is not a signal. A stitch of 40 mm with ECMWF 24 mm and GFS
25 mm is. Missing ECMWF is not a signal even when the stitch and GFS are heavy.

Watch and elevated rain, and unverified or disagreed heavy rain, show as
"near" on the dashboard. They do not send a Telegram alert by themselves.
Wind can still alert on the same game.

The old Low-rain trigger (`> 2 mm` and temp `< 50°F`) is retired for signals,
discovery, and alerts. `LOW_RAIN_TEMP_MAX_F` remains in config only as the
name of that retired gate. v1's rain component is a different number and
still uses the frozen tiers.

Code: `process.rain_tier`, `process.rain_agreement`. Constants
`PROCESS_RAIN_IGNORE_MM`, `PROCESS_RAIN_WATCH_MM`, `PROCESS_RAIN_SIGNAL_MM`,
`PROCESS_RAIN_AGREE_TOL_MM`.

Ensemble member sampling (`pipeline/weather/screening.py`) is not this gate.
Wind sampling uses the 15 mph flag. The existing 3 mph buffer (4 mph before
detail) is measured down from that flag, so it still reaches the 12 mph
watch. Temperature does not block sampling unless that league's optional
gate is on; when it is on, the existing temperature buffer sits above the
configured max. Rain sampling stays at a 1.5 mm game-window total, plus a
0.25 mm tail, so a light deterministic total can still pull members. A 5 mm
sampler would skip that case. Sampled rain is not an alert.

### 4. Price move

He does not want the under once it has moved more than about 3–5% from the
**consensus opener across books**. The move includes the total, not only the
juice at whatever number is posted now.

The gate compares `process.under_prob_move` with
`PROCESS_UNDER_PROB_MOVE_MAX`, default **0.04** (4 percentage points).
Override with the env var `PROCESS_UNDER_PROB_MOVE_MAX` (a number strictly
between 0 and 1). `process.price_blocked` is true when that adverse move is
past the limit. Blocked games leave the alert set and the default dashboard
candidate list (`discovery` kind `moved`). They stay visible with "show all".

The number is the sum of two adverse pieces. Each piece is floored at zero,
so they cannot cancel:

```
line_prob_move = total_shift(sport, line_now, line_open, "under")
adverse_juice = max(under_prob_price_move, 0)    # only with a two-book price consensus
adverse_line  = max(line_prob_move, 0)           # only when the total dropped
        under_prob_move = max(adverse_juice + adverse_line, worst single book)
```

`under_prob_price_move` is the signed juice move (now minus opener). A longer
under price is negative and contributes 0. `line_prob_move` restates the
current number at the opener with `fair.total_shift` and `PTS_PROB_TOTAL`:
**0.026 per NFL point**, **0.020 per CFB point**. A lower total is positive.
A higher total is negative and contributes 0: the number got better for a
new under, so the line piece alone does not block. This replaces the earlier
absolute-value rule, which blocked a rise from 47 to 49 at even money.

Worked examples, NFL:

| Opener | Now | Price | Adverse move | Blocked |
|---|---|---|---|---|
| 47 | 43 | -110/-110 both ends | 4 × 0.026 = 0.104 | yes |
| 47 | 43 | under +130 / over -150 | juice is about −0.080, so 0; line stays 0.104 | yes |
| 47 | 46 | -110/-110 | 0.026 | no |
| 47 | 49 | -110/-110 | line is −0.052, so 0 | no |

The same 4-point drop in CFB is 4 × 0.020 = 0.080 and blocks.

Any pricing book that has both the current total and the opener counts. One
book is enough. Several are averaged with `BOOK_WEIGHTS`.

The average is not enough on its own. Any pricing book that has both a
current under price and an opener under price is also scored alone, with the
same adverse rule (its juice, plus its own dropped total, neither
cancelling). If that one book is past the cap, the gate blocks even when the
other books pull the average back inside it. A small move on one book still
does not block. One book going from under -110 to -180, total unchanged, is
about 12 percentage points and blocks. Pinnacle drifting to +130 does not
cancel BetOnline at -180: the average is about zero, and BetOnline still
blocks.

`REFERENCE_BOOKS` (BetCris) is left out of this gate entirely. It does not
enter the average, the line, or the single-book check. BetCris under -110 to
-180, or a BetCris total from 47 to 43, does not block when the pricing books
are unchanged. A two-sided BetCris price does not pull the average.

If no book has an opener, `line_open_missing` is true. The line piece is not
invented and does not block, because there is no measured move. The flag is
on the card, the board says the opener line is unknown, and Telegram says
"Opener line unknown". Screen version `process-v5` is this rule.

How the price consensus is built (`process.under_prob_move`):

| Prices on hand | Basis | Gate |
|---|---|---|
| Two or more books with both over and under, at the opener and now | `novig_consensus` (multiplicative devig, book weights from `BOOK_WEIGHTS`) | yes |
| Two or more books with an under price but not a two-sided pair on both ends of the comparison | `under_only_implied` | yes, and the UI says it is an approximation |
| Only one book | `single_book` | the average ignores its juice; that book's own adverse move still blocks when it is past the cap |
| Not enough prices on one end | `unavailable` | no |

When the opener is under-only and the current price is two-sided, both ends
use the implied under probability so the subtraction is not mixing a devigged
number with a raw one. The basis label says so.

Historical files in git are not enough to rebuild this. They carry one blended
forecast and, per league, under-only prices at one book. Those rows stay as
they are. New runs store both sides (see below). A move computed from the old
files would be the labeled approximation at best, and only where two books
exist.

Code: `pipeline/model/process.py`, `pipeline/model/fair.py` `devig_pair` and `total_shift`.
The card field is filled in `pipeline/outputs/json_out.py` `build_card`.

### 5. Forecast models

ECMWF (IFS, with AIFS filling a missing field) is the signal-driving forecast
when the lead is **over 48 hours** and `PROCESS_ECMWF_FIRST` is on (default).
Inside 48 hours the signal uses the same stitch v1 uses (HRRR / NBM), where
the short-range models are the better read.

If ECMWF is missing past 48 h, the signal falls back to the stitch and
`signal_source` is `stitched_fallback`.

Disagreement is a flag, not a gate. ECMWF "stands apart" when its wind, temp,
or rain differs from the median of the other models by at least 5 mph, 5°F,
or 5 mm (`PROCESS_ECMWF_*_APART_*`) and at least one other model has that
field. The dashboard appends "ECMWF stands apart" and lists the fields. The
game stays on the list.

The drawer shows a per-model side-by-side of sustained wind, temperature, and
rain millimetres (ECMWF first, then NBM, GFS, HRRR, anything else).

Code: `process.select_signal_forecast`, `process.ecmwf_disagreement`,
`pipeline/weather/merge.py` `collect_model_windows` (called from
`build_forecast` without changing `choose_regime`).

### 6. What gets stored

Per-model windows and both sides' prices are written onto the GameCard
(`weather.models`, `weather.signal_*`, `process.books` with over and under
and the opener odds). Snapshots are the full card list at
`data/snapshots/{sport}/{season}/{week}/{run_id}.json`, which is gitignored
and published to R2 the same way as before. Nothing new is committed under
`data/`.

`wx_history.json` and D1 `weather_history` stay the blended change-only
series. Adding a model column there would rewrite the history contract and
grow the state file. The snapshot card is the per-model record from this
change forward. Older git history is not backfilled.

`history.json` / D1 `odds_history` already append one row per
`game_id|market|side|book` when the scrapers emit the side
(`pipeline/state.py` `update_history`). Both over and under are kept when the
book posts both. The limitation in the git history is the old capture, not
this writer.

### 7. Dashboard

`site/web/discovery.js` `discoveryState` uses the same cuts as the Python
screen. Sustained wind and the signal-driving temperature/rain are the
inputs. Gusts and `wind_p90` are not.

| Kind | Meaning | Default candidate list |
|---|---|---|
| `signal` | sustained wind ≥ 15 mph, or agreed heavy rain, and the optional temperature gate (if on) passes; price not blocked | shown |
| `near` | 12–15 mph wind watch (labeled not a flag), rain watch / elevated watch, or heavy rain without agreement | shown |
| `moved` | weather hit, consensus under move past the gate | hidden until "show all" |
| `quiet` | outside the screen | hidden until "show all" |
| `unknown` | forecast or CFB opener missing | shown as unknown |
| `ineligible` | dome / closed / indoor, retractable roof not confirmed open, or CFB opener outside ±10 | hidden |

Heat and altitude presets still exist on the Signals tab. They are not part
of this under screen. With the temperature gate off, a warm game with
sustained wind ≥ 15 mph is a signal. The 12–15 mph band is `near` and the
label says it is not a flag.

`site/web/signals.js` wind flags use the same 15 mph floor. High / Very High
labels above 21.5 mph are severity only and still use their temperature
bands, so a warm flag is Low rather than High.

The Signal column uses the published severity label. When that label is
rain and `process.rain_agreement` is not `agree`, the column says "Rain
models disagree" or "Rain not confirmed" instead of reading as a yes. A
wind flag on the same game still shows as wind. A retractable roof is
eligible only when `roof_state` is `open`, matching `weather_exposed`.

### 8. Exposed-game forecast log

This is a settled log, not a dashboard list and not an alert. The old
"Watch: warm + wind" list is gone. A free 2018–25 check of actual weather
on outdoor games (6,377 CFB and 1,563 NFL) found no over edge for warm and
windy games: CFB finished over the close 49.4% of the time (n=578), and NFL
leaned under.

The log is `exposed_forecasts` on `board/backtest.json`, built from
snapshots in `pipeline/backtest.py` `exposed_forecast_report`. It includes
every exposed game, including calm games, so the calm games are the
comparison group. A covered roof is left out. For each included game it
keeps:

- early forecast: the pre-kickoff snapshot closest to 72 hours, among leads
  of at least 48 hours; if there is none, the earliest pre-kickoff snapshot
- final forecast: the last snapshot before kickoff
- consensus open total, closing total, actual total, and the result versus
  the close (`over`, `under`, or `push`)

`forecast eased` is a tag on that log, not a Telegram alert and not a board
filter. It is set when an earlier pre-kickoff forecast had sustained wind of
at least 15 mph and the latest forecast does not. The recorded move is the
latest consensus total minus the consensus total on the last snapshot that
still met the flag. A positive move means the total rose after that flag.
Exactly 15 mph is still a flag, so a latest forecast of 15 mph is not eased.

### 9. Alerts

Telegram still fires from `pipeline/alerts.py` when a game qualifies and the
published tier is at least `TELEGRAM_MIN_TIER` (default Low). Qualification
is now `process.alert`, not "any legacy Low+". That means:

- the optional temperature gate, when on for that league, blocks a game at
  or above its threshold; while it is off, temperature does not block
- sustained wind under 15 mph does not alert; 12–15 mph is a watch only
- agreed heavy rain can still alert without a wind flag
- rain under 23 mm, or 23 mm without ECMWF agreement, does not alert on rain
- a consensus under-probability move past the gate does not alert, and an
  open alert closes silently the same way a forecast that leaves the screen does
- a missing price or a non-positive model edge still does not block a weather alert. A single book's juice stays out of the consensus average, but that book's own adverse move blocks when it is past the cap
- the forecast-eased tag is a settled-log mark only. It does not send Telegram

v1 impact text in the message is still the stitched-forecast impact. Past
48 h that number can disagree with the ECMWF signal forecast. Both are on
the card; the message did not grow a second impact line.

Open PR #13 retires football Telegram behind `FOOTBALL_TELEGRAM_ENABLED`
(exact `1` would turn delivery back on). This change does not take a side
in that debate and does not add the flag. If that PR merges, these
thresholds apply only when delivery is enabled. Until then, the current
sender uses the new gate.

## Context for the cuts

These are observed frequencies against the closing total. They are not a
record, a rate of return, or a reason to bet a side.

A free 2018–25 NFL check on actual weather is why the flag is 15 mph and
why temperature is not a hard gate:

- Games with sustained wind above 10 mph finished under about 59% of the time.
- Cutting those games on temperature added little to that rate.
- Forecasts earlier in the week were noisy enough that the flag uses 15 mph
  rather than 10.

An earlier 2024–25 check, on the previous colder-and-windier cut, is context
only and is not the current flag:

- Games flagged with sustained wind above 10 mph, temperature under 65°F, and
  a price still within about 5% of the opener finished under the closing total
  about 54.6% of the time.
- Flags taken 3–72 hours before kickoff finished under more often than flags
  taken 120–240 hours out.
- Splitting NFL flags by crosswind lined up with a higher under rate. The
  same split on CFB did not.
- Rain could not be tested on that sample.

## Knobs

| Name | Default | Where |
|---|---|---|
| `PROCESS_TEMP_MAX_F` | 65 | threshold when the optional gate is on; strict `<` |
| `PROCESS_TEMP_GATE` | off | env. See the temperature section |
| `PROCESS_TEMP_GATE_NFL` | unset | env override for NFL; follows the global switch when unset |
| `PROCESS_TEMP_GATE_CFB` | unset | env override for CFB; follows the global switch when unset |
| `PROCESS_WIND_WATCH_MPH` | 12 | inclusive start of the display watch; not a flag |
| `PROCESS_WIND_FLAG_MPH` | 15 | inclusive flag for both leagues |
| `PROCESS_RAIN_SIGNAL_MM` | 23 | `config.py` |
| `PROCESS_RAIN_AGREE_TOL_MM` | 10 | `config.py` |
| `PROCESS_UNDER_PROB_MOVE_MAX` | 0.04 | `config.py`, env override |
| `PROCESS_ECMWF_FIRST` | on | env `0` / `false` / `off` uses the stitch for the signal too |
| `PROCESS_ECMWF_LEAD_H` | 48 | `config.py` |
| `TELEGRAM_MIN_TIER` | `low` | existing alert floor, applied after the process gate |
