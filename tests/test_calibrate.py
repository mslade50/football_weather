"""pipeline/calibrate.py (PLAN Phase 6): refits the v2 coefficients into a valid
calibration.json, recovers a known curve from synthetic closings, honours the >=4-week
guard and the bounds, and NEVER touches v1 (config.py bytes, V2_DEFAULTS, golden v1)."""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

from pipeline import calibrate as CAL  # noqa: N812
from pipeline.model import config as C
from pipeline.model import impact as I  # noqa: N812

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = ROOT / "pipeline" / "model" / "config.py"
SHIPPED = ROOT / "data" / "calibration.json"

V1_SNAPSHOT = {
    "WIND_TIERS": [(25.0, 10.0), (17.0, 6.5), (15.0, 3.5), (12.0, 2.0)],
    "RAIN_TIERS_MM": [(12.0, 6.5), (6.0, 3.0), (1.0, 1.5)],
    "ALT_TIERS_M": {"nfl": [(1283.0, 3.5), (900.0, 2.0)], "cfb": [(1000.0, 3.5)]},
    "HEAT_AWAY_CUTOFF_F": {"cfb": 54.0},
    "COLD_BASE_F": 30.0, "HEAT_BASE_F": 80.0, "COLD_PER_F": 0.125,
}


def _true_cal(**over: float) -> dict[str, float]:
    cal = dict(C.V2_DEFAULTS)
    cal.update(over)
    return cal


def synth_rows(n: int = 240, weeks: int = 6, seed: int = 7, true_cal: dict[str, float] | None = None,
               noise: float = 0.15) -> list[dict]:
    """Games whose closing total is the opener adjusted by a *known* v2 curve (+ noise)."""
    rng = random.Random(seed)
    true_cal = true_cal or _true_cal()
    rows = []
    for i in range(n):
        sport = "nfl" if i % 3 == 0 else "cfb"
        wind = rng.uniform(0.0, 30.0)
        gust = wind + rng.uniform(0.0, 12.0)
        temp = rng.uniform(20.0, 95.0)
        rain = rng.choice([0.0, 0.0, 0.0, 0.5, 2.0, 8.0])
        pop = rng.uniform(0.0, 1.0) if rain else 0.0
        alt = rng.choice([0.0, 0.0, 300.0, 1200.0, 1600.0])
        home_t, away_t = rng.uniform(45.0, 80.0), rng.uniform(40.0, 80.0)
        imp = I.compute_impact_v2(sport, temp, wind, gust, rain, pop, alt, home_t, away_t,
                                  wind_dir_deg=rng.uniform(0, 360), orientation_deg=rng.choice([0.0, 45.0, 90.0]),
                                  cal=true_cal)
        total_open = rng.choice([41.5, 44.0, 47.5, 51.0, 55.5])
        spread_open = rng.choice([-3.0, -6.5, 4.0, -10.0, 7.5])
        rows.append({
            "sport": sport, "season": 2025, "week": 1 + i % weeks, "kickoff_utc": f"2025-{9 + (i % 3):02d}-14T17:00:00Z",
            "temp_fg": temp, "wind_fg": wind, "gust_fg": gust, "rain_fg": rain, "precip_prob": pop,
            "wind_dir_deg": None, "orientation_deg": None, "travel_alt": alt, "home_temp": home_t, "away_temp": away_t,
            "total_open": total_open,
            "total_close": round(total_open * (1 + imp.gs_fg_pct / 100.0) + rng.gauss(0, noise), 2),
            "spread_open": spread_open,
            "spread_close": round(spread_open * (1 + imp.away_fg_pct / 100.0) + rng.gauss(0, noise / 3), 2),
        })
    return rows


@pytest.fixture
def config_hash() -> str:
    return hashlib.sha256(CONFIG_PY.read_bytes()).hexdigest()


# ---------------------------------------------------------------- rows / loading


