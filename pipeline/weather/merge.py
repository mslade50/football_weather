"""Per-game window aggregation + model stitching -> WeatherForecast (ARCH §6).

Deterministic Open-Meteo models + NWS fallback, plus (Phase 5) the ensemble
members: ``wind_vol_fc = P90-P10`` of pooled member wind over kickoff..+3h,
``wind_p10/p50/p90``, per-hour p10/p90 on the display strip and
``precip_prob_ens`` (fraction of members with >0.1 mm in the window, carried on
``WeatherForecast.precip_prob_ens`` and mirrored on :class:`MergeResult` for the
build's ``wx_extras``). With no ensemble every one of those stays None and a
Degradation(info) says so — downstream falls back to the static wind_vol.

Stitching by lead time (hours from `now` to kickoff):
  * lead <= 18 h (<= 48 h when HRRR covers the window): temp/wind/gust/precip HRRR, PoP NBM
  * 18 h < lead <= 264 h: temp/wind/PoP/precip NBM, gusts GFS
  * lead > 264 h: mean of GFS + ECMWF, Degradation(info, low_confidence)
Each field falls through the preference list to the first non-null model; NWS
fills anything still null when lead <= 7 d. No Open-Meteo at all -> NWS-only +
Degradation(warn).

Legacy window: mean of the 3 hourly samples at kickoff hour, +1h, +2h
(matching old wind_fg arithmetic); rain_fg = sum of mm over the same 3 hours.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from pipeline.contracts import Degradation, WeatherForecast, WeatherPoint
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import EnsembleLocation
from pipeline.weather.parsers.openmeteo import ParsedLocation

HRRR = "ncep_hrrr_conus"
NBM = "ncep_nbm_conus"
GFS = "ncep_gfs_seamless"
ECMWF = "ecmwf_ifs025"
BEST = "best_match"
NWS = "nws"

SHORT_LEAD_H = 18.0
HRRR_SYNOPTIC_LEAD_H = 48.0
MID_LEAD_H = 11 * 24.0
NWS_HORIZON_H = 7 * 24.0
WINDOW_HOURS = 3
DISPLAY_BEFORE_H = 1
DISPLAY_AFTER_H = 4

FIELDS = ("temp", "wind", "gust", "dir", "precip", "pop")

ENS_PRECIP_THRESHOLD_MM = 0.1
MIN_ENSEMBLE_MEMBERS = 10

# Retractable-roof heuristic (ARCH §6): closed if any of these hold, else open.
ROOF_CLOSE_TEMP_F = 40.0
ROOF_CLOSE_POP = 0.6
ROOF_CLOSE_WIND_MPH = 20.0

COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass16(deg: Optional[float]) -> Optional[str]:
    if deg is None or (isinstance(deg, float) and math.isnan(deg)):
        return None
    idx = int(((deg % 360.0) + 11.25) // 22.5) % 16
    return COMPASS_16[idx]


def vector_mean_deg(dirs: Sequence[Optional[float]]) -> Optional[float]:
    """Mean direction of unit vectors (deg from north, clockwise)."""
    xs = [d for d in dirs if d is not None]
    if not xs:
        return None
    sx = sum(math.sin(math.radians(d)) for d in xs)
    cx = sum(math.cos(math.radians(d)) for d in xs)
    if abs(sx) < 1e-12 and abs(cx) < 1e-12:
        return None
    deg = math.degrees(math.atan2(sx, cx)) % 360.0
    return 0.0 if deg >= 360.0 - 1e-9 else deg


def mean3(vals: Sequence[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def hour_floor(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def wind_components(wind: Optional[float], wind_dir_deg: Optional[float], orientation_deg: Optional[float]):
    if wind is None or wind_dir_deg is None or orientation_deg is None:
        return None, None
    theta = math.radians(wind_dir_deg - orientation_deg)
    return abs(wind * math.sin(theta)), abs(wind * math.cos(theta))


@dataclass
class Regime:
    label: str
    prefs: dict[str, list[str]]
    average: bool = False


def _index(rows: Sequence[HourlyRow]) -> dict[datetime, HourlyRow]:
    return {r.t: r for r in rows}


def _hrrr_covers(om: Optional[ParsedLocation], hours: Sequence[datetime]) -> bool:
    if om is None or HRRR not in om.models:
        return False
    idx = _index(om.models[HRRR])
    return all(h in idx and idx[h].wind is not None for h in hours)


def choose_regime(lead_hours: float, hrrr_covers: bool) -> Regime:
    if lead_hours <= SHORT_LEAD_H or (lead_hours <= HRRR_SYNOPTIC_LEAD_H and hrrr_covers):
        main = [HRRR, NBM, GFS, BEST, ECMWF]
        return Regime(
            label="hrrr",
            prefs={"temp": main, "wind": main, "gust": main, "dir": main, "precip": main, "pop": [NBM, HRRR, GFS, BEST, ECMWF]},
        )
    if lead_hours <= MID_LEAD_H:
        main = [NBM, GFS, BEST, ECMWF, HRRR]
        return Regime(
            label="nbm",
            prefs={"temp": main, "wind": main, "dir": main, "precip": main, "pop": main, "gust": [GFS, BEST, ECMWF, NBM, HRRR]},
        )
    main = [GFS, ECMWF, BEST, NBM]
    return Regime(label="gfs_ecmwf", prefs={f: main for f in FIELDS}, average=True)


def _model_value(om: Optional[ParsedLocation], model: str, t: datetime, name: str) -> Optional[float]:
    if om is None:
        return None
    rows = om.models.get(model)
    if not rows:
        return None
    for r in rows:  # window is tiny; linear scan is fine
        if r.t == t:
            return getattr(r, name)
    return None


def _pick(om: Optional[ParsedLocation], regime: Regime, t: datetime, name: str) -> Optional[float]:
    prefs = regime.prefs[name]
    if regime.average:
        top = [v for v in (_model_value(om, m, t, name) for m in prefs[:2]) if v is not None]
        if top:
            return vector_mean_deg(top) if name == "dir" else sum(top) / len(top)
        prefs = prefs[2:]
    for m in prefs:
        v = _model_value(om, m, t, name)
        if v is not None:
            return v
    return None


def merge_hour(
    t: datetime,
    om: Optional[ParsedLocation],
    regime: Regime,
    nws_idx: dict[datetime, HourlyRow],
    allow_nws: bool,
) -> HourlyRow:
    vals: dict[str, Optional[float]] = {name: _pick(om, regime, t, name) for name in FIELDS}
    if allow_nws and t in nws_idx:
        n = nws_idx[t]
        for name in FIELDS:
            if vals[name] is None:
                vals[name] = getattr(n, name)
    return HourlyRow(t=t, **vals)


def model_disagreement(om: Optional[ParsedLocation], hours: Sequence[datetime]) -> Optional[float]:
    if om is None:
        return None
    means: list[float] = []
    for _model, rows in om.models.items():
        idx = _index(rows)
        winds = [idx[h].wind for h in hours if h in idx and idx[h].wind is not None]
        if len(winds) == len(hours):
            means.append(sum(winds) / len(winds))
    if len(means) < 2:
        return None
    return max(means) - min(means)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in 0..1) of a non-empty sequence; None when empty."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass
class EnsembleStats:
    wind_p10: Optional[float] = None
    wind_p50: Optional[float] = None
    wind_p90: Optional[float] = None
    wind_vol_fc: Optional[float] = None
    gust_p90: Optional[float] = None
    precip_prob_ens: Optional[float] = None
    hourly_p10: dict[datetime, float] = field(default_factory=dict)
    hourly_p90: dict[datetime, float] = field(default_factory=dict)
    n_members: int = 0
    models: list[str] = field(default_factory=list)


def ensemble_stats(
    ens: Optional[EnsembleLocation], window: Sequence[datetime], display: Sequence[datetime] = ()
) -> Optional[EnsembleStats]:
    """Pool every member's window-mean wind (all models together) -> P10/P50/P90 and
    wind_vol_fc = P90-P10. precip_prob_ens = share of members with >0.1 mm summed over
    the window. Per-hour p10/p90 across members feed the hourly strip band."""
    if ens is None or not ens.members or not ens.times:
        return None
    tidx = {t: i for i, t in enumerate(ens.times)}
    if not all(t in tidx for t in window):
        return None
    win_i = [tidx[t] for t in window]
    means: list[float] = []
    gusts: list[float] = []
    wet = 0
    n_precip = 0
    for m in ens.members.values():
        w = [m.wind[i] for i in win_i if i < len(m.wind) and m.wind[i] is not None]
        if len(w) == len(win_i):
            means.append(sum(w) / len(w))
        g = [m.gust[i] for i in win_i if i < len(m.gust) and m.gust[i] is not None]
        if g:
            gusts.append(sum(g) / len(g))
        p = [m.precip[i] for i in win_i if i < len(m.precip) and m.precip[i] is not None]
        if p:
            n_precip += 1
            if sum(p) > ENS_PRECIP_THRESHOLD_MM:
                wet += 1
    if len(means) < MIN_ENSEMBLE_MEMBERS:
        return None
    st = EnsembleStats(
        wind_p10=percentile(means, 0.10),
        wind_p50=percentile(means, 0.50),
        wind_p90=percentile(means, 0.90),
        gust_p90=percentile(gusts, 0.90) if gusts else None,
        precip_prob_ens=(wet / n_precip) if n_precip else None,
        n_members=len(means),
        models=ens.models,
    )
    st.wind_vol_fc = st.wind_p90 - st.wind_p10  # type: ignore[operator]
    for t in display:
        i = tidx.get(t)
        if i is None:
            continue
        col = [m.wind[i] for m in ens.members.values() if i < len(m.wind) and m.wind[i] is not None]
        if len(col) >= MIN_ENSEMBLE_MEMBERS:
            st.hourly_p10[t] = percentile(col, 0.10)  # type: ignore[assignment]
            st.hourly_p90[t] = percentile(col, 0.90)  # type: ignore[assignment]
    return st


def roof_state_for(
    roof_state: Optional[str],
    roof_type: Optional[str],
    temp_fg: Optional[float],
    precip_prob: Optional[float],
    wind_fg: Optional[float],
) -> Optional[str]:
    """Schedule-provided roof_state (nflverse) wins; else derive from the stadium's
    roof_type, with the retractable heuristic (closed if cold / wet / windy)."""
    if roof_state:
        return roof_state
    if roof_type == "dome":
        return "dome"
    if roof_type == "open":
        return "outdoors"
    if roof_type == "retractable":
        if (
            (temp_fg is not None and temp_fg < ROOF_CLOSE_TEMP_F)
            or (precip_prob is not None and precip_prob > ROOF_CLOSE_POP)
            or (wind_fg is not None and wind_fg > ROOF_CLOSE_WIND_MPH)
        ):
            return "closed"
        return "open"
    return None


@dataclass
class MergeResult:
    forecast: WeatherForecast
    degradations: list[Degradation] = field(default_factory=list)
    regime: str = ""
    precip_prob_ens: Optional[float] = None
    ensemble: Optional[EnsembleStats] = None
    roof_heuristic: bool = False


def build_forecast(
    game_id: str,
    kickoff_utc: datetime,
    now_utc: datetime,
    om: Optional[ParsedLocation],
    nws_rows: Optional[Sequence[HourlyRow]] = None,
    orientation_deg: Optional[float] = None,
    roof_state: Optional[str] = None,
    run_id: Optional[str] = None,
    ens: Optional[EnsembleLocation] = None,
    roof_type: Optional[str] = None,
    expect_ensemble: bool = False,
) -> MergeResult:
    degradations: list[Degradation] = []
    kickoff_utc = kickoff_utc.astimezone(timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    lead_hours = (kickoff_utc - now_utc).total_seconds() / 3600.0
    h0 = hour_floor(kickoff_utc)
    window = [h0 + timedelta(hours=i) for i in range(WINDOW_HOURS)]
    display = [h0 + timedelta(hours=i) for i in range(-DISPLAY_BEFORE_H, DISPLAY_AFTER_H + 1)]

    om_usable = om is not None and any(om.models.values())
    regime = choose_regime(lead_hours, _hrrr_covers(om, window))
    nws_idx = _index(nws_rows or [])
    allow_nws = lead_hours <= NWS_HORIZON_H and bool(nws_idx)

    def _deg(reason: str, severity: str = "warn") -> None:
        degradations.append(Degradation(component="weather", reason=f"{game_id}: {reason}", severity=severity, run_id=run_id, ts=now_utc))

    if not om_usable:
        om = None
        if allow_nws:
            _deg("open-meteo unavailable; NWS-only forecast", "warn")
        else:
            _deg("no weather source available", "error")
    elif regime.average:
        _deg(f"lead {lead_hours:.0f}h > {MID_LEAD_H:.0f}h; low_confidence gfs/ecmwf blend", "info")

    merged = {t: merge_hour(t, om, regime, nws_idx, allow_nws) for t in display}
    win = [merged[t] for t in window]

    wind_fg = mean3([r.wind for r in win])
    temp_fg = mean3([r.temp for r in win])
    gust_fg = mean3([r.gust for r in win])
    precips = [r.precip for r in win if r.precip is not None]
    rain_fg = sum(precips) if precips else None
    pop_mean = mean3([r.pop for r in win])
    precip_prob = pop_mean / 100.0 if pop_mean is not None else None
    wind_dir_deg = vector_mean_deg([r.dir for r in win])

    if any(r.wind is None for r in win) and om is not None:
        _deg("missing wind sample(s) inside kickoff window", "warn")

    source = regime.label if om is not None else NWS
    if om is not None and allow_nws and any(
        _pick(om, regime, t, "wind") is None and t in nws_idx for t in window
    ):
        source = f"{regime.label}+nws"

    given_roof = roof_state
    roof_state = roof_state_for(roof_state, roof_type, temp_fg, precip_prob, wind_fg)
    roof_heuristic = given_roof is None and roof_type == "retractable" and roof_state is not None

    closed = roof_state in ("dome", "closed")
    if closed:
        cross, head = 0.0, 0.0
    else:
        cross, head = wind_components(wind_fg, wind_dir_deg, orientation_deg)

    stats = ensemble_stats(ens, window, display)
    if stats is None and expect_ensemble:
        _deg("ensemble missing; wind_vol falls back to static", "info")

    hourly = [
        WeatherPoint(
            t=r.t, temp=r.temp, wind=r.wind, gust=r.gust, dir=r.dir, precip=r.precip, pop=r.pop,
            p10=stats.hourly_p10.get(r.t) if stats else None,
            p90=stats.hourly_p90.get(r.t) if stats else None,
        )
        for r in (merged[t] for t in display)
    ]

    forecast = WeatherForecast(
        game_id=game_id,
        source=source,
        run_time=now_utc,
        lead_hours=lead_hours,
        temp_fg=temp_fg,
        wind_fg=wind_fg,
        gust_fg=gust_fg,
        wind_dir_1h=compass16(win[1].dir),
        wind_dir_2h=compass16(win[2].dir),
        wind_dir_fg=compass16(wind_dir_deg),
        wind_dir_deg=wind_dir_deg,
        rain_fg_mm=rain_fg,
        precip_prob=precip_prob,
        wind_vol_fc=stats.wind_vol_fc if stats else None,
        wind_p10=stats.wind_p10 if stats else None,
        wind_p50=stats.wind_p50 if stats else None,
        wind_p90=stats.wind_p90 if stats else None,
        cross_mph=cross,
        head_mph=head,
        model_disagreement=model_disagreement(om, window),
        precip_prob_ens=stats.precip_prob_ens if stats else None,
        roof_state=roof_state,
        hourly=hourly,
    )
    return MergeResult(
        forecast=forecast,
        degradations=degradations,
        regime=regime.label,
        precip_prob_ens=stats.precip_prob_ens if stats else None,
        ensemble=stats,
        roof_heuristic=roof_heuristic,
    )


__all__ = [
    "HRRR",
    "NBM",
    "GFS",
    "ECMWF",
    "BEST",
    "NWS",
    "compass16",
    "vector_mean_deg",
    "mean3",
    "hour_floor",
    "wind_components",
    "choose_regime",
    "merge_hour",
    "model_disagreement",
    "percentile",
    "EnsembleStats",
    "ensemble_stats",
    "roof_state_for",
    "MergeResult",
    "build_forecast",
]
