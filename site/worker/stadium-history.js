// Frozen aggregate research input, independent of current board / quote clocks.
import nfl from '../../data/raw/nfl_stadium_signal_history.json' with {type: 'json'};
import cfb from '../../data/raw/cfb_stadium_signal_history.json' with {type: 'json'};

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
      coverage_by_period:{full2014_2025:'full_eligible_n',recent2022_2025:'recent_eligible_n'},
      source_sha256:'99053e1daddcf3138685534ddbea31a55f518cd469a320efe1c4db377a958950'}, cfb:cfbHistoryDataset()}};
}

export function cfbHistoryDataset() {
  const rows=Object.values(cfb.by_venue_id), slugs=new Map();
  for(const row of rows) if(row.canonical_stadium_slug)
    slugs.set(row.canonical_stadium_slug,(slugs.get(row.canonical_stadium_slug)||0)+1);
  const venues={}, held_sources={}, coverage_rows=[], record_rows=[];
  for(const row of rows) {
    const id=String(row.venue_id), slug=row.canonical_stadium_slug;
    if(row.record_validity_status==='frozen_location_conflicted_record') held_sources[id]='Venue / weather geography conflict; historical record withheld';
    if(slug && slugs.get(slug)===1 && row.mapping_status==='matched_existing_registry'
      && row.production_use_status==='descriptive_label_only' && !(row.geography_audit_issues||[]).length) venues[slug]=id;
    const full=row.periods.full_2021_2025, recent=row.periods.recent_2024_2025;
    coverage_rows.push([id,[row.venue_name],full.common_weather_games,full.covered_seasons,recent.common_weather_games,recent.covered_seasons]);
    for(const [period,stats] of Object.entries(row.periods)) if(stats.n>0)
      record_rows.push([id,'cfb_wind',period,stats.n,stats.signal_seasons,
        stats.opening_under.wins,stats.opening_under.losses,stats.opening_under.pushes,
        stats.closing_under.wins,stats.closing_under.losses,stats.closing_under.pushes]);
  }
  return {league:'CFB',research_snapshot_date:'2026-10-14',venues,held_sources,
    source_sha256:'d74cf5f7e6b3e1c5dd48f87da0015b43a16175c0a6a1e727dacdc36e90150334',
    rules:{cfb_wind:'Sustained hourly mean wind >14 mph; temperature <70°F; absolute median opening spread ≤10'},
    periods:{full_2021_2025:[2021,2025],recent_2024_2025:[2024,2025]},
    coverage_by_period:{full_2021_2025:'full_eligible_n',recent_2024_2025:'recent_eligible_n'},
    coverage_columns:nfl.coverage_columns,coverage_rows,
    record_columns:nfl.record_columns.slice(0,11),record_rows,
    audit:{full:cfb.full_reconciliation,recent:cfb.recent_reconciliation,known_venues:rows.length},
    methodology:{
      selection:'Same qualifying games for opener and close; all line movements. Full 2021–2025 and recent 2024–2025 overlap.',
      weather:'Frozen IEM airport routine observations nearest kickoff +0/+1/+2/+3h; 45-minute tolerance; unique reports; at least three valid reports. Realized weather, not a forecast or field-level wind.',
      roof:'Preserve the frozen common-hourly outdoor cohort and its roof, venue and station exclusions.',
      line_basis:'Paired same-provider-subset median opening / closing totals. Historical prices were missing and imputed at −110; no observed executions. Some medians are quarter-point synthetic score thresholds, not offered bets or Asian split-stake settlement.',
      under_outcome:'W–L–P compares final score with the corresponding synthetic median threshold; these are descriptive score-threshold outcomes.',
      interpretation:'Retrospective actual-weather labels, not validated forecast entry edges. No ROI ranking, venue effect or executable opening strategy is implied.',
      small_sample:'Every CFB signal venue has at most four full-period games; do not rank or select venues by these records.',
      geography:'Conflicted venues 3699, 3713 and 4258 are withheld. Unresolved mappings and multiple source IDs at one slug are never pooled.',
      mapping:'Audited existing registry IDs or unique exact name / alias with state and spatial guards; no fuzzy or team-home reassignment. Historical field locations and roof states are not independently surveyed.',
    }};
}
