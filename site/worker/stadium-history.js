// Frozen aggregate research input, independent of current board / quote clocks.
import nfl from '../../data/raw/nfl_stadium_signal_history.json' with {type: 'json'};

// Audited against data/stadiums.csv nflverse_stadium_id, never home-team names.
export const NFL_VENUES = {
  'state-farm-stadium':'PHO00', 'mercedes-benz-stadium':'ATL97', 'highmark-stadium':'BUF00',
  'bank-of-america-stadium':'CAR00', 'soldier-field':'CHI98', 'paycor-stadium':'CIN00',
  'huntington-bank-field':'CLE00', 'att-stadium':'DAL00', 'empower-field-at-mile-high':'DEN00',
  'lambeau-field':'GNB00', 'nrg-stadium':'HOU00', 'lucas-oil-stadium':'IND00',
  'everbank-stadium':'JAX00', 'geha-field-at-arrowhead-stadium':'KAN00', 'hard-rock-stadium':'MIA00',
  'gillette-stadium':'BOS00', 'metlife-stadium':'NYC01', 'lincoln-financial-field':'PHI00',
  'acrisure-stadium':'PIT00', 'lumen-field':'SEA00', 'levis-stadium':'SFO01',
  'raymond-james-stadium':'TAM00', 'nissan-stadium':'NAS00', 'northwest-stadium':'WAS00',
};

export function stadiumHistoryPayload() {
  return {schema_version:1, kind:'frozen_aggregate_research',
    leagues:{nfl:{...nfl, venues:NFL_VENUES, venue_valid_through:{BUF00:2025},
      source_sha256:'99053e1daddcf3138685534ddbea31a55f518cd469a320efe1c4db377a958950'}}};
}
