"""Preseason stadium reference builder (PLAN Phase 5) -> data/stadiums.csv.

Sources, in the order they are merged (later never overwrites an earlier
non-blank value unless stated):

1. `data/stadiums.csv` (existing rows; ids and columns preserved) + `data/teams.csv`
   (which stadiums are in scope: every NFL + FBS home stadium plus every row that
   carries an nflverse stadium id, i.e. the international venues).
2. CFBD `/venues` + `/teams?classification=fbs` when `CFBD_API_KEY` is set, else
   ESPN team lists + per-team core venue (espn_venue_id, surface, roof fill).
3. nflverse `games.csv` stadium ids (validation that every scheduled venue maps).
4. Wikidata SPARQL (one query for every American-football venue with P625, then a
   per-stadium `wikibase:around` fallback): wikidata_qid, P1083 capacity,
   P571 year_built, P2044 elevation (fallback only).
5. OSM Overpass: one batched union query per ~40 stadiums (`leisure=pitch`
   `sport=american_football` + `leisure=stadium`, around:400 m) with a 2-5 s
   throttle. The pitch inside the stadium polygon wins -> orientation from the
   minimum rotated rectangle of the pitch (`orientation_src=osm_pitch`); else the
   stadium polygon MRR (`osm_stadium_mrr`); else the curated value stays.
6. Elevation: USGS EPQS for US points, Open-Meteo elevation (batched 100) elsewhere.
7. timezonefinder for `timezone`.
8. `data/climatology.csv` (see climatology.py) fills blank avg_wind_sep..jan / avg_temp_f.
9. `data/stadiums_overrides.csv` always wins.

Validation: every NFL/FBS home team maps to a row; csv lat/lon must lie within
300 m of the OSM centroid (else `needs_review` + `review_note`); rows without any
orientation are flagged. Provenance columns: orientation_src, elev_src, latlon_src.

Every remote payload is cached under `--cache-dir` (json) so re-runs are offline
and deterministic; `--offline` refuses network entirely.

CLI: python -m pipeline.stadiums.build_stadiums [--all] [--cache-dir DIR] [--offline]
       [--no-climatology] [--report PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pipeline.stadiums.loader import DATA_DIR, apply_overrides, load_stadium_book, slug

USER_AGENT = "football_weather stadium builder (mckinleyslade@gmail.com)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_RADIUS_M = 400
OVERPASS_BATCH = 40
OVERPASS_SLEEP_S = 3.0
WIKIDATA_URL = "https://query.wikidata.org/sparql"
WIKIDATA_MATCH_M = 1500.0
EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
OPENMETEO_ELEV_URL = "https://api.open-meteo.com/v1/elevation"
OPENMETEO_ELEV_BATCH = 100
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/teams"
ESPN_CORE_TEAM = "https://sports.core.api.espn.com/v2/sports/football/leagues/{league}/teams/{team_id}"
ESPN_LEAGUE = {"nfl": "nfl", "cfb": "college-football"}
CFBD_BASE = "https://api.collegefootballdata.com"
NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
LATLON_TOLERANCE_M = 300.0
PITCH_MAX_M = 250.0  # a pitch with no enclosing stadium polygon must be this close to the csv point
NEW_COLUMNS = ("elev_src", "latlon_src", "review_note")
MONTHS = ("sep", "oct", "nov", "dec", "jan")


# ---- geometry (pure python; shapely optional) --------------------------------------

LonLat = tuple[float, float]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _project(points: Sequence[LonLat]) -> tuple[list[tuple[float, float]], float]:
    """Local equirectangular projection to metres (x east, y north) around the centroid."""
    lat0 = sum(p[1] for p in points) / len(points)
    lon0 = sum(p[0] for p in points) / len(points)
    k = 111320.0
    cos0 = math.cos(math.radians(lat0))
    return [((lon - lon0) * k * cos0, (lat - lat0) * k) for lon, lat in points], lat0


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _shapely_mrr_bearing(xy: list[tuple[float, float]]) -> Optional[float]:
    try:
        from shapely.geometry import Polygon  # type: ignore

        rect = Polygon(xy).minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        if not isinstance(coords, list) or len(coords) < 4:
            return None
        e1 = (coords[1][0] - coords[0][0], coords[1][1] - coords[0][1])
        e2 = (coords[2][0] - coords[1][0], coords[2][1] - coords[1][1])
        long = e1 if math.hypot(*e1) >= math.hypot(*e2) else e2
        return _bearing_from_vector(long[0], long[1])
    except Exception:  # noqa: BLE001 - shapely missing/stubbed -> pure python
        return None


def _bearing_from_vector(dx: float, dy: float) -> float:
    """Compass bearing (0=N, 90=E) folded into [0, 180) for an undirected axis."""
    b = math.degrees(math.atan2(dx, dy)) % 360.0
    b = b % 180.0
    return round(b, 1) % 180.0


def mrr_axis_bearing(points: Sequence[LonLat]) -> Optional[float]:
    """Bearing (0-180) of the long axis of the minimum rotated rectangle of a lon/lat ring."""
    pts = [p for p in points if p is not None]
    if len(pts) < 3:
        return None
    xy, _ = _project(pts)
    b = _shapely_mrr_bearing(xy)
    if b is not None:
        return b
    hull = _convex_hull(xy)
    if len(hull) < 3:
        return None
    best: tuple[float, float] | None = None  # (area, bearing)
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length == 0:
            continue
        ux, uy = ex / length, ey / length  # edge direction
        vx, vy = -uy, ux  # normal
        us = [px * ux + py * uy for px, py in hull]
        vs = [px * vx + py * vy for px, py in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        area = w * h
        if best is None or area < best[0] - 1e-9:
            long_vec = (ux, uy) if w >= h else (vx, vy)
            best = (area, _bearing_from_vector(long_vec[0], long_vec[1]))
    return None if best is None else best[1]


def orientation_bucket(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    d = float(deg) % 180.0
    if d < 22.5 or d >= 157.5:
        return "N-S"
    if d < 67.5:
        return "NE-SW"
    if d < 112.5:
        return "E-W"
    return "NW-SE"


def point_in_ring(lon: float, lat: float, ring: Sequence[LonLat]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_int = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_int:
                inside = not inside
    return inside


def ring_centroid(ring: Sequence[LonLat]) -> LonLat:
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    xy, _ = _project(pts)
    a = cx = cy = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    if abs(a) < 1e-6:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    cx, cy = cx / (3 * a), cy / (3 * a)
    lat0 = sum(p[1] for p in pts) / len(pts)
    lon0 = sum(p[0] for p in pts) / len(pts)
    k = 111320.0
    return (lon0 + cx / (k * math.cos(math.radians(lat0))), lat0 + cy / k)


# ---- OSM ----------------------------------------------------------------------------


@dataclass
class OsmFeature:
    kind: str  # "pitch" | "stadium"
    osm_id: str  # "way/123" | "relation/456"
    name: Optional[str]
    ring: list[LonLat]
    centroid: LonLat
    tags: dict[str, str] = field(default_factory=dict)

    def contains(self, lon: float, lat: float) -> bool:
        return len(self.ring) >= 3 and point_in_ring(lon, lat, self.ring)


@dataclass
class OsmMatch:
    orientation_deg: Optional[float] = None
    orientation_src: Optional[str] = None
    osm_id: Optional[str] = None
    centroid: Optional[LonLat] = None
    stadium_name: Optional[str] = None
    distance_m: Optional[float] = None
    inside_stadium: bool = False


def build_overpass_query(coords: Sequence[tuple[float, float]], radius_m: int = OVERPASS_RADIUS_M, timeout_s: int = 120) -> str:
    parts = []
    for lat, lon in coords:
        a = f"around:{radius_m},{lat:.6f},{lon:.6f}"
        parts.append(f"  way({a})[leisure=pitch][sport~\"american_football\"];")
        parts.append(f"  way({a})[leisure=stadium];")
        parts.append(f"  relation({a})[leisure=stadium];")
    body = "\n".join(parts)
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout geom;\n"


def _way_ring(el: dict[str, Any]) -> list[LonLat]:
    return [(float(p["lon"]), float(p["lat"])) for p in el.get("geometry") or [] if p]


def _relation_outer_ring(el: dict[str, Any]) -> list[LonLat]:
    """Concatenate outer member ways (good enough for MRR/containment of a stadium)."""
    pts: list[LonLat] = []
    for m in el.get("members") or []:
        if m.get("type") == "way" and m.get("role", "outer") in ("outer", ""):
            pts.extend((float(p["lon"]), float(p["lat"])) for p in m.get("geometry") or [] if p)
    if not pts and el.get("bounds"):
        b = el["bounds"]
        pts = [(b["minlon"], b["minlat"]), (b["maxlon"], b["minlat"]), (b["maxlon"], b["maxlat"]), (b["minlon"], b["maxlat"])]
    return pts


def parse_overpass(payload: dict[str, Any]) -> list[OsmFeature]:
    out: list[OsmFeature] = []
    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        leisure = tags.get("leisure")
        if leisure == "pitch":
            if "american_football" not in (tags.get("sport") or ""):
                continue
            kind = "pitch"
        elif leisure == "stadium":
            kind = "stadium"
        else:
            continue
        ring = _way_ring(el) if el.get("type") == "way" else _relation_outer_ring(el)
        if len(ring) < 3:
            continue
        if kind == "stadium":
            # convex hull of the outer ring keeps concatenated relation members usable for containment
            xy, _ = _project(ring)
            hull_xy = _convex_hull(xy)
            lookup = {p: r for p, r in zip(xy, ring, strict=False)}
            ring = [lookup[p] for p in hull_xy if p in lookup] or ring
        out.append(OsmFeature(kind=kind, osm_id=f"{el['type']}/{el['id']}", name=tags.get("name"), ring=ring, centroid=ring_centroid(ring), tags=tags))
    return out


def select_osm(lat: float, lon: float, features: Iterable[OsmFeature], name: Optional[str] = None) -> OsmMatch:
    """Pick the pitch inside the stadium polygon around (lat, lon); fall back to the stadium MRR."""
    feats = [f for f in features if haversine_m(lat, lon, f.centroid[1], f.centroid[0]) <= OVERPASS_RADIUS_M + 50]
    stadiums = [f for f in feats if f.kind == "stadium"]
    pitches = [f for f in feats if f.kind == "pitch"]

    def dist(f: OsmFeature) -> float:
        return haversine_m(lat, lon, f.centroid[1], f.centroid[0])

    stadium: Optional[OsmFeature] = None
    containing = [s for s in stadiums if s.contains(lon, lat)]
    if containing:
        stadium = min(containing, key=lambda s: -len(s.ring))  # richest polygon
    elif stadiums:
        named = [s for s in stadiums if name and s.name and slug(s.name) == slug(name)]
        stadium = min(named or stadiums, key=dist)

    chosen: Optional[OsmFeature] = None
    inside = False
    if stadium is not None:
        inner = [p for p in pitches if stadium.contains(p.centroid[0], p.centroid[1])]
        if inner:
            chosen = min(inner, key=lambda p: haversine_m(stadium.centroid[1], stadium.centroid[0], p.centroid[1], p.centroid[0]))
            inside = True
    if chosen is None and stadium is None and pitches:
        near = min(pitches, key=dist)
        if dist(near) <= PITCH_MAX_M:
            chosen = near

    m = OsmMatch(stadium_name=stadium.name if stadium else None, inside_stadium=inside)
    if chosen is not None:
        m.orientation_deg = mrr_axis_bearing(chosen.ring)
        m.orientation_src = "osm_pitch"
        m.osm_id = chosen.osm_id
        m.centroid = chosen.centroid
    elif stadium is not None:
        m.orientation_deg = mrr_axis_bearing(stadium.ring)
        m.orientation_src = "osm_stadium_mrr"
        m.osm_id = stadium.osm_id
        m.centroid = stadium.centroid
    if m.centroid is not None:
        m.distance_m = haversine_m(lat, lon, m.centroid[1], m.centroid[0])
    return m


# ---- HTTP + cache ----------------------------------------------------------------------


class Fetcher:
    """httpx wrapper with a json disk cache keyed by name; `offline` never touches the network."""

    def __init__(self, cache_dir: Optional[Path], offline: bool = False, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print):
        self.cache_dir = cache_dir
        self.offline = offline
        self.sleep = sleep
        self.log = log
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=180.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        return self._client

    def _path(self, name: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        return self.cache_dir / f"{safe}.json"

    def cached(self, name: str) -> Any:
        p = self._path(name)
        if p is not None and p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def store(self, name: str, payload: Any) -> None:
        p = self._path(name)
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload), encoding="utf-8")

    def json(self, name: str, method: str, url: str, *, params: Optional[dict[str, Any]] = None, data: Optional[dict[str, Any]] = None,
             headers: Optional[dict[str, str]] = None, throttle: float = 0.0, retries: int = 3, text: bool = False, timeout: float = 180.0) -> Any:
        hit = self.cached(name)
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError(f"offline: no cache for {name}")
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.client().request(method, url, params=params, data=data, headers=headers, timeout=timeout)
                if r.status_code in (429, 502, 503, 504) and attempt < retries - 1:
                    wait = (30.0 if r.status_code == 429 else 10.0) * (attempt + 1)
                    self.log(f"  {name}: status {r.status_code}; retry in {wait:.0f}s")
                    self.sleep(wait)
                    continue
                r.raise_for_status()
                payload = r.text if text else r.json()
                self.store(name, payload)
                if throttle:
                    self.sleep(throttle)
                return payload
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries - 1:
                    self.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"{name}: {last}")


# ---- source fetchers -----------------------------------------------------------------


def fetch_osm_features(fetcher: Fetcher, coords: dict[str, tuple[float, float]], batch: int = OVERPASS_BATCH, sleep_s: float = OVERPASS_SLEEP_S) -> list[OsmFeature]:
    ids = sorted(coords)
    feats: list[OsmFeature] = []
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        q = build_overpass_query([coords[s] for s in chunk])
        name = f"osm_batch_{i // batch:02d}_{_digest(chunk)}"
        fetcher.log(f"overpass batch {i // batch + 1}/{(len(ids) + batch - 1) // batch} ({len(chunk)} stadiums)")
        payload = fetcher.json(name, "POST", OVERPASS_URL, data={"data": q}, throttle=sleep_s, retries=4)
        feats.extend(parse_overpass(payload))
    # de-dup features returned by overlapping around: clauses
    seen: dict[str, OsmFeature] = {}
    for f in feats:
        seen.setdefault(f.osm_id, f)
    return list(seen.values())


def _digest(items: Sequence[str]) -> str:
    import hashlib

    return hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:8]


WIKIDATA_ALL_QUERY = """SELECT ?item ?itemLabel ?coord ?elev ?inception ?capacity WHERE {
  ?item wdt:P641 wd:Q41323; wdt:P625 ?coord .
  OPTIONAL { ?item wdt:P2044 ?elev }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P1083 ?capacity }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

