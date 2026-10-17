"""Game-window stadium climate: aggregation, sample size, and the file cap."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from pipeline.schedule.espn import parse_espn_scoreboard
from scripts.export_stadium_climate import (
    CELL_COLUMNS,
    MAX_BYTES,
    ClimateGame,
    aggregate,
    build_payload,
    encode,
    fetch_cfb_games,
    game_is_exposed,
    hour_keys,
    hourly_index,
    last_full_season,
    percentile,
    sample_game,
    season_span,
    select_cfb_games,
    select_nfl_games,
    validate_payload,
    window_index,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "raw"
UTC = timezone.utc


class _Stadium:
    def __init__(self, stadium_id: str, timezone_name: str) -> None:
        self.stadium_id = stadium_id
        self.timezone = timezone_name


class _Book:
    def __init__(self, mapping: dict[str, _Stadium]) -> None:
        self.mapping = mapping

    def find_stadium(self, key: str | None) -> _Stadium | None:
        if not key:
            return None
        return self.mapping.get(str(key).strip())

    def stadium_for_team(self, sport: str, team_id: str) -> None:
        return None

    def resolve_team(self, sport: str, raw: str, fuzzy: bool = True) -> None:
        return None


def _candidates() -> dict[str, dict[str, str]]:
    return {
        "lambeau-field": {"stadium_id": "lambeau-field", "roof_type": "open", "timezone": "America/Chicago"},
        "state-farm-stadium": {"stadium_id": "state-farm-stadium", "roof_type": "retractable", "timezone": "America/Phoenix"},
    }


def _game(**overrides: object) -> ClimateGame:
    base = dict(
        game_id="nfl:2024:5:chi@gb", sport="nfl", season=2024, stadium_id="lambeau-field",
        kickoff_utc=datetime(2024, 10, 13, 16, 0, tzinfo=UTC), timezone="America/Chicago", roof_type="open",
    )
    base.update(overrides)
    return ClimateGame(**base)  # type: ignore[arg-type]


def test_last_full_season_waits_until_march() -> None:
    assert last_full_season(date(2026, 10, 17)) == 2025
    assert last_full_season(date(2027, 2, 28)) == 2025
    assert last_full_season(date(2027, 3, 1)) == 2026
    assert season_span(date(2026, 10, 17)) == list(range(2018, 2026))


def test_windows_and_three_hour_keys() -> None:
    assert [window_index(hour) for hour in (0, 14, 15, 17, 18, 20, 21, 23)] == [0, 0, 1, 1, 2, 2, 3, 3]
    kick = datetime(2024, 1, 14, 1, 20, tzinfo=UTC)
    assert hour_keys(kick) == ["2024-01-14T01", "2024-01-14T02", "2024-01-14T03"]
    local = kick.astimezone(ZoneInfo("America/Los_Angeles"))
    assert (local.month, local.hour, window_index(local.hour)) == (1, 17, 1)


def test_exposure_keeps_open_roofs_and_only_open_retractable_games() -> None:
    assert game_is_exposed("open", "closed") is True
    assert game_is_exposed("open", None) is True
    assert game_is_exposed("retractable", "open") is True
    assert game_is_exposed("retractable", "outdoors") is True
    assert game_is_exposed("retractable", "closed") is False
    assert game_is_exposed("retractable", None) is False
    assert game_is_exposed("dome", "outdoors") is False


def test_era5_fixture_uses_the_kickoff_hour_and_the_next_two() -> None:
    payload = json.loads((FIXTURES / "openmeteo" / "era5_three_hours.json").read_text(encoding="utf-8"))
    sample = sample_game(_game(), hourly_index(payload))
    assert sample is not None
    assert sample["month"] == 10 and sample["window"] == 0
    assert sample["wind"] == pytest.approx((10 + 14 + 16) / 3)
    assert sample["temp"] == pytest.approx((55 + 54 + 53) / 3)
    assert sample["precip"] == pytest.approx(1.5)
    assert sample_game(_game(kickoff_utc=datetime(2024, 10, 13, 19, 0, tzinfo=UTC)), hourly_index(payload)) is None


def test_cells_carry_n_counts_and_do_not_rank() -> None:
    samples = [
        {"stadium_id": "lambeau-field", "season": 2018, "month": 12, "window": 3, "wind": 20.0, "temp": 30.0, "precip": 24.0},
        {"stadium_id": "lambeau-field", "season": 2019, "month": 9, "window": 0, "wind": 10.0, "temp": 60.0, "precip": 0.0},
        {"stadium_id": "lambeau-field", "season": 2020, "month": 9, "window": 0, "wind": 16.0, "temp": 50.0, "precip": 6.0},
    ]
    venues = aggregate(samples)
    rows = venues["lambeau-field"]["rows"]
    assert [row[0] for row in rows] == [9, 12]
    september, december = rows
    assert september[2] == 2 and september[11] == 1 and september[12] == 1 and september[13] == 1 and september[14] == 0
    assert december[2] == 1 and december[14] == 1
    assert september[4] == pytest.approx(13.0)
    assert venues["lambeau-field"]["n"] == 3
    assert percentile([10.0, 20.0], 0.5) == 15.0


def test_nfl_and_cfb_fixtures_keep_outdoor_finals_only() -> None:
    book = _Book({
        "GNB00": _Stadium("lambeau-field", "America/Chicago"),
        "PHO00": _Stadium("state-farm-stadium", "America/Phoenix"),
        "DET00": _Stadium("ford-field", "America/Detroit"),
        "3985": _Stadium("warren-mcguirk-alumni-stadium", "America/New_York"),
    })
    text = (FIXTURES / "nflverse" / "games_climate.csv").read_text(encoding="utf-8")
    games, stats = select_nfl_games(text, book, [2024], _candidates())
    assert sorted(game.game_id for game in games) == [
        "nfl:2024:5:chi@gb",
        "nfl:2024:5:dal@ari",
    ]
    assert stats["not_exposed"] == 1 and stats["tbd"] == 1 and stats["not_outdoor_candidate"] == 1
    lambeau = next(game for game in games if game.stadium_id == "lambeau-field")
    assert lambeau.kickoff_utc == datetime(2024, 10, 13, 16, 0, tzinfo=UTC)

    payload = json.loads((FIXTURES / "espn" / "cfb_climate_one_game.json").read_text(encoding="utf-8"))
    parsed = parse_espn_scoreboard(payload, "cfb", season=2018, book=book)
    candidates = _candidates()
    candidates["warren-mcguirk-alumni-stadium"] = {
        "stadium_id": "warren-mcguirk-alumni-stadium", "roof_type": "open", "timezone": "America/New_York",
    }
    cfb, cfb_stats = select_cfb_games(parsed, candidates)
    assert cfb_stats["kept"] == 1
    assert cfb[0].kickoff_utc.hour == 21
    local = cfb[0].kickoff_utc.astimezone(ZoneInfo("America/New_York"))
    assert (local.month, local.hour, window_index(local.hour)) == (8, 17, 1)


def test_payload_validation_rejects_thin_ranking_fields_and_oversized_output() -> None:
    samples = [{"stadium_id": "lambeau-field", "season": 2024, "month": 10, "window": 0, "wind": 13.0, "temp": 54.0, "precip": 1.5}]
    payload = build_payload(aggregate(samples), [2024], {"nfl": {}}, {"games": 1}, "2026-10-17T00:00:00Z")
    assert validate_payload(payload, [2024]) == []
    encoded = encode(payload)
    assert "NaN" not in encoded and encoded.endswith("\n")
    payload["venues"]["lambeau-field"]["rows"][0][12] = 9
    assert any("counts" in error for error in validate_payload(payload, [2024]))
    payload["venues"]["lambeau-field"]["rank"] = 1
    assert any("forbidden" in error for error in validate_payload(payload, [2024]))


def test_cfbd_failure_does_not_log_the_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CFBD_API_KEY", "secret-value")
    notes: list[str] = []
    with pytest.raises(RuntimeError, match="offline"):
        fetch_cfb_games([2018], None, tmp_path, True, notes.append)
    assert notes
    assert all("secret-value" not in note and "Bearer" not in note for note in notes)


def test_workflow_is_manual_and_does_not_add_a_cron() -> None:
    path = ROOT / ".github" / "workflows" / "stadium-climate.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert "workflow_dispatch" in workflow[True]
    assert "schedule" not in workflow[True]
    assert "schedule:" not in text and "crons" not in text
    runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["export"]["steps"])
    assert "python -m scripts.export_stadium_climate" in runs
    assert "git commit" not in runs and "git push" not in runs
    assert workflow["permissions"]["contents"] == "write"
    wrangler = (ROOT / "site" / "worker" / "wrangler.toml").read_text(encoding="utf-8")
    assert wrangler.count('crons = [') == 1


def test_committed_climate_json_is_real_and_under_the_cap() -> None:
    path = ROOT / "data" / "raw" / "stadium_game_climate.json"
    raw = path.read_bytes()
    assert len(raw) <= MAX_BYTES
    assert len(raw) > 20_000
    payload = json.loads(raw)
    assert payload["schema_version"] == 1
    assert payload["kind"] == "stadium_game_window_climate"
    assert payload["seasons"] == [2018, 2025]
    assert payload["season_years"] == list(range(2018, 2026))
    assert payload["cell_columns"] == list(CELL_COLUMNS)
    assert "n" in payload["cell_columns"]
    assert payload["coverage"]["games"] >= 1000
    assert payload["coverage"]["venues"] >= 100
    assert payload["coverage"]["cfb_provider"] in {"espn", "cfbd"}
    text = raw.decode("utf-8")
    assert "Bearer" not in text
    assert "CFBD_API_KEY" not in text
    assert validate_payload(payload, payload["season_years"]) == []
    assert sum(venue["n"] for venue in payload["venues"].values()) == payload["coverage"]["games"]
