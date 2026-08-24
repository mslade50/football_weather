"""All model constants in one place (ARCHITECTURE §7.1, §7.3, §7.4).

v1 constants are frozen: they reproduce the legacy gs_fg/away_fg exactly and are
never refit. Values are in PERCENT; legacy NFL output divides by 100.
"""

from __future__ import annotations

import os

MODEL_VERSION_V1 = "v1"
MODEL_VERSION_V2 = "v2"
MODEL_VERSIONS = (MODEL_VERSION_V1, MODEL_VERSION_V2)
# Which model feeds fair lines / edges / Telegram alerts. Promotion to "v2" is a
# manual edit here (Phase 6 gate: v2 CLV >= v1 over >=4 weeks); the env var is a
# dry-run override for local comparison only.
ALERT_MODEL = os.environ.get("ALERT_MODEL", "v1").strip().lower() or "v1"
if ALERT_MODEL not in MODEL_VERSIONS:
    ALERT_MODEL = "v1"


def alert_model() -> str:
    """Model version used for alerts; read at call time so tests can monkeypatch ``ALERT_MODEL``."""
    return ALERT_MODEL if ALERT_MODEL in MODEL_VERSIONS else MODEL_VERSION_V1


# ---- v2 impact defaults (ARCH §7.5); data/calibration.json overrides these ----
V2_DEFAULTS: dict[str, float] = {
    "gust_blend": 0.7,        # w_eff = gust_blend*wind + (1-gust_blend)*gust
    "head_weight": 0.5,       # w_dir = sqrt(cross^2 + head_weight*head^2)
    "wind_offset_mph": 10.0,  # wind_c2 = min(cap, coeff*max(0, w_dir-offset)^exp)
    "wind_coeff": 0.56,
    "wind_exp": 1.02,
    "wind_cap": 12.0,
    "dir_mult_weak": 0.5,
    "rain_prob_min": 0.4,
    "alt_base_m": 800.0,
    "alt_slope_per_m": 0.0035,
    "alt_cap": 3.5,
    "heat_away_delta_f": 12.0,
}

# ---- v1 impact (AUDIT §5; boundaries verified against the full golden history) ----
SPORTS = ("nfl", "cfb")
# (threshold, component) — first match walking descending thresholds.
WIND_TIERS: list[tuple[float, float]] = [(25.0, 10.0), (17.0, 6.5), (15.0, 3.5), (12.0, 2.0)]
COLD_BASE_F = 30.0
COLD_PER_F = 0.125
HEAT_BASE_F = 80.0
HEAT_PER_F = 0.125
# Legacy rain tiers: >12 -> 6.5, >=6 -> 3.0, >1 -> 1.5 (golden: 12.0->3.0 / 12.1->6.5
# and 1.00->0 / 1.05->1.5). v2's tier() treats every threshold as >=; v1 applies
# RAIN_TIER_STRICT_MM to make 12.0 and 1.0 exclusive.
RAIN_TIERS_MM: list[tuple[float, float]] = [(12.0, 6.5), (6.0, 3.0), (1.0, 1.5)]
RAIN_TIER_STRICT_MM = {12.0, 1.0}
# Suppression keys on the RUN month (the generator's clock), not the game month:
# September runs zeroed rain even for early-October kickoffs.
RAIN_SUPPRESS_MONTHS = {9}
# heat_away: NFL fires on home_temp - away_temp >= 10 in every era (interval
# (8.31, 11.37]). CFB used the same delta rule until the 2024-09-26 run, then
# switched to away_temp < 54.
HEAT_AWAY_DELTA_F = 10.0
HEAT_AWAY_CUTOFF_F: dict[str, float] = {"cfb": 54.0}
CFB_HEAT_AWAY_DELTA_UNTIL = "2024-09-27"  # first run date under the cutoff rule
COLD_AWAY_BASE_F = 32.0
COLD_AWAY_PER_F = 0.125
# cold_away away-temp floor: CFB 65 in every era; NFL 65 through Dec 2025, then 60
# from Jan 2026 (interval (57.04, 61.89] — 61.89 fires, 57.04 does not).
COLD_AWAY_AWAY_TEMP_MIN_F: dict[str, float] = {"nfl": 60.0, "cfb": 65.0}
NFL_COLD_AWAY_LEGACY_MIN_F = 65.0
NFL_COLD_AWAY_ERA = "2026-01-01"  # runs before this date use the 65 floor
# NFL 3.5 tier fires at 1283 m (Denver from ~sea level); interval (929, 1283].
ALT_TIERS_M: dict[str, list[tuple[float, float]]] = {
    "nfl": [(1283.0, 3.5), (900.0, 2.0)],
    "cfb": [(1000.0, 3.5)],
}
# CFB second tier: travel_alt >= 700 AND home-stadium elevation >= 1100 m -> 2.0
# (travel interval (669.9, 701.3]; elevation interval (976, 1184); 0/34319 golden
# violations). Skipped when home elevation is unknown.
CFB_ALT2_C = 2.0
CFB_ALT2_TRAVEL_MIN_M = 700.0
CFB_ALT2_HOME_ELEV_MIN_M = 1100.0
CLOSED_ROOF_STATES = {"dome", "closed"}

