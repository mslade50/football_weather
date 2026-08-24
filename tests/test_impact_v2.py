"""v2 impact model (ARCH §7.5): continuous wind curve calibrated to the v1 tier
midpoints (+/-0.5), weak-direction parsing, probabilistic rain, continuous altitude,
heat-away delta, roof handling, calibration loading, fair v2 + confidence, and the
GameCard / D1 side-by-side blocks. v1 is never touched."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import Game, GameLine, Stadium, WeatherForecast, WeatherPoint
from pipeline.model import config as C
from pipeline.model import fair as F
from pipeline.model import impact as I  # noqa: N812
from pipeline.outputs import d1_out, json_out

UTC = timezone.utc
CAL = I.load_v2_calibration()


def _v2(sport="nfl", **kw):
    base = dict(temp_fg=60.0, wind_fg=0.0, gust_fg=0.0, rain_fg_mm=0.0, precip_prob=0.0,
                travel_alt_m=0.0, home_temp=60.0, away_temp=60.0)
    base.update(kw)
    return I.compute_impact_v2(sport, **base)


# ---------------------------------------------------------------- calibration file


def test_calibration_json_loads_and_matches_defaults():
    p = Path(__file__).resolve().parent.parent / "data" / "calibration.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data["v2"]) == set(C.V2_DEFAULTS)
    cal = I.load_v2_calibration(p, use_cache=False)
    assert cal == {k: float(v) for k, v in data["v2"].items()}
    # missing / broken file -> defaults, unknown keys ignored, non-numeric ignored
    assert I.load_v2_calibration(p.with_name("nope.json"), use_cache=False) == C.V2_DEFAULTS


def test_calibration_override(tmp_path: Path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"v2": {"wind_cap": 5.0, "bogus": 1, "wind_coeff": "x"}}), encoding="utf-8")
    cal = I.load_v2_calibration(p, use_cache=False)
    assert cal["wind_cap"] == 5.0 and cal["wind_coeff"] == C.V2_DEFAULTS["wind_coeff"] and "bogus" not in cal
    imp = _v2(wind_fg=40.0, gust_fg=40.0, wind_dir_deg=90.0, orientation_deg=0.0, cal=cal)
    assert imp.wind_c == 5.0


def test_v1_constants_untouched_by_v2():
    assert C.WIND_TIERS == [(25.0, 10.0), (17.0, 6.5), (15.0, 3.5), (12.0, 2.0)]
    assert C.ALT_TIERS_M == {"nfl": [(1300.0, 3.5), (900.0, 2.0)], "cfb": [(1000.0, 3.5)]}
    assert C.RAIN_SUPPRESS_MONTHS == {9}
    v1 = I.compute_impact_v1("nfl", 10, 60.0, 16.0, 0.0, None, None)
    assert v1.wind_c == 3.5 and v1.model_version == "v1"


# ---------------------------------------------------------------- wind curve


@pytest.mark.parametrize("mid,expected", [(13.5, 2.0), (16.0, 3.5), (21.0, 6.5), (27.5, 10.0)])
def test_wind_curve_matches_v1_tier_midpoints(mid: float, expected: float):
    """Pure crosswind with gust == wind so w_dir == wind_fg: v1 tier value +/- 0.5."""
    imp = _v2(wind_fg=mid, gust_fg=mid, wind_dir_deg=90.0, orientation_deg=0.0)
    assert imp.w_eff == pytest.approx(mid) and imp.w_dir == pytest.approx(mid)
    assert abs(imp.wind_c - expected) <= 0.5
    assert I.wind_curve(mid) == pytest.approx(imp.wind_c)


def test_wind_curve_continuous_monotone_capped_and_zero_below_offset():
    assert I.wind_curve(None) == 0.0 and I.wind_curve(9.9) == 0.0 and I.wind_curve(CAL["wind_offset_mph"]) == 0.0
    prev = 0.0
    for w in range(10, 60):
        cur = I.wind_curve(float(w))
        assert cur >= prev
        prev = cur
    assert I.wind_curve(80.0) == CAL["wind_cap"]
    # no tier jumps: consecutive 0.1 mph steps move the component by < 0.1
    steps = [I.wind_curve(10 + i / 10) for i in range(300)]
    assert max(b - a for a, b in zip(steps, steps[1:], strict=False)) < 0.1


def test_gust_blend_and_orientation():
    assert I.effective_wind(10.0, 20.0) == pytest.approx(0.7 * 10 + 0.3 * 20)
    assert I.effective_wind(10.0, None) == 10.0 and I.effective_wind(10.0, 5.0) == 10.0  # gust never below wind
    assert I.effective_wind(None, 20.0) is None
    # head wind counts half (in variance): w_dir = sqrt(0.5) * w
    assert I.directional_wind(20.0, 0.0, 0.0) == pytest.approx(20.0 * 0.5 ** 0.5)
    assert I.directional_wind(20.0, 90.0, 0.0) == pytest.approx(20.0)
    assert I.directional_wind(20.0, 180.0, 90.0) == pytest.approx(20.0)
    assert I.directional_wind(20.0, None, 0.0) == 20.0 and I.directional_wind(20.0, 45.0, None) == 20.0
    head = _v2(wind_fg=20.0, gust_fg=20.0, wind_dir_deg=0.0, orientation_deg=0.0)
    cross = _v2(wind_fg=20.0, gust_fg=20.0, wind_dir_deg=90.0, orientation_deg=0.0)
    assert 0 < head.wind_c < cross.wind_c


# ---------------------------------------------------------------- direction multiplier


def test_parse_weak_set():
    assert I.parse_weak_set("x N") == set(I.COMPASS_8) - {"N"}
    assert I.parse_weak_set("E/W") == {"E", "W"}
    assert I.parse_weak_set("all") == set() and I.parse_weak_set(None) == set() and I.parse_weak_set("") == set()
    assert I.parse_weak_set("N, S") == {"N", "S"}
    assert I.parse_weak_set("x NE/SW") == set(I.COMPASS_8) - {"NE", "SW"}
    assert I.parse_weak_set("nonsense") == set()


def test_compass_helpers():
    assert I.to_compass8(0.0) == "N" and I.to_compass8(359.0) == "N" and I.to_compass8(44.9) == "NE"
    assert I.to_compass8(None) is None
    assert I.compass16_to_8("NNE") == "NE" and I.compass16_to_8("ENE") == "E" and I.compass16_to_8("WSW") == "W"
    assert I.compass16_to_8("NNW") == "N" and I.compass16_to_8("SSE") == "S"
    assert I.compass16_to_8("sw") == "SW" and I.compass16_to_8("??") is None


def test_dir_multiplier_applies_to_wind_component():
    w = CAL["dir_mult_weak"]
    assert I.dir_multiplier("E", "E/W") == w and I.dir_multiplier("N", "E/W") == 1.0
    assert I.dir_multiplier("N", "x N") == 1.0 and I.dir_multiplier("S", "x N") == w
    assert I.dir_multiplier("SSE", "x N") == w and I.dir_multiplier("NNW", "x N") == 1.0
    assert I.dir_multiplier(None, "x N", wind_dir_deg=180.0) == w and I.dir_multiplier(None, "x N") == 1.0
    assert I.dir_multiplier("E", "all") == 1.0 and I.dir_multiplier("E", None) == 1.0
    full = _v2(wind_fg=20.0, gust_fg=20.0, wind_dir_deg=90.0, wind_dir_fg="E", orientation_deg=0.0, weakest_wind_effect="all")
    weak = _v2(wind_fg=20.0, gust_fg=20.0, wind_dir_deg=90.0, wind_dir_fg="E", orientation_deg=0.0, weakest_wind_effect="E/W")
    assert full.dir_mult == 1.0 and weak.dir_mult == w
    assert weak.wind_c == pytest.approx(full.wind_c * w)


# ---------------------------------------------------------------- rain / alt / heat-away / cold


def test_rain_probabilistic_no_september_suppression():
    # 10 mm at 50% -> expected 5 mm -> tier 1.5 (>=1); at 30% -> below prob floor -> 0
    assert I.rain_component_v2(10.0, 0.5) == (1.5, 5.0)
    assert I.rain_component_v2(10.0, 0.3) == (0.0, 3.0)
    # ensemble fraction beats NBM PoP; neither -> deterministic
    assert I.rain_component_v2(10.0, 0.3, precip_prob_ens=0.9)[0] == 3.0   # 9 mm -> tier 3.0
    assert I.rain_component_v2(10.0, None) == (3.0, 10.0)  # deterministic: full 10 mm -> tier 3.0
    assert I.rain_component_v2(0.0, None) == (0.0, 0.0) and I.rain_component_v2(None, None) == (0.0, 0.0)
    assert I.rain_component_v2(30.0, 1.0)[0] == 6.5
    imp = _v2(rain_fg_mm=10.0, precip_prob=0.5)
    assert imp.rain_c == 1.5 and imp.expected_mm == 5.0 and imp.gs_fg_pct == -1.5
    sep_v1 = I.compute_impact_v1("cfb", 9, 60.0, 0.0, 10.0, None, None)
    assert sep_v1.rain_c == 0.0  # v1 suppresses September; v2 does not (no month input)
    assert _v2(rain_fg_mm=10.0, precip_prob=0.5, precip_prob_ens=0.8).ensemble is True
    assert _v2(rain_fg_mm=10.0, precip_prob=0.5).ensemble is False


def test_altitude_continuous():
    assert I.alt_component_v2(None) == 0.0 and I.alt_component_v2(800.0) == 0.0 and I.alt_component_v2(-100.0) == 0.0
    assert I.alt_component_v2(1000.0) == pytest.approx(0.7)
    assert I.alt_component_v2(1800.0) == 3.5 and I.alt_component_v2(5000.0) == 3.5
    imp = _v2(travel_alt_m=1600.0)
    assert imp.alt_c == pytest.approx(2.8) and imp.away_fg_pct == pytest.approx(-2.8)


def test_heat_away_delta_and_cold_unchanged():
    hot = _v2(temp_fg=88.0, home_temp=75.0, away_temp=60.0)   # delta 15 >= 12
    assert hot.heat_c == pytest.approx(1.0) and hot.heat_away == pytest.approx(1.0) and hot.away_fg_pct == -1.0
    mild = _v2(temp_fg=88.0, home_temp=70.0, away_temp=60.0)  # delta 10 < 12
    assert mild.heat_away == 0.0 and mild.away_fg_pct == 0.0
    assert _v2(temp_fg=79.0, home_temp=90.0, away_temp=40.0).heat_away == 0.0
    assert _v2(temp_fg=88.0, home_temp=None, away_temp=60.0).heat_away == 0.0
    cold = _v2(temp_fg=20.0, away_temp=70.0)
    v1 = I.compute_impact_v1("nfl", 12, 20.0, 0.0, 0.0, None, 70.0)
    assert cold.cold_c == v1.cold_c == 1.25 and cold.cold_away == v1.cold_away == 1.5
    # alt vs heat/cold override kept: away = -max(heat_away + cold_away, alt)
    both = _v2(temp_fg=20.0, away_temp=70.0, travel_alt_m=1800.0)
    assert both.away_fg_pct == -3.5


# ---------------------------------------------------------------- roof / missing inputs


def test_roof_closed_zeroes_site_components_keeps_alt():
    imp = _v2(temp_fg=20.0, wind_fg=30.0, gust_fg=40.0, wind_dir_deg=90.0, orientation_deg=0.0,
              rain_fg_mm=20.0, precip_prob=1.0, travel_alt_m=1800.0, away_temp=70.0, roof_state="closed")
    assert imp.roof_closed and imp.gs_fg_pct == 0.0
    assert imp.wind_c == imp.cold_c == imp.rain_c == imp.cold_away == 0.0
    assert imp.alt_c == 3.5 and imp.away_fg_pct == -3.5
    assert imp.w_eff is not None  # still reported for display
    dome = _v2(wind_fg=30.0, gust_fg=40.0, roof_state="dome")
    assert dome.gs_fg_pct == 0.0
    assert _v2(wind_fg=30.0, gust_fg=40.0, roof_state="open").gs_fg_pct < 0


def test_missing_inputs_and_unknown_sport():
    imp = I.compute_impact_v2("cfb", None, None, None, None, None, None, None, None)
    assert imp.gs_fg_pct == 0.0 and imp.away_fg_pct == 0.0 and imp.w_eff is None and imp.w_dir is None
    assert imp.model_version == "v2" and imp.dir_mult == 1.0
    assert imp.gs_fg_legacy == 0.0 and imp.components()["wind"] == 0.0
    nfl = I.compute_impact_v2("nfl", 60.0, 20.0, 20.0, 0.0, 0.0, 0.0, 60.0, 60.0)
    assert nfl.gs_fg_legacy == pytest.approx(nfl.gs_fg_pct / 100.0)
    with pytest.raises(ValueError):
        I.compute_impact_v2("xfl", 60.0, 0.0, 0.0, 0.0, 0.0, 0.0, 60.0, 60.0)


def test_nan_inputs_treated_as_missing():
    nan = float("nan")
    imp = _v2(temp_fg=nan, wind_fg=nan, gust_fg=nan, rain_fg_mm=nan, precip_prob=nan, travel_alt_m=nan,
              wind_dir_deg=nan, orientation_deg=nan)
    assert imp.gs_fg_pct == 0.0 and imp.away_fg_pct == 0.0


# ---------------------------------------------------------------- fair v2 + confidence


def _lines(gid: str) -> list[GameLine]:
    return [
        GameLine("nfl", gid, "pinnacle", "total", "over", -110, line=44.5),
        GameLine("nfl", gid, "pinnacle", "total", "under", -110, line=44.5),
        GameLine("nfl", gid, "betonline", "total", "over", -105, line=45.0),
        GameLine("nfl", gid, "betonline", "total", "under", -115, line=45.0),
        GameLine("nfl", gid, "pinnacle", "spread", "home", -110, line=-3.0),
        GameLine("nfl", gid, "pinnacle", "spread", "away", -110, line=3.0),
        GameLine("nfl", gid, "betonline", "spread", "home", -110, line=-3.0),
        GameLine("nfl", gid, "betonline", "spread", "away", -110, line=3.0),
    ]


def test_confidence_from_wind_vol_fc_vs_static():
    live = F.confidence(3.0, 0.0, 12.0)
    static_low = F.confidence(None, 0.0, 12.0, "low")
    static_vh = F.confidence(None, 0.0, 12.0, "very high")
    assert live == pytest.approx(0.9) and static_low == pytest.approx(0.9)
    assert static_vh == pytest.approx(0.5) and F.confidence(15.0, 0.0, 12.0) == pytest.approx(0.5)
    assert F.confidence(float("nan"), None, None, "mid") == pytest.approx(0.75)
    assert F.confidence(30.0, 20.0, 400.0) == 0.0


def test_fair_v2_lines_and_edges():
    gid = "nfl:2026:3:sea@ne"
    imp = _v2(wind_fg=21.0, gust_fg=21.0, wind_dir_deg=90.0, orientation_deg=0.0)
    fv = F.fair_v2("nfl", 44.5, -3.0, imp, wind_vol_fc=4.0, model_disagreement=1.0, lead_hours=20.0)
    assert fv.fair_total == pytest.approx(44.5 * (1 + imp.gs_fg_pct / 100))
    assert fv.fair_spread == -3.0 and fv.ensemble and fv.weather_driven
    assert 0.0 <= fv.confidence <= 1.0
    assert F.fair_v2("nfl", None, None, imp, wind_vol_static="high").fair_total is None
    assert F.fair_v2("nfl", 44.5, -3.0, imp, wind_vol_static="high").ensemble is False

    gf = F.evaluate_game_v2("nfl", gid, _lines(gid), imp, wind_vol_fc=4.0, lead_hours=20.0)
    assert gf.fair_total == pytest.approx(fv.fair_total)
    assert gf.edges and all(e.model_version == "v2" for e in gf.edges)
    under = gf.best("total", "under")
    assert under is not None and under.edge_pts > 0
    gf1 = F.evaluate_game("nfl", gid, _lines(gid), -6.5, 0.0, rain_c=0.0, lead_hours=20.0)
    assert all(e.model_version == "v1" for e in gf1.edges)


# ---------------------------------------------------------------- outputs (GameCard / D1)


def _game(gid="nfl:2026:3:sea@ne") -> Game:
    k = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    return Game(gid, "nfl", 2026, 3, k, k, "America/New_York", "ne", "sea", "gillette-stadium")


def _stadium() -> Stadium:
    return Stadium("gillette-stadium", "Gillette Stadium", 42.09, -71.26, orientation_deg=158.0, roof_type="open",
                   wind_vol_static="high", weakest_wind_effect="x N")


def _fc(gid: str) -> WeatherForecast:
    t = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    return WeatherForecast(gid, "hrrr", run_time=t, lead_hours=6.0, temp_fg=55.0, wind_fg=18.0, gust_fg=26.0,
                           wind_dir_fg="E", wind_dir_deg=90.0, rain_fg_mm=0.0, precip_prob=0.1, wind_vol_fc=5.0,
                           wind_p10=14.0, wind_p50=18.0, wind_p90=19.0, cross_mph=16.0, head_mph=8.0,
                           model_disagreement=2.0, roof_state="outdoors",
                           hourly=[WeatherPoint(t=t, wind=18.0, p10=14.0, p90=19.0)])


def test_gamecard_impact_v2_block_and_model_version():
    gid = "nfl:2026:3:sea@ne"
    g, st, fc = _game(gid), _stadium(), _fc(gid)
    v1 = I.compute_impact_v1("nfl", 9, fc.temp_fg, fc.wind_fg, fc.rain_fg_mm, 0.0, 60.0, roof_state="outdoors")
    v2 = I.compute_impact_v2("nfl", fc.temp_fg, fc.wind_fg, fc.gust_fg, fc.rain_fg_mm, fc.precip_prob, 0.0, 60.0, 60.0,
                             wind_dir_deg=fc.wind_dir_deg, wind_dir_fg=fc.wind_dir_fg, orientation_deg=st.orientation_deg,
                             weakest_wind_effect=st.weakest_wind_effect, roof_state="outdoors", conf=0.8)
    gf2 = F.evaluate_game_v2("nfl", gid, _lines(gid), v2, wind_vol_fc=fc.wind_vol_fc, lead_hours=6.0)
    from pipeline.model import signals
    sig = signals.nfl_signal(fc.wind_fg, fc.temp_fg, fc.rain_fg_mm)
    card = json_out.build_card("nfl", g, st, None, None, fc, v1, sig, [], lines=_lines(gid), impact_v2=v2, fair_v2=gf2,
                               model_version="v1")
    imp = card["impact"]
    assert imp["model_version"] == "v1" and imp["v1"]["gs_fg_pct"] == v1.gs_fg_pct
    b = imp["v2"]
    assert b["gs_fg_pct"] == v2.gs_fg_pct and b["away_fg_pct"] == v2.away_fg_pct
    assert set(b["components"]) == {"wind", "cold", "heat", "rain", "alt", "heat_away", "cold_away"}
    assert b["w_eff"] == pytest.approx(0.7 * 18 + 0.3 * 26) and b["dir_mult"] == 0.5 and b["conf"] == 0.8
    assert b["ensemble"] is False and b["roof_closed"] is False
    wx = card["weather"]
    assert wx["wind_vol_fc"] == 5.0 and wx["wind_p10"] == 14.0 and wx["wind_p90"] == 19.0
    assert wx["cross_mph"] == 16.0 and wx["source"] == "hrrr" and wx["lead_hours"] == 6.0
    assert wx["hourly"][0]["p10"] == 14.0 and wx["hourly"][0]["p90"] == 19.0
    fair = card["fair"]
    assert fair["fair_total_v2"] == pytest.approx(gf2.fair_total) and fair["fair_spread_v2"] == pytest.approx(gf2.fair_spread)
    assert fair["confidence_v2"] == pytest.approx(gf2.confidence)
    assert fair["edges"] == [] and fair["fair_total"] is None  # no v1 fair passed
    row = json_out.table_row(card)
    assert row["gs_fg_v2"] == v2.gs_fg_pct and row["fair_total_v2"] == pytest.approx(gf2.fair_total)
    assert row["wind_vol_fc"] == 5.0 and row["model_version"] == "v1"
    json.dumps(card, allow_nan=False, default=str)

    # ALERT_MODEL=v2 stamps the card and the edges it carries
    card2 = json_out.build_card("nfl", g, st, None, None, fc, v1, sig, [], lines=_lines(gid), fair=gf2, impact_v2=v2,
                                fair_v2=gf2, model_version="v2")
    assert card2["impact"]["model_version"] == "v2"
    assert all(e["model_version"] == "v2" for e in card2["fair"]["edges"])
    none = json_out.build_card("nfl", g, st, None, None, None, None, sig, [])
    assert none["impact"] == {"v1": None, "v2": None, "model_version": "v1"}


def test_d1_rows_carry_v2_columns():
    gid = "nfl:2026:3:sea@ne"
    g, fc = _game(gid), _fc(gid)
    v1 = I.compute_impact_v1("nfl", 9, 55.0, 18.0, 0.0, 0.0, 60.0)
    v2 = _v2(wind_fg=18.0, gust_fg=26.0, wind_dir_deg=90.0, orientation_deg=158.0)
    row = d1_out.weather_row(fc, v1, "2026-09-20T11:00:00Z", "r1", impact_v2=v2)
    assert set(row) == set(d1_out.WX_COLS)
    assert row["gs_fg"] == v1.gs_fg_pct and row["gs_fg_v2"] == v2.gs_fg_pct and row["away_fg_v2"] == v2.away_fg_pct
    assert row["wind_vol"] == 5.0 and row["wind_p10"] == 14.0 and row["model_version"] == "v1"
    assert d1_out.weather_row(fc, v1, "x", "r1")["gs_fg_v2"] is None
    # a v2-only move is a change point
    last: dict = {}
    assert d1_out.weather_deltas([row], last) == [row]
    moved = dict(row, gs_fg_v2=row["gs_fg_v2"] - 1.0)
    assert d1_out.weather_deltas([moved], last) == [moved]
    assert d1_out.weather_deltas([moved], last) == []

    grows = d1_out.game_rows([g], "now", impacts={gid: v1}, impacts_v2={gid: v2})
    assert set(grows[0]) == set(d1_out.GAME_COLS)
    assert grows[0]["gs_fg"] == v1.gs_fg_pct and grows[0]["gs_fg_v2"] == v2.gs_fg_pct
    bare = d1_out.game_rows([g], "now")[0]
    assert bare["gs_fg_v2"] is None and bare["gs_fg"] is None
    sql = d1_out.upsert_sql("games", d1_out.GAME_COLS, ["game_id"], grows)
    assert sql and "gs_fg_v2" in sql[0]


def test_migration_0004_adds_v2_columns():
    p = Path(__file__).resolve().parent.parent / "site" / "worker" / "migrations" / "0004_v2.sql"
    text = p.read_text(encoding="utf-8")
    for col in ("gs_fg", "away_fg", "gs_fg_v2", "away_fg_v2"):
        assert f"ALTER TABLE games ADD COLUMN {col} REAL;" in text
    extra = [c for c in d1_out.GAME_COLS if c not in ("gs_fg", "away_fg", "gs_fg_v2", "away_fg_v2")]
    init = (p.parent / "0001_init.sql").read_text(encoding="utf-8")
    assert all(c in init for c in extra)


def test_alert_model_selection_defaults_to_v1(monkeypatch):
    assert C.alert_model() == "v1"
    monkeypatch.setattr(C, "ALERT_MODEL", "v2")
    assert C.alert_model() == "v2"
    monkeypatch.setattr(C, "ALERT_MODEL", "v9")
    assert C.alert_model() == "v1"
