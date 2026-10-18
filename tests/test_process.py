"""Process screen: shared NFL/CFB gates, ECMWF signal forecast, price move.

v1 impact tiers are asserted unchanged. The stitched forecast at a 72 h lead
stays NBM; ECMWF is the signal-driving forecast beside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.model import config as C
from pipeline.model import process as P
from pipeline.weather.merge import build_forecast
from pipeline.weather.parsers import HourlyRow
from pipeline.weather.parsers.openmeteo import ParsedLocation

WEB = Path(__file__).resolve().parents[1] / "site" / "web"
UTC = timezone.utc


def test_v1_rain_tiers_stay_frozen() -> None:
    assert C.RAIN_TIERS_MM == [(12.0, 6.5), (6.0, 3.0), (1.0, 1.5)]
    assert C.RAIN_TIER_STRICT_MM == {12.0, 1.0}


def test_wind_temp_and_rain_bands() -> None:
    assert P.wind_band(11.99) == "none"
    assert P.wind_band(12) == "watch"
    assert P.wind_band(14.99) == "watch"
    assert P.wind_band(15) == "flag"
    assert P.rain_tier(4.99) == "ignore"
    assert P.rain_tier(5) == "watch"
    assert P.rain_tier(14.99) == "watch"
    assert P.rain_tier(15) == "elevated"
    assert P.rain_tier(22.99) == "elevated"
    assert P.rain_tier(23) == "signal_amount"


def test_crosswind_uses_orientation_not_gust() -> None:
    assert P.crosswind_factor(90, 0) == pytest.approx(1.0)
    assert P.crosswind_factor(0, 0) == pytest.approx(0.0)
    cross, head, factor = P.wind_components(10, 90, 0)
    assert cross == pytest.approx(10) and head == pytest.approx(0) and factor == pytest.approx(1)


def test_rain_signal_needs_ecmwf_and_another_model() -> None:
    windows = {
        P.ECMWF_IFS: {"rain_mm": 24.0, "wind_mph": 4.0, "temp_f": 40.0},
        P.ECMWF_AIFS: {"rain_mm": 30.0, "wind_mph": 4.0, "temp_f": 40.0},
        "ncep_gfs_seamless": {"rain_mm": 26.0, "wind_mph": 6.0, "temp_f": 42.0},
    }
    assert P.rain_agreement(windows)["agree"] is True
    windows["ncep_gfs_seamless"]["rain_mm"] = 8.0
    refused = P.rain_agreement(windows)
    assert refused["agree"] is False and refused["status"] == "disagree"
    assert refused["partner"] is None
    only_ecmwf = {P.ECMWF_IFS: {"rain_mm": 30.0}, P.ECMWF_AIFS: {"rain_mm": 28.0}}
    assert P.rain_agreement(only_ecmwf)["status"] == "unverified"


def test_rain_partner_is_the_closest_model_that_clears_the_signal() -> None:
    windows = {
        P.ECMWF_IFS: {"rain_mm": 25.0},
        "ncep_nbm_conus": {"rain_mm": 22.0},
        "ncep_gfs_seamless": {"rain_mm": 30.0},
    }
    agreed = P.rain_agreement(windows)
    assert agreed["agree"] is True and agreed["status"] == "agree"
    assert agreed["partner"] == "ncep_gfs_seamless"
    assert agreed["partner_mm"] == pytest.approx(30.0)
    windows["ncep_gfs_seamless"]["rain_mm"] = 10.0
    none = P.rain_agreement(windows)
    assert none["agree"] is False and none["status"] == "disagree"
    assert none["partner"] is None and none["partner_mm"] is None
    windows["ncep_gfs_seamless"]["rain_mm"] = 40.0
    far = P.rain_agreement(windows)
    assert far["partner"] == "ncep_gfs_seamless" and far["agree"] is False


def test_ecmwf_apart_is_a_flag() -> None:
    windows = {
        P.ECMWF_IFS: {"wind_mph": 20.0, "temp_f": 40.0, "rain_mm": 1.0},
        "ncep_gfs_seamless": {"wind_mph": 8.0, "temp_f": 41.0, "rain_mm": 1.0},
        "ncep_nbm_conus": {"wind_mph": 9.0, "temp_f": 42.0, "rain_mm": 0.5},
    }
    apart = P.ecmwf_disagreement(windows)
    assert apart["apart"] is True and "wind" in apart["fields"] and "temp" not in apart["fields"]


def test_signal_forecast_prefers_ecmwf_only_past_48h() -> None:
    windows = {
        P.ECMWF_IFS: {"temp_f": 40.0, "wind_mph": 18.0, "rain_mm": 2.0, "wind_dir_deg": 90.0},
        "ncep_nbm_conus": {"temp_f": 55.0, "wind_mph": 6.0, "rain_mm": 0.0, "wind_dir_deg": 10.0},
    }
    stitched = {"temp_f": 55.0, "wind_mph": 6.0, "rain_mm": 0.0, "wind_dir_deg": 10.0}
    near = P.select_signal_forecast(24, stitched, windows)
    assert near["source"] == "stitched" and near["wind_mph"] == 6.0
    far = P.select_signal_forecast(72, stitched, windows)
    assert far["source"].startswith("ecmwf") and far["wind_mph"] == 18.0 and far["temp_f"] == 40.0
    off = P.select_signal_forecast(72, stitched, windows, ecmwf_first=False)
    assert off["source"] == "stitched" and off["wind_mph"] == 6.0


def test_price_move_uses_consensus_and_labels_the_approximation() -> None:
    calm = [
        {"book": "pinnacle", "under": -110, "over": -110, "open_under": -110, "open_over": -110},
        {"book": "betonline", "under": -110, "over": -110, "open_under": -110, "open_over": -110},
    ]
    still = P.under_prob_move(calm)
    assert still["basis"] == P.BASIS_NOVIG and still["under_prob_move"] == pytest.approx(0.0)
    assert still["price_blocked"] is False
    moved = [
        {"book": "pinnacle", "under": -150, "over": 130, "open_under": -110, "open_over": -110},
        {"book": "betonline", "under": -150, "over": 130, "open_under": -110, "open_over": -110},
    ]
    jumped = P.under_prob_move(moved)
    assert jumped["basis"] == P.BASIS_NOVIG and jumped["under_prob_move"] > 0.04
    assert jumped["price_blocked"] is True
    implied = [
        {"book": "pinnacle", "under": -180, "over": None, "open_under": -110, "open_over": None},
        {"book": "fanduel", "under": -180, "over": None, "open_under": -110, "open_over": None},
    ]
    approx = P.under_prob_move(implied)
    assert approx["basis"] == P.BASIS_IMPLIED and approx["price_blocked"] is True
    one = P.under_prob_move(implied[:1])
    assert one["basis"] == P.BASIS_SINGLE and one["price_blocked"] is True


def _even_books(line: float, open_line: float, under: int = -110, over: int = -110) -> list[dict]:
    row = {
        "line": line, "open_line": open_line,
        "under": under, "over": over, "open_under": -110, "open_over": -110,
    }
    return [{"book": "pinnacle", **row}, {"book": "betonline", **row}]


def test_moved_total_counts_even_when_the_price_is_back_at_even() -> None:
    moved = P.under_prob_move(_even_books(43, 47), sport="nfl")
    assert moved["basis"] == P.BASIS_NOVIG
    assert moved["under_prob_price_move"] == pytest.approx(0.0)
    assert moved["line_open"] == pytest.approx(47) and moved["line_now"] == pytest.approx(43)
    assert moved["line_prob_move"] == pytest.approx(4 * C.PTS_PROB_TOTAL["nfl"])
    assert moved["line_open_missing"] is False
    assert moved["under_prob_move"] == pytest.approx(0.104)
    assert moved["price_blocked"] is True
    one_point = P.under_prob_move(_even_books(46, 47), sport="nfl")
    assert one_point["line_prob_move"] == pytest.approx(C.PTS_PROB_TOTAL["nfl"])
    assert one_point["price_blocked"] is False
    # A longer under price does not cancel the drop. Adverse juice is 0; the line stays.
    longer = P.under_prob_move(_even_books(43, 47, under=130, over=-150), sport="nfl")
    assert longer["under_prob_price_move"] < 0 < longer["line_prob_move"]
    assert longer["under_prob_move"] == pytest.approx(longer["line_prob_move"])
    assert longer["under_prob_move"] == pytest.approx(0.104)
    assert longer["price_blocked"] is True
    # A higher total is better for the under and does not block on the line alone.
    risen = P.under_prob_move(_even_books(49, 47), sport="nfl")
    assert risen["line_prob_move"] == pytest.approx(-2 * C.PTS_PROB_TOTAL["nfl"])
    assert risen["under_prob_move"] == pytest.approx(0.0)
    assert risen["price_blocked"] is False
    cfb = P.under_prob_move(_even_books(43, 47), sport="cfb")
    assert cfb["line_prob_move"] == pytest.approx(4 * C.PTS_PROB_TOTAL["cfb"])
    assert cfb["price_blocked"] is True
    card = _card(wind_fg=16, temp_fg=40)
    card["odds"] = {
        "pinnacle": {"total": {"line": 43, "open_line": 47, "under": 130, "over": -150, "open_under": -110, "open_over": -110}},
        "betonline": {"total": {"line": 43, "open_line": 47, "under": 130, "over": -150, "open_under": -110, "open_over": -110}},
    }
    screen = P.evaluate_card(card)
    assert screen["candidate"] is True and screen["price_blocked"] is True and screen["alert"] is False
    assert screen["line_open_missing"] is False


def test_one_book_with_an_opener_is_enough_for_the_line() -> None:
    single = P.under_prob_move(_even_books(43, 47)[:1], sport="nfl")
    assert single["basis"] == P.BASIS_SINGLE
    assert single["line_open"] == pytest.approx(47) and single["line_now"] == pytest.approx(43)
    assert single["line_prob_move"] == pytest.approx(0.104)
    assert single["line_open_missing"] is False
    assert single["price_blocked"] is True
    # Current prices at two books, but only one recorded opener. That opener still counts.
    partial = _even_books(43, 47)
    del partial[1]["open_line"]
    one_opener = P.under_prob_move(partial, sport="nfl")
    assert one_opener["line_open"] == pytest.approx(47) and one_opener["line_prob_move"] == pytest.approx(0.104)
    assert one_opener["price_blocked"] is True
    # No opener anywhere: do not invent a line move, and do not pretend the check ran.
    naked = [
        {"book": "pinnacle", "line": 43, "under": -110, "over": -110},
        {"book": "betonline", "line": 43, "under": -110, "over": -110},
    ]
    missing = P.under_prob_move(naked, sport="nfl")
    assert missing["line_prob_move"] is None and missing["line_open_missing"] is True
    assert missing["price_blocked"] is False
    card = _card(wind_fg=16, temp_fg=40)
    card["odds"] = {"betonline": {"total": {"line": 43, "open_line": 47, "under": -110, "over": -110, "open_under": -110, "open_over": -110}}}
    blocked = P.evaluate_card(card)
    assert blocked["alert"] is False and blocked["price_blocked"] is True
    card["odds"] = {"betonline": {"total": {"line": 43, "under": -110, "over": -110}}}
    flagged = P.evaluate_card(card)
    assert flagged["line_open_missing"] is True and flagged["alert"] is True and flagged["price_blocked"] is False


def test_one_books_own_adverse_price_blocks_even_when_the_average_does_not() -> None:
    single = [{
        "book": "betonline", "line": 47, "open_line": 47,
        "under": -180, "over": None, "open_under": -110, "open_over": None,
    }]
    one = P.under_prob_move(single, sport="nfl")
    assert one["basis"] == P.BASIS_SINGLE
    assert one["line_prob_move"] == pytest.approx(0.0)
    assert one["book_adverse_max"] > C.PROCESS_UNDER_PROB_MOVE_MAX
    assert one["price_blocked"] is True and one["price_block_book"] == "betonline"
    mild = [{
        "book": "betonline", "line": 47, "open_line": 47,
        "under": -115, "over": None, "open_under": -110, "open_over": None,
    }]
    assert P.under_prob_move(mild, sport="nfl")["price_blocked"] is False
    split = [
        {"book": "pinnacle", "line": 47, "open_line": 47, "under": 130, "over": None, "open_under": -110, "open_over": None},
        {"book": "betonline", "line": 47, "open_line": 47, "under": -180, "over": None, "open_under": -110, "open_over": None},
    ]
    mixed = P.under_prob_move(split, sport="nfl")
    assert mixed["under_prob_price_move"] < C.PROCESS_UNDER_PROB_MOVE_MAX
    assert mixed["book_adverse_max"] > C.PROCESS_UNDER_PROB_MOVE_MAX
    assert mixed["price_blocked"] is True and mixed["price_block_book"] == "betonline"
    card = _card(wind_fg=16, temp_fg=40)
    card["odds"] = {
        "pinnacle": {"total": {"line": 47, "open_line": 47, "under": 130, "open_under": -110}},
        "betonline": {"total": {"line": 47, "open_line": 47, "under": -180, "open_under": -110}},
    }
    screen = P.evaluate_card(card)
    assert screen["alert"] is False and screen["price_blocked"] is True


def _flat_total() -> dict:
    return {
        "line": 47, "open_line": 47,
        "under": -110, "over": -110, "open_under": -110, "open_over": -110,
    }


def test_reference_books_stay_out_of_the_price_gate() -> None:
    flat = _flat_total()
    pricing = [{"book": "pinnacle", **flat}, {"book": "betonline", **flat}]
    juice = P.under_prob_move(pricing + [{
        "book": "betcris", "line": 47, "open_line": 47,
        "under": -180, "over": -110, "open_under": -110, "open_over": -110,
    }], sport="nfl")
    assert juice["price_blocked"] is False
    assert juice["under_prob_move"] == pytest.approx(0.0)
    assert juice["book_adverse_max"] == pytest.approx(0.0)
    assert all(row["book"] != "betcris" for row in juice["books"])
    dropped = P.under_prob_move(pricing + [{
        "book": "betcris", "line": 43, "open_line": 47,
        "under": -110, "over": -110, "open_under": -110, "open_over": -110,
    }], sport="nfl")
    assert dropped["line_prob_move"] == pytest.approx(0.0)
    assert dropped["price_blocked"] is False
    sided = P.under_prob_move(pricing + [{
        "book": "betcris", "line": 47, "open_line": 47,
        "under": -180, "over": 150, "open_under": -110, "open_over": -110,
    }], sport="nfl")
    assert sided["basis"] == P.BASIS_NOVIG
    assert sided["under_prob_price_move"] == pytest.approx(0.0)
    assert sided["price_blocked"] is False
    only_reference = P.under_prob_move([{
        "book": "betcris", "line": 43, "open_line": 47,
        "under": -180, "over": 150, "open_under": -110, "open_over": -110,
    }], sport="nfl")
    assert only_reference["books"] == []
    assert only_reference["line_open_missing"] is True and only_reference["price_blocked"] is False
    short = dict(flat, under=-180, over=None, open_over=None)
    still = P.under_prob_move([
        {"book": "pinnacle", **flat},
        {"book": "betonline", **short},
        {"book": "betcris", **flat},
    ], sport="nfl")
    assert still["price_blocked"] is True and still["price_block_book"] == "betonline"
    card = _card(wind_fg=16, temp_fg=40)
    card["odds"] = {
        "pinnacle": {"total": flat},
        "betonline": {"total": flat},
        "betcris": {"total": {
            "line": 43, "open_line": 47,
            "under": -180, "over": 150, "open_under": -110, "open_over": -110,
        }},
    }
    screen = P.evaluate_card(card)
    assert screen["alert"] is True and screen["price_blocked"] is False


def _card(**weather: float) -> dict:
    return {
        "sport": "nfl",
        "stadium": {"roof_type": "open", "roof_state": "outdoors", "orient_deg": 0},
        "weather": {"wind_fg": 4.0, "temp_fg": 50.0, "rain_fg": 0.0, "wind_dir_deg": 90.0, **weather},
        "consensus": {},
        "odds": {},
    }


def test_warm_wind_buckets_are_separate_from_the_under_screen() -> None:
    assert P.warm_wind_bucket(64.9, 18) is None
    assert P.warm_wind_bucket(65, 10) is None
    assert P.warm_wind_bucket(65, 10.01) == "10_12"
    assert P.warm_wind_bucket(80, 12) == "10_12"
    assert P.warm_wind_bucket(80, 12.01) == "12_15"
    assert P.warm_wind_bucket(80, 15) == "12_15"
    assert P.warm_wind_bucket(80, 15.01) == "gt_15"
    warm_flag = P.evaluate_card(_card(wind_fg=18, temp_fg=65))
    assert warm_flag["alert"] is True and warm_flag["wind_band"] == "flag"
    assert warm_flag["temp_gate"] is False
    track = P.warm_wind_track(_card(wind_fg=14, temp_fg=72))
    assert track["tracked"] is True and track["bucket"] == "12_15" and track["alert"] is False
    wide = _card(wind_fg=11, temp_fg=78)
    wide["sport"] = "cfb"
    wide["consensus"] = {"spread_open": 21}
    assert P.evaluate_card(wide)["alert"] is False
    assert P.warm_wind_track(wide)["tracked"] is True
    dome = _card(wind_fg=20, temp_fg=80)
    dome["stadium"] = {"roof_type": "dome", "roof_state": "dome"}
    assert P.warm_wind_track(dome)["tracked"] is False


def test_evaluate_card_shared_gates_for_both_sports() -> None:
    flagged = P.evaluate_card(_card(wind_fg=15, temp_fg=80))
    assert flagged["alert"] is True and flagged["wind_band"] == "flag"
    assert flagged["crosswind_factor"] == pytest.approx(1.0)
    assert P.evaluate_card(_card(wind_fg=14.99, temp_fg=40))["alert"] is False
    breeze = P.evaluate_card(_card(wind_fg=12.5, temp_fg=40))
    assert breeze["wind_band"] == "watch" and breeze["alert"] is False and breeze["watch"] is True
    watch = P.evaluate_card(_card(wind_fg=3, temp_fg=40, rain_fg=8))
    assert watch["alert"] is False and watch["watch"] is True and watch["rain_tier"] == "watch"
    cfb = _card(wind_fg=16, temp_fg=50)
    cfb["sport"] = "cfb"
    cfb["consensus"] = {"spread_open": 10.5}
    assert P.evaluate_card(cfb)["alert"] is False
    cfb["consensus"] = {"spread_open": 3}
    assert P.evaluate_card(cfb)["alert"] is True


def test_rain_agreement_follows_ecmwf_not_the_driving_total() -> None:
    """The three cases where a stitch total and ECMWF disagree."""
    low_ecmwf = P.rain_agreement({
        P.ECMWF_IFS: {"rain_mm": 8.0},
        "ncep_gfs_seamless": {"rain_mm": 26.0},
    })
    assert low_ecmwf["agree"] is False and low_ecmwf["ecmwf_mm"] == pytest.approx(8.0)
    matched = P.rain_agreement({
        P.ECMWF_IFS: {"rain_mm": 24.0},
        "ncep_gfs_seamless": {"rain_mm": 25.0},
    })
    assert matched["agree"] is True and matched["partner"] == "ncep_gfs_seamless"
    missing = P.rain_agreement({"ncep_gfs_seamless": {"rain_mm": 26.0}})
    assert missing["status"] == "missing_ecmwf" and missing["agree"] is False
    stitch_high = _card(wind_fg=3, temp_fg=50, rain_fg=25)
    stitch_high["weather"]["models"] = [
        {"id": P.ECMWF_IFS, "rain_mm": 8.0},
        {"id": "ncep_gfs_seamless", "rain_mm": 26.0},
    ]
    assert P.evaluate_card(stitch_high)["alert"] is False
    stitch_far = _card(wind_fg=3, temp_fg=50, rain_fg=40)
    stitch_far["weather"]["models"] = [
        {"id": P.ECMWF_IFS, "rain_mm": 24.0},
        {"id": "ncep_gfs_seamless", "rain_mm": 25.0},
    ]
    assert P.evaluate_card(stitch_far)["alert"] is True
    no_ecmwf = _card(wind_fg=3, temp_fg=50, rain_fg=25)
    no_ecmwf["weather"]["models"] = [{"id": "ncep_gfs_seamless", "rain_mm": 26.0}]
    screen = P.evaluate_card(no_ecmwf)
    assert screen["alert"] is False and screen["rain_agreement"] == "missing_ecmwf"


def test_heavy_rain_without_agreement_does_not_alert() -> None:
    card = _card(wind_fg=3, temp_fg=40, rain_fg=24)
    card["weather"]["models"] = [
        {"id": P.ECMWF_IFS, "rain_mm": 24.0, "wind_mph": 3.0, "temp_f": 40.0},
        {"id": "ncep_gfs_seamless", "rain_mm": 4.0, "wind_mph": 3.0, "temp_f": 40.0},
    ]
    screen = P.evaluate_card(card)
    assert screen["alert"] is False and screen["rain_tier"] == "disagree"
    card["weather"]["models"][1]["rain_mm"] = 25.0
    agreed = P.evaluate_card(card)
    assert agreed["alert"] is True and agreed["rain_agreement"] == "agree"


def test_stitched_forecast_stays_nbm_while_signal_uses_ecmwf() -> None:
    kick = datetime(2026, 10, 18, 18, tzinfo=UTC)
    now = kick - timedelta(hours=72)

    def rows(wind: float, temp: float) -> list[HourlyRow]:
        return [
            HourlyRow(t=kick + timedelta(hours=i), wind=wind, temp=temp, precip=0.2, dir=180, gust=wind + 8, pop=10)
            for i in range(3)
        ]

    om = ParsedLocation(
        latitude=42.0, longitude=-71.0,
        models={"ncep_nbm_conus": rows(6.0, 55.0), P.ECMWF_IFS: rows(18.0, 42.0), "ncep_gfs_seamless": rows(7.0, 54.0)},
    )
    forecast = build_forecast("nfl:2026:6:a@b", kick, now, om, auto_climo=False).forecast
    assert forecast.source == "nbm"
    assert forecast.wind_fg == pytest.approx(6.0)
    assert forecast.temp_fg == pytest.approx(55.0)
    assert forecast.signal_source is not None and forecast.signal_source.startswith("ecmwf")
    assert forecast.signal_wind_mph == pytest.approx(18.0)
    assert forecast.signal_temp_f == pytest.approx(42.0)
    assert "ecmwf_ifs025" in forecast.model_windows
    assert forecast.ecmwf_apart is True and "wind" in forecast.ecmwf_apart_fields


def test_published_tier_follows_ecmwf_while_v1_stays_on_the_stitch() -> None:
    from pipeline.build import build_record
    from pipeline.contracts import Game, WeatherForecast
    from pipeline.model.signals import HIGH, NO

    kick = datetime(2026, 10, 18, 18, tzinfo=UTC)
    game = Game(
        game_id="nfl:2026:6:a@b", sport="nfl", season=2026, week=6,
        kickoff_utc=kick, kickoff_local=kick, tz="UTC",
        home_id="b", away_id="a", stadium_id=None, roof_state="open",
    )
    stitch_only = WeatherForecast(
        "nfl:2026:6:a@b", "nbm", temp_fg=70.0, wind_fg=6.0, rain_fg_mm=0.0,
    )
    forecast = WeatherForecast(
        "nfl:2026:6:a@b", "nbm", temp_fg=70.0, wind_fg=6.0, rain_fg_mm=0.0,
        signal_source="ecmwf_ifs025", signal_temp_f=40.0, signal_wind_mph=18.0, signal_rain_mm=0.0,
    )
    _record, stitch_impact, stitch_sig, _flags = build_record(
        "nfl", game, None, None, None, stitch_only, roof_state="open",
    )
    _record, impact, sig, flags = build_record("nfl", game, None, None, None, forecast, roof_state="open")
    assert impact.gs_fg_pct == stitch_impact.gs_fg_pct
    assert stitch_sig.level == NO
    assert sig.level == HIGH and "NFL Wind" in flags


def test_optional_temp_gate_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROCESS_TEMP_GATE", raising=False)
    monkeypatch.delenv("PROCESS_TEMP_GATE_NFL", raising=False)
    monkeypatch.delenv("PROCESS_TEMP_GATE_CFB", raising=False)
    assert P.temp_gate("nfl") == (False, 65.0)
    assert P.evaluate_card(_card(wind_fg=16, temp_fg=80))["alert"] is True
    monkeypatch.setenv("PROCESS_TEMP_GATE", "1")
    blocked = P.evaluate_card(_card(wind_fg=16, temp_fg=65))
    assert blocked["alert"] is False and blocked["temp_gate"] is True
    assert P.evaluate_card(_card(wind_fg=16, temp_fg=64.9))["alert"] is True
    monkeypatch.setenv("PROCESS_TEMP_GATE_NFL", "0")
    monkeypatch.setenv("PROCESS_TEMP_GATE_CFB", "70")
    assert P.evaluate_card(_card(wind_fg=16, temp_fg=80))["alert"] is True
    cfb = _card(wind_fg=16, temp_fg=72)
    cfb["sport"] = "cfb"
    cfb["consensus"] = {"spread_open": 3}
    assert P.evaluate_card(cfb)["alert"] is False
    cfb["weather"]["temp_fg"] = 69
    assert P.evaluate_card(cfb)["alert"] is True


def test_dashboard_constants_match_the_python_screen() -> None:
    src = (WEB / "discovery.js").read_text(encoding="utf8")
    assert "const PROCESS_TEMP_GATE = false;" in src
    for name, value in (
        ("PROCESS_TEMP_MAX_F", C.PROCESS_TEMP_MAX_F),
        ("PROCESS_WIND_WATCH_MPH", C.PROCESS_WIND_WATCH_MPH),
        ("PROCESS_WIND_FLAG_MPH", C.PROCESS_WIND_FLAG_MPH),
        ("PROCESS_RAIN_IGNORE_MM", C.PROCESS_RAIN_IGNORE_MM),
        ("PROCESS_RAIN_WATCH_MM", C.PROCESS_RAIN_WATCH_MM),
        ("PROCESS_RAIN_SIGNAL_MM", C.PROCESS_RAIN_SIGNAL_MM),
        ("PROCESS_RAIN_AGREE_TOL_MM", C.PROCESS_RAIN_AGREE_TOL_MM),
        ("PROCESS_UNDER_PROB_MOVE_MAX", C.PROCESS_UNDER_PROB_MOVE_MAX),
        ("WARM_WIND_LOW_MPH", C.PROCESS_WARM_WIND_LOW_MPH),
        ("WARM_WIND_MID_MPH", C.PROCESS_WARM_WIND_MID_MPH),
        ("WARM_WIND_HIGH_MPH", C.PROCESS_WARM_WIND_HIGH_MPH),
    ):
        assert f"const {name} = {int(value) if value >= 1 else value};" in src