def test_row_from_dict_flat_and_gamecard_shapes():
    flat = CAL.row_from_dict({"sport": "NCAAF", "season": 2025, "week": 3, "wind_fg": 12, "temp_fg": 60,
                              "total_open": 50, "total_close": 48.5, "kickoff_utc": "2025-10-04T20:00:00Z"})
    assert flat and flat.sport == "cfb" and flat.month == 10 and flat.has_total and not flat.has_spread
    # pipeline.backtest GameRow spellings (wind_fc / temp_fc / gust_fc / rain_fc)
    gr = CAL.row_from_dict({"sport": "cfb", "season": 2025, "week": 2, "wind_fc": 14.0, "temp_fc": 50.0, "gust_fc": 20.0,
                            "rain_fc": 1.0, "total_open": 55.0, "total_close": 53.0})
    assert gr and gr.wind_fg == 14.0 and gr.temp_fg == 50.0 and gr.gust_fg == 20.0 and gr.rain_fg == 1.0
    card = CAL.row_from_dict({"sport": "nfl", "season": 2025, "week": 3, "weather": {"wind_fg": 18.0, "temp_fg": 40.0, "gust_fg": 25.0},
                              "stadium": {"orient_deg": 90.0, "weakest_wind_effect": "x N", "roof_state": "outdoors"},
                              "consensus": {"total_open": 44.0, "spread_open": -3.0},
                              "closing": {"total": 42.5, "spread": -2.5}, "travel_alt": 10})
    assert card and card.orientation_deg == 90.0 and card.weakest_wind_effect == "x N" and card.has_spread
    assert CAL.row_from_dict({"sport": "mlb", "wind_fg": 1}) is None


def test_v1_scores_with_run_month_not_game_month():
    """v1 rain suppression keys on the RUN month (impact.compute_impact_v1): an October game
    forecast by a September run scores rain=0. Sources: explicit column, weather.fetched_at,
    the backtest's src_forecast snapshot stamp, kickoff - lead; else the game month."""
    base = {"sport": "cfb", "season": 2025, "week": 6, "kickoff_utc": "2025-10-04T20:00:00Z", "wind_fg": 5.0,
            "temp_fg": 60.0, "rain_fg": 8.0, "total_open": 50.0, "total_close": 48.0}
    game_only = CAL.row_from_dict(base)
    assert game_only.month == 10 and game_only.run_month is None
    assert CAL._v1_pcts(game_only)[0] == pytest.approx(-3.0)          # fallback: game month -> rain tier 3.0
    snap = CAL.row_from_dict({**base, "src_forecast": "snapshot:2025-09-28T12:17:00Z"})
    assert snap.run_month == 9 and CAL._v1_pcts(snap)[0] == 0.0       # September run -> rain suppressed
    assert CAL.row_from_dict({**base, "lead_fc": 96.0}).run_month == 9     # Oct 4 kickoff - 4 d = Sep 30
    assert CAL.row_from_dict({**base, "lead_fc": 6.0}).run_month == 10
    assert CAL.row_from_dict({**base, "weather": {"fetched_at": "2025-09-29T09:17:00Z"}}).run_month == 9
    assert CAL.row_from_dict({**base, "run_month": 9, "src_forecast": "snapshot:2025-10-03T12:00:00Z"}).run_month == 9
    assert CAL.row_from_dict({**base, "commit_date": "2025-09-30"}).run_month == 9
    assert CAL.row_from_dict({**base, "src_forecast": "d1:games"}).run_month is None


def test_load_rows_json_and_csv(tmp_path: Path):
    rows = synth_rows(12, weeks=2)
    (tmp_path / "bt.json").write_text(json.dumps({"games": rows}), encoding="utf-8")
    import pandas as pd

    pd.DataFrame(rows).to_csv(tmp_path / "bt.csv", index=False)
    a = CAL.load_rows([tmp_path / "bt.json"])
    b = CAL.load_rows([tmp_path / "bt.csv"])
    assert len(a) == len(b) == 12
    assert CAL.weeks_of(a) == [(2025, 1), (2025, 2)]
    assert CAL.load_rows([tmp_path / "missing.json"]) == []


# ---------------------------------------------------------------- fit


