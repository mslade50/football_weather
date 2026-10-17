"""Betting-process screen (docs/PROCESS_SPEC.md).

Pure functions. v1 impact (``compute_impact_v1``) and the stitched forecast it
consumes are not inputs to this module's gates. Beyond 48 h the signal-driving
forecast prefers ECMWF; the stitched ``wind_fg`` / ``temp_fg`` / ``rain_fg``
stay the v1 inputs.

Same weather rules for NFL and CFB. CFB keeps the opening-spread ±10 gate.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional

from pipeline.model import config as C
from pipeline.model.fair import american_to_prob, devig_pair
from pipeline.stadiums.roofs import weather_exposed

ECMWF_IFS = "ecmwf_ifs025"
ECMWF_AIFS = "ecmwf_aifs025_single"
ECMWF_MODELS = (ECMWF_IFS, ECMWF_AIFS)
MODEL_LABELS = {
    ECMWF_IFS: "ECMWF IFS",
    ECMWF_AIFS: "ECMWF AIFS",
    "ncep_nbm_conus": "NBM",
    "ncep_gfs_seamless": "GFS",
    "ncep_hrrr_conus": "HRRR",
    "best_match": "Best match",
    "nws": "NWS",
}
MODEL_ORDER = (
    ECMWF_IFS, ECMWF_AIFS, "ncep_nbm_conus", "ncep_gfs_seamless",
    "ncep_hrrr_conus", "best_match", "nws",
)

BASIS_NOVIG = "novig_consensus"
BASIS_IMPLIED = "under_only_implied"
BASIS_SINGLE = "single_book"
BASIS_UNAVAILABLE = "unavailable"


def _f(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def under_prob_move_max() -> float:
    """Absolute no-vig under-probability move vs the consensus opener. Default 4%."""
    raw = os.environ.get("PROCESS_UNDER_PROB_MOVE_MAX", "").strip()
    if not raw:
        return C.PROCESS_UNDER_PROB_MOVE_MAX
    try:
        value = float(raw)
    except ValueError:
        return C.PROCESS_UNDER_PROB_MOVE_MAX
    if not 0.0 < value < 1.0:
        return C.PROCESS_UNDER_PROB_MOVE_MAX
    return value


def ecmwf_first_enabled() -> bool:
    """Signal forecast beyond ``PROCESS_ECMWF_LEAD_H`` prefers ECMWF. ``0`` turns it off."""
    raw = os.environ.get("PROCESS_ECMWF_FIRST", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def crosswind_factor(wind_dir_deg: Optional[float], orientation_deg: Optional[float]) -> Optional[float]:
    """``|sin(wind_dir − field orientation)|`` in 0..1. 1 is a pure crosswind."""
    direction, orientation = _f(wind_dir_deg), _f(orientation_deg)
    if direction is None or orientation is None:
        return None
    return abs(math.sin(math.radians(direction - orientation)))


def wind_components(
    wind_mph: Optional[float], wind_dir_deg: Optional[float], orientation_deg: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """``(cross_mph, head_mph, crosswind_factor)`` from sustained wind, not gusts."""
    wind = _f(wind_mph)
    factor = crosswind_factor(wind_dir_deg, orientation_deg)
    if wind is None or factor is None:
        return None, None, factor
    direction, orientation = _f(wind_dir_deg), _f(orientation_deg)
    assert direction is not None and orientation is not None
    along = abs(math.cos(math.radians(direction - orientation)))
    return abs(wind * factor), abs(wind * along), factor


def rain_tier(rain_mm: Optional[float]) -> str:
    """``ignore`` / ``watch`` / ``elevated`` / ``signal_amount``.

    ``signal_amount`` means the total is at the heavy-rain threshold. It becomes
    a rain signal only when ECMWF and another model agree (see ``rain_agreement``).
    15–23 mm is ``elevated``: above the named watch band, still short of ~23 mm.
    """
    rain = _f(rain_mm)
    if rain is None or rain < C.PROCESS_RAIN_IGNORE_MM:
        return "ignore"
    if rain < C.PROCESS_RAIN_WATCH_MM:
        return "watch"
    if rain < C.PROCESS_RAIN_SIGNAL_MM:
        return "elevated"
    return "signal_amount"


def wind_band(wind_mph: Optional[float]) -> str:
    """``none`` / ``consider`` / ``ideal``. Gusts are not an input."""
    wind = _f(wind_mph)
    if wind is None or wind <= C.PROCESS_WIND_CONSIDER_MPH:
        return "none"
    if wind <= C.PROCESS_WIND_IDEAL_MPH:
        return "consider"
    return "ideal"


def _window(windows: dict[str, Any], model: str) -> dict[str, Any]:
    row = windows.get(model) or {}
    return row if isinstance(row, dict) else {}


def ecmwf_fields(windows: dict[str, Any]) -> dict[str, Any]:
    """IFS when a field is present, otherwise AIFS. Mixed fields record both ids."""
    ifs, aifs = _window(windows, ECMWF_IFS), _window(windows, ECMWF_AIFS)
    out: dict[str, Any] = {}
    used: list[str] = []
    for key in ("temp_f", "wind_mph", "rain_mm", "wind_dir_deg"):
        if _f(ifs.get(key)) is not None:
            out[key] = _f(ifs.get(key))
            if ECMWF_IFS not in used:
                used.append(ECMWF_IFS)
        elif _f(aifs.get(key)) is not None:
            out[key] = _f(aifs.get(key))
            if ECMWF_AIFS not in used:
                used.append(ECMWF_AIFS)
        else:
            out[key] = None
    out["models"] = used
    return out


def select_signal_forecast(
    lead_hours: Optional[float],
    stitched: dict[str, Any],
    windows: Optional[dict[str, Any]] = None,
    *,
    ecmwf_first: Optional[bool] = None,
) -> dict[str, Any]:
    """Forecast the process screen reads.

    Lead > 48 h and ``PROCESS_ECMWF_FIRST`` (default on): ECMWF raw window
    (IFS, AIFS fills gaps). Otherwise the stitched values, which are what v1
    impact already consumed. A missing ECMWF window falls back to the stitch
    and sets ``ecmwf_missing``.
    """
    windows = windows or {}
    lead = _f(lead_hours)
    prefer = ecmwf_first_enabled() if ecmwf_first is None else ecmwf_first
    ecmwf = ecmwf_fields(windows)
    use_ecmwf = bool(
        prefer and lead is not None and lead > C.PROCESS_ECMWF_LEAD_H
        and ecmwf.get("temp_f") is not None and ecmwf.get("wind_mph") is not None
    )
    if use_ecmwf:
        return {
            "source": "ecmwf:" + "+".join(ecmwf["models"]),
            "temp_f": ecmwf["temp_f"],
            "wind_mph": ecmwf["wind_mph"],
            "rain_mm": ecmwf["rain_mm"],
            "wind_dir_deg": ecmwf["wind_dir_deg"],
            "ecmwf_missing": False,
        }
    missing = bool(prefer and lead is not None and lead > C.PROCESS_ECMWF_LEAD_H and not ecmwf["models"])
    return {
        "source": "stitched_fallback" if missing else "stitched",
        "temp_f": _f(stitched.get("temp_f")),
        "wind_mph": _f(stitched.get("wind_mph")),
        "rain_mm": _f(stitched.get("rain_mm")),
        "wind_dir_deg": _f(stitched.get("wind_dir_deg")),
        "ecmwf_missing": missing,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def ecmwf_disagreement(windows: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Flag when ECMWF stands apart from the median of the other models.

    Not a gate. ``fields`` lists ``wind`` / ``temp`` / ``rain`` that cleared
    the configured gap and had at least one other model.
    """
    ecmwf = ecmwf_fields(windows or {})
    others = [
        _window(windows or {}, model)
        for model in (windows or {})
        if model not in ECMWF_MODELS
    ]
    flagged: list[str] = []
    detail: dict[str, Optional[float]] = {}
    checks = (
        ("wind", "wind_mph", C.PROCESS_ECMWF_WIND_APART_MPH),
        ("temp", "temp_f", C.PROCESS_ECMWF_TEMP_APART_F),
        ("rain", "rain_mm", C.PROCESS_ECMWF_RAIN_APART_MM),
    )
    for name, key, tol in checks:
        mine = ecmwf.get(key)
        vals = [v for v in (_f(row.get(key)) for row in others) if v is not None]
        if mine is None or not vals:
            detail[name] = None
            continue
        delta = abs(float(mine) - _median(vals))
        detail[name] = delta
        if delta >= tol:
            flagged.append(name)
    return {"apart": bool(flagged), "fields": flagged, "delta": detail, "ecmwf_models": ecmwf["models"]}


