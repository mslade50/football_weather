-- Phase 5: v2 impact model side by side with v1 (ARCHITECTURE.md §7.5).
-- games carries the latest impact percentages per model so the Table / Backtest views
-- can compare v1 vs v2 without replaying weather_history (which already has gs_fg_v2/away_fg_v2).
-- Written by pipeline/outputs/d1_out.py game_rows() via ON CONFLICT DO UPDATE.
ALTER TABLE games ADD COLUMN gs_fg REAL;
ALTER TABLE games ADD COLUMN away_fg REAL;
ALTER TABLE games ADD COLUMN gs_fg_v2 REAL;
ALTER TABLE games ADD COLUMN away_fg_v2 REAL;