def test_fit_recovers_known_curve_and_lowers_loss():
    true = _true_cal(wind_coeff=0.85, wind_offset_mph=8.0, gust_blend=0.5)
    rows = CAL.usable([CAL.row_from_dict(d) for d in synth_rows(300, weeks=6, true_cal=true, noise=0.05)])
    res = CAL.fit(rows, dict(C.V2_DEFAULTS), ridge=0.0)
    assert res.loss_after < res.loss_before
    lt_before, _ = CAL.loss_v2(rows, C.V2_DEFAULTS)
    lt_after, _ = CAL.loss_v2(rows, res.cal)
    assert lt_after < lt_before * 0.5
    # every refit key stays inside its bounds; carried keys are untouched
    for k, (lo, hi) in CAL.BOUNDS.items():
        assert lo <= res.cal[k] <= hi
    for k in ("head_weight", "dir_mult_weak", "alt_base_m", "alt_cap"):
        assert res.cal[k] == C.V2_DEFAULTS[k]
    assert "wind_coeff" in res.changed or "wind_offset_mph" in res.changed or "gust_blend" in res.changed


def test_fit_is_deterministic():
    rows = CAL.usable([CAL.row_from_dict(d) for d in synth_rows(60, weeks=4)])
    a = CAL.fit(rows, dict(C.V2_DEFAULTS))
    b = CAL.fit(rows, dict(C.V2_DEFAULTS))
    assert a.cal == b.cal and a.loss_after == b.loss_after


def test_fit_without_spreads_skips_spread_keys():
    rows = [CAL.row_from_dict({**d, "spread_open": None, "spread_close": None}) for d in synth_rows(40, weeks=4)]
    rows = CAL.usable(rows)
    assert rows and not any(r.has_spread for r in rows)
    res = CAL.fit(rows, dict(C.V2_DEFAULTS))
    for k in CAL.SPREAD_KEYS:
        assert res.cal[k] == C.V2_DEFAULTS[k]


# ---------------------------------------------------------------- output schema


def test_build_and_write_valid_calibration(tmp_path: Path):
    rows = CAL.usable([CAL.row_from_dict(d) for d in synth_rows(80, weeks=5)])
    res = CAL.fit(rows, dict(C.V2_DEFAULTS), rounds=5)
    data = CAL.build_calibration(res, rows, inputs=[Path("x.parquet")], ridge=0.1,
                                 backtest={"clv": {"weeks": 5, "by_model": {"v1": {"n": 30, "avg_clv": 0.4}, "v2": {"n": 30, "avg_clv": 0.6}}}})
    assert CAL.validate_calibration(data) == []
    assert data["schema_version"] == 1 and set(data["v2"]) == set(C.V2_DEFAULTS)
    assert data["fit"]["n_weeks"] == 5 and data["fit"]["refit_keys"] == list(CAL.REFIT_KEYS)
    assert {"baseline", "v1", "v2_before", "v2_after"} <= set(data["fit"]["mse"])
    assert data["promotion"]["eligible"] is True and "ALERT_MODEL" in data["promotion"]["rule"]
    assert data["promotion"]["alert_model_now"] == "v1"
    out = CAL.write_calibration(tmp_path / "calibration.json", data)
    loaded = I.load_v2_calibration(out, use_cache=False)      # what impact.py will actually read
    assert loaded == {k: float(v) for k, v in data["v2"].items()}
    assert "NaN" not in out.read_text(encoding="utf-8")


def test_write_refuses_invalid_schema(tmp_path: Path):
    bad = {"schema_version": 1, "v2": {**C.V2_DEFAULTS, "wind_cap": 99.0}}
    assert CAL.validate_calibration(bad) == ["v2.wind_cap out of bounds"]
    with pytest.raises(ValueError):
        CAL.write_calibration(tmp_path / "c.json", bad)
    assert not (tmp_path / "c.json").exists()
    missing = {"schema_version": 1, "v2": {k: v for k, v in C.V2_DEFAULTS.items() if k != "wind_exp"}}
    assert any("v2 keys" in e for e in CAL.validate_calibration(missing))


def test_shipped_calibration_is_valid():
    assert CAL.validate_calibration(json.loads(SHIPPED.read_text(encoding="utf-8"))) == []


def test_promotion_verdict_requires_four_weeks_and_v2_ge_v1():
    bt = {"clv": {"weeks": 3, "by_model": {"v1": {"avg_clv": 0.1}, "v2": {"avg_clv": 0.5}}}}
    assert CAL.promotion_verdict(bt)["eligible"] is False
    bt["clv"]["weeks"] = 4
    assert CAL.promotion_verdict(bt)["eligible"] is True
    bt["clv"]["by_model"]["v2"]["avg_clv"] = 0.05
    assert CAL.promotion_verdict(bt)["eligible"] is False
    assert CAL.promotion_verdict(None)["eligible"] is False


