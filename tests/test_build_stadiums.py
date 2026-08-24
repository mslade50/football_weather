"""build_stadiums: MRR bearing on real OSM polygons, pitch-inside-stadium selection,
Wikidata matching, csv merge/provenance, overrides winning, workflow contract."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml

from pipeline.stadiums import build_stadiums as B

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "raw"
WORKFLOW = ROOT / ".github" / "workflows" / "build-stadiums.yml"

GILLETTE = (42.0909, -71.2643)
ATT = (32.7473, -97.0945)
OHIO = (40.0017, -83.0197)


@pytest.fixture(scope="module")
def osm_features() -> list[B.OsmFeature]:
    payload = json.loads((FIX / "osm" / "overpass_three_stadiums.json").read_text(encoding="utf-8"))
    return B.parse_overpass(payload)


@pytest.fixture(scope="module")
def wikidata_venues() -> list[B.WikidataVenue]:
    payload = json.loads((FIX / "wikidata" / "sparql_three_stadiums.json").read_text(encoding="utf-8"))
    return B.parse_wikidata(payload)


# ---- geometry ------------------------------------------------------------------------


def _rect(lat: float, lon: float, length_m: float, width_m: float, bearing: float) -> list[tuple[float, float]]:
    import math

    k = 111320.0
    c = math.cos(math.radians(lat))
    b = math.radians(bearing)
    ux, uy = math.sin(b), math.cos(b)  # along the long axis (east, north)
    vx, vy = -uy, ux
    pts = []
    for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1)):
        x = sx * length_m / 2 * ux + sy * width_m / 2 * vx
        y = sx * length_m / 2 * uy + sy * width_m / 2 * vy
        pts.append((lon + x / (k * c), lat + y / k))
    return pts


@pytest.mark.parametrize("bearing", [0.0, 7.0, 45.0, 69.0, 90.0, 120.0, 158.0, 179.0])
def test_mrr_bearing_recovers_synthetic_rectangle(bearing: float) -> None:
    ring = _rect(40.0, -83.0, 110.0, 49.0, bearing)
    got = B.mrr_axis_bearing(ring)
    assert got is not None
    diff = min(abs(got - bearing), 180 - abs(got - bearing))
    assert diff < 0.6


def test_bearing_is_folded_into_half_turn() -> None:
    assert B.mrr_axis_bearing(_rect(40.0, -83.0, 110.0, 49.0, 200.0)) == pytest.approx(20.0, abs=0.6)
    assert 0 <= B.mrr_axis_bearing(_rect(40.0, -83.0, 110.0, 49.0, 359.0)) < 180


def test_orientation_bucket_boundaries() -> None:
    assert B.orientation_bucket(0) == "N-S"
    assert B.orientation_bucket(158) == "N-S"
    assert B.orientation_bucket(45) == "NE-SW"
    assert B.orientation_bucket(69) == "E-W"
    assert B.orientation_bucket(90) == "E-W"
    assert B.orientation_bucket(135) == "NW-SE"
    assert B.orientation_bucket(None) is None


def test_point_in_ring_and_centroid() -> None:
    ring = _rect(40.0, -83.0, 100.0, 50.0, 30.0)
    assert B.point_in_ring(-83.0, 40.0, ring)
    assert not B.point_in_ring(-83.01, 40.0, ring)
    lon, lat = B.ring_centroid(ring)
    assert lat == pytest.approx(40.0, abs=1e-6) and lon == pytest.approx(-83.0, abs=1e-6)


def test_haversine() -> None:
    assert B.haversine_m(42.0, -71.0, 42.0, -71.0) == 0
    assert B.haversine_m(0, 0, 0, 1) == pytest.approx(111195, rel=1e-3)


# ---- OSM fixtures: Gillette ~158, AT&T ~69, Ohio Stadium ~7 -------------------------------


def test_parse_overpass_keeps_pitches_and_stadiums(osm_features: list[B.OsmFeature]) -> None:
    kinds = {f.kind for f in osm_features}
    assert kinds == {"pitch", "stadium"}
    names = {f.name for f in osm_features if f.kind == "stadium"}
    assert {"Gillette Stadium", "AT&T Stadium", "Ohio Stadium"} <= names
    rel = next(f for f in osm_features if f.osm_id == "relation/18511570")
    assert len(rel.ring) >= 4 and rel.contains(GILLETTE[1], GILLETTE[0])


@pytest.mark.parametrize("latlon,expected,name", [(GILLETTE, 158.0, "Gillette Stadium"), (ATT, 69.0, "AT&T Stadium"), (OHIO, 7.0, "Ohio Stadium")])
def test_pitch_inside_stadium_polygon_gives_expected_bearing(osm_features: list[B.OsmFeature], latlon: tuple[float, float], expected: float, name: str) -> None:
    m = B.select_osm(latlon[0], latlon[1], osm_features, name=name)
    assert m.orientation_src == "osm_pitch", m
    assert m.inside_stadium and m.stadium_name == name
    assert m.orientation_deg == pytest.approx(expected, abs=5.0)  # Gillette: stadium pitch polygon 162.4, adjacent practice fields 158
    assert m.distance_m is not None and m.distance_m < 300
    assert B.orientation_bucket(m.orientation_deg) == {158.0: "N-S", 69.0: "E-W", 7.0: "N-S"}[expected]


def test_ohio_selection_ignores_neighbouring_park_pitch(osm_features: list[B.OsmFeature]) -> None:
    m = B.select_osm(OHIO[0], OHIO[1], osm_features, name="Ohio Stadium")
    assert m.osm_id == "way/24816039"  # Safelite Field (inside Ohio Stadium), not Lincoln Tower Park


def test_stadium_mrr_fallback_when_no_pitch(osm_features: list[B.OsmFeature]) -> None:
    only_stadiums = [f for f in osm_features if f.kind == "stadium"]
    m = B.select_osm(ATT[0], ATT[1], only_stadiums, name="AT&T Stadium")
    assert m.orientation_src == "osm_stadium_mrr" and m.osm_id == "way/47086748"
    assert m.orientation_deg == pytest.approx(69.0, abs=8.0)


def test_far_away_point_matches_nothing(osm_features: list[B.OsmFeature]) -> None:
    m = B.select_osm(30.0, -90.0, osm_features)
    assert m.orientation_src is None and m.orientation_deg is None


def test_pitch_without_stadium_polygon_must_be_close(osm_features: list[B.OsmFeature]) -> None:
    pitches = [f for f in osm_features if f.kind == "pitch"]
    close = B.select_osm(GILLETTE[0], GILLETTE[1], pitches)
    assert close.orientation_src == "osm_pitch" and not close.inside_stadium
    far = B.select_osm(GILLETTE[0] + 0.0035, GILLETTE[1], pitches)  # ~390 m north
    assert far.orientation_src is None


def test_overpass_query_is_one_batched_union() -> None:
    q = B.build_overpass_query([GILLETTE, ATT])
    assert q.count("leisure=pitch") == 2 and q.count("leisure=stadium") == 4
    assert q.count("around:400") == 6 and q.strip().endswith("out geom;")
    assert q.startswith("[out:json]")


# ---- Wikidata -----------------------------------------------------------------------------


def test_wikidata_parse_and_match(wikidata_venues: list[B.WikidataVenue]) -> None:
    v = B.match_wikidata(GILLETTE[0], GILLETTE[1], "Gillette Stadium", wikidata_venues)
    assert v is not None and v.qid == "Q373355" and v.year_built == 2002
    v2 = B.match_wikidata(ATT[0], ATT[1], "AT&T Stadium", wikidata_venues)
    assert v2 is not None and v2.label == "AT&T Stadium" and v2.capacity and v2.capacity >= 80000
    assert B.match_wikidata(30.0, -90.0, "Nowhere", wikidata_venues) is None


# ---- merge + csv round trip -----------------------------------------------------------------


def test_merge_osm_moves_latlon_within_tolerance_and_flags_far() -> None:
    row = {"lat": "42.0909", "lon": "-71.2643", "orientation_deg": "0", "orientation_bucket": "N-S", "orientation_src": "curated",
           "needs_review": "0", "review_note": "", "osm_way_id": "", "latlon_src": ""}
    m = B.OsmMatch(orientation_deg=158.2, orientation_src="osm_pitch", osm_id="way/1", centroid=(-71.2644, 42.0890), distance_m=210.0, inside_stadium=True)
    B.merge_osm(row, m)
    assert row["orientation_deg"] == "158.2" and row["orientation_bucket"] == "N-S" and row["orientation_src"] == "osm_pitch"
    assert row["lat"] == "42.089" and row["latlon_src"] == "osm_pitch" and row["needs_review"] == "0"

    far = dict(row, needs_review="0", review_note="")
    B.merge_osm(far, B.OsmMatch(orientation_deg=90.0, orientation_src="osm_stadium_mrr", osm_id="way/2", centroid=(-71.27, 42.08), distance_m=950.0))
    assert far["needs_review"] == "1" and "950 m" in far["review_note"]
    assert far["lat"] == "42.089"  # not moved


def _mini_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "aliases").mkdir(parents=True)
    for name in ("aliases/nfl.json", "aliases/cfb.json"):
        shutil.copy(ROOT / "data" / name, d / name)
    src_rows = list(csv.DictReader((ROOT / "data" / "stadiums.csv").open(encoding="utf-8")))
    keep = [r for r in src_rows if r["stadium_id"] in ("gillette-stadium", "att-stadium", "ohio-stadium")]
    assert len(keep) == 3
    for r in keep:  # start from the legacy curated state regardless of what the real csv currently holds
        r.update(orientation_deg="0", orientation_bucket="N-S", orientation_src="curated", osm_way_id="", wikidata_qid="", espn_venue_id="",
                 lat="42.0909" if r["stadium_id"] == "gillette-stadium" else r["lat"], elevation_m="89", timezone="America/New_York",
                 avg_wind_static="6.23", weakest_wind_effect="x S", avg_wind_sep="", avg_temp_f="")
        for c in ("elev_src", "latlon_src", "review_note"):
            r.pop(c, None)
    cols = list(src_rows[0].keys())
    with (d / "stadiums.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(keep)
    (d / "teams.csv").write_text(
        "team_id,sport,name,short,home_stadium_id,avg_temp_f,conference,classification,aliases\n"
        "ne,nfl,New England Patriots,NE,gillette-stadium,50.5,,nfl,NE|Patriots\n"
        "dal,nfl,Dallas Cowboys,DAL,att-stadium,65.0,,nfl,DAL|Cowboys\n"
        "ohio-state,cfb,Ohio State,OSU,ohio-stadium,52.0,Big Ten,fbs,Ohio State|Buckeyes\n",
        encoding="utf-8",
    )
    (d / "stadiums_overrides.csv").write_text("stadium_id,field,value,note\natt-stadium,orientation_deg,71,manual survey\natt-stadium,orientation_src,manual,\n", encoding="utf-8")
    (d / "climatology.csv").write_text(
        "stadium_id,lat,lon,start_date,end_date,avg_wind_sep,avg_wind_oct,avg_wind_nov,avg_wind_dec,avg_wind_jan,avg_temp_f,n_days,fetched_at\n"
        "att-stadium,32.7,-97.1,2015-01-01,2024-12-31,7.1,7.4,7.9,8.0,8.3,66.2,3653,2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    return d


def test_build_offline_merges_osm_overrides_and_climatology(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _mini_data_dir(tmp_path)
    osm_payload = json.loads((FIX / "osm" / "overpass_three_stadiums.json").read_text(encoding="utf-8"))
    wd_payload = json.loads((FIX / "wikidata" / "sparql_three_stadiums.json").read_text(encoding="utf-8"))

    def fake_json(self, name, method, url, **kw):  # noqa: ANN001 - test double
        if name.startswith("osm_batch"):
            return osm_payload
        if name.startswith("wikidata_american"):
            return wd_payload
        if name.startswith("epqs_"):
            return {"value": "88.4"}
        if name.startswith("espn_teams_"):
            return {"sports": [{"leagues": [{"teams": []}]}]}
        if name == "nflverse_games":
            return "game_id,season,stadium_id,stadium\n"
        raise AssertionError(f"unexpected fetch {name}")

    monkeypatch.setattr(B.Fetcher, "json", fake_json)
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    monkeypatch.setattr(B, "_tz_for", lambda lat, lon: "America/Test")
    out = tmp_path / "out.csv"
    rows, cols, rep = B.build(d, cache_dir=None, offline=True, out_path=out, log=lambda s: None, sleep=lambda s: None)
    by = {r["stadium_id"]: r for r in rows}

    assert rep.in_scope == 3 and not rep.unmapped_teams
    assert rep.osm_share() == pytest.approx(2 / 3)  # AT&T overridden to manual
    g = by["gillette-stadium"]
    assert g["orientation_src"] == "osm_pitch" and float(g["orientation_deg"]) == pytest.approx(160, abs=3)
    assert g["latlon_src"] == "osm_pitch" and g["elev_src"] == "epqs" and g["elevation_m"] == "88.4"
    assert g["timezone"] == "America/Test" and g["wikidata_qid"] == "Q373355" and g["needs_review"] == "0"
    assert g["osm_way_id"].startswith("way/")
    o = by["ohio-stadium"]
    assert float(o["orientation_deg"]) == pytest.approx(7, abs=2.5) and o["orientation_bucket"] == "N-S"
    # overrides win over OSM; climatology fills blanks only
    a = by["att-stadium"]
    assert a["orientation_deg"] == "71" and a["orientation_src"] == "manual"
    assert a["avg_wind_sep"] == "7.1" and a["avg_temp_f"] == "66.2"
    assert g["avg_wind_sep"] == ""  # no climatology row -> untouched
    # legacy static columns preserved, new provenance columns appended
    for c in ("avg_wind_static", "wind_vol_static", "weakest_wind_effect", "nflverse_stadium_id", "cfbd_venue_id"):
        assert c in cols
    assert cols[-3:] == ["elev_src", "latlon_src", "review_note"]
    assert g["avg_wind_static"] == "6.23" and g["weakest_wind_effect"] == "x S"
    # csv round trip
    written = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["stadium_id"] for r in written] == [r["stadium_id"] for r in rows]
    assert written[0].keys() == set(cols) or list(written[0].keys()) == cols


def test_build_flags_missing_orientation_and_unmapped_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _mini_data_dir(tmp_path)
    with (d / "teams.csv").open("a", encoding="utf-8") as fh:
        fh.write("ghost,cfb,Ghost U,GU,ghost-stadium,50,,fbs,Ghost\n")
    def fake_json(self, name, method, url, **kw):  # noqa: ANN001
        if name.startswith("osm_batch"):
            return {"elements": []}
        if name.startswith("wikidata_american"):
            return {"results": {"bindings": []}}
        if name.startswith("epqs_"):
            return {"value": "-1000000"}
        if name.startswith("espn_teams_"):
            return {"sports": [{"leagues": [{"teams": []}]}]}
        if name == "nflverse_games":
            return "game_id,season,stadium_id,stadium\n"
        raise AssertionError(name)

    monkeypatch.setattr(B.Fetcher, "json", fake_json)
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    monkeypatch.setattr(B, "_tz_for", lambda lat, lon: None)
    rows, _cols, rep = B.build(d, offline=True, log=lambda s: None, sleep=lambda s: None)
    by = {r["stadium_id"]: r for r in rows}
    assert "cfb:ghost->ghost-stadium" in rep.unmapped_teams
    # no OSM -> curated orientation kept with its provenance, curated elevation/latlon marked
    g = by["gillette-stadium"]
    assert g["orientation_src"] == "curated" and g["orientation_deg"] == "0"
    assert g["elev_src"] == "curated" and g["latlon_src"] == "curated" and g["needs_review"] == "0"
    assert g["timezone"] == "America/New_York"  # untouched when timezonefinder unavailable
    assert rep.orientation_src == {"curated": 2, "manual": 1}


def test_write_rows_formats_numbers(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    B.write_rows(p, [{"a": 1.0, "b": 158.25, "c": None, "d": True, "e": float("nan")}], ["a", "b", "c", "d", "e"])
    assert p.read_text(encoding="utf-8") == "a,b,c,d,e\n1,158.25,,1,\n"


# ---- workflow contract ----------------------------------------------------------------------


def test_build_stadiums_workflow_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    on = wf[True]
    assert "workflow_dispatch" in on and "push" not in on
    assert wf["concurrency"]["group"] == "football-refresh"
    steps = wf["jobs"]["build"]["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("peter-evans/create-pull-request@") for u in uses)
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "python -m pipeline.stadiums.climatology" in runs
    assert "python -m pipeline.stadiums.build_stadiums" in runs
    assert "git commit" not in runs and "git push" not in runs
    assert "CFBD_API_KEY" in text and "if: failure()" in text
    assert wf["permissions"]["contents"] == "write" and wf["permissions"]["pull-requests"] == "write"
