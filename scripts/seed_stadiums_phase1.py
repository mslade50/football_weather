"""Phase 1 seed: data/stadiums.csv, data/stadiums_overrides.csv, data/teams.csv, data/aliases/{nfl,cfb}.json.

Sources (all local except two optional network fetches):
  data/raw/nfl_stadium_curated.csv     legacy NFL static columns (25 outdoor/closed stadiums)
  data/raw/nfl_team_temp_curated.csv   legacy NFL home_temp per team (32)
  data/raw/cfb_locations_updated.csv   CFBD teams+venues dump (658 rows, all divisions)
  data/raw/cfb_stadium_curated.csv     legacy CFB static columns (129 home teams)
  nflverse games.csv (httpx, optional) distinct stadium_id season>=2025 incl. international
  Open-Meteo elevation (httpx, optional) for CFB venues with no elevation
  timezonefinder (optional import)     for CFB venues with no timezone

Hand-maintained tables below (NFL_TEAMS, NFL_STADIUMS, CFB_EXTRA_ALIASES) cover what
the raw files lack: NFL dome coordinates/elevations/timezones, international venues,
roof types, legacy lowercase-city names.

Usage: python scripts/seed_stadiums_phase1.py [--no-network] [--games-csv PATH]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

RAW = ROOT / "data" / "raw"
DATA = ROOT / "data"
ALIASES_DIR = DATA / "aliases"
NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

STADIUM_COLUMNS = [
    "stadium_id", "name", "aliases", "city", "state", "country", "lat", "lon", "elevation_m", "timezone",
    "orientation_deg", "orientation_bucket", "orientation_src", "roof_type", "surface", "capacity", "year_built",
    "avg_wind_static", "wind_vol_static", "wind_impact_static", "weakest_wind_effect",
    "avg_wind_sep", "avg_wind_oct", "avg_wind_nov", "avg_wind_dec", "avg_wind_jan", "avg_temp_f",
    "wikidata_qid", "osm_way_id", "cfbd_venue_id", "espn_venue_id", "nflverse_stadium_id", "needs_review",
]
TEAM_COLUMNS = ["team_id", "sport", "name", "short", "home_stadium_id", "avg_temp_f", "conference", "classification", "aliases"]
OVERRIDE_COLUMNS = ["stadium_id", "field", "value", "note"]

ORIENTATION_DEG = {"N-S": 0.0, "NE-SW": 45.0, "E-W": 90.0, "NW-SE": 135.0, "E": 90.0, "NE": 45.0}

# abbr: (legacy lowercase city, full name, mascot, nflverse stadium_id, extra aliases)
NFL_TEAMS: dict[str, tuple[str, str, str, str, list[str]]] = {
    "ARI": ("arizona", "Arizona Cardinals", "Cardinals", "PHO00", ["ARZ", "Ari"]),
    "ATL": ("atlanta", "Atlanta Falcons", "Falcons", "ATL97", []),
    "BAL": ("baltimore", "Baltimore Ravens", "Ravens", "BAL00", ["BLT"]),
    "BUF": ("buffalo", "Buffalo Bills", "Bills", "BUF00", []),
    "CAR": ("carolina", "Carolina Panthers", "Panthers", "CAR00", []),
    "CHI": ("chicago", "Chicago Bears", "Bears", "CHI98", []),
    "CIN": ("cincinnati", "Cincinnati Bengals", "Bengals", "CIN00", []),
    "CLE": ("cleveland", "Cleveland Browns", "Browns", "CLE00", ["CLV"]),
    "DAL": ("dallas", "Dallas Cowboys", "Cowboys", "DAL00", []),
    "DEN": ("denver", "Denver Broncos", "Broncos", "DEN00", []),
    "DET": ("detroit", "Detroit Lions", "Lions", "DET00", []),
    "GB": ("green bay", "Green Bay Packers", "Packers", "GNB00", ["GNB"]),
    "HOU": ("houston", "Houston Texans", "Texans", "HOU00", []),
    "IND": ("indianapolis", "Indianapolis Colts", "Colts", "IND00", []),
    "JAX": ("jacksonville", "Jacksonville Jaguars", "Jaguars", "JAX00", ["JAC"]),
    "KC": ("kansas city", "Kansas City Chiefs", "Chiefs", "KAN00", ["KAN"]),
    "LA": ("l.a. rams", "Los Angeles Rams", "Rams", "LAX01", ["LAR", "LA Rams", "Los Angeles R"]),
    "LAC": ("l.a. chargers", "Los Angeles Chargers", "Chargers", "LAX01", ["LA Chargers", "Los Angeles C", "SD"]),
    "LV": ("las vegas", "Las Vegas Raiders", "Raiders", "VEG00", ["LVR", "OAK", "Oakland"]),
    "MIA": ("miami", "Miami Dolphins", "Dolphins", "MIA00", []),
    "MIN": ("minnesota", "Minnesota Vikings", "Vikings", "MIN01", []),
    "NE": ("new england", "New England Patriots", "Patriots", "BOS00", ["NWE"]),
    "NO": ("new orleans", "New Orleans Saints", "Saints", "NOR00", ["NOR"]),
    "NYG": ("n.y. giants", "New York Giants", "Giants", "NYC01", ["NY Giants", "New York G", "ny giants"]),
    "NYJ": ("n.y. jets", "New York Jets", "Jets", "NYC01", ["NY Jets", "New York J", "ny jets"]),
    "PHI": ("philadelphia", "Philadelphia Eagles", "Eagles", "PHI00", []),
    "PIT": ("pittsburgh", "Pittsburgh Steelers", "Steelers", "PIT00", []),
    "SEA": ("seattle", "Seattle Seahawks", "Seahawks", "SEA00", []),
    "SF": ("san francisco", "San Francisco 49ers", "49ers", "SFO01", ["SFO"]),
    "TB": ("tampa bay", "Tampa Bay Buccaneers", "Buccaneers", "TAM00", ["TAM", "Bucs"]),
    "TEN": ("tennessee", "Tennessee Titans", "Titans", "NAS00", []),
    "WAS": ("washington", "Washington Commanders", "Commanders", "WAS00", ["WSH", "Redskins", "Football Team"]),
}

# nflverse stadium_id -> stadium record. lat/lon here are used only when the curated file has none.
NFL_STADIUMS: dict[str, dict[str, Any]] = {
    "PHO00": dict(name="State Farm Stadium", city="Glendale", state="AZ", country="US", lat=33.5276, lon=-112.2626, elevation_m=331, timezone="America/Phoenix", roof_type="retractable", year_built=2006, capacity=63400),
    "ATL97": dict(name="Mercedes-Benz Stadium", city="Atlanta", state="GA", country="US", lat=33.7554, lon=-84.4010, elevation_m=305, timezone="America/New_York", roof_type="retractable", year_built=2017, capacity=71000),
    "BAL00": dict(name="M&T Bank Stadium", city="Baltimore", state="MD", country="US", elevation_m=10, timezone="America/New_York", roof_type="open"),
    "BUF00": dict(name="Highmark Stadium", aliases=["New Era Field", "Bills Stadium", "Ralph Wilson Stadium"], city="Orchard Park", state="NY", country="US", elevation_m=180, timezone="America/New_York", roof_type="open"),
    "CAR00": dict(name="Bank of America Stadium", city="Charlotte", state="NC", country="US", elevation_m=220, timezone="America/New_York", roof_type="open"),
    "CHI98": dict(name="Soldier Field", city="Chicago", state="IL", country="US", elevation_m=180, timezone="America/Chicago", roof_type="open"),
    "CIN00": dict(name="Paycor Stadium", aliases=["Paul Brown Stadium"], city="Cincinnati", state="OH", country="US", elevation_m=150, timezone="America/New_York", roof_type="open"),
    "CLE00": dict(name="Huntington Bank Field", aliases=["FirstEnergy Stadium", "Cleveland Browns Stadium"], city="Cleveland", state="OH", country="US", elevation_m=180, timezone="America/New_York", roof_type="open"),
    "DAL00": dict(name="AT&T Stadium", city="Arlington", state="TX", country="US", lat=32.7473, lon=-97.0945, elevation_m=170, timezone="America/Chicago", roof_type="retractable", year_built=2009, capacity=80000),
    "DEN00": dict(name="Empower Field at Mile High", city="Denver", state="CO", country="US", elevation_m=1609, timezone="America/Denver", roof_type="open"),
    "DET00": dict(name="Ford Field", city="Detroit", state="MI", country="US", lat=42.3400, lon=-83.0456, elevation_m=185, timezone="America/Detroit", roof_type="dome", year_built=2002, capacity=65000),
    "GNB00": dict(name="Lambeau Field", city="Green Bay", state="WI", country="US", elevation_m=209, timezone="America/Chicago", roof_type="open"),
    "HOU00": dict(name="NRG Stadium", aliases=["Reliant Stadium"], city="Houston", state="TX", country="US", lat=29.6847, lon=-95.4107, elevation_m=15, timezone="America/Chicago", roof_type="retractable", year_built=2002, capacity=72220),
    "IND00": dict(name="Lucas Oil Stadium", city="Indianapolis", state="IN", country="US", elevation_m=218, timezone="America/Indiana/Indianapolis", roof_type="retractable"),
    "JAX00": dict(name="EverBank Stadium", aliases=["TIAA Bank Field", "TIAA Bank Stadium"], city="Jacksonville", state="FL", country="US", elevation_m=5, timezone="America/New_York", roof_type="open"),
    "KAN00": dict(name="GEHA Field at Arrowhead Stadium", aliases=["Arrowhead Stadium"], city="Kansas City", state="MO", country="US", elevation_m=270, timezone="America/Chicago", roof_type="open"),
    "LAX01": dict(name="SoFi Stadium", city="Inglewood", state="CA", country="US", elevation_m=30, timezone="America/Los_Angeles", roof_type="dome"),
    "VEG00": dict(name="Allegiant Stadium", city="Las Vegas", state="NV", country="US", lat=36.0909, lon=-115.1833, elevation_m=610, timezone="America/Los_Angeles", roof_type="dome", year_built=2020, capacity=65000),
    "MIA00": dict(name="Hard Rock Stadium", city="Miami Gardens", state="FL", country="US", elevation_m=3, timezone="America/New_York", roof_type="open"),
    "MIN01": dict(name="U.S. Bank Stadium", city="Minneapolis", state="MN", country="US", elevation_m=250, timezone="America/Chicago", roof_type="dome"),
    "BOS00": dict(name="Gillette Stadium", city="Foxborough", state="MA", country="US", elevation_m=90, timezone="America/New_York", roof_type="open"),
    "NOR00": dict(name="Caesars Superdome", aliases=["Mercedes-Benz Superdome", "Superdome"], city="New Orleans", state="LA", country="US", lat=29.9511, lon=-90.0812, elevation_m=0, timezone="America/Chicago", roof_type="dome", year_built=1975, capacity=73000),
    "NYC01": dict(name="MetLife Stadium", city="East Rutherford", state="NJ", country="US", elevation_m=2, timezone="America/New_York", roof_type="open"),
    "PHI00": dict(name="Lincoln Financial Field", city="Philadelphia", state="PA", country="US", elevation_m=5, timezone="America/New_York", roof_type="open"),
    "PIT00": dict(name="Acrisure Stadium", aliases=["Heinz Field"], city="Pittsburgh", state="PA", country="US", elevation_m=230, timezone="America/New_York", roof_type="open"),
    "SEA00": dict(name="Lumen Field", aliases=["CenturyLink Field"], city="Seattle", state="WA", country="US", elevation_m=5, timezone="America/Los_Angeles", roof_type="open"),
    "SFO01": dict(name="Levi's Stadium", city="Santa Clara", state="CA", country="US", elevation_m=5, timezone="America/Los_Angeles", roof_type="open"),
    "TAM00": dict(name="Raymond James Stadium", city="Tampa", state="FL", country="US", elevation_m=10, timezone="America/New_York", roof_type="open"),
    "NAS00": dict(name="Nissan Stadium", city="Nashville", state="TN", country="US", elevation_m=130, timezone="America/Chicago", roof_type="open"),
    "WAS00": dict(name="Northwest Stadium", aliases=["FedExField", "FedEx Field", "Commanders Field"], city="Landover", state="MD", country="US", elevation_m=60, timezone="America/New_York", roof_type="open"),
    # international
    "LON00": dict(name="Wembley Stadium", city="London", state=None, country="GB", lat=51.5560, lon=-0.2795, elevation_m=40, timezone="Europe/London", roof_type="open", year_built=2007, capacity=90000),
    "LON02": dict(name="Tottenham Hotspur Stadium", city="London", state=None, country="GB", lat=51.6043, lon=-0.0664, elevation_m=30, timezone="Europe/London", roof_type="open", year_built=2019, capacity=62850),
    "MEL00": dict(name="Melbourne Cricket Ground", city="Melbourne", state="VIC", country="AU", lat=-37.8200, lon=144.9834, elevation_m=20, timezone="Australia/Melbourne", roof_type="open", year_built=1853, capacity=100024),
    "MEX00": dict(name="Estadio Banorte", aliases=["Estadio Azteca"], city="Mexico City", state=None, country="MX", lat=19.3029, lon=-99.1505, elevation_m=2200, timezone="America/Mexico_City", roof_type="open", year_built=1966, capacity=83264),
    "MUN01": dict(name="Allianz Arena", aliases=["FC Bayern Munich Stadium"], city="Munich", state=None, country="DE", lat=48.2188, lon=11.6247, elevation_m=510, timezone="Europe/Berlin", roof_type="open", year_built=2005, capacity=75000),
    "PAR00": dict(name="Stade de France", city="Saint-Denis", state=None, country="FR", lat=48.9244, lon=2.3601, elevation_m=35, timezone="Europe/Paris", roof_type="open", year_built=1998, capacity=80698),
    "RIO00": dict(name="Maracana Stadium", aliases=["Estadio do Maracana", "Maracanã"], city="Rio de Janeiro", state=None, country="BR", lat=-22.9121, lon=-43.2302, elevation_m=5, timezone="America/Sao_Paulo", roof_type="open", year_built=1950, capacity=78838),
    "MAD01": dict(name="Santiago Bernabeu Stadium", aliases=["Bernabeu", "Estadio Santiago Bernabéu"], city="Madrid", state=None, country="ES", lat=40.4531, lon=-3.6883, elevation_m=690, timezone="Europe/Madrid", roof_type="retractable", year_built=1947, capacity=78297),
    "DUB00": dict(name="Croke Park", city="Dublin", state=None, country="IE", lat=53.3607, lon=-6.2512, elevation_m=20, timezone="Europe/Dublin", roof_type="open", year_built=1884, capacity=82300),
    "SAO00": dict(name="Neo Quimica Arena", aliases=["Arena Corinthians"], city="Sao Paulo", state=None, country="BR", lat=-23.5453, lon=-46.4742, elevation_m=760, timezone="America/Sao_Paulo", roof_type="open", year_built=2014, capacity=49205),
    "FRA00": dict(name="Deutsche Bank Park", aliases=["Waldstadion", "Frankfurt Stadium"], city="Frankfurt", state=None, country="DE", lat=50.0686, lon=8.6455, elevation_m=110, timezone="Europe/Berlin", roof_type="open", year_built=1925, capacity=51500),
}

# legacy / book spellings that CFBD alt names do not cover
CFB_EXTRA_ALIASES: dict[str, list[str]] = {
    "Connecticut": ["UConn", "Uconn", "Connecticut Huskies"],
    "Florida International": ["FIU", "Florida Intl", "Fla International"],
    "Brigham Young": ["BYU", "BYU Cougars"],
    "Hawaii": ["Hawai'i", "Hawaii Rainbow Warriors"],
    "Massachusetts": ["UMass", "Massachusetts Minutemen"],
    "Louisiana": ["Louisiana Lafayette", "UL Lafayette", "ULL", "Louisiana-Lafayette", "Louisiana Ragin' Cajuns"],
    "Louisiana Monroe": ["UL Monroe", "ULM", "Louisiana-Monroe"],
    "Sam Houston State": ["Sam Houston", "Sam Houston St"],
    "Southern Miss": ["Southern Mississippi", "Southern Miss Golden Eagles"],
    "Miami (FL)": ["Miami", "Miami Florida", "Miami FL", "Miami Hurricanes"],
    "Miami (OH)": ["Miami Ohio", "Miami OH", "Miami RedHawks"],
    "Ole Miss": ["Mississippi", "Ole Miss Rebels"],
    "Texas A&M": ["Texas AM", "Texas A and M", "Texas A&M Aggies"],
    "Appalachian State": ["App State", "Appalachian St"],
    "UT San Antonio": ["UTSA", "Texas San Antonio"],
    "Pittsburgh": ["Pitt"],
    "North Carolina State": ["NC State", "N.C. State", "North Carolina St"],
    "San Jose State": ["San José State", "San Jose St"],
    "Central Florida": ["UCF"],
    "UCF": ["Central Florida", "UCF Knights"],
    "USC": ["Southern California", "Southern Cal"],
    "LSU": ["Louisiana State"],
    "SMU": ["Southern Methodist"],
    "TCU": ["Texas Christian"],
    "UAB": ["Alabama Birmingham", "Alabama-Birmingham"],
    "UTEP": ["Texas El Paso", "Texas-El Paso"],
    "UNLV": ["Nevada Las Vegas", "Nevada-Las Vegas"],
    "Middle Tennessee": ["Middle Tennessee State", "MTSU"],
    "Army": ["Army West Point", "Army Black Knights"],
    "Navy": ["Navy Midshipmen"],
    "Jacksonville State": ["Jax State"],
    "Kennesaw State": ["Kennesaw St"],
}


def slug(text: str) -> str:
    t = str(text).lower().replace("&", "").replace("'", "").replace("’", "").replace(".", "")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def parse_game_loc(s: Any) -> tuple[Optional[float], Optional[float]]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None, None
    parts = [p.strip() for p in str(s).split(",")]
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None, None


def clean(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def uniq(items: list[str]) -> list[str]:
    seen: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.append(it)
    return seen


def fetch_nflverse(games_csv: Optional[Path], network: bool) -> Optional[pd.DataFrame]:
    if games_csv and games_csv.exists():
        return pd.read_csv(games_csv, low_memory=False)
    if not network:
        return None
    try:
        import httpx

        r = httpx.get(NFLVERSE_GAMES_URL, timeout=60, follow_redirects=True)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] nflverse fetch failed: {exc}")
        return None
    import io

    print(f"[ok] nflverse games.csv fetched ({len(r.content)} bytes)")
    return pd.read_csv(io.BytesIO(r.content), low_memory=False)


def fetch_elevations(points: list[tuple[float, float]], network: bool) -> list[Optional[float]]:
    if not points or not network:
        return [None] * len(points)
    out: list[Optional[float]] = []
    try:
        import httpx

        for i in range(0, len(points), 100):
            chunk = points[i : i + 100]
            lat = ",".join(f"{p[0]:.5f}" for p in chunk)
            lon = ",".join(f"{p[1]:.5f}" for p in chunk)
            r = httpx.get("https://api.open-meteo.com/v1/elevation", params={"latitude": lat, "longitude": lon}, timeout=30)
            r.raise_for_status()
            vals = r.json().get("elevation", [])
            out += [float(v) if v is not None else None for v in vals] + [None] * (len(chunk) - len(vals))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] open-meteo elevation failed: {exc}")
    return out + [None] * (len(points) - len(out))


_TZF: list[Any] = []


def tz_lookup(lat: float, lon: float) -> Optional[str]:
    if not _TZF:
        try:
            from timezonefinder import TimezoneFinder

            _TZF.append(TimezoneFinder(in_memory=True))
        except ImportError:
            return None
    try:
        tz = _TZF[0].timezone_at(lat=lat, lng=lon)
        return tz if isinstance(tz, str) else None
    except Exception:  # noqa: BLE001
        return None


def build_nfl(games: Optional[pd.DataFrame]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    curated = pd.read_csv(RAW / "nfl_stadium_curated.csv")
    temps = pd.read_csv(RAW / "nfl_team_temp_curated.csv")
    temp_by_city = {r.team: float(r.avg_temp) for r in temps.itertuples()}
    city_to_abbr = {v[0]: k for k, v in NFL_TEAMS.items()}
    curated_by_nvid: dict[str, dict[str, Any]] = {}
    for r in curated.to_dict("records"):
        abbr = city_to_abbr.get(r["home_team"])
        if not abbr:
            print(f"[warn] curated NFL row with unknown team {r['home_team']!r}")
            continue
        nvid = NFL_TEAMS[abbr][3]
        curated_by_nvid.setdefault(nvid, r)  # first row wins (rows are identical per stadium)

    nflverse_ids: dict[str, dict[str, Any]] = {}
    if games is not None:
        recent = games[games["season"] >= 2025]
        for nvid, grp in recent.groupby("stadium_id"):
            last = grp.sort_values("gameday").iloc[-1]
            nflverse_ids[str(nvid)] = {
                "stadium": last.get("stadium"),
                "roof": last.get("roof"),
                "surface": last.get("surface"),
                "names": sorted(set(grp["stadium"].dropna().astype(str))),
            }
        missing = sorted(set(nflverse_ids) - set(NFL_STADIUMS))
        if missing:
            print(f"[warn] nflverse stadium_ids without a manual record (needs_review): {missing}")
        for nvid in missing:
            NFL_STADIUMS[nvid] = dict(name=str(nflverse_ids[nvid]["stadium"]), roof_type=None)

    stadiums: list[dict[str, Any]] = []
    for nvid, rec in NFL_STADIUMS.items():
        cur = curated_by_nvid.get(nvid, {})
        lat, lon = parse_game_loc(cur.get("game_loc"))
        if lat is None:
            lat, lon = rec.get("lat"), rec.get("lon")
        nv = nflverse_ids.get(nvid, {})
        aliases = uniq(list(rec.get("aliases", [])) + list(nv.get("names", [])) + ([cur["stadium"]] if cur else []))
        aliases = [a for a in aliases if a != rec["name"]]
        orient = cur.get("orient")
        roof = rec.get("roof_type")
        if roof is None:
            roof = {"dome": "dome", "closed": "retractable", "open": "retractable", "outdoors": "open"}.get(str(nv.get("roof")), None)
        avg_wind = cur.get("avg_wind")
        if roof == "dome" and (avg_wind is None or pd.isna(avg_wind)):
            avg_wind = 0.0
        stadiums.append({
            "stadium_id": slug(rec["name"]),
            "name": rec["name"],
            "aliases": "|".join(aliases),
            "city": rec.get("city"),
            "state": rec.get("state"),
            "country": rec.get("country"),
            "lat": lat,
            "lon": lon,
            "elevation_m": rec.get("elevation_m"),
            "timezone": rec.get("timezone"),
            "orientation_deg": ORIENTATION_DEG.get(str(orient)) if orient else None,
            "orientation_bucket": orient,
            "orientation_src": "curated" if orient else None,
            "roof_type": roof,
            "surface": nv.get("surface"),
            "capacity": rec.get("capacity"),
            "year_built": cur.get("year_built", rec.get("year_built")),
            "avg_wind_static": avg_wind,
            "wind_vol_static": cur.get("wind_vol"),
            "wind_impact_static": cur.get("wind_impact"),
            "weakest_wind_effect": cur.get("weakest_wind_effect"),
            "avg_wind_sep": None, "avg_wind_oct": None, "avg_wind_nov": None, "avg_wind_dec": None, "avg_wind_jan": None,
            "avg_temp_f": cur.get("home_temp"),
            "wikidata_qid": None, "osm_way_id": None, "cfbd_venue_id": None, "espn_venue_id": None,
            "nflverse_stadium_id": nvid,
            "needs_review": 1 if (lat is None or lon is None) else 0,
        })

    teams: list[dict[str, Any]] = []
    aliases_json: dict[str, list[str]] = {}
    nvid_to_sid = {s["nflverse_stadium_id"]: s["stadium_id"] for s in stadiums}
    for abbr, (city, full, mascot, nvid, extra) in NFL_TEAMS.items():
        team_id = abbr.lower()
        city_title = full[: -len(mascot)].strip()
        al = uniq([abbr, full, city, mascot, city_title, city_title.lower(), f"{city} {mascot.lower()}"] + extra)
        aliases_json[team_id] = al
        teams.append({
            "team_id": team_id, "sport": "nfl", "name": full, "short": abbr,
            "home_stadium_id": nvid_to_sid.get(nvid), "avg_temp_f": temp_by_city.get(city),
            "conference": None, "classification": "nfl", "aliases": "|".join(al),
        })
    return stadiums, teams, aliases_json


def build_cfb(existing_ids: dict[str, dict[str, Any]], network: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    loc = pd.read_csv(RAW / "cfb_locations_updated.csv")
    cur = pd.read_csv(RAW / "cfb_stadium_curated.csv")
    cur_by_school = {r["home_team"]: r for r in cur.to_dict("records")}
    unmatched = [s for s in cur_by_school if s not in set(loc["School"])]
    if unmatched:
        print(f"[warn] curated CFB home teams not in CFBD dump: {unmatched}")

    stadiums: dict[str, dict[str, Any]] = {}
    venue_to_sid: dict[str, str] = {}
    name_slugs: dict[str, str] = {}  # slug(name) -> venue id that claimed it
    teams: list[dict[str, Any]] = []
    aliases_json: dict[str, list[str]] = {}
    rows = loc.sort_values(["Classification", "School"], key=lambda s: s.map({"fbs": 0, "fcs": 1, "ii": 2, "iii": 3}) if s.name == "Classification" else s)

    for r in rows.to_dict("records"):
        school = r["School"]
        vid = str(int(r["Location Venue Id"])) if not pd.isna(r["Location Venue Id"]) else None
        vname = r.get("Location Name")
        lat, lon = r.get("Location Latitude"), r.get("Location Longitude")
        lat = None if pd.isna(lat) else float(lat)
        lon = None if pd.isna(lon) else float(lon)
        c = cur_by_school.get(school, {})
        if lat is None and c:
            lat, lon = parse_game_loc(c.get("game_loc"))

        sid: Optional[str] = None
        if vid and vid in venue_to_sid:
            sid = venue_to_sid[vid]
        elif isinstance(vname, str) and vname:
            base = slug(vname)
            if base in existing_ids:
                sid = base  # shared with an NFL stadium (Allegiant, Raymond James, Lincoln Financial)
                existing_ids[base]["cfbd_venue_id"] = vid
            elif base in name_slugs and name_slugs[base] != vid:
                sid = f"{base}-{slug(r.get('Location City') or school)}"
            else:
                sid = base
            name_slugs.setdefault(base, vid or school)
        if sid is None:
            sid = f"{slug(school)}-home"
        if vid:
            venue_to_sid[vid] = sid

        if sid not in stadiums and sid not in existing_ids:
            orient = c.get("orient") if c else None
            dome = bool(r.get("Location Dome")) if not pd.isna(r.get("Location Dome")) else False
            grass = r.get("Location Grass")
            stadiums[sid] = {
                "stadium_id": sid,
                "name": vname if isinstance(vname, str) else f"{school} home field",
                "aliases": "|".join(a for a in uniq([c.get("stadium")] if c and isinstance(c.get("stadium"), str) else []) if a != vname),
                "city": r.get("Location City"),
                "state": r.get("Location State"),
                "country": r.get("Location Country Code") or "US",
                "lat": lat,
                "lon": lon,
                "elevation_m": None if pd.isna(r.get("Location Elevation")) else round(float(r["Location Elevation"]), 1),
                "timezone": r.get("Location Timezone") if isinstance(r.get("Location Timezone"), str) else None,
                "orientation_deg": ORIENTATION_DEG.get(str(orient)) if isinstance(orient, str) else None,
                "orientation_bucket": orient if isinstance(orient, str) else None,
                "orientation_src": "curated" if isinstance(orient, str) else None,
                "roof_type": "dome" if dome else "open",
                "surface": None if pd.isna(grass) else ("grass" if bool(grass) else "turf"),
                "capacity": None if pd.isna(r.get("Location Capacity")) else int(r["Location Capacity"]),
                "year_built": (c.get("year_built") if c and not pd.isna(c.get("year_built")) else None)
                or (None if pd.isna(r.get("Location Year Constructed")) else int(r["Location Year Constructed"])),
                "avg_wind_static": None,
                "wind_vol_static": (c.get("wind_vol") if c and isinstance(c.get("wind_vol"), str) else None) or (r.get("wind_vol") if isinstance(r.get("wind_vol"), str) else None),
                "wind_impact_static": c.get("wind_impact") if c and isinstance(c.get("wind_impact"), str) else None,
                "weakest_wind_effect": c.get("weakest_wind_effect") if c and isinstance(c.get("weakest_wind_effect"), str) else None,
                "avg_wind_sep": r.get("avg_wind_sep"), "avg_wind_oct": r.get("avg_wind_oct"),
                "avg_wind_nov": r.get("avg_wind_nov"), "avg_wind_dec": r.get("avg_wind_dec"), "avg_wind_jan": None,
                "avg_temp_f": r.get("Avg_temp"),
                "wikidata_qid": None, "osm_way_id": None, "cfbd_venue_id": vid, "espn_venue_id": None, "nflverse_stadium_id": None,
                "needs_review": 1 if (lat is None or lon is None) else 0,
            }

        team_id = slug(school)
        al = uniq([school, r.get("Abbreviation"), r.get("Alt Name1"), r.get("Alt Name2"), r.get("Alt Name3"),
                   f"{school} {r.get('Mascot')}" if isinstance(r.get("Mascot"), str) else None]
                  + CFB_EXTRA_ALIASES.get(school, []))
        al = [a for a in al if isinstance(a, str) and a]
        aliases_json[team_id] = al
        avg_temp = c.get("home_temp") if c and not pd.isna(c.get("home_temp")) else r.get("Avg_temp")
        teams.append({
            "team_id": team_id, "sport": "cfb", "name": school, "short": r.get("Abbreviation"),
            "home_stadium_id": sid, "avg_temp_f": None if pd.isna(avg_temp) else float(avg_temp),
            "conference": r.get("Conference"), "classification": r.get("Classification"), "aliases": "|".join(al),
        })

    # fill missing timezone / elevation for stadiums with coordinates (FBS first; all rows are cheap)
    need_elev = [s for s in stadiums.values() if s["elevation_m"] is None and s["lat"] is not None]
    elevs = fetch_elevations([(s["lat"], s["lon"]) for s in need_elev], network)
    for s, e in zip(need_elev, elevs, strict=False):
        if e is not None:
            s["elevation_m"] = round(e, 1)
    for s in stadiums.values():
        if s["timezone"] is None and s["lat"] is not None:
            s["timezone"] = tz_lookup(s["lat"], s["lon"])
    return list(stadiums.values()), teams, aliases_json


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: clean(r.get(k)) for k in columns})


OVERRIDES: list[dict[str, str]] = [
    {"stadium_id": "melbourne-cricket-ground", "field": "roof_type", "value": "open", "note": "nflverse lists dome; MCG is open-air"},
    {"stadium_id": "sofi-stadium", "field": "roof_type", "value": "dome", "note": "fixed translucent roof, open sides; legacy avg_wind=0"},
    {"stadium_id": "estadio-banorte", "field": "elevation_m", "value": "2200", "note": "Mexico City altitude drives alt component"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--games-csv", type=Path, default=None, help="local nflverse games.csv (skips download)")
    args = ap.parse_args()
    network = not args.no_network

    games = fetch_nflverse(args.games_csv, network)
    if games is None:
        print("[warn] nflverse unavailable; NFL stadium list from manual table only")
    nfl_stadiums, nfl_teams, nfl_aliases = build_nfl(games)
    existing = {s["stadium_id"]: s for s in nfl_stadiums}
    cfb_stadiums, cfb_teams, cfb_aliases = build_cfb(existing, network)

    stadiums = nfl_stadiums + cfb_stadiums
    teams = nfl_teams + cfb_teams
    write_csv(DATA / "stadiums.csv", stadiums, STADIUM_COLUMNS)
    write_csv(DATA / "teams.csv", teams, TEAM_COLUMNS)
    if not (DATA / "stadiums_overrides.csv").exists():
        write_csv(DATA / "stadiums_overrides.csv", OVERRIDES, OVERRIDE_COLUMNS)
    ALIASES_DIR.mkdir(parents=True, exist_ok=True)
    (ALIASES_DIR / "nfl.json").write_text(json.dumps(nfl_aliases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (ALIASES_DIR / "cfb.json").write_text(json.dumps(cfb_aliases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    n_review = sum(1 for s in stadiums if s["needs_review"])
    fbs = [t for t in cfb_teams if t["classification"] == "fbs"]
    print(f"stadiums={len(stadiums)} (nfl={len(nfl_stadiums)}, cfb={len(cfb_stadiums)}) needs_review={n_review}")
    print(f"teams={len(teams)} (nfl={len(nfl_teams)}, cfb={len(cfb_teams)}, fbs={len(fbs)})")
    no_tz = [s["stadium_id"] for s in stadiums if not s["timezone"]]
    no_el = [s["stadium_id"] for s in stadiums if s["elevation_m"] is None]
    print(f"missing timezone: {len(no_tz)}  missing elevation: {len(no_el)}")
    fbs_missing = [t["team_id"] for t in fbs if not t["home_stadium_id"]]
    print(f"fbs teams without stadium: {fbs_missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
