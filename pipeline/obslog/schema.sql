-- Local shadow schema, deliberately NOT a production D1 migration.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    attempted TEXT NOT NULL, successful TEXT NOT NULL,
    books TEXT NOT NULL, sports TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_observations (
    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    run_id TEXT NOT NULL, kickoff TEXT NOT NULL, sport TEXT NOT NULL,
    season INTEGER NOT NULL, week INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS game_asof ON game_observations(game_id, observed_at);
CREATE TABLE IF NOT EXISTS quote_observations (
    id TEXT PRIMARY KEY, capture_id TEXT NOT NULL, run_id TEXT NOT NULL,
    game_id TEXT NOT NULL, sport TEXT NOT NULL, book TEXT NOT NULL,
    market TEXT NOT NULL, side TEXT NOT NULL, line REAL, odds INTEGER,
    prob_raw REAL, is_main INTEGER NOT NULL,
    observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, quote_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('quote', 'withdrawn')),
    source_updated_at TEXT, expires_at TEXT, actionable_until TEXT, source_id TEXT,
    opening_line REAL, opening_odds INTEGER, opened_at TEXT, opening_source TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quote_asof ON quote_observations(game_id, book, market, side, observed_at, recorded_at);
CREATE INDEX IF NOT EXISTS quote_capture ON quote_observations(capture_id, game_id, book, market);
CREATE TABLE IF NOT EXISTS forecast_observations (
    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, source TEXT NOT NULL,
    model TEXT NOT NULL, run_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
    run_time TEXT, lead_hours REAL, valid_start TEXT, valid_end TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_asof ON forecast_observations(game_id, source, model, fetched_at);
-- v2: whole-log clock scans and per-key series must not sort every receipt.
CREATE INDEX IF NOT EXISTS quote_clock ON quote_observations(observed_at, recorded_at, id);
CREATE INDEX IF NOT EXISTS forecast_clock ON forecast_observations(fetched_at, id);
CREATE INDEX IF NOT EXISTS game_clock ON game_observations(observed_at, id);
CREATE INDEX IF NOT EXISTS run_clock ON runs(finished_at);
