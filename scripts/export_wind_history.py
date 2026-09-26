"""Export the existing historical replay caches for alert context (no network).

python -m scripts.export_wind_history --output data/backtest/wind-history-v1.json
Source caches were produced by backtest/historical-replay, commit 0a3dd68.
Keep the generated JSON in R2 backtest/, never in Git or mutable board state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pipeline.backtest import grade_under
from pipeline.schedule.cfb import parse_cfbd_games
from pipeline.schedule.nfl import parse_nflverse_games
from pipeline.stadiums import load_stadium_book
from utils.timeutil import now_utc


def export_history(cache: Path, data_dir: Path) -> dict:
    import pandas as pd

    book = load_stadium_book(data_dir=data_dir)
    hist_path, actual_path = cache / "hist_games.parquet", cache / "stadium_wx_games.parquet"
    # pandas JSON conversion normalizes NaN/numpy scalars before strict JSON output.
    forecast = json.loads(pd.read_parquet(hist_path).to_json(orient="records"))
    actual = json.loads(pd.read_parquet(actual_path).to_json(orient="records"))
    sources = [hist_path, actual_path, cache / "git/nflverse_games.csv"]
    schedule = {}
    for season in sorted({r["season"] for r in forecast}):
        path = cache / f"git/cfbd_games_{season}.json"
        sources.append(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        # The general schedule parser may fall back to a venue name; disallow that
        # here because names such as Memorial Stadium are not unique.
        verified = [r for r in payload if str(r.get("venueId", r.get("venue_id"))) in book.cfbd_venue_index]
        games = parse_cfbd_games(verified, season=season,
                                 book=book, include_final=True)
        games += parse_nflverse_games(sources[2].read_text(encoding="utf-8"), season,
                                      book=book, include_final=True)
        schedule.update({g.game_id: g for g in games})
    rows, observed = [], []
    skipped = {"unresolved_venue": 0, "no_closing_forecast": 0}
    for row in forecast:
        game = schedule.get(row["game_id"])
        # Never resolve by stadium name alone: several CFB venues share a name.
        stadium = book.stadiums.get(game.stadium_id) if game else None
        if stadium is None:
            skipped["unresolved_venue"] += 1
            continue
        lead = row.get("close_lead_h")
        if lead is None or lead < 0 or row.get("wind_fc") is None or row.get("temp_fc") is None:
            skipped["no_closing_forecast"] += 1
            continue
        roof = game.roof_state or {"open": "outdoors", "dome": "dome"}.get(stadium.roof_type)
        rows.append(normalize_row(row, stadium.stadium_id, roof, forecast=True))
    for row in actual:
        if row.get("stadium_id") and row.get("wind_act") is not None:
            observed.append(normalize_row(row, row["stadium_id"], row.get("roof_state"), forecast=False))
    return {"schema_version": 1, "meta": {
        "generated_at": now_utc().isoformat(), "source_commit": "0a3dd68c9e1c246c464bfdc96967325eecc5d392",
        "forecast_basis": "2024-25 archived closing forecasts and closing totals",
        "actual_basis": "2015-24 ERA5 game-time wind and closing totals; descriptive, not forecast signals",
        "source_sha256": {str(p.relative_to(cache)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        "forecast_skipped": skipped, "forecast_games": len(rows), "actual_games": len(observed),
    }, "games": rows, "actual_games": observed}


def normalize_row(row: dict, stadium_id: str, roof: str | None, *, forecast: bool) -> dict:
    total = row.get("close_total" if forecast else "total_close")
    result = grade_under(total, row.get("actual_total"))
    if result != row.get("close_result"):
        raise ValueError(f"Closing grade mismatch for {row['game_id']}")
    return {**{k: row.get(k) for k in ("game_id", "sport", "season", "kickoff_utc", "spread_open")},
            "stadium_id": stadium_id, "roof_state": roof, "total_close": total, "under_result": result,
            **({"wind_fc": row.get("wind_fc"), "temp_fc": row.get("temp_fc")} if forecast else
               {"wind_act": row.get("wind_act")})}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/backtest"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = export_history(args.cache_dir, args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(payload["meta"], indent=2))


if __name__ == "__main__":
    main()
