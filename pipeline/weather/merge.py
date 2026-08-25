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
  * 18 h < lead <= 48 h: temp/wind/PoP/precip NBM, gusts GFS (unchanged legacy band)
  * 48 h < lead <= 7 d: NBM first; anything NBM lacks (gusts, nulls) from the
    medium-range blend below instead of a single model
  * lead > 7 d (``forecast_blend.medium_range_start_h``): medium range = weighted mean
    of {AIFS, IFS, GFS} (``forecast_blend.medium_range_weights``, default
    aifs 0.4 / ifs 0.35 / gfs 0.25; members missing a field are simply left out of
    that field's mean, so AIFS having no gusts never zeroes gust_fg);
    Degradation(info, low_confidence); ``source`` = ``medium:aifs+ifs+gfs`` listing
    the members that actually covered the window.
Each field falls through the preference list to the first non-null model; NWS
fills anything still null when lead <= 7 d. No Open-Meteo at all -> NWS-only +
Degradation(warn). AIFS is a plain member of ``model_disagreement``.

Climatology shrinkage (``weather/climatology_blend.py``): after the window means
are formed, ``wind_fg`` / ``gust_fg`` / ``temp_fg`` / ``precip_prob`` and the
ensemble P10/P50/P90 band are pulled toward the stadium × ISO-week × time-of-day
ERA5 cell by the lead-weighted curves in ``data/calibration.json``
``forecast_blend.weights`` (w = 1 up to 48 h). The BLENDED values are what
``wind_fg`` etc. carry (impact / signals consume them); the raw window means ride
along as ``wind_fg_raw`` / ``temp_fg_raw`` with ``blend_w`` (wind weight) and the
cell's ``climo_wind`` / ``climo_temp``. Rain AMOUNT (``rain_fg_mm``) is never
blended. No cell for the stadium -> blend_w = 1 and a Degradation(info).

Legacy window: mean of the 3 hourly samples at kickoff hour, +1h, +2h
(matching old wind_fg arithmetic); rain_fg = sum of mm over the same 3 hours.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from pipeline.contracts import Degradation, WeatherForecast, WeatherPoint
from pipeline.weather import climatology_blend as CB
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.ensemble import EnsembleLocation
from pipeline.weather.parsers.openmeteo import ParsedLocation

HRRR = "ncep_hrrr_conus"
NBM = "ncep_nbm_conus"
GFS = "ncep_gfs_seamless"
ECMWF = "ecmwf_ifs025"
AIFS = "ecmwf_aifs025_single"
BEST = "best_match"
NWS = "nws"
MEDIUM = "medium"  # pseudo-model: weighted mean of the medium-range members below

# calibration alias -> Open-Meteo model id (forecast_blend.medium_range_weights keys)
MEDIUM_MEMBERS: dict[str, str] = {"aifs": AIFS, "ifs": ECMWF, "gfs": GFS}

SHORT_LEAD_H = 18.0
HRRR_SYNOPTIC_LEAD_H = 48.0
MEDIUM_FALLBACK_LEAD_H = 48.0
MID_LEAD_H = CB.DEFAULT_MEDIUM_START_H
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


def vector_mean_deg(dirs: Sequence[Optional[float]], weights: Optional[Sequence[float]] = None) -> Optional[float]:
    """Mean direction of unit vectors (deg from north, clockwise); optionally weighted."""
    pairs = [(d, 1.0 if weights is None else weights[i]) for i, d in enumerate(dirs) if d is not None]
    if not pairs:
        return None
    sx = sum(w * math.sin(math.radians(d)) for d, w in pairs)
    cx = sum(w * math.cos(math.radians(d)) for d, w in pairs)
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
    weights: dict[str, float] = field(default_factory=dict)  # model id -> weight for MEDIUM


def _index(rows: Sequence[HourlyRow]) -> dict[datetime, HourlyRow]:
    return {r.t: r for r in rows}


def _hrrr_covers(om: Optional[ParsedLocation], hours: Sequence[datetime]) -> bool:
    if om is None or HRRR not in om.models:
        return False
    idx = _index(om.models[HRRR])
    return all(h in idx and idx[h].wind is not None for h in hours)


def _default_blend_cfg() -> CB.BlendConfig:
    """``forecast_blend`` block of data/calibration.json (cached); tests stub this."""
    return CB.load_blend_config()


def medium_weights(cfg: Optional[CB.BlendConfig] = None) -> dict[str, float]:
    """``forecast_blend.medium_range_weights`` mapped onto Open-Meteo model ids."""
    cfg = cfg or _default_blend_cfg()
    out = {MEDIUM_MEMBERS[a]: float(w) for a, w in cfg.medium_weights.items() if a in MEDIUM_MEMBERS and w > 0.0}
    return out or {MEDIUM_MEMBERS[a]: w for a, w in CB.DEFAULT_MEDIUM_WEIGHTS.items()}


def choose_regime(lead_hours: float, hrrr_covers: bool, cfg: Optional[CB.BlendConfig] = None) -> Regime:
    cfg = cfg or _default_blend_cfg()
    weights = medium_weights(cfg)
    if lead_hours <= SHORT_LEAD_H or (lead_hours <= HRRR_SYNOPTIC_LEAD_H and hrrr_covers):
        main = [HRRR, NBM, GFS, BEST, ECMWF, AIFS]
        return Regime(
            label="hrrr",
            prefs={"temp": main, "wind": main, "gust": main, "dir": main, "precip": main, "pop": [NBM, HRRR, GFS, BEST, ECMWF]},
            weights=weights,
        )
    if lead_hours <= MEDIUM_FALLBACK_LEAD_H:
        main = [NBM, GFS, BEST, ECMWF, AIFS, HRRR]
        return Regime(
            label="nbm",
            prefs={"temp": main, "wind": main, "dir": main, "precip": main, "pop": main, "gust": [GFS, BEST, ECMWF, NBM, HRRR]},
            weights=weights,
        )
    if lead_hours <= cfg.medium_start_h:
        main = [NBM, MEDIUM, BEST, HRRR]
        return Regime(
            label="nbm",
            prefs={"temp": main, "wind": main, "dir": main, "precip": main, "pop": main, "gust": [MEDIUM, NBM, BEST, HRRR]},
            weights=weights,
        )
    main = [MEDIUM, NBM, BEST, HRRR]
    return Regime(label=MEDIUM, prefs={f: main for f in FIELDS}, average=True, weights=weights)


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


def medium_value(om: Optional[ParsedLocation], t: datetime, name: str, weights: Mapping[str, float]) -> Optional[float]:
    """Weighted mean of the medium-range members that carry ``name`` at ``t``; members
    with a null (AIFS gusts / PoP) drop out of THIS field's mean only."""
    pairs = [(w, _model_value(om, m, t, name)) for m, w in weights.items() if w > 0.0]
    pairs = [(w, v) for w, v in pairs if v is not None]
    if not pairs:
        return None
    if name == "dir":
        return vector_mean_deg([v for _, v in pairs], [w for w, _ in pairs])
    tot = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / tot


def _pick(om: Optional[ParsedLocation], regime: Regime, t: datetime, name: str) -> Optional[float]:
    for m in regime.prefs[name]:
        v = medium_value(om, t, name, regime.weights) if m == MEDIUM else _model_value(om, m, t, name)
        if v is not None:
            return v
    return None


def medium_members_present(om: Optional[ParsedLocation], hours: Sequence[datetime], weights: Mapping[str, float]) -> list[str]:
    """Calibration aliases (aifs/ifs/gfs, weight order) whose wind covers every hour."""
    if om is None:
        return []
    by_id = {mid: alias for alias, mid in MEDIUM_MEMBERS.items()}
    out = []
    for mid in weights:
        if all(_model_value(om, mid, h, "wind") is not None for h in hours):
            out.append(by_id.get(mid, mid))
    return out


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


def _default_climo() -> Optional[CB.ClimoTable]:
    """data/climatology.csv cells (cached); tests stub this to keep merges data-free."""
    return CB.default_table()


@dataclass
class MergeResult:
    forecast: WeatherForecast
    degradations: list[Degradation] = field(default_factory=list)
    regime: str = ""
    precip_prob_ens: Optional[float] = None
    ensemble: Optional[EnsembleStats] = None
    roof_heuristic: bool = False
    climo_cell: Optional[CB.ClimoCell] = None
    blend_w: float = 1.0


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
    stadium_id: Optional[str] = None,
    climo: Optional[CB.ClimoTable] = None,
    auto_climo: bool = True,
    blend_cfg: Optional[CB.BlendConfig] = None,
) -> MergeResult:
    """``climo`` (or, when None and ``auto_climo``, the data/climatology.csv table) supplies
    the shrinkage base rate; the cell is found by ``stadium_id`` or else by the nearest
    stadium to the Open-Meteo / ensemble coordinates. ``blend_cfg`` defaults to the
    ``forecast_blend`` block of data/calibration.json."""
    degradations: list[Degradation] = []
    kickoff_utc = kickoff_utc.astimezone(timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    lead_hours = (kickoff_utc - now_utc).total_seconds() / 3600.0
    h0 = hour_floor(kickoff_utc)
    window = [h0 + timedelta(hours=i) for i in range(WINDOW_HOURS)]
    display = [h0 + timedelta(hours=i) for i in range(-DISPLAY_BEFORE_H, DISPLAY_AFTER_H + 1)]
    cfg = blend_cfg or _default_blend_cfg()

    om_usable = om is not None and any(om.models.values())
    regime = choose_regime(lead_hours, _hrrr_covers(om, window), cfg)
    nws_idx = _index(nws_rows or [])
    allow_nws = lead_hours <= NWS_HORIZON_H and bool(nws_idx)

    def _deg(reason: str, severity: str = "warn") -> None:
        degradations.append(Degradation(component="weather", reason=f"{game_id}: {reason}", severity=severity, run_id=run_id, ts=now_utc))

    source = regime.label
    if not om_usable:
        om = None
        source = NWS
        if allow_nws:
            _deg("open-meteo unavailable; NWS-only forecast", "warn")
        else:
            _deg("no weather source available", "error")
    elif regime.average:
        members = medium_members_present(om, window, regime.weights)
        source = f"{MEDIUM}:{'+'.join(members)}" if members else MEDIUM
        _deg(f"lead {lead_hours:.0f}h > {cfg.medium_start_h:.0f}h; low_confidence medium-range blend ({source})", "info")

    merged = {t: merge_hour(t, om, regime, nws_idx, allow_nws) for t in display}
    win = [merged[t] for t in window]

    wind_raw = mean3([r.wind for r in win])
    temp_raw = mean3([r.temp for r in win])
    gust_raw = mean3([r.gust for r in win])
    precips = [r.precip for r in win if r.precip is not None]
    rain_fg = sum(precips) if precips else None
    pop_mean = mean3([r.pop for r in win])
    pop_raw = pop_mean / 100.0 if pop_mean is not None else None
    wind_dir_deg = vector_mean_deg([r.dir for r in win])

    if any(r.wind is None for r in win) and om is not None:
        _deg("missing wind sample(s) inside kickoff window", "warn")

    if om is not None and allow_nws and any(
        _pick(om, regime, t, "wind") is None and t in nws_idx for t in window
    ):
        source = f"{source}+nws"

    stats = ensemble_stats(ens, window, display)
    if stats is None and expect_ensemble:
        _deg("ensemble missing; wind_vol falls back to static", "info")

    # ---- climatology shrinkage (lead-weighted) --------------------------------------
    table = climo if climo is not None else (_default_climo() if auto_climo else None)
    lat, lon = _coords(om, ens)
    cell = table.lookup(h0 + timedelta(hours=1), stadium_id=stadium_id, lat=lat, lon=lon) if table is not None else None
    w_wind = cfg.weight(lead_hours, "wind")
    blend_active = min(cfg.weight(lead_hours, k) for k in CB.KINDS) < 1.0
    if cell is None:
        w_wind = 1.0
        if table is not None and blend_active and wind_raw is not None:
            _deg("no climatology cell for this stadium/week; forecast used unblended (blend_w=1)", "info")
        wind_fg, gust_fg, temp_fg, precip_prob = wind_raw, gust_raw, temp_raw, pop_raw
        p10, p50, p90 = (stats.wind_p10, stats.wind_p50, stats.wind_p90) if stats else (None, None, None)
    else:
        wind_fg = CB.blend(wind_raw, cell.wind_mean, lead_hours, "wind", cfg)
        gust_fg = CB.blend(gust_raw, cell.gust_mean, lead_hours, "gust", cfg)
        temp_fg = CB.blend(temp_raw, cell.temp_mean, lead_hours, "temp", cfg)
        precip_prob = CB.blend(pop_raw, cell.rain_freq, lead_hours, "rain_prob", cfg)
        p10 = CB.blend(stats.wind_p10, cell.wind_p10, lead_hours, "wind", cfg) if stats else None
        p50 = CB.blend(stats.wind_p50, cell.wind_p50, lead_hours, "wind", cfg) if stats else None
        p90 = CB.blend(stats.wind_p90, cell.wind_p90, lead_hours, "wind", cfg) if stats else None
    wind_vol_fc = (p90 - p10) if (p10 is not None and p90 is not None) else None

    given_roof = roof_state
    roof_state = roof_state_for(roof_state, roof_type, temp_fg, precip_prob, wind_fg)
    roof_heuristic = given_roof is None and roof_type == "retractable" and roof_state is not None

    closed = roof_state in ("dome", "closed")
    if closed:
        cross, head = 0.0, 0.0
    else:
        cross, head = wind_components(wind_fg, wind_dir_deg, orientation_deg)

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
        wind_vol_fc=wind_vol_fc,
        wind_p10=p10,
        wind_p50=p50,
        wind_p90=p90,
        cross_mph=cross,
        head_mph=head,
        model_disagreement=model_disagreement(om, window),
        precip_prob_ens=stats.precip_prob_ens if stats else None,
        roof_state=roof_state,
        hourly=hourly,
        wind_fg_raw=wind_raw,
        temp_fg_raw=temp_raw,
        blend_w=w_wind,
        climo_wind=cell.wind_mean if cell else None,
        climo_temp=cell.temp_mean if cell else None,
    )
    return MergeResult(
        forecast=forecast,
        degradations=degradations,
        regime=regime.label,
        precip_prob_ens=stats.precip_prob_ens if stats else None,
        ensemble=stats,
        roof_heuristic=roof_heuristic,
        climo_cell=cell,
        blend_w=w_wind,
    )


def _coords(om: Optional[ParsedLocation], ens: Optional[EnsembleLocation]) -> tuple[Optional[float], Optional[float]]:
    for loc in (om, ens):
        lat, lon = getattr(loc, "latitude", None), getattr(loc, "longitude", None)
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat, lon) != (0.0, 0.0):
            return float(lat), float(lon)
    return None, None


__all__ = [
    "HRRR",
    "NBM",
    "GFS",
    "ECMWF",
    "AIFS",
    "BEST",
    "NWS",
    "MEDIUM",
    "MEDIUM_MEMBERS",
    "compass16",
    "vector_mean_deg",
    "mean3",
    "hour_floor",
    "wind_components",
    "medium_weights",
    "choose_regime",
    "medium_value",
    "medium_members_present",
    "merge_hour",
    "model_disagreement",
    "percentile",
    "EnsembleStats",
    "ensemble_stats",
    "roof_state_for",
    "MergeResult",
    "build_forecast",
]