def rain_agreement(windows: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Heavy rain counts only when ECMWF and at least one other model agree.

    Both totals must be ≥ ``PROCESS_RAIN_SIGNAL_MM`` and within
    ``PROCESS_RAIN_AGREE_TOL_MM`` of each other. The closest other model at or
    above the threshold is the partner. IFS/AIFS do not count as each other's
    confirming model.
    """
    ecmwf = ecmwf_fields(windows or {})
    ecmwf_mm = ecmwf.get("rain_mm")
    partners: list[tuple[float, str, float]] = []
    for model, row in (windows or {}).items():
        if model in ECMWF_MODELS or not isinstance(row, dict):
            continue
        other = _f(row.get("rain_mm"))
        if other is None or ecmwf_mm is None:
            continue
        partners.append((abs(other - float(ecmwf_mm)), model, other))
    partners.sort()
    if ecmwf_mm is None:
        return {"status": "missing_ecmwf", "ecmwf_mm": None, "partner": None, "partner_mm": None, "agree": False}
    if not partners:
        return {"status": "unverified", "ecmwf_mm": ecmwf_mm, "partner": None, "partner_mm": None, "agree": False}
    _gap, partner, partner_mm = partners[0]
    agree = (
        float(ecmwf_mm) >= C.PROCESS_RAIN_SIGNAL_MM
        and partner_mm >= C.PROCESS_RAIN_SIGNAL_MM
        and abs(partner_mm - float(ecmwf_mm)) <= C.PROCESS_RAIN_AGREE_TOL_MM
    )
    return {
        "status": "agree" if agree else "disagree",
        "ecmwf_mm": ecmwf_mm,
        "partner": partner,
        "partner_mm": partner_mm,
        "agree": agree,
    }


def _american(value: Any) -> Optional[int]:
    number = _f(value)
    if number is None:
        return None
    odds = int(round(number))
    if abs(odds) < 100:
        return None
    return odds


def _book_weight(book: str) -> float:
    return float(C.BOOK_WEIGHTS.get(book, C.DEFAULT_BOOK_WEIGHT))


def _side_probs(under_odds: Any, over_odds: Any) -> dict[str, Optional[float]]:
    under, over = _american(under_odds), _american(over_odds)
    novig = implied = None
    if under is not None and over is not None:
        novig = devig_pair(under, over)[0]
        implied = american_to_prob(under)
    elif under is not None:
        implied = american_to_prob(under)
    return {"novig": novig, "implied": implied}


def _weighted(pairs: list[tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 2:
        return None
    total = sum(weight for weight, _prob in pairs)
    if total <= 0:
        return None
    return sum(weight * prob for weight, prob in pairs) / total


def under_prob_move(books: list[dict[str, Any]], *, move_max: Optional[float] = None) -> dict[str, Any]:
    """Consensus no-vig under probability now minus the consensus opener.

    ``books`` rows carry ``book``, current ``under``/``over`` American odds and
    opener ``open_under``/``open_over``. Two-sided prices use multiplicative
    devig. When either side of the comparison lacks two-sided prices at two or
    more books, both sides of the comparison fall back to the raw implied
    under probability and ``basis`` is ``under_only_implied``. One book is
    reported as ``single_book`` and does not trip the gate: the process wants
    a cross-book consensus, not one book's move.
    """
    limit = under_prob_move_max() if move_max is None else move_max
    rows = []
    for book in books:
        name = str(book.get("book") or "")
        if not name or name == "consensus":
            continue
        now = _side_probs(book.get("under"), book.get("over"))
        opener = _side_probs(book.get("open_under"), book.get("open_over"))
        rows.append({
            "book": name,
            "weight": _book_weight(name),
            "line": _f(book.get("line")),
            "over": _american(book.get("over")),
            "under": _american(book.get("under")),
            "open_over": _american(book.get("open_over")),
            "open_under": _american(book.get("open_under")),
            "under_prob_now_novig": now["novig"],
            "under_prob_now_implied": now["implied"],
            "under_prob_open_novig": opener["novig"],
            "under_prob_open_implied": opener["implied"],
        })

    def consensus(key: str) -> tuple[Optional[float], int]:
        pairs = [(row["weight"], row[key]) for row in rows if row[key] is not None]
        if len(pairs) >= 2:
            return _weighted(pairs), len(pairs)
        if len(pairs) == 1:
            return pairs[0][1], 1
        return None, 0

    open_novig, n_open_novig = consensus("under_prob_open_novig")
    now_novig, n_now_novig = consensus("under_prob_now_novig")
    open_implied, n_open_implied = consensus("under_prob_open_implied")
    now_implied, n_now_implied = consensus("under_prob_now_implied")
    if n_open_novig >= 2 and n_now_novig >= 2:
        basis, open_p, now_p = BASIS_NOVIG, open_novig, now_novig
    elif n_open_implied >= 2 and n_now_implied >= 2:
        basis, open_p, now_p = BASIS_IMPLIED, open_implied, now_implied
    elif (n_open_novig == 1 or n_open_implied == 1) and (n_now_novig == 1 or n_now_implied == 1):
        basis = BASIS_SINGLE
        open_p = open_novig if open_novig is not None else open_implied
        now_p = now_novig if now_novig is not None else now_implied
    else:
        basis, open_p, now_p = BASIS_UNAVAILABLE, None, None
    move = (now_p - open_p) if open_p is not None and now_p is not None else None
    blocked = bool(basis in (BASIS_NOVIG, BASIS_IMPLIED) and move is not None and abs(move) > limit)
    return {
        "basis": basis,
        "under_prob_open": open_p,
        "under_prob_now": now_p,
        "under_prob_move": move,
        "price_gate_max": limit,
        "price_blocked": blocked,
        "books": rows,
    }


def _models_dict(models: Any) -> dict[str, Any]:
    if isinstance(models, dict):
        return {str(k): v for k, v in models.items() if isinstance(v, dict)}
    out: dict[str, Any] = {}
    if isinstance(models, list):
        for row in models:
            if isinstance(row, dict) and row.get("id"):
                out[str(row["id"])] = row
    return out


def _spread_ok(sport: str, spread_open: Any) -> bool:
    if sport != "cfb":
        return True
    spread = _f(spread_open)
    return spread is not None and abs(spread) <= C.CFB_OPEN_SPREAD_MAX


def evaluate_card(card: dict[str, Any]) -> dict[str, Any]:
    """Process block stored on a GameCard and read by alerts."""
    weather = card.get("weather") or {}
    stadium = card.get("stadium") or {}
    exposed = weather_exposed(stadium.get("roof_type"), stadium.get("roof_state") or card.get("roof_state"))
    windows = _models_dict(weather.get("models") or weather.get("model_windows"))
    if weather.get("signal_source"):
        driving = {
            "source": weather.get("signal_source"),
            "temp_f": _f(weather.get("signal_temp_f")),
            "wind_mph": _f(weather.get("signal_wind_mph")),
            "rain_mm": _f(weather.get("signal_rain_mm")),
            "wind_dir_deg": _f(weather.get("signal_wind_dir_deg")),
            "ecmwf_missing": str(weather.get("signal_source") or "").startswith("stitched_fallback"),
        }
    else:
        driving = select_signal_forecast(
            _f(weather.get("lead_hours")),
            {
                "temp_f": weather.get("temp_f") if weather.get("temp_f") is not None else weather.get("temp_fg"),
                "wind_mph": weather.get("wind_mph") if weather.get("wind_mph") is not None else weather.get("wind_fg"),
                "rain_mm": weather.get("rain_mm") if weather.get("rain_mm") is not None else weather.get("rain_fg"),
                "wind_dir_deg": weather.get("wind_dir_deg"),
            },
            windows,
        )
    temp, wind, rain = driving["temp_f"], driving["wind_mph"], driving["rain_mm"]
    temp_ok = temp is not None and temp < C.PROCESS_TEMP_MAX_F
    band = wind_band(wind) if temp_ok else "none"
    tier = rain_tier(rain) if temp_ok else "ignore"
    agreement = rain_agreement(windows)
    rain_signal = bool(temp_ok and tier == "signal_amount" and agreement["agree"])
    if temp_ok and tier == "signal_amount" and not agreement["agree"]:
        tier = "unverified" if agreement["status"] in ("unverified", "missing_ecmwf") else "disagree"
    apart = ecmwf_disagreement(windows)
    orient = stadium.get("orient_deg")
    if orient is None:
        orient = stadium.get("orientation_deg")
    cross, head, factor = wind_components(wind, driving["wind_dir_deg"], orient)
    books = []
    for name, markets in (card.get("odds") or {}).items():
        total = (markets or {}).get("total") or {}
        if not isinstance(total, dict):
            continue
        books.append({"book": name, **{k: total.get(k) for k in ("line", "over", "under", "open_over", "open_under")}})
    price = under_prob_move(books)
    spread_open = (card.get("consensus") or {}).get("spread_open")
    spread_ok = _spread_ok(str(card.get("sport") or ""), spread_open)
    wind_ok = band in ("consider", "ideal")
    weather_hit = bool(exposed and spread_ok and temp_ok and (wind_ok or rain_signal))
    watch = bool(
        exposed and spread_ok and temp_ok and not weather_hit
        and tier in ("watch", "elevated", "unverified", "disagree")
    )
    return {
        "version": C.PROCESS_VERSION,
        "forecast_source": driving["source"],
        "ecmwf_missing": bool(driving.get("ecmwf_missing")),
        "temp_f": temp,
        "wind_mph": wind,
        "rain_mm": rain,
        "wind_dir_deg": driving["wind_dir_deg"],
        "temp_ok": bool(temp_ok and exposed),
        "wind_band": band if exposed and spread_ok else "none",
        "rain_tier": tier if exposed and spread_ok else "ignore",
        "rain_agreement": agreement["status"] if rain_signal or tier in ("unverified", "disagree") else agreement["status"],
        "rain_partner": agreement.get("partner"),
        "rain_partner_mm": agreement.get("partner_mm"),
        "ecmwf_mm": agreement.get("ecmwf_mm"),
        "cross_mph": cross,
        "head_mph": head,
        "crosswind_factor": factor,
        "ecmwf_apart": bool(apart["apart"]),
        "ecmwf_apart_fields": list(apart["fields"]),
        "under_prob_open": price["under_prob_open"],
        "under_prob_now": price["under_prob_now"],
        "under_prob_move": price["under_prob_move"],
        "under_prob_basis": price["basis"],
        "price_gate_max": price["price_gate_max"],
        "price_blocked": bool(price["price_blocked"] and weather_hit),
        "candidate": weather_hit,
        "watch": watch,
        "alert": bool(weather_hit and not price["price_blocked"]),
        "books": price["books"],
    }


def model_rows(windows: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable side-by-side order: ECMWF first, then NBM / GFS / HRRR / others."""
    keys = [key for key in MODEL_ORDER if key in windows]
    keys.extend(sorted(key for key in windows if key not in MODEL_ORDER))
    rows = []
    for key in keys:
        row = windows[key] if isinstance(windows[key], dict) else {}
        rows.append({
            "id": key,
            "label": MODEL_LABELS.get(key, key),
            "temp_f": _f(row.get("temp_f")),
            "wind_mph": _f(row.get("wind_mph")),
            "rain_mm": _f(row.get("rain_mm")),
            "wind_dir_deg": _f(row.get("wind_dir_deg")),
        })
    return rows


__all__ = [
    "BASIS_IMPLIED",
    "BASIS_NOVIG",
    "BASIS_SINGLE",
    "BASIS_UNAVAILABLE",
    "ECMWF_AIFS",
    "ECMWF_IFS",
    "crosswind_factor",
    "ecmwf_disagreement",
    "ecmwf_first_enabled",
    "evaluate_card",
    "model_rows",
    "rain_agreement",
    "rain_tier",
    "select_signal_forecast",
    "under_prob_move",
    "under_prob_move_max",
    "wind_band",
    "wind_components",
]
