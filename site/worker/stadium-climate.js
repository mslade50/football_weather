// Committed outdoor game-window climate. Independent of the frozen W-L history.
import climate from '../../data/raw/stadium_game_climate.json' with {type: 'json'};

export function stadiumClimatePayload() {
  return climate;
}