def test_clv_block_reads_pipeline_backtest_alerts_clv_shape():
    """pipeline.backtest.alerts_clv: list-shaped by_model rows + weeks derived from the settled alerts."""
    bt = {"meta": {"run_id": "backtest-x"},
          "alerts_clv": {"n": 6, "by_model": [{"key": "v2", "n": 3, "avg_clv": 0.7, "pos": 2, "pos_frac": 0.667},
                                              {"key": "v1", "n": 3, "avg_clv": 0.4, "pos": 2, "pos_frac": 0.667}],
                         "alerts": [{"season": 2025, "week": w, "clv_pts": 0.5} for w in (1, 2, 3, 4)] + [{"season": 2025, "week": 4}]}}
    blk = CAL.clv_block(bt)
    assert blk["weeks"] == 4 and blk["n"] == 6
    assert blk["by_model"]["v2"] == {"n": 3, "avg_clv": 0.7, "pos_frac": 0.667}
    v = CAL.promotion_verdict(bt)
    assert v["eligible"] is True and v["v1_clv_avg"] == 0.4 and v["v2_n"] == 3
    assert CAL.clv_block({"alerts_clv": {"by_model": []}}) == {"n": 0, "weeks": 0, "by_model": {}}


# ---------------------------------------------------------------- CLI + v1 frozen


def test_cli_refuses_under_four_weeks_and_writes_nothing(tmp_path: Path, config_hash: str, capsys):
    (tmp_path / "bt.json").write_text(json.dumps({"games": synth_rows(30, weeks=3)}), encoding="utf-8")
    out = tmp_path / "calibration.json"
    rc = CAL.main(["--input", str(tmp_path / "bt.json"), "--out", str(out), "--target", "close"])
    assert rc == 2 and not out.exists()
    assert "refusing to refit" in capsys.readouterr().out
    assert hashlib.sha256(CONFIG_PY.read_bytes()).hexdigest() == config_hash


def test_cli_writes_calibration_and_never_touches_v1(tmp_path: Path, config_hash: str, capsys):
    (tmp_path / "bt.json").write_text(json.dumps({"games": synth_rows(120, weeks=5)}), encoding="utf-8")
    (tmp_path / "backtest.json").write_text(json.dumps({"clv": {"weeks": 5, "by_model": {"v1": {"avg_clv": 0.3}, "v2": {"avg_clv": 0.2}}}}),
                                            encoding="utf-8")
    out = tmp_path / "calibration.json"
    rc = CAL.main(["--input", str(tmp_path / "bt.json"), "--out", str(out), "--backtest", str(tmp_path / "backtest.json"),
                   "--rounds", "8", "--target", "close"])
    assert rc == 0 and out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert CAL.validate_calibration(data) == []
    assert data["promotion"]["eligible"] is False and data["promotion"]["v1_clv_avg"] == 0.3
    printed = capsys.readouterr().out
    assert "promotion:" in printed and "wrote" in printed
    # v1 frozen: config.py bytes, the v1 constants and the v1 numbers are all unchanged
    assert hashlib.sha256(CONFIG_PY.read_bytes()).hexdigest() == config_hash
    for name, val in V1_SNAPSHOT.items():
        assert getattr(C, name) == val, name
    v1 = I.compute_impact_v1("nfl", 10, 60.0, 16.0, 0.0, None, None)
    assert v1.wind_c == 3.5 and v1.gs_fg_pct == -3.5
    # the shipped file and the module defaults are untouched by a run against --out elsewhere
    assert json.loads(SHIPPED.read_text(encoding="utf-8"))["v2"] == {k: float(v) for k, v in C.V2_DEFAULTS.items()}


def test_dry_run_writes_nothing(tmp_path: Path):
    (tmp_path / "bt.json").write_text(json.dumps({"games": synth_rows(60, weeks=4)}), encoding="utf-8")
    out = tmp_path / "calibration.json"
    assert CAL.main(["--input", str(tmp_path / "bt.json"), "--out", str(out), "--dry-run", "--rounds", "3",
                     "--target", "close"]) == 0
    assert not out.exists()


