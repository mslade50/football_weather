-- Run ledger + per-stadium backtest results (ARCHITECTURE.md §4.3).
-- runs: one row per pipeline.build run (ON CONFLICT DO UPDATE); feeds
-- /api/runs, /api/status and board/status.json. stadium_results: Phase 6.
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, sport TEXT, season INTEGER, week INTEGER, git_sha TEXT, scope TEXT,
  started_at TEXT, finished_at TEXT, duration_s REAL, status TEXT,
  stage_timings_json TEXT, counts_json TEXT, degradations_json TEXT, unresolved_json TEXT,
  n_games INTEGER, n_lines INTEGER, n_alerts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS stadium_results (
  stadium_id TEXT NOT NULL, sport TEXT NOT NULL, season INTEGER NOT NULL,
  under_w INTEGER, under_l INTEGER, under_p INTEGER, roi REAL, n INTEGER, updated_at TEXT,
  PRIMARY KEY (stadium_id, sport, season)
);
