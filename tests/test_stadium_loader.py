"""Stadium/team reference loader: seeded csvs, overrides, neutral games, domes, missing stadiums."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.contracts import Degradation, Game, make_game_id
from pipeline.run_context import RunContext
from pipeline.stadiums.loader import (
    ResolvedGame,
    StadiumBook,
    apply_overrides,
    load_stadium_book,
    normalize_alias,
    slug,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@pytest.fixture(scope="module")
def book() -> StadiumBook:
    return load_stadium_book(DATA)


def _game(sport: str, home: str, away: str, stadium_id, neutral: bool = False, season: int = 2026, week: int = 1, **kw) -> Game:
    kick = datetime(season, 9, 13, 17, 0, tzinfo=timezone.utc)
    return Game(
        game_id=make_game_id(sport, season, week, away, home),
        sport=sport,
        season=season,
        week=week,
        kickoff_utc=kick,
        kickoff_local=kick,
        tz="UTC",
        home_id=home,
        away_id=away,
        stadium_id=stadium_id,
        neutral=neutral,
        **kw,
    )


def test_seed_files_exist() -> None:
    for name in ("stadiums.csv", "stadiums_overrides.csv", "teams.csv", "aliases/nfl.json", "aliases/cfb.json"):
        assert (DATA / name).exists(), name


def test_slug_and_alias_normalisation() -> None:
    assert slug("Texas A&M") == "texas-am"
    assert slug("Miami (FL)") == "miami-fl"
    assert slug("Ole Miss") == "ole-miss"
    assert slug("Hawai'i") == "hawaii"
    assert normalize_alias("N.Y. Giants") == "nygiants"


def test_book_counts(book: StadiumBook) -> None:
    assert len(book.stadiums) >= 150
    assert not book.skipped, [r["stadium_id"] for r in book.skipped]
    nfl = book.teams_for("nfl")
    assert len(nfl) == 32
    fbs = [t for t in book.teams_for("cfb") if book.classification[("cfb", t.team_id)] == "fbs"]
    assert len(fbs) >= 130


def test_every_team_has_a_stadium(book: StadiumBook) -> None:
    missing = []
    for (sport, tid), _t in book.teams.items():
        if sport == "cfb" and book.classification[(sport, tid)] not in ("fbs", "fcs"):
            continue
        st = book.stadium_for_team(sport, tid)
        if st is None or st.needs_review:
            missing.append(f"{sport}:{tid}")
    assert not missing, missing


def test_nfl_stadium_static_columns(book: StadiumBook) -> None:
    st = book.find_stadium("BOS00")
    assert st is not None and st.stadium_id == "gillette-stadium"
    assert st.orientation_bucket == "N-S" and st.orientation_deg == pytest.approx(162.4)
    assert st.orientation_src == "osm_pitch"
    assert st.wind_vol_static == "high" and st.weakest_wind_effect == "x S"
    assert st.avg_wind_static == pytest.approx(6.23)
    assert st.year_built == 2002
    assert st.timezone == "America/New_York"
    assert book.find_stadium("Gillette Stadium") is st


def test_alias_resolution(book: StadiumBook) -> None:
    assert book.resolve_team("nfl", "n.y. giants") == "nyg"
    assert book.resolve_team("nfl", "NYG") == "nyg"
    assert book.resolve_team("nfl", "Los Angeles Chargers") == "lac"
    assert book.resolve_team("cfb", "UConn") == "connecticut"
    assert book.resolve_team("cfb", "FIU") == "florida-international"
    assert book.resolve_team("cfb", "Miami (FL)") == "miami-fl"
    assert book.resolve_team("cfb", "Miami (OH)") == "miami-oh"
    assert book.resolve_team("cfb", "Texas A&M") == "texas-am"
    assert book.resolve_team("cfb", "Ole Miss") == "ole-miss"
    assert book.resolve_team("cfb", "Texas A&M Aggiess", fuzzy=True) == "texas-am"
    assert book.resolve_team("cfb", "Nonexistent University XYZ", fuzzy=True) is None


def test_resolve_regular_home_game(book: StadiumBook) -> None:
    g = _game("nfl", "den", "mia", "DEN00")
    rg = book.resolve(g)
    assert isinstance(rg, ResolvedGame)
    assert rg.stadium is not None and rg.stadium.stadium_id == "empower-field-at-mile-high"
    assert rg.stadium_source == "game.stadium_id"
    assert rg.roof_state == "outdoors"
    assert rg.travel_alt == pytest.approx(1583.5 - 2.6, abs=1.0)  # EPQS Empower minus Miami
    assert rg.home_temp == pytest.approx(50.26) and rg.away_temp == pytest.approx(75.59)
    assert rg.penalized_side == "away"
    assert rg.game_loc == "39.743942, -105.020107"


def test_resolve_falls_back_to_home_stadium(book: StadiumBook) -> None:
    g = _game("cfb", "air-force", "navy", None)
    rg = book.resolve(g)
    assert rg.stadium is not None and rg.stadium.stadium_id == "falcon-stadium"
    assert rg.stadium_source == "home_team"
    assert rg.wind_avg == pytest.approx(7.6)  # avg_wind_sep for a September kickoff
    assert rg.travel_alt is not None and rg.travel_alt > 1900


def test_dome_zeroes_wind_avg_and_marks_roof(book: StadiumBook) -> None:
    g = _game("nfl", "det", "no", "DET00")
    rg = book.resolve(g)
    assert rg.stadium is not None and rg.stadium.is_dome
    assert rg.roof_state == "dome"
    assert rg.wind_avg == 0.0
    g2 = _game("nfl", "det", "no", "DET00", roof_state="closed")
    assert book.resolve(g2).roof_state == "closed"  # nflverse per-game roof wins


def test_retractable_roof_state_left_to_weather(book: StadiumBook) -> None:
    rg = book.resolve(_game("nfl", "dal", "was", "DAL00"))
    assert rg.stadium is not None and rg.stadium.roof_type == "retractable"
    assert rg.roof_state is None


def test_neutral_game_penalises_larger_altitude_side(book: StadiumBook) -> None:
    # Mexico City (2200 m): Minnesota (250 m) vs San Francisco (5 m) -> SF (schedule home) climbs more
    g = _game("nfl", "sf", "min", "MEX00", neutral=True)
    rg = book.resolve(g)
    assert rg.stadium is not None and rg.stadium.stadium_id == "estadio-banorte"
    assert rg.travel_alt_home == pytest.approx(2200 - 4, abs=1.0)
    assert rg.travel_alt_away == pytest.approx(2200 - 255.7, abs=1.0)
    assert rg.penalized_side == "home"
    assert rg.travel_alt == rg.travel_alt_home
    assert rg.away_temp == pytest.approx(59.94)  # SF's climate becomes the model's "away"
    assert rg.home_temp == pytest.approx(46.12)
    # legacy columns keep schedule sides
    assert rg.game.home_id == "sf" and rg.game.away_id == "min"


def test_neutral_tie_defaults_to_schedule_away(book: StadiumBook) -> None:
    g = _game("cfb", "miami-fl", "texas-am", "Mercedes-Benz Stadium", neutral=True)
    rg = book.resolve(g)
    assert rg.stadium is not None and rg.stadium.stadium_id == "mercedes-benz-stadium"
    assert rg.penalized_side in ("home", "away")
    assert rg.travel_alt == (rg.travel_alt_home if rg.penalized_side == "home" else rg.travel_alt_away)


def test_missing_stadium_degrades(book: StadiumBook) -> None:
    ctx = RunContext(sport="cfb", git_sha="test")
    g = _game("cfb", "no-such-team", "navy", "ZZZ99")
    rg = book.resolve(g, ctx)
    assert rg.stadium is None and rg.stadium_source == "none"
    comps = [d.component for d in ctx.degradations]
    assert "stadiums" in comps and "teams" in comps
    # per-game missing stadium is a warn (row still written with NaN statics), never an error
    assert not any(d.severity == "error" for d in ctx.degradations)
    assert any(d.component == "stadiums" and d.severity == "warn" for d in ctx.degradations)
    assert g.game_id in book.unresolved

    sink: list = []
    book.resolve(g, sink)
    assert sink and all(isinstance(d, Degradation) for d in sink)


def test_neutral_unknown_venue_uses_home_with_warning(book: StadiumBook) -> None:
    ctx = RunContext(sport="nfl", git_sha="test")
    rg = book.resolve(_game("nfl", "jax", "hou", "XXX00", neutral=True), ctx)
    assert rg.stadium is not None and rg.stadium_source == "home_team_neutral_fallback"
    assert any(d.component == "stadiums" and d.severity == "warn" for d in ctx.degradations)


def test_overrides_applied(book: StadiumBook) -> None:
    mcg = book.stadiums["melbourne-cricket-ground"]
    assert mcg.roof_type == "open"
    rows = [{"stadium_id": "x", "lat": "1", "lon": "2", "roof_type": "open", "name": "X"}]
    out = apply_overrides(rows, [{"stadium_id": "x", "field": "roof_type", "value": "dome"}, {"stadium_id": "x", "field": "bogus", "value": "1"}])
    assert out[0]["roof_type"] == "dome" and "bogus" not in out[0]


def test_rows_without_coordinates_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "stadiums.csv").write_text(
        "stadium_id,name,lat,lon,roof_type\nok,Ok Field,40.0,-80.0,open\nbad,No Coords,,,open\n", encoding="utf-8"
    )
    (tmp_path / "teams.csv").write_text("team_id,sport,name,home_stadium_id,avg_temp_f\nt1,nfl,T One,ok,50\n", encoding="utf-8")
    b = load_stadium_book(tmp_path)
    assert set(b.stadiums) == {"ok"}
    assert [r["stadium_id"] for r in b.skipped] == ["bad"]
    assert b.team("nfl", "t1") is not None and b.stadium_for_team("nfl", "t1").stadium_id == "ok"