def test_module_never_writes_python_sources():
    """The only writer in calibrate.py is write_calibration (a JSON path); config.py is read
    for its fingerprint and the promotion note, never opened for writing."""
    src = (ROOT / "pipeline" / "calibrate.py").read_text(encoding="utf-8")
    assert src.count("write_text") == 1 and "def write_calibration" in src
    assert "open(" not in src.replace("json.loads", "")
    assert "ALERT_MODEL" not in src.split('"""', 2)[2].replace("PROMOTION_RULE", "").split("PROMOTION_RULE")[0] \
        or "alert_model()" in src   # read-only accessor only
    assert sys.version_info >= (3, 10)


# ---------------------------------------------------------------- --target result (final score)


def synth_result_rows(n: int, scale: dict[str, float], noise: float = 0.5, seed: int = 11) -> list[dict]:
    """Games whose FINAL total is the opener moved by ``scale[sport]`` x the unscaled v2 shade (+ noise)."""
    rng = random.Random(seed)
    unscaled = _true_cal(gs_scale_nfl=1.0, gs_scale_cfb=1.0)
    rows = []
    for i in range(n):
        sport = "nfl" if i % 2 == 0 else "cfb"
        wind, temp = rng.uniform(0.0, 30.0), rng.uniform(25.0, 85.0)
        imp = I.compute_impact_v2(sport, temp, wind, None, 0.0, None, None, None, None, cal=unscaled)
        line = rng.choice([41.5, 44.0, 47.5, 51.0, 55.5])
        rows.append({"sport": sport, "season": 2026, "week": 1 + i % 6, "kickoff_utc": "2026-10-10T17:00:00Z",
                     "temp_fg": temp, "wind_fg": wind, "total_open": line + 3.0, "total_at_forecast": line,
                     "actual_total": line * (1 + scale[sport] * imp.gs_fg_pct / 100.0) + rng.gauss(0, noise)})
    return rows


def test_v2_cfb_shade_is_scaled_and_nfl_is_not():
    raw = _true_cal(gs_scale_nfl=1.0, gs_scale_cfb=1.0)
    for sport, k in (("nfl", 1.0), ("cfb", 0.4)):
        full = I.compute_impact_v2(sport, 40.0, 22.0, None, 0.0, None, None, None, None, cal=raw)
        shipped = I.compute_impact_v2(sport, 40.0, 22.0, None, 0.0, None, None, None, None, cal=dict(C.V2_DEFAULTS))
        assert full.gs_fg_pct < 0
        assert shipped.gs_fg_pct == pytest.approx(k * full.gs_fg_pct)
        assert shipped.wind_c == full.wind_c          # components are not scaled
    # v1 is untouched by the v2 scale
    assert I.compute_impact_v1("cfb", 10, 40.0, 22.0, 0.0, None, None).gs_fg_pct == -6.5 - 0.0


def test_fit_scale_recovers_strong_signal_and_keeps_prior_when_thin():
    rows = [CAL.row_from_dict(d) for d in synth_result_rows(600, {"nfl": 0.7, "cfb": 0.9}, noise=0.2)]
    fits = {f.sport: f for f in (CAL.fit_scale(rows, sp, dict(C.V2_DEFAULTS)) for sp in C.SPORTS)}
    assert fits["nfl"].slope == pytest.approx(0.7, abs=0.02) and fits["nfl"].posterior == pytest.approx(0.7, abs=0.03)
    assert fits["cfb"].slope == pytest.approx(0.9, abs=0.02) and fits["cfb"].posterior == pytest.approx(0.9, abs=0.03)
    thin = [CAL.row_from_dict(d) for d in synth_result_rows(20, {"nfl": 0.0, "cfb": 0.0})]
    for sp, (prior, _) in CAL.RESULT_PRIOR.items():
        f = CAL.fit_scale(thin, sp, dict(C.V2_DEFAULTS))
        assert f.slope is None and f.posterior == prior


def test_noisy_data_barely_moves_the_prior():
    rows = [CAL.row_from_dict(d) for d in synth_result_rows(200, {"nfl": 0.0, "cfb": 0.0}, noise=14.0)]
    f = CAL.fit_scale(rows, "cfb", dict(C.V2_DEFAULTS))
    assert f.se is not None and f.se > 0.5
    assert abs(f.posterior - CAL.RESULT_PRIOR["cfb"][0]) < abs(f.slope - CAL.RESULT_PRIOR["cfb"][0])


