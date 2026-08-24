-- football-odds D1 schema (ARCHITECTURE.md §4.3). Applied by
-- `npx wrangler d1 migrations apply football-odds --remote` (deploy.yml).
-- Pipeline writes: INSERT OR IGNORE (history/openers) or
-- INSERT ... ON CONFLICT(pk) DO UPDATE (games/stadiums/teams), chunked <=100 rows.

CREATE TABLE IF NOT EXISTS stadiums (
  stadium_id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, state TEXT, country TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL, elevation_m REAL, timezone TEXT,
  orientation_deg REAL, orientation_bucket TEXT, orientation_src TEXT,
  roof_type TEXT CHECK(roof_type IN ('open','dome','retractable')), surface TEXT,
  capacity INTEGER, year_built INTEGER,
  avg_wind_static REAL, wind_vol_static TEXT, wind_impact_static TEXT, weakest_wind_effect TEXT,
  avg_wind_sep REAL, avg_wind_oct REAL, avg_wind_nov REAL, avg_wind_dec REAL, avg_wind_jan REAL,
  avg_temp_f REAL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  team_id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL, short TEXT,
  home_stadium_id TEXT REFERENCES stadiums(stadium_id), avg_temp_f REAL, conference TEXT, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY, sport TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL,
  kickoff_utc TEXT NOT NULL, kickoff_local TEXT, tz TEXT,
  home_id TEXT NOT NULL, away_id TEXT NOT NULL, stadium_id TEXT, neutral INTEGER DEFAULT 0,
  roof_state TEXT, status TEXT, source TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_week ON games(sport, season, week);
CREATE INDEX IF NOT EXISTS idx_games_kick ON games(kickoff_utc);

-- change-only: insert when any of wind/gust/temp/precip/pop/gs_fg moved vs last row for (game_id, source)
CREATE TABLE IF NOT EXISTS weather_history (
  game_id TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL, run_id TEXT NOT NULL,
  lead_hours REAL, temp_f REAL, wind_mph REAL, gust_mph REAL, wind_dir_deg REAL, wind_dir TEXT,
  precip_mm REAL, precip_prob REAL, wind_vol REAL, wind_p10 REAL, wind_p90 REAL, cross_mph REAL, head_mph REAL,
  gs_fg REAL, away_fg REAL, gs_fg_v2 REAL, away_fg_v2 REAL, model_version TEXT,
  PRIMARY KEY (game_id, source, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_wx_game ON weather_history(game_id, fetched_at);

-- change-only: first row per (game_id, book, market, side) = opener
CREATE TABLE IF NOT EXISTS odds_history (
  scraped_at TEXT NOT NULL, game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, prob REAL, fair_line REAL, fair_prob REAL, edge_pts REAL, edge_prob REAL,
  is_main INTEGER DEFAULT 1, run_id TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side, scraped_at)
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_history(game_id, market, scraped_at);
CREATE INDEX IF NOT EXISTS idx_odds_time ON odds_history(scraped_at);
CREATE INDEX IF NOT EXISTS idx_odds_book ON odds_history(book, scraped_at);

CREATE TABLE IF NOT EXISTS openers (
  game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, seen_at TEXT NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side)
);

CREATE TABLE IF NOT EXISTS closings (
  game_id TEXT NOT NULL, book TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
  line REAL, odds INTEGER, scraped_at TEXT NOT NULL, kickoff_utc TEXT NOT NULL, frozen_at TEXT NOT NULL,
  PRIMARY KEY (game_id, book, market, side)
);
