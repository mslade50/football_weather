"""Golden reproduction of legacy gs_fg / away_fg (v1).

Requires tests/fixtures/golden_v1.parquet (built by scripts/extract_golden.py).
Skips when absent. Asserts >=97% exact match (1e-6 on the percent scale) and
prints mismatch buckets grouped by documented ambiguity bands.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pytest

from pipeline.model import config as C
from pipeline.model.impact import (
    alt_component,
    ambiguous_buckets,
    cold_away_component,
    cold_component,
    compute_impact_v1,
    heat_away_component,
    heat_component,
    legacy_scale,
    rain_component,
    tier,
    wind_component,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_v1.parquet"
TOL = 1e-6
# CFB legacy xlsx stores gs_fg/away_fg rounded to 2dp and temp_fg rounded to
# 2dp while the model ran on the unrounded temperature (0.125 * 0.005 slack),
# so a CFB row counts as matching within 0.005 + 0.000625 + eps.
CFB_ROUND_TOL = 0.0057
MIN_MATCH = 0.97


# ---- unit tests on documented tier boundaries (AUDIT §5) ---------------------

@pytest.mark.parametrize(
    "wind,expected",
    [(11.99, 0.0), (12.0, 2.0), (14.99, 2.0), (15.0, 3.5), (16.96, 3.5), (17.0, 6.5), (17.46, 6.5), (23.9, 6.5), (25.0, 10.0), (25.2, 10.0)],
)
def test_wind_tiers(wind: float, expected: float) -> None:
    assert wind_component(wind) == expected


def test_cold_heat_linear() -> None:
    assert cold_component(30.0) == 0.0
    assert cold_component(22.0) == pytest.approx(1.0)
    assert heat_component(80.0) == 0.0
    assert heat_component(92.0) == pytest.approx(1.5)
    assert heat_component(70.0) == 0.0


@pytest.mark.parametrize(
    "mm,month,expected",
    [(0.5, 10, 0.0), (1.0, 10, 1.5), (5.1, 10, 1.5), (6.6, 10, 3.0), (22.2, 11, 6.5), (22.2, 9, 0.0), (None, 10, 0.0), (float("nan"), 10, 0.0)],
)
def test_rain_tiers_and_september_suppression(mm, month, expected) -> None:
    assert rain_component(mm, month) == expected


def test_away_components() -> None:
    assert heat_away_component(85.0, 60.0, "nfl") == pytest.approx(0.625)
    assert heat_away_component(85.0, 66.0, "nfl") == 0.0
    assert heat_away_component(85.0, 53.0, "cfb") == pytest.approx(0.625)
    assert heat_away_component(85.0, 55.0, "cfb") == 0.0
    assert cold_away_component(24.0, 70.0) == pytest.approx(1.0)
    assert cold_away_component(24.0, 60.0) == 0.0
    assert cold_away_component(33.0, 70.0) == 0.0
    assert alt_component(1300, "nfl") == 3.5
    assert alt_component(900, "nfl") == 2.0
    assert alt_component(899, "nfl") == 0.0
    assert alt_component(1000, "cfb") == 3.5
    assert alt_component(930, "cfb") == 0.0


def test_alt_overrides_heat_not_sum() -> None:
    # tennessee @ denver style row: heat_away present but alt dominates via max().
    r = compute_impact_v1("nfl", 10, 86.0, 5.0, 0.0, 1600.0, 60.0)
    assert r.heat_away == pytest.approx(0.75)
    assert r.alt_c == 3.5
    assert r.away_fg_pct == -3.5


def test_gs_fg_full_and_legacy_scale() -> None:
    r = compute_impact_v1("cfb", 11, 22.0, 18.0, 7.0, 0.0, 70.0)
    assert r.gs_fg_pct == pytest.approx(-(6.5 + 1.0 + 0.0 + 3.0))
    assert r.away_fg_pct == pytest.approx(-1.25)
    assert r.gs_fg_legacy == r.gs_fg_pct
    n = compute_impact_v1("nfl", 11, 60.0, 15.5, 0.0, 0.0, 70.0)
    assert n.gs_fg_pct == -3.5
    assert n.gs_fg_legacy == pytest.approx(-0.035)
    assert legacy_scale(-3.5, "nfl") == pytest.approx(-0.035)


def test_roof_closed_zeroes_site_components_only() -> None:
    r = compute_impact_v1("nfl", 11, 10.0, 30.0, 25.0, 1400.0, 80.0, roof_state="dome")
    assert r.gs_fg_pct == 0.0
    assert r.roof_closed is True
    assert r.away_fg_pct == -3.5


def test_none_inputs_zero() -> None:
    r = compute_impact_v1("cfb", None, None, None, None, None, None)
    assert r.gs_fg_pct == 0.0 and r.away_fg_pct == 0.0
    assert tier(None, C.WIND_TIERS) == 0.0


def test_unknown_sport_rejected() -> None:
    with pytest.raises(ValueError):
        compute_impact_v1("xfl", 10, 60.0, 0.0, 0.0, 0.0, 60.0)


# ---- golden ------------------------------------------------------------------

def _col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    raise KeyError(names)


def _val(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _fmt(x, scale: float) -> str:
    return "None" if x is None else f"{x * scale:.4f}"


def test_golden_v1_reproduction() -> None:
    if not GOLDEN.exists():
        pytest.skip(f"golden fixture missing: {GOLDEN}")
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    df = pd.read_parquet(GOLDEN)
    assert len(df) > 0

    sport_s = _col(df, "sport")
    month_s = _col(df, "month")
    temp_s = _col(df, "temp_fg")
    wind_s = _col(df, "wind_fg")
    rain_s = _col(df, "rain_fg", "rain_fg_mm")
    alt_s = _col(df, "travel_alt", "travel_alt_m")
    away_t_s = _col(df, "away_temp")
    gs_s = _col(df, "gs_fg")
    away_s = _col(df, "away_fg")
    home_t_s = df["home_temp"] if "home_temp" in df.columns else None

    total = 0
    exact = 0
    gs_only = away_only = both = 0
    rounded = 0
    buckets: Counter = Counter()
    examples: list = []

    for i in range(len(df)):
        sport = str(sport_s.iloc[i]).lower()
        if sport not in ("nfl", "cfb"):
            continue
        exp_gs = _val(gs_s.iloc[i])
        exp_away = _val(away_s.iloc[i])
        if exp_gs is None and exp_away is None:
            continue
        month = _val(month_s.iloc[i])
        res = compute_impact_v1(
            sport,
            int(month) if month is not None else None,
            _val(temp_s.iloc[i]),
            _val(wind_s.iloc[i]),
            _val(rain_s.iloc[i]),
            _val(alt_s.iloc[i]),
            _val(away_t_s.iloc[i]),
            home_temp=_val(home_t_s.iloc[i]) if home_t_s is not None else None,
        )
        scale = 100.0 if sport == "nfl" else 1.0
        tol = CFB_ROUND_TOL if sport == "cfb" else TOL
        gs_diff = 0.0 if exp_gs is None else abs(exp_gs * scale - res.gs_fg_pct)
        away_diff = 0.0 if exp_away is None else abs(exp_away * scale - res.away_fg_pct)
        gs_ok = gs_diff <= tol
        away_ok = away_diff <= tol
        total += 1
        if gs_ok and away_ok:
            exact += 1
            if max(gs_diff, away_diff) > TOL:
                rounded += 1
            continue
        if not gs_ok and not away_ok:
            both += 1
        elif not gs_ok:
            gs_only += 1
        else:
            away_only += 1
        bands = ambiguous_buckets(_val(temp_s.iloc[i]), _val(rain_s.iloc[i]), _val(alt_s.iloc[i]), _val(away_t_s.iloc[i]), sport, wind_fg=_val(wind_s.iloc[i]))
        key = f"{sport}:" + ("+".join(bands) if bands else "unexplained")
        buckets[key] += 1
        if len(examples) < 25:
            examples.append(
                f"row={i} {sport} m={month} temp={_val(temp_s.iloc[i])} wind={_val(wind_s.iloc[i])} "
                f"rain={_val(rain_s.iloc[i])} alt={_val(alt_s.iloc[i])} away_t={_val(away_t_s.iloc[i])} "
                f"exp_gs={_fmt(exp_gs, scale)} got_gs={res.gs_fg_pct:.4f} "
                f"exp_away={_fmt(exp_away, scale)} got_away={res.away_fg_pct:.4f} "
                f"comps={res.components()} bands={bands}"
            )

    assert total > 0, "golden fixture has no usable rows"
    rate = exact / total
    print(f"\n[golden_v1] rows={total} exact={exact} rate={rate:.4f} gs_only={gs_only} away_only={away_only} both={both}")
    for k, v in buckets.most_common():
        print(f"[golden_v1] mismatch bucket {k}: {v}")
    for line in examples:
        print(f"[golden_v1] {line}")
    assert rate >= MIN_MATCH, f"v1 exact-match rate {rate:.4f} < {MIN_MATCH}"
