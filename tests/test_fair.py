"""pipeline.model.fair — golden legacy columns, devig sanity, key-number tables,
consensus and edge/tier behaviour."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pipeline.contracts import GameLine
from pipeline.model import config as C
from pipeline.model import fair as F

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_fair_2024.parquet"
GID = "cfb:2026:1:tennessee@oklahoma"


def _gl(book: str, market: str, side: str, odds: int, line: float, sport: str = "cfb", **kw) -> GameLine:
    return GameLine(sport=sport, game_id=GID, book=book, market=market, side=side, odds=odds, line=line, **kw)


def _pair(book: str, market: str, home_line: float, home_odds: int = -110, away_odds: int = -110, sport: str = "cfb"):
    if market == "spread":
        return [
            _gl(book, "spread", "home", home_odds, home_line, sport),
            _gl(book, "spread", "away", away_odds, -home_line, sport),
        ]
    return [
        _gl(book, "total", "over", home_odds, home_line, sport),
        _gl(book, "total", "under", away_odds, home_line, sport),
    ]


# ---- golden ---------------------------------------------------------------------
@pytest.mark.skipif(not GOLDEN.exists(), reason="golden_fair_2024.parquet missing")
def test_golden_legacy_derived():
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    df = pd.read_parquet(GOLDEN)
    df = df.dropna(subset=["Total_proj", "Spread", "FD_now", "gs_fg", "away_fg"])
    assert len(df) > 100
    bad = []
    for r in df.itertuples(index=False):
        out = F.legacy_derived(r.Total_proj, r.Spread, r.FD_now, r.gs_fg, r.away_fg)
        for col in ("My_total", "Edge", "My_spread", "Edge_s"):
            ref = getattr(r, col)
            got = out[col]
            if ref is None or (isinstance(ref, float) and math.isnan(ref)):
                continue
            if got is None or not math.isclose(got, ref, rel_tol=1e-4, abs_tol=1e-3):
                bad.append((r.Game, col, ref, got))
    assert not bad, bad[:10]


# ---- devig ----------------------------------------------------------------------
def test_devig_symmetric_and_sums_to_one():
    pa, pb = F.devig_pair(-110, -110)
    assert math.isclose(pa, 0.5) and math.isclose(pa + pb, 1.0)
    pa, pb = F.devig_pair(-150, +130)
    assert pa > 0.5 > pb and math.isclose(pa + pb, 1.0)
    assert math.isclose(F.american_to_prob(-110), 110 / 210)
    assert F.prob_to_american(0.5) == -100
    assert F.prob_to_american(0.25) == 300
    assert F.american_to_decimal(+150) == 2.5
    assert F.devig_pair(0, 0) == (0.5, 0.5)


def test_vigfree_exchange_uses_prob_raw():
    a = _gl("kalshi", "total", "over", -110, 55.5, prob_raw=0.62)
    b = _gl("kalshi", "total", "under", -110, 55.5, prob_raw=0.38)
    assert F.vigfree_prob(a, b) == 0.62
    assert F.vigfree_prob(b, a) == 0.38


# ---- key-number tables -----------------------------------------------------------
@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_spread_cumulative_monotone_and_odd_symmetric(sport):
    xs = [i * 0.5 for i in range(-40, 41)]
    ys = [F.spread_cumulative(sport, x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:], strict=False))
    assert F.spread_cumulative(sport, 0.0) == 0.0
    assert math.isclose(F.spread_cumulative(sport, -3.0), -F.spread_cumulative(sport, 3.0))
    # key numbers dominate: 2.5->3 is the biggest single step
    steps = {k: F.spread_cumulative(sport, k) - F.spread_cumulative(sport, k - 0.5) for k in [1.0, 2.0, 3.0, 4.0, 7.0]}
    assert steps[3.0] == max(steps.values())
    assert steps[7.0] > steps[4.0]
    assert all(v > 0 for v in F.SPREAD_KEY_PROB[sport].values())


def test_spread_shift_sign_and_totals_linear():
    # home gains points -> home gains probability
    assert F.spread_shift("nfl", -3.0, -2.5, "home") > 0
    assert F.spread_shift("nfl", -3.0, -2.5, "away") < 0
    assert math.isclose(F.spread_shift("nfl", -3.0, -2.5, "home"), C.PTS_PROB_TOTAL["nfl"] * 0 + F.SPREAD_KEY_PROB["nfl"][3.0])
    assert math.isclose(F.total_shift("nfl", 45.0, 44.0, "over"), C.PTS_PROB_TOTAL["nfl"])
    assert math.isclose(F.total_shift("cfb", 45.0, 44.0, "under"), -C.PTS_PROB_TOTAL["cfb"])


# ---- consensus -------------------------------------------------------------------
def test_weighted_median_pinnacle_dominates():
    lines = (
        _pair("pinnacle", "total", 55.5)
        + _pair("fanduel", "total", 57.5)
        + _pair("novig", "total", 57.5)
        + _pair("kalshi", "total", 58.0)
    )
    cons = F.consensus("cfb", lines, "total")
    assert cons.line == 56.5  # pinnacle(3) ties fanduel+novig(2) -> midpoint, unweighted median would be 57.5
    assert cons.n_books == 4 and not cons.thin
    cons2 = F.consensus("cfb", lines + _pair("betcris", "total", 55.5), "total")
    assert cons2.line == 55.5  # pinnacle+betcris (4.5) outweigh three books at >=57.5 (3.0)
    assert cons.ref_book == "pinnacle"
    assert 0.4 < cons.prob < 0.6


def test_consensus_thin_single_book():
    cons = F.consensus("nfl", _pair("fanduel", "spread", -3.5, sport="nfl"), "spread")
    assert cons.thin and cons.n_books == 1 and cons.line == -3.5
    assert F.consensus("nfl", [], "spread").line is None


def test_consensus_spread_away_rows_flip_to_home_relative():
    lines = [_gl("betcris", "spread", "away", -110, 3.5), _gl("betcris", "spread", "home", -110, -3.5)]
    lines += _pair("pinnacle", "spread", -3.5)
    cons = F.consensus("cfb", lines, "spread")
    assert cons.line == -3.5 and cons.books["betcris"] == -3.5


def test_main_lines_prefers_is_main():
    alt = [_gl("kalshi", "total", "over", -110, 60.5, is_main=False), _gl("kalshi", "total", "under", -110, 60.5, is_main=False)]
    main = [_gl("kalshi", "total", "over", -110, 55.5), _gl("kalshi", "total", "under", -110, 55.5)]
    picked = F.main_lines(alt + main, "total")
    assert picked["kalshi"]["over"].line == 55.5


# ---- fair / edges / tiers ---------------------------------------------------------
def test_fair_lines_from_impact():
    assert math.isclose(F.fair_total(55.0, -8.16), 50.512)
    assert math.isclose(F.fair_spread(20.0, -3.5), 19.3)
    assert F.fair_total(None, -3.0) is None
    assert F.fair_total(55.0, float("nan")) == 55.0


def test_edge_pts_sign():
    assert F.edge_pts("total", "under", 50.0, 55.0) == 5.0
    assert F.edge_pts("total", "over", 50.0, 55.0) == -5.0
    assert F.edge_pts("spread", "home", -3.0, -1.0) == 2.0
    assert F.edge_pts("spread", "away", -3.0, -1.0) == -2.0


def test_confidence_bounds_and_static_fallback():
    assert F.confidence(0.0, 0.0, 0.0) == 1.0
    assert F.confidence(30.0, 20.0, 500.0) == 0.0
    assert math.isclose(F.confidence(None, None, None, "very high"), 0.5)
    assert math.isclose(F.confidence(None, None, None, "low"), 0.9)


def test_evaluate_game_windy_under_is_strong():
    lines = (
        _pair("pinnacle", "total", 56.0)
        + _pair("fanduel", "total", 56.5)
        + _pair("betcris", "total", 56.0)
        + _pair("pinnacle", "spread", -7.0)
        + _pair("fanduel", "spread", -7.0)
    )
    gf = F.evaluate_game("cfb", GID, lines, gs_fg_pct=-8.16, away_fg_pct=0.0, rain_c=0.0, wind_vol_fc=2.0, lead_hours=24.0)
    assert math.isclose(gf.fair_total, 56.0 * (1 - 0.0816))
    under = gf.best("total", "under")
    assert under is not None and under.book == "fanduel"
    assert under.edge_pts > 4.0 and under.edge_prob > 0.03
    assert under.tier == "strong" and under.n_books == 3 and under.ref_book == "pinnacle"
    over = gf.best("total", "over")
    assert over.tier == "none" and over.edge_pts < 0
    # spread untouched (away_fg 0) -> no edge
    assert all(e.tier == "none" for e in gf.edges if e.market == "spread")
    assert all(0.0 <= e.confidence <= 1.0 for e in gf.edges)


def test_evaluate_game_thin_and_not_weather_driven():
    thin = F.evaluate_game("nfl", GID, _pair("fanduel", "total", 45.0, sport="nfl"), -10.0, 0.0, lead_hours=10.0)
    assert all(e.tier == "none" and e.edge_pts is None for e in thin.edges)
    lines = _pair("pinnacle", "total", 45.0, sport="nfl") + _pair("fanduel", "total", 47.5, sport="nfl")
    gf = F.evaluate_game("nfl", GID, lines, gs_fg_pct=-2.0, away_fg_pct=0.0, rain_c=0.0, wind_vol_fc=0.0, lead_hours=10.0)
    assert not gf.weather_driven
    assert gf.best("total", "under").tier == "watch"


def test_tier_thresholds():
    assert F.tier("nfl", "total", 2.6, 0.05, 0.6, 10.0) == "strong"
    assert F.tier("nfl", "total", 1.6, 0.05, 0.3, 10.0) == "edge"  # lead <= 36 bypasses conf
    assert F.tier("nfl", "total", 1.6, 0.05, 0.3, 100.0) == "watch"
    assert F.tier("nfl", "total", 1.6, 0.01, 0.9, 10.0) == "watch"  # edge_prob too small
    assert F.tier("nfl", "total", 0.5, 0.05, 0.9, 10.0) == "none"
    assert F.tier("nfl", "ml", 5.0, 0.2, 1.0, 1.0) == "none"


# ---- legacy now / columns ------------------------------------------------------
def test_legacy_columns_use_sport_book_then_fallback():
    lines = (
        _pair("pinnacle", "total", 55.0)
        + _pair("fanduel", "total", 56.5, -105, -115)
        + _pair("pinnacle", "spread", -8.0)
        + _pair("fanduel", "spread", -7.0, -108, -112)
    )
    cols = F.legacy_columns("cfb", lines, gs_fg_pct=-8.16, away_fg_pct=0.0)
    assert cols["Total_proj"] == 55.0 and cols["Spread"] == -8.0 and cols["ref_book"] == "pinnacle"
    assert cols["Total_now"] == 56.5 and cols["Under_now"] == -115 and cols["now_book"] == "fanduel"
    assert cols["Spread_now"] == -7.0 and cols["Odds_now"] == -108
    assert math.isclose(cols["My_total"], 50.512)
    assert math.isclose(cols["Edge"], (56.5 - 50.512) / 50.512)
    # NFL prefers betonline; absent -> consensus fallback
    nfl = _pair("pinnacle", "total", 45.0, sport="nfl") + _pair("betcris", "total", 46.0, sport="nfl")
    cols = F.legacy_columns("nfl", nfl, -3.5, 0.0)
    assert cols["Total_now"] == 45.0 and cols["now_book"] == "pinnacle" and cols["Under_now"] is None


def test_apply_calibration_overrides(tmp_path):
    orig_tot = C.PTS_PROB_TOTAL["nfl"]
    orig_tbl = dict(F.SPREAD_KEY_PROB["nfl"])
    p = tmp_path / "calibration.json"
    p.write_text('{"pts_prob_total": {"nfl": 0.03}, "spread_key_prob": {"nfl": {"3.0": 0.2}}}', encoding="utf-8")
    try:
        F.apply_calibration(F.load_calibration(p))
        assert C.PTS_PROB_TOTAL["nfl"] == 0.03 and F.SPREAD_KEY_PROB["nfl"] == {3.0: 0.2}
        assert F.load_calibration(tmp_path / "missing.json") == {}
    finally:
        C.PTS_PROB_TOTAL["nfl"] = orig_tot
        F.SPREAD_KEY_PROB["nfl"] = orig_tbl
