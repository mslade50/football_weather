from __future__ import annotations

import pytest

from pipeline.model.signals import (
    HIGH,
    LOW,
    MID,
    NO,
    VERY_HIGH,
    cfb_low_wind_threshold,
    cfb_signal,
    combined_color,
    combined_flags,
    dot_size,
    nfl_signal,
    nfl_wind_vol,
    wind_diff,
)

MON, WED, FRI, SAT, SUN = 0, 2, 4, 5, 6


# ---- NFL ---------------------------------------------------------------------

def test_nfl_purple_first() -> None:
    s = nfl_signal(wind_fg=18.0, temp_fg=40.0, rain_fg=0.0)
    assert (s.level, s.color, s.size) == (HIGH, "purple", 40)


def test_nfl_purple_beats_rain() -> None:
    s = nfl_signal(wind_fg=16.0, temp_fg=32.0, rain_fg=5.0)
    assert s.level == HIGH


def test_nfl_low_rain() -> None:
    s = nfl_signal(wind_fg=3.0, temp_fg=75.0, rain_fg=2.1)
    assert (s.level, s.color, s.size) == (LOW, "blue", 15)


def test_nfl_low_wind_band() -> None:
    assert nfl_signal(9.0, 55.0, 0.0).level == LOW
    assert nfl_signal(8.0, 55.0, 0.0).level == NO
    assert nfl_signal(9.0, 60.0, 0.0).level == NO


def test_nfl_mid() -> None:
    s = nfl_signal(wind_fg=16.0, temp_fg=50.0, rain_fg=0.0)
    assert (s.level, s.color, s.size) == (MID, "orange", 25)
    assert nfl_signal(15.0, 50.0, 0.0).level == NO  # strict >15 and not in 8<w<15


def test_nfl_none_inputs() -> None:
    s = nfl_signal(None, None, None)
    assert (s.level, s.color, s.size) == (NO, "green", 7)
    assert nfl_signal(float("nan"), 40.0, 3.0).level == LOW


def test_nfl_wind_vol_override() -> None:
    assert nfl_wind_vol("high", 11.98) == "Low"
    assert nfl_wind_vol("high", 11.99) == "high"
    assert nfl_wind_vol("mid", None) == "mid"


def test_wind_diff() -> None:
    assert wind_diff(12.5, 10.0) == pytest.approx(2.5)
    assert wind_diff(None, 10.0) is None


# ---- CFB ---------------------------------------------------------------------

def test_cfb_dow_thresholds() -> None:
    assert cfb_low_wind_threshold(MON) == 11.14
    assert cfb_low_wind_threshold(WED) == 10.10
    assert cfb_low_wind_threshold(FRI) == 9.31
    assert cfb_low_wind_threshold(SAT) == 8.79
    assert cfb_low_wind_threshold(SUN) == 11.93
    assert cfb_low_wind_threshold(99) == 10.0


def _cfb(wind, temp, rain=0.0, open_spread=-3.0, alt=0.0, home_temp=60.0, away_temp=60.0, weekday=SAT):
    return cfb_signal(wind, temp, rain, open_spread, alt, home_temp, away_temp, weekday)


def test_cfb_very_high() -> None:
    s = _cfb(wind=16.3, temp=45.0, open_spread=10.5, weekday=SAT)  # hi = 8.79+7.5 = 16.29
    assert (s.level, s.color, s.size) == (VERY_HIGH, "darkred", 50)
    assert _cfb(wind=16.2, temp=45.0, weekday=SAT).level == LOW  # below hi -> only low band


def test_cfb_high_spread_gate() -> None:
    assert _cfb(wind=20.0, temp=60.0, open_spread=-10.5).level == HIGH
    s = _cfb(wind=20.0, temp=60.0, open_spread=-11.0)
    assert (s.level, s.color) == (MID, "orange")
    assert _cfb(wind=20.0, temp=60.0, open_spread=-21.0).level == NO


def test_cfb_dow_shifts_boundary() -> None:
    # Sunday hi = 11.93+7.5 = 19.43; Saturday hi = 16.29.
    assert _cfb(wind=18.0, temp=45.0, weekday=SAT).level == VERY_HIGH
    assert _cfb(wind=18.0, temp=45.0, weekday=SUN).level == LOW
    assert _cfb(wind=11.5, temp=45.0, weekday=SUN).level == NO
    assert _cfb(wind=11.5, temp=45.0, weekday=MON).level == LOW


def test_cfb_mid_alt_heat() -> None:
    s = _cfb(wind=2.0, temp=76.0, alt=801.0, open_spread=20.5)
    assert s.level == MID
    assert _cfb(wind=2.0, temp=76.0, alt=800.0, open_spread=20.5).level == NO


def test_cfb_low_colors() -> None:
    rain = _cfb(wind=2.0, temp=70.0, rain=2.5)
    assert (rain.level, rain.color, rain.label) == (LOW, "black", "Low (Rain)")
    heat = _cfb(wind=2.0, temp=81.0, home_temp=56.0, away_temp=50.0)
    assert (heat.level, heat.color, heat.label) == (LOW, "red", "Low (Temp)")
    windy = _cfb(wind=9.0, temp=60.0, weekday=SAT)
    assert (windy.level, windy.color, windy.label, windy.size) == (LOW, "blue", "Low (Wind)", 15)
    # rain wins over heat for color, as in the page's nested lambda
    both = _cfb(wind=2.0, temp=81.0, rain=3.0, home_temp=50.0, away_temp=50.0)
    assert both.color == "black"


def test_cfb_missing_spread_never_matches() -> None:
    assert _cfb(wind=25.0, temp=30.0, open_spread=None).level == NO
    assert _cfb(wind=25.0, temp=30.0, open_spread=float("nan")).level == NO


# ---- combined ----------------------------------------------------------------

def test_combined_flags_cfb() -> None:
    flags = combined_flags("cfb", wind_fg=15.0, temp_fg=69.0, open_spread=-10.0, travel_alt=900.0, home_temp=50.0, away_temp=50.0)
    assert flags == ["CFB Wind"]
    flags = combined_flags("cfb", wind_fg=1.0, temp_fg=81.0, open_spread=10.0, travel_alt=900.0, home_temp=50.0, away_temp=50.0)
    assert flags == ["Heat", "Alt+Heat"]
    assert combined_flags("cfb", 1.0, 76.0, 10.1, 900.0, 60.0, 60.0) == []
    assert combined_flags("cfb", 15.0, 69.0, -10.5, 0.0, 60.0, 60.0) == []


def test_combined_flags_nfl() -> None:
    assert combined_flags("nfl", 15.1, 59.0, -3.0, 0.0, 60.0, 60.0) == ["NFL Wind"]
    assert combined_flags("nfl", 15.0, 59.0, -3.0, 0.0, 60.0, 60.0) == []
    assert combined_flags("nfl", 1.0, 81.0, -3.0, 2000.0, 56.0, 56.0) == ["Heat"]  # no Alt+Heat for NFL


def test_combined_colors_and_dot_size() -> None:
    assert combined_color("CFB Wind") == "purple"
    assert combined_color("NFL Wind") == "blue"
    assert combined_color("Heat") == "red"
    assert combined_color("Alt+Heat") == "saddlebrown"
    assert dot_size(-3.5) == pytest.approx(21.0)
    assert dot_size(None) == 7.0
