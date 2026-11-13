-- Local shadow schema (v4), deliberately NOT a production D1 migration.
-- Change-only: a quote/forecast/game row is written only when its semantic value
-- differs from the latest logged one; `coverage` rows say which scrapes saw
-- what, so "still live and unchanged at T" is derived, not stored. Raw provider
-- payloads live in the raw captures (R2), referenced here by capture/run id.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    attempted TEXT NOT NULL, successful TEXT NOT NULL,
    books TEXT NOT NULL, sports TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_clock ON runs(finished_at);
-- One row per odds receipt; quote and coverage rows carry the small integer.
CREATE TABLE IF NOT EXISTS captures (
    cap INTEGER PRIMARY KEY, capture_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_observations (
    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    run_id TEXT NOT NULL, kickoff TEXT NOT NULL, sport TEXT NOT NULL,
    season INTEGER NOT NULL, week INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS game_asof ON game_observations(game_id, observed_at);
CREATE INDEX IF NOT EXISTS game_clock ON game_observations(observed_at, id);
-- Main lines only (alternates stay in the raw captures). quote_at is NULL for a
-- quote (it equals observed_at) and the original quote clock for a withdrawal.
CREATE TABLE IF NOT EXISTS quote_observations (
    game_id TEXT NOT NULL, sport TEXT NOT NULL, book TEXT NOT NULL,
    market TEXT NOT NULL, side TEXT NOT NULL, line REAL, odds INTEGER, prob_raw REAL,
    observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, quote_at TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('quote', 'withdrawn')), cap INTEGER NOT NULL,
    expires_at TEXT, actionable_until TEXT,
    opening_line REAL, opening_odds INTEGER, opened_at TEXT, opening_source TEXT
);
CREATE INDEX IF NOT EXISTS quote_asof ON quote_observations(game_id, book, market, side, observed_at, recorded_at);
CREATE INDEX IF NOT EXISTS quote_head ON quote_observations(book, sport, game_id, market, side, recorded_at);
-- keys NULL: the scrape succeeded in full and confirms every live quote of the
-- (book, sport) scope. keys set: a partial scrape; it confirms only the listed
-- (game, side) pairs (zlib JSON) and never withdraws.
CREATE TABLE IF NOT EXISTS coverage (
    cap INTEGER NOT NULL, book TEXT NOT NULL, sport TEXT NOT NULL, market TEXT NOT NULL,
    observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, keys BLOB
);
CREATE INDEX IF NOT EXISTS coverage_scope ON coverage(book, sport, market, recorded_at);
CREATE TABLE IF NOT EXISTS forecast_observations (
    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, source TEXT NOT NULL,
    model TEXT NOT NULL, run_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
    run_time TEXT, lead_hours REAL, valid_start TEXT, valid_end TEXT,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_asof ON forecast_observations(game_id, source, model, fetched_at);