WIKIDATA_AROUND_QUERY = """SELECT ?item ?itemLabel ?coord ?elev ?inception ?capacity WHERE {
  SERVICE wikibase:around {
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral ;
                    wikibase:radius "1.5" .
  }
  ?item wdt:P31/wdt:P279* wd:Q483110 .
  OPTIONAL { ?item wdt:P2044 ?elev }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P1083 ?capacity }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


@dataclass
class WikidataVenue:
    qid: str
    label: str
    lat: float
    lon: float
    elevation_m: Optional[float] = None
    year_built: Optional[int] = None
    capacity: Optional[int] = None


def parse_wikidata(payload: dict[str, Any]) -> list[WikidataVenue]:
    out: dict[str, WikidataVenue] = {}
    for b in (payload.get("results") or {}).get("bindings") or []:
        try:
            qid = b["item"]["value"].rsplit("/", 1)[-1]
            m = re.match(r"Point\(([-0-9.eE+]+) ([-0-9.eE+]+)\)", b["coord"]["value"])
            if not m:
                continue
            lon, lat = float(m.group(1)), float(m.group(2))
        except (KeyError, ValueError):
            continue
        v = out.get(qid) or WikidataVenue(qid=qid, label=b.get("itemLabel", {}).get("value", qid), lat=lat, lon=lon)
        if "elev" in b and v.elevation_m is None:
            try:
                v.elevation_m = float(b["elev"]["value"])
            except ValueError:
                pass
        if "inception" in b and v.year_built is None:
            m2 = re.match(r"(-?\d{4})", b["inception"]["value"])
            if m2:
                v.year_built = int(m2.group(1))
        if "capacity" in b:
            try:
                cap = int(float(b["capacity"]["value"]))
                v.capacity = max(cap, v.capacity or 0)
            except ValueError:
                pass
        out[qid] = v
    return list(out.values())


def match_wikidata(lat: float, lon: float, name: str, venues: Iterable[WikidataVenue], max_m: float = WIKIDATA_MATCH_M) -> Optional[WikidataVenue]:
    cands = [(haversine_m(lat, lon, v.lat, v.lon), v) for v in venues]
    cands = [(d, v) for d, v in cands if d <= max_m]
    if not cands:
        return None
    s = slug(name)
    exact = [(d, v) for d, v in cands if slug(v.label) == s]
    if exact:
        return min(exact, key=lambda t: t[0])[1]
    return min(cands, key=lambda t: t[0])[1]


def fetch_wikidata_all(fetcher: Fetcher) -> list[WikidataVenue]:
    payload = fetcher.json("wikidata_american_football_venues", "GET", WIKIDATA_URL,
                           params={"query": WIKIDATA_ALL_QUERY, "format": "json"}, headers={"Accept": "application/sparql-results+json"}, throttle=1.0)
    return parse_wikidata(payload)


def fetch_wikidata_around(fetcher: Fetcher, stadium_id: str, lat: float, lon: float) -> list[WikidataVenue]:
    q = WIKIDATA_AROUND_QUERY.replace("{lon}", f"{lon:.5f}").replace("{lat}", f"{lat:.5f}")
    payload = fetcher.json(f"wikidata_around_{stadium_id}", "GET", WIKIDATA_URL, params={"query": q, "format": "json"},
                           headers={"Accept": "application/sparql-results+json"}, throttle=1.0)
    return parse_wikidata(payload)


def fetch_elevation_epqs(fetcher: Fetcher, stadium_id: str, lat: float, lon: float) -> Optional[float]:
    payload = fetcher.json(f"epqs_{stadium_id}", "GET", EPQS_URL, params={"x": f"{lon:.6f}", "y": f"{lat:.6f}", "units": "Meters", "wkid": 4326, "includeDate": "false"}, throttle=0.25, retries=2, timeout=25.0)
    try:
        v = float(payload.get("value"))
    except (TypeError, ValueError, AttributeError):
        return None
    return None if v < -1000 or v > 9000 else round(v, 1)


def fetch_elevation_openmeteo(fetcher: Fetcher, points: dict[str, tuple[float, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    ids = sorted(points)
    for i in range(0, len(ids), OPENMETEO_ELEV_BATCH):
        chunk = ids[i:i + OPENMETEO_ELEV_BATCH]
        params = {"latitude": ",".join(f"{points[s][0]:.5f}" for s in chunk), "longitude": ",".join(f"{points[s][1]:.5f}" for s in chunk)}
        payload = fetcher.json(f"openmeteo_elev_{_digest(chunk)}", "GET", OPENMETEO_ELEV_URL, params=params, throttle=1.0)
        elev = payload.get("elevation") or []
        for sid, e in zip(chunk, elev, strict=False):
            if e is not None:
                out[sid] = round(float(e), 1)
    return out


def fetch_espn_venues(fetcher: Fetcher, book: Any, wanted: dict[tuple[str, str], str]) -> dict[str, dict[str, Any]]:
    """{stadium_id: {espn_venue_id, name, grass, indoor}} for (sport, team_id) -> stadium_id in `wanted`."""
    out: dict[str, dict[str, Any]] = {}
    import httpx

    # ESPN's CDN 403s custom/browser-ish UAs but accepts the httpx default (what pipeline.schedule.espn sends)
    espn_headers = {"User-Agent": f"python-httpx/{httpx.__version__}"}
    for sport in ("nfl", "cfb"):
        league = ESPN_LEAGUE[sport]
        params: dict[str, Any] = {"limit": 500}
        if sport == "cfb":
            params["groups"] = 80
        payload = fetcher.json(f"espn_teams_{sport}", "GET", ESPN_SITE.format(league=league), params=params, headers=espn_headers, throttle=0.5)
        try:
            teams = payload["sports"][0]["leagues"][0]["teams"]
        except (KeyError, IndexError, TypeError):
            fetcher.log(f"  espn {sport}: unexpected team list payload")
            continue
        for entry in teams:
            t = entry.get("team") or {}
            tid = None
            for raw in (t.get("displayName"), t.get("location"), t.get("abbreviation"), t.get("slug")):
                tid = book.resolve_team(sport, raw, fuzzy=False) if raw else None
                if tid:
                    break
            if not tid or (sport, tid) not in wanted:
                continue
            sid = wanted[(sport, tid)]
            if sid in out:
                continue
            core = fetcher.json(f"espn_team_{sport}_{t.get('id')}", "GET", ESPN_CORE_TEAM.format(league=league, team_id=t.get("id")), headers=espn_headers, throttle=0.2)
            venue = (core or {}).get("venue") or {}
            if venue.get("id"):
                out[sid] = {"espn_venue_id": str(venue["id"]), "name": venue.get("fullName"), "grass": venue.get("grass"), "indoor": venue.get("indoor"),
                            "city": (venue.get("address") or {}).get("city"), "state": (venue.get("address") or {}).get("state")}
    return out


def fetch_cfbd(fetcher: Fetcher, api_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    venues = fetcher.json("cfbd_venues", "GET", f"{CFBD_BASE}/venues", headers=headers, throttle=1.0)
    teams = fetcher.json("cfbd_teams_fbs", "GET", f"{CFBD_BASE}/teams", params={"classification": "fbs"}, headers=headers, throttle=1.0)
    return list(venues or []), list(teams or [])


def fetch_nflverse_stadium_ids(fetcher: Fetcher, seasons: Iterable[int]) -> dict[str, str]:
    text = fetcher.json("nflverse_games", "GET", NFLVERSE_GAMES_URL, text=True)
    out: dict[str, str] = {}
    want = {int(s) for s in seasons}
    for row in csv.DictReader(text.splitlines()):
        try:
            if int(row.get("season") or 0) not in want:
                continue
        except ValueError:
            continue
        sid = (row.get("stadium_id") or "").strip()
        if sid:
            out.setdefault(sid, (row.get("stadium") or "").strip())
    return out


def _tz_for(lat: float, lon: float) -> Optional[str]:
    try:
        from timezonefinder import TimezoneFinder  # type: ignore

        tf = _tz_for.__dict__.setdefault("_tf", TimezoneFinder())
        tz = tf.timezone_at(lat=lat, lng=lon)
        return tz if isinstance(tz, str) and "/" in tz else None
    except Exception:  # noqa: BLE001 - missing/stubbed
        return None


# ---- csv helpers -------------------------------------------------------------------------


def _read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        return [dict(r) for r in rd], list(rd.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], columns: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: _fmt(r.get(c)) for c in columns})


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v != v:  # nan
            return ""
        s = f"{v:.7f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    return str(v)


def _f(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def scope_ids(rows: list[dict[str, str]], book: Any, all_rows: bool = False) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Stadium ids to rebuild + (sport, team_id) -> stadium_id for NFL/FBS home teams."""
    wanted: dict[tuple[str, str], str] = {}
    for (sport, tid), t in book.teams.items():
        cls = book.classification.get((sport, tid))
        if sport == "nfl" or (sport == "cfb" and cls == "fbs"):
            if t.home_stadium_id:
                wanted[(sport, tid)] = t.home_stadium_id
    ids = set(wanted.values()) | {r["stadium_id"] for r in rows if not _blank(r.get("nflverse_stadium_id"))}
    if all_rows:
        ids = {r["stadium_id"] for r in rows}
    return ids, wanted


