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
    assert P.wind_band(10) == "none"
    assert P.wind_band(10.01) == "consider"
    assert P.wind_band(12) == "consider"
    assert P.wind_band(12.01) == "ideal"
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
    only_ecmwf = {P.ECMWF_IFS: {"rain_mm": 30.0}, P.ECMWF_AIFS: {"rain_mm": 28.0}}
    assert P.rain_agreement(only_ecmwf)["status"] == "unverified"


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
    assert one["basis"] == P.BASIS_SINGLE and one["price_blocked"] is False


def _card(**weather: float) -> dict:
    return {
        "sport": "nfl",
        "stadium": {"roof_type": "open", "roof_state": "outdoors", "orient_deg": 0},
        "weather": {"wind_fg": 4.0, "temp_fg": 50.0, "rain_fg": 0.0, "wind_dir_deg": 90.0, **weather},
        "consensus": {},
        "odds": {},
    }


def test_evaluate_card_shared_gates_for_both_sports() -> None:
    cold_wind = P.evaluate_card(_card(wind_fg=11, temp_fg=64.9))
    assert cold_wind["alert"] is True and cold_wind["wind_band"] == "consider"
    assert cold_wind["crosswind_factor"] == pytest.approx(1.0)
    assert P.evaluate_card(_card(wind_fg=10, temp_fg=50))["alert"] is False
    assert P.evaluate_card(_card(wind_fg=18, temp_fg=65))["alert"] is False
    ideal = P.evaluate_card(_card(wind_fg=12.5, temp_fg=40))
    assert ideal["wind_band"] == "ideal" and ideal["alert"] is True
    watch = P.evaluate_card(_card(wind_fg=3, temp_fg=40, rain_fg=8))
    assert watch["alert"] is False and watch["watch"] is True and watch["rain_tier"] == "watch"
    cfb = _card(wind_fg=16, temp_fg=50)
    cfb["sport"] = "cfb"
    cfb["consensus"] = {"spread_open": 10.5}
    assert P.evaluate_card(cfb)["alert"] is False
    cfb["consensus"] = {"spread_open": 3}
    assert P.evaluate_card(cfb)["alert"] is True


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


def test_dashboard_constants_match_the_python_screen() -> None:
    src = (WEB / "discovery.js").read_text(encoding="utf8")
    for name, value in (
        ("PROCESS_TEMP_MAX_F", C.PROCESS_TEMP_MAX_F),
        ("PROCESS_WIND_CONSIDER_MPH", C.PROCESS_WIND_CONSIDER_MPH),
        ("PROCESS_WIND_IDEAL_MPH", C.PROCESS_WIND_IDEAL_MPH),
        ("PROCESS_RAIN_IGNORE_MM", C.PROCESS_RAIN_IGNORE_MM),
        ("PROCESS_RAIN_WATCH_MM", C.PROCESS_RAIN_WATCH_MM),
        ("PROCESS_RAIN_SIGNAL_MM", C.PROCESS_RAIN_SIGNAL_MM),
        ("PROCESS_RAIN_AGREE_TOL_MM", C.PROCESS_RAIN_AGREE_TOL_MM),
        ("PROCESS_UNDER_PROB_MOVE_MAX", C.PROCESS_UNDER_PROB_MOVE_MAX),
    ):
        assert f"const {name} = {int(value) if value >= 1 else value};" in src