# Documented ambiguous bands (mismatches inside these are logged, not failed).
AMBIGUOUS_BANDS = {
    "rain_mm": (5.95, 6.05),             # tier-2 boundary sits exactly at 6.0
    "heat_away_delta_f": (8.31, 11.37),  # delta threshold interval (10.0 chosen)
    "alt_nfl_2_0_m": (800.0, 929.0),     # 2.0-tier lower edge unobserved
    "alt_nfl_3_5_m": (929.0, 1283.0),    # 3.5-tier interval (1283.0 chosen)
    "cfb_alt2_travel_m": (669.9, 701.3),
    "cfb_alt2_home_elev_m": (976.0, 1184.0),
}

# ---- signals (§7.4) ----------------------------------------------------------
NFL_WIND_VOL_LOW_BELOW = 11.99
CFB_DOW_LOW_WIND: dict[int, float] = {0: 11.14, 1: 11.14, 2: 10.10, 3: 10.10, 4: 9.31, 5: 8.79, 6: 11.93}
CFB_DOW_DEFAULT = 10.0
CFB_HIGH_OFFSET = 7.5
SIGNAL_SIZES: dict[str, int] = {
    "No Impact": 7,
    "Low Impact": 15,
    "Mid Impact": 25,
    "High Impact": 40,
    "Very High Impact": 50,
}
COMBINED_COLORS: dict[str, str] = {
    "CFB Wind": "purple",
    "NFL Wind": "blue",
    "Heat": "red",
    "Alt+Heat": "saddlebrown",
}
DOT_SIZE_SLOPE = 4.0
DOT_SIZE_BASE = 7.0

# ---- consensus / edges (§7.3; used from Phase 2) ------------------------------
BOOK_WEIGHTS: dict[str, float] = {
    "pinnacle": 3.0,
    "betonline": 2.0,
    "betcris": 1.5,
    "fanduel": 1.0,
    "draftkings": 1.0,
    "kalshi": 1.0,
    "novig": 1.0,
    "prophetx": 0.75,
}
PTS_PROB_TOTAL: dict[str, float] = {"nfl": 0.026, "cfb": 0.020}
EDGE_THRESH: dict[str, dict[str, float]] = {
    "nfl": {"total": 1.5, "spread": 1.0},
    "cfb": {"total": 2.5, "spread": 1.5},
}
STRONG_THRESH: dict[str, dict[str, float]] = {
    "nfl": {"total": 2.5, "spread": 1.5},
    "cfb": {"total": 4.0, "spread": 2.5},
}
WATCH_FRACTION = 0.6
WIND_VOL_STATIC_TO_FC: dict[str, float] = {"low": 0.2, "mid": 0.5, "high": 0.75, "very high": 1.0}
WEATHER_DRIVEN_GS_MAX = -3.5
WEATHER_DRIVEN_AWAY_MAX = -2.0