def load_climatology(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    rows, _ = _read_rows(path)
    return {r["stadium_id"]: r for r in rows}


# ---- merge --------------------------------------------------------------------------------


@dataclass
class BuildReport:
    total: int = 0
    in_scope: int = 0
    orientation_src: dict[str, int] = field(default_factory=dict)
    latlon_src: dict[str, int] = field(default_factory=dict)
    elev_src: dict[str, int] = field(default_factory=dict)
    needs_review: list[str] = field(default_factory=list)
    unmapped_teams: list[str] = field(default_factory=list)
    unmapped_nflverse: list[str] = field(default_factory=list)
    wikidata_matched: int = 0
    espn_matched: int = 0
    notes: list[str] = field(default_factory=list)

    def osm_share(self) -> float:
        n = sum(self.orientation_src.values())
        good = self.orientation_src.get("osm_pitch", 0) + self.orientation_src.get("osm_stadium_mrr", 0)
        return good / n if n else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["osm_orientation_share"] = round(self.osm_share(), 4)
        return d


def merge_osm(row: dict[str, str], match: OsmMatch, tolerance_m: float = LATLON_TOLERANCE_M) -> None:
    """Apply an OSM match to a csv row in place (orientation, lat/lon, provenance, review flag)."""
    notes: list[str] = []
    if match.orientation_deg is not None and match.orientation_src:
        row["orientation_deg"] = _fmt(round(match.orientation_deg, 1))
        row["orientation_bucket"] = orientation_bucket(match.orientation_deg) or ""
        row["orientation_src"] = match.orientation_src
        row["osm_way_id"] = match.osm_id or ""
    if match.centroid is not None and match.distance_m is not None:
        if match.distance_m <= tolerance_m or match.inside_stadium:
            row["lat"] = _fmt(round(match.centroid[1], 6))
            row["lon"] = _fmt(round(match.centroid[0], 6))
            row["latlon_src"] = match.orientation_src or "osm"
            if match.distance_m > tolerance_m:
                notes.append(f"csv point {match.distance_m:.0f} m from OSM pitch (inside stadium polygon; moved)")
        else:
            row["needs_review"] = "1"
            notes.append(f"csv lat/lon {match.distance_m:.0f} m from OSM {match.orientation_src} centroid")
    if notes:
        row["review_note"] = "; ".join(filter(None, [row.get("review_note", ""), *notes]))


def merge_wikidata(row: dict[str, str], v: WikidataVenue) -> None:
    row["wikidata_qid"] = v.qid
    if _blank(row.get("capacity")) and v.capacity:
        row["capacity"] = str(v.capacity)
    if _blank(row.get("year_built")) and v.year_built:
        row["year_built"] = str(v.year_built)
    if _blank(row.get("elevation_m")) and v.elevation_m is not None:
        row["elevation_m"] = _fmt(v.elevation_m)
        row["elev_src"] = "wikidata"


def merge_espn(row: dict[str, str], v: dict[str, Any], keep_id: bool = False) -> None:
    if keep_id:  # off by default: loader.find_stadium() shares one numeric namespace across CFBD/ESPN ids (3803 collides)
        row["espn_venue_id"] = str(v.get("espn_venue_id") or "")
    if _blank(row.get("surface")) and v.get("grass") is not None:
        row["surface"] = "grass" if v["grass"] else "turf"
    if _blank(row.get("roof_type")) and v.get("indoor") is not None:
        row["roof_type"] = "dome" if v["indoor"] else "open"
    if _blank(row.get("city")) and v.get("city"):
        row["city"] = v["city"]
    if _blank(row.get("state")) and v.get("state"):
        row["state"] = v["state"]


def merge_cfbd(row: dict[str, str], venue: dict[str, Any]) -> None:
    row["cfbd_venue_id"] = str(venue.get("id") or row.get("cfbd_venue_id") or "")
    if _blank(row.get("capacity")) and venue.get("capacity"):
        row["capacity"] = str(venue["capacity"])
    if _blank(row.get("year_built")) and (venue.get("year_constructed") or venue.get("constructionYear")):
        row["year_built"] = str(venue.get("year_constructed") or venue.get("constructionYear"))
    if _blank(row.get("surface")) and venue.get("grass") is not None:
        row["surface"] = "grass" if venue["grass"] else "turf"
    if _blank(row.get("roof_type")) and venue.get("dome") is not None:
        row["roof_type"] = "dome" if venue["dome"] else "open"
    if _blank(row.get("lat")) and venue.get("latitude") is not None:
        row["lat"], row["lon"] = _fmt(venue["latitude"]), _fmt(venue["longitude"])
        row["latlon_src"] = "cfbd"
    if _blank(row.get("elevation_m")) and venue.get("elevation") not in (None, ""):
        row["elevation_m"] = _fmt(_f(venue["elevation"]))
        row["elev_src"] = "cfbd"
    for k_src, k_dst in (("city", "city"), ("state", "state"), ("timezone", "timezone")):
        if _blank(row.get(k_dst)) and venue.get(k_src):
            row[k_dst] = str(venue[k_src])


def merge_climatology(row: dict[str, str], clim: dict[str, str], overwrite: bool = False) -> None:
    # domes: legacy avg_wind = 0 and the loader zeroes wind_avg only when the month columns are blank
    if (row.get("roof_type") or "") != "dome":
        for m in MONTHS:
            k = f"avg_wind_{m}"
            if (overwrite or _blank(row.get(k))) and not _blank(clim.get(k)):
                row[k] = clim[k]
    if (overwrite or _blank(row.get("avg_temp_f"))) and not _blank(clim.get("avg_temp_f")):
        row["avg_temp_f"] = clim["avg_temp_f"]


def build(
    data_dir: Path = DATA_DIR,
    cache_dir: Optional[Path] = None,
    offline: bool = False,
    all_rows: bool = False,
    use_climatology: bool = True,
    espn_ids: bool = False,
    overwrite_climatology: bool = False,
    out_path: Optional[Path] = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, str]], list[str], BuildReport]:
    src_path = data_dir / "stadiums.csv"
    rows, columns = _read_rows(src_path)
    for c in NEW_COLUMNS:
        if c not in columns:
            columns.append(c)
    for r in rows:
        for c in columns:
            r.setdefault(c, "")
        r["review_note"] = ""  # regenerated every build
        r["needs_review"] = "0" if r.get("needs_review") in ("", "0", "false", "False") else "1"
    book = load_stadium_book(data_dir)
    ids, wanted = scope_ids(rows, book, all_rows=all_rows)
    by_id = {r["stadium_id"]: r for r in rows}
    report = BuildReport(total=len(rows), in_scope=len(ids))
    fetcher = Fetcher(cache_dir, offline=offline, sleep=sleep, log=log)

    # -- every NFL/FBS home team must map to an existing row
    for (sport, tid), sid in sorted(wanted.items()):
        if sid not in by_id:
            report.unmapped_teams.append(f"{sport}:{tid}->{sid}")
    for (sport, tid), t in book.teams.items():
        if (sport == "nfl" or book.classification.get((sport, tid)) == "fbs") and not t.home_stadium_id:
            report.unmapped_teams.append(f"{sport}:{tid}->(none)")

    coords = {sid: (_f(by_id[sid]["lat"]), _f(by_id[sid]["lon"])) for sid in ids if sid in by_id}
    coords = {sid: (la, lo) for sid, (la, lo) in coords.items() if la is not None and lo is not None}

    # -- CFBD (optional) / ESPN venues
    api_key = os.environ.get("CFBD_API_KEY", "").strip()
    if api_key:
        try:
            venues, teams = fetch_cfbd(fetcher, api_key)
            vid_index = {str(v.get("id")): v for v in venues}
            cfbd_by_school: dict[str, dict[str, Any]] = {}
            for t in teams:
                loc = t.get("location") or {}
                vid = loc.get("venue_id") or loc.get("venueId") or loc.get("id")
                if vid and str(vid) in vid_index:
                    cfbd_by_school[str(t.get("school") or "")] = vid_index[str(vid)]
            for (sport, tid), sid in wanted.items():
                if sport != "cfb" or sid not in by_id or sid not in ids:
                    continue
                row = by_id[sid]
                venue = vid_index.get(str(row.get("cfbd_venue_id") or ""))
                if venue is None:
                    for school, v in cfbd_by_school.items():
                        if book.resolve_team("cfb", school, fuzzy=False) == tid:
                            venue = v
                            break
                if venue is not None:
                    merge_cfbd(row, venue)
            report.notes.append(f"cfbd: {len(venues)} venues, {len(teams)} fbs teams")
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"cfbd failed: {exc}")
            log(f"cfbd failed: {exc}")
    try:
        espn = fetch_espn_venues(fetcher, book, {k: v for k, v in wanted.items() if v in ids})
        for sid, v in espn.items():
            if sid in by_id:
                merge_espn(by_id[sid], v, keep_id=espn_ids)
        report.espn_matched = len(espn)
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"espn failed: {exc}")
        log(f"espn failed: {exc}")

    # -- nflverse stadium ids present in the current schedule
    try:
        import datetime as _dt

        yr = _dt.date.today().year
        nv = fetch_nflverse_stadium_ids(fetcher, (yr - 1, yr))
        known = {r.get("nflverse_stadium_id") for r in rows}
        for nv_id, nm in sorted(nv.items()):
            if nv_id not in known:
                report.unmapped_nflverse.append(f"{nv_id} ({nm})")
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"nflverse failed: {exc}")
        log(f"nflverse failed: {exc}")

    # -- Wikidata
    try:
        venues_wd = fetch_wikidata_all(fetcher)
        for sid in sorted(ids):
            if sid not in coords:
                continue
            lat, lon = coords[sid]
            row = by_id[sid]
            v = match_wikidata(lat, lon, row["name"], venues_wd)
            if v is None and (row.get("country") or "US") != "US":
                v = match_wikidata(lat, lon, row["name"], fetch_wikidata_around(fetcher, sid, lat, lon))
            if v is not None:
                merge_wikidata(row, v)
                report.wikidata_matched += 1
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"wikidata failed: {exc}")
        log(f"wikidata failed: {exc}")

    # -- OSM orientation + lat/lon validation
    try:
        feats = fetch_osm_features(fetcher, coords)
        for sid in sorted(ids):
            if sid not in coords:
                continue
            lat, lon = coords[sid]
            m = select_osm(lat, lon, feats, name=by_id[sid]["name"])
            merge_osm(by_id[sid], m)
        report.notes.append(f"osm: {len(feats)} features")
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"osm failed: {exc}")
        log(f"osm failed: {exc}")

    # -- elevation (after lat/lon may have moved to the pitch centroid)
    us_pts: dict[str, tuple[float, float]] = {}
    intl_pts: dict[str, tuple[float, float]] = {}
    for sid in ids:
        row = by_id.get(sid)
        if row is None:
            continue
        la, lo = _f(row.get("lat")), _f(row.get("lon"))
        if la is None or lo is None:
            continue
        (us_pts if (row.get("country") or "US") == "US" else intl_pts)[sid] = (la, lo)
    for sid, (la, lo) in sorted(us_pts.items()):
        try:
            e = fetch_elevation_epqs(fetcher, sid, la, lo)
        except Exception as exc:  # noqa: BLE001
            log(f"epqs {sid} failed: {exc}")
            e = None
        if e is not None:
            by_id[sid]["elevation_m"], by_id[sid]["elev_src"] = _fmt(e), "epqs"
        else:
            intl_pts[sid] = (la, lo)  # EPQS down/slow -> Open-Meteo elevation fallback
    if intl_pts:
        try:
            for sid, e in fetch_elevation_openmeteo(fetcher, intl_pts).items():
                by_id[sid]["elevation_m"], by_id[sid]["elev_src"] = _fmt(e), "openmeteo"
        except Exception as exc:  # noqa: BLE001
            log(f"openmeteo elevation failed: {exc}")
    for sid in ids:
        row = by_id.get(sid)
        if row is not None and not _blank(row.get("elevation_m")) and _blank(row.get("elev_src")):
            row["elev_src"] = "curated"

    # -- timezone
    for sid in ids:
        row = by_id.get(sid)
        if row is None:
            continue
        la, lo = _f(row.get("lat")), _f(row.get("lon"))
        if la is None or lo is None:
            continue
        tz = _tz_for(la, lo)
        if tz:
            row["timezone"] = tz

    # -- climatology
    if use_climatology:
        clim = load_climatology(data_dir / "climatology.csv")
        for sid in ids:
            if sid in by_id and sid in clim:
                merge_climatology(by_id[sid], clim[sid], overwrite=overwrite_climatology)

    # -- provenance defaults + validation flags
    for sid in ids:
        row = by_id.get(sid)
        if row is None:
            continue
        if _blank(row.get("latlon_src")) and not _blank(row.get("lat")):
            row["latlon_src"] = "curated"
        if not _blank(row.get("orientation_deg")):
            row["orientation_bucket"] = orientation_bucket(_f(row["orientation_deg"])) or row.get("orientation_bucket", "")
            if _blank(row.get("orientation_src")):
                row["orientation_src"] = "curated"
        else:
            row["needs_review"] = "1"
            row["review_note"] = "; ".join(filter(None, [row.get("review_note", ""), "no orientation (no OSM pitch/stadium polygon)"]))

    # -- overrides win (and can clear needs_review after a manual check)
    apply_overrides(rows, _read_rows(data_dir / "stadiums_overrides.csv")[0] if (data_dir / "stadiums_overrides.csv").exists() else [])
    for r in rows:
        r["needs_review"] = "1" if str(r.get("needs_review")).strip().lower() in ("1", "true", "yes") else "0"

    for sid in sorted(ids):
        row = by_id.get(sid)
        if row is None:
            continue
        report.orientation_src[row.get("orientation_src") or "none"] = report.orientation_src.get(row.get("orientation_src") or "none", 0) + 1
        report.latlon_src[row.get("latlon_src") or "none"] = report.latlon_src.get(row.get("latlon_src") or "none", 0) + 1
        report.elev_src[row.get("elev_src") or "none"] = report.elev_src.get(row.get("elev_src") or "none", 0) + 1
        if row["needs_review"] == "1":
            report.needs_review.append(f"{sid}: {row.get('review_note') or '(flagged)'}")

    if out_path is not None:
        write_rows(out_path, rows, columns)
    return rows, columns, report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--cache-dir", type=Path, default=None, help="json cache for every remote payload (re-runs are offline)")
    ap.add_argument("--offline", action="store_true", help="never touch the network; fail on cache miss")
    ap.add_argument("--all", action="store_true", help="rebuild every row, not just NFL/FBS + nflverse venues")
    ap.add_argument("--no-climatology", action="store_true")
    ap.add_argument("--espn-ids", action="store_true", help="also write espn_venue_id (off: numeric ids collide with CFBD ids in loader.find_stadium)")
    ap.add_argument("--overwrite-climatology", action="store_true", help="ERA5 means replace curated avg_wind_*/avg_temp_f")
    ap.add_argument("--report", type=Path, default=None, help="write the build report json here")
    ap.add_argument("--dry-run", action="store_true", help="do not write stadiums.csv")
    ap.add_argument("--out", type=Path, default=None, help="write to this path instead of data/stadiums.csv")
    a = ap.parse_args(argv)
    out = None if a.dry_run else (a.out or a.data_dir / "stadiums.csv")
    _rows, _cols, rep = build(a.data_dir, cache_dir=a.cache_dir, offline=a.offline, all_rows=a.all,
                              use_climatology=not a.no_climatology, espn_ids=a.espn_ids, overwrite_climatology=a.overwrite_climatology, out_path=out)
    d = rep.as_dict()
    print(json.dumps(d, indent=2))
    if a.report is not None:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return 1 if rep.unmapped_teams else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BuildReport",
    "Fetcher",
    "OsmFeature",
    "OsmMatch",
    "WikidataVenue",
    "build",
    "build_overpass_query",
    "haversine_m",
    "match_wikidata",
    "merge_osm",
    "mrr_axis_bearing",
    "orientation_bucket",
    "parse_overpass",
    "parse_wikidata",
    "point_in_ring",
    "ring_centroid",
    "select_osm",
    "write_rows",
]
