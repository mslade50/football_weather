"""Pin every loaded stadium field, including scalar types, before ownership changes."""

import json
from dataclasses import asdict
from pathlib import Path

from pipeline.stadiums.loader import load_stadium_book, load_stadium_rows

FIXTURE = Path(__file__).parent / "fixtures" / "stadiums_before_split.json"


def test_stadium_ownership_split_exact_parity() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = {sid: asdict(st) for sid, st in load_stadium_book().stadiums.items()}
    # JSON text distinguishes int/float/bool as well as values; object order is immaterial.
    assert json.dumps(actual, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    )


def test_join_precedence_and_summary_selection(tmp_path: Path) -> None:
    (tmp_path / "stadiums.csv").write_text(
        "stadium_id,name,lat,lon,orientation_deg,avg_temp_f,needs_review,roof_type\n"
        "x,Curated,1,2,0,55,0,open\ny,Dome,3,4,,,,dome\n", encoding="utf-8"
    )
    (tmp_path / "stadiums_enriched.csv").write_text(
        "stadium_id,name,lat,lon,orientation_deg,avg_temp_f,avg_wind_sep,needs_review\n"
        "x,Machine,9,9,90,999,999,1\ny,Machine,9,9,90,999,999,1\n"
        "proposal,Unreviewed,5,6,90,,,1\n", encoding="utf-8"
    )
    climate = tmp_path / "climatology.csv"
    climate.write_text(
        "stadium_id,iso_week,avg_wind_sep,avg_temp_f\nx,,7,50\ny,,8,60\nx,40,999,999\n",
        encoding="utf-8",
    )
    book = load_stadium_book(tmp_path)
    assert set(book.stadiums) == {"x", "y"}
    x, y = book.stadiums["x"], book.stadiums["y"]
    assert (x.name, x.lat, x.lon, x.orientation_deg, x.avg_temp_f, x.needs_review) == (
        "Curated", 1.0, 2.0, 0.0, 55.0, False
    )
    assert x.avg_wind_by_month == {"sep": 7.0}
    assert y.avg_temp_f == 60.0 and not y.avg_wind_by_month
    climate.write_text(climate.read_text(encoding="utf-8").replace("x,,7,50", "x,,12,50"), encoding="utf-8")
    assert load_stadium_book(tmp_path).stadiums["x"].avg_wind_by_month == {"sep": 12.0}
    assert len(load_stadium_rows(tmp_path)) == 2