def test_result_update_starts_from_the_base_prior_not_the_last_file():
    """Re-running on the same season must not double-count: the start file's scale is ignored."""
    rows = [CAL.row_from_dict(d) for d in synth_result_rows(300, {"nfl": 1.2, "cfb": 0.1}, noise=3.0)]
    a, _ = CAL.fit_result(rows, dict(C.V2_DEFAULTS))
    b, _ = CAL.fit_result(rows, {**C.V2_DEFAULTS, "gs_scale_nfl": 0.3, "gs_scale_cfb": 1.4})
    assert a.cal["gs_scale_nfl"] == b.cal["gs_scale_nfl"] and a.cal["gs_scale_cfb"] == b.cal["gs_scale_cfb"]
    for k in CAL.REFIT_KEYS:                      # the wind curve is carried through untouched
        assert a.cal[k] == C.V2_DEFAULTS[k]


def test_cli_default_target_is_result_and_writes_valid_scales(tmp_path: Path, config_hash: str, capsys):
    (tmp_path / "bt.json").write_text(json.dumps({"games": synth_result_rows(240, {"nfl": 0.9, "cfb": 0.5}, noise=2.0)}),
                                      encoding="utf-8")
    out = tmp_path / "calibration.json"
    assert CAL.main(["--input", str(tmp_path / "bt.json"), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert CAL.validate_calibration(data) == [] and data["fit"]["target"] == "result"
    assert {s["sport"] for s in data["fit"]["scales"]} == {"nfl", "cfb"}
    assert "gs_scale_cfb" in capsys.readouterr().out
    assert hashlib.sha256(CONFIG_PY.read_bytes()).hexdigest() == config_hash


def test_validate_rejects_out_of_bounds_scale():
    bad = {"schema_version": 1, "v2": {**C.V2_DEFAULTS, "gs_scale_cfb": 3.0}}
    assert "v2.gs_scale_cfb out of bounds" in CAL.validate_calibration(bad)


def test_result_target_measures_from_the_contemporaneous_line_not_the_opener():
    """Market already moved from the opener to the weather-adjusted number: relative to the line the
    board actually prices from, nothing is left, so the scale must go to ~0 (no double counting)."""
    cal = dict(C.V2_DEFAULTS)
    unscaled = {**cal, "gs_scale_nfl": 1.0}
    rows = []
    for i in range(120):
        d = {"sport": "nfl", "temp_fg": 60.0, "wind_fg": 12.0 + i / 6, "total_open": 50.0}
        moved = 50.0 * (1 + CAL._v2(CAL.row_from_dict(d), unscaled).gs_fg_pct / 100.0)
        rows.append(CAL.row_from_dict({**d, "total_close": moved, "actual_total": moved + (-1) ** i * 0.01}))
    f = CAL.fit_scale(rows, "nfl", cal)
    assert f.slope == pytest.approx(0.0, abs=0.01) and f.posterior == pytest.approx(0.0, abs=0.05)
    # the opener alone is not a usable baseline for the result target
    only_open = CAL.row_from_dict({"sport": "nfl", "temp_fg": 60.0, "wind_fg": 20.0, "total_open": 50.0, "actual_total": 44.0})
    assert CAL.usable([only_open], "result") == []


def test_perfect_zero_slope_fit_overrides_prior():
    rows = [CAL.row_from_dict({"sport": "cfb", "temp_fg": 60.0, "wind_fg": 11 + i / 3, "total_at_forecast": 50.0,
                               "actual_total": 50.0}) for i in range(60)]
    f = CAL.fit_scale(rows, "cfb", dict(C.V2_DEFAULTS))
    assert f.slope == pytest.approx(0.0) and f.se == 0.0 and f.posterior == 0.0


def test_constant_shade_keeps_prior():
    rows = [CAL.row_from_dict({"sport": "cfb", "temp_fg": 60.0, "wind_fg": 20.0, "total_at_forecast": 50.0,
                               "actual_total": 50.0}) for _ in range(60)]
    f = CAL.fit_scale(rows, "cfb", dict(C.V2_DEFAULTS))
    assert f.slope is None and f.posterior == CAL.RESULT_PRIOR["cfb"][0]
