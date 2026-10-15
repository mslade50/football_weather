"""Schedule producers -> physical venue -> publication/expiry -> browser regressions.

No test supplies venue_provenance: it must originate in each schedule parser.
"""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pipeline.contracts import GameLine, WeatherForecast
from pipeline.current_quotes import publication_guard
from pipeline.model.total_prices import compare_totals
from pipeline.outputs import json_out
from pipeline.schedule.cfb import parse_cfbd_games
from pipeline.schedule.espn import parse_espn_scoreboard
from pipeline.schedule.nfl import parse_nflverse_games
from pipeline.stadiums.loader import load_stadium_book

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def test_schedule_publication_expiry_and_ui(tmp_path, monkeypatch):
    book = load_stadium_book(ROOT / "data")
    cfb = [{"id": i, "season": 2026, "week": 7, "startDate": "2026-10-18T17:00:00Z",
            "homeTeam": home, "awayTeam": "Texas", "homeClassification": "fbs",
            "venueId": vid, "venue": name, "neutralSite": neutral}
           for i, (home, vid, name, neutral) in enumerate([
               ("Wisconsin", 347, "Camp Randall Stadium", False),
               ("Oklahoma", 499, "Cotton Bowl", True),
               ("Alabama", 3699, "Bryant-Denny Stadium", False),
               ("Michigan", 999999, "Unknown explicit neutral venue", True)])]
    nfl = "game_id,season,week,gameday,gametime,home_team,away_team,stadium_id,stadium,roof\n" + "\n".join(
        f"fixture_{i},2026,7,2026-10-18,13:00,{home},SEA,{sid},{name},outdoors"
        for i, (home, sid, name) in enumerate([
            ("CLE", "CLE00", "Huntington Bank Field"), ("GB", "GNB00", "Lambeau Field"),
            ("CHI", "CHI98", "Soldier Field"), ("DAL", "DAL00", "AT&T Stadium")]))
    games = parse_cfbd_games(cfb, 2026, book=book) + parse_nflverse_games(nfl, 2026, book=book)
    fallback = json.loads((ROOT / "tests/fixtures/raw/espn/scoreboard_cfb_sample.json").read_text())
    games += parse_espn_scoreboard(fallback, "cfb", 2025, book=book)[:1]
    cards = {"nfl": [], "cfb": []}
    monkeypatch.setattr(json_out, "compare_totals", lambda sport, lines, fair: compare_totals(sport, lines, fair, now=NOW))
    fair = SimpleNamespace(fair_total=46, total=SimpleNamespace(thin=False, prob=.5), edges=[])
    for game in games:
        rg = book.resolve(game)
        fc = WeatherForecast(game.game_id, "fixture", NOW, 72, temp_fg=75, wind_fg=2)
        # A sportsbook expires first; exchange clocks are independent. The second
        # exchange is already stale but remains descriptive source evidence.
        lines = [GameLine(game.sport, game.game_id, b, "total", "under", -110, 46.5,
                          scraped_at=NOW - timedelta(seconds=age),
                          expires_at=NOW + timedelta(seconds=ttl), prob_raw=.52 if b == "kalshi" else None)
                 for b, age, ttl in [("betcris", 10, 5), ("kalshi", 10, 120), ("novig", 180, -10)]]
        card = json_out.build_card(game.sport, game, rg.stadium, rg.home_team, rg.away_team,
                                   fc, None, None, [], lines=lines, run_id="publication-regression")
        card["consensus"].update(total_now=46.5, spread_open=3)
        # Evaluate posted references using the comparator with a deterministic fair.
        card["total_prices"] = compare_totals(game.sport, lines, fair, now=NOW)
        cards[game.sport].append(card)
    json_out.write_board(tmp_path, cards, {"run_id": "publication-regression", "sport_counts": {"nfl": 4, "cfb": 5}})
    original = {s: json.loads((tmp_path / f"games_{s}.json").read_text()) for s in cards}
    publication_guard(tmp_path, NOW + timedelta(seconds=6))
    guarded = {s: json.loads((tmp_path / f"games_{s}.json").read_text()) for s in cards}
    assert all(c["venue_provenance"]["source_id"] for p in original.values() for c in p["games"])
    assert all(c["total_prices"]["quote_evidence"] for p in guarded.values() for c in p["games"])
    assert guarded["cfb"]["games"][0]["total_prices"]["best_under"] is None
    result = subprocess.run([shutil.which("node") or "node", "tests/helpers/dashboard-publication.mjs"],
                            input=json.dumps({"original": original, "guarded": guarded}), text=True,
                            capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
