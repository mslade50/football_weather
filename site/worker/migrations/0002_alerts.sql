-- Alerts mirror (ARCHITECTURE.md §4.3 / §10). Written by pipeline/alerts.py
-- via d1_out.py (INSERT OR IGNORE on first send, ON CONFLICT DO UPDATE after);
-- read by /api/alerts and used to rehydrate board/alerts.json when R2 is missing.
CREATE TABLE IF NOT EXISTS alerts (
  alert_key TEXT PRIMARY KEY, family TEXT NOT NULL, game_id TEXT, sport TEXT, season INTEGER, week INTEGER,
  market TEXT, side TEXT, book TEXT, tier TEXT, model_version TEXT,
  first_sent_at TEXT NOT NULL, last_sent_at TEXT NOT NULL, sends INTEGER DEFAULT 1,
  first_line REAL, first_odds INTEGER, first_fair REAL, first_edge REAL,
  last_line REAL, last_odds INTEGER, last_fair REAL, last_edge REAL,
  closing_line REAL, clv_pts REAL, status TEXT CHECK(status IN ('open','closed','settled')), run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_week ON alerts(sport, season, week);
CREATE INDEX IF NOT EXISTS idx_alerts_game ON alerts(game_id);
