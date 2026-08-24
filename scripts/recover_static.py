"""Phase 0: recover curated static stadium/team data from git history.

Outputs (data/raw/):
  nfl_stadium_curated.csv   last-seen (per-column)  static columns per (stadium, home team)
  nfl_team_temp_curated.csv last-seen (per-column)  annual avg temp per NFL team (home or away)
  cfb_stadium_curated.csv   last-seen (per-column)  static columns per CFB home team (FBS sheet)
  cfb_locations_updated.csv git show 3aa1fa2:cfb_locations_updated.csv
tests/fixtures/raw/legacy_db/{bol_ncaaf.db, fd_cfb.db} from commit 25250b0.

Usage: python scripts/recover_static.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_history import REPO, git, iter_snapshots, path_exists_at, ref_exists  # noqa: E402

RAW = REPO / "data" / "raw"
LEGACY_DB = REPO / "tests" / "fixtures" / "raw" / "legacy_db"

NFL_STATIC = ["avg_wind", "wind_vol", "orient", "wind_impact", "weakest_wind_effect",
              "game_loc", "year_built", "home_temp"]
CFB_STATIC = ["stadium", "wind_vol", "orient", "wind_impact", "weakest_wind_effect",
              "game_loc", "year_built", "home_temp"]
LOC_REF = "3aa1fa2"
DB_REF = "25250b0"


def _split_game(game: str, sep: str) -> tuple[str, str] | None:
    if not isinstance(game, str) or sep not in game:
        return None
    away, home = game.split(sep, 1)
    return away.strip(), home.strip()


# Early snapshots used long-form vocab ("Northwest-Southeast", "medium", "except n/w");
# later ones the abbreviated vocab the UI/legacy writer expects. Normalise to the latter.
ORIENT_MAP = {
    "north-south": "N-S", "n-s": "N-S",
    "east-west": "E-W", "e-w": "E-W",
    "northwest-southeast": "NW-SE", "nw-se": "NW-SE",
    "northeast-southwest": "NE-SW", "ne-sw": "NE-SW",
}
IMPACT_MAP = {"low": "low", "medium": "med", "med": "med", "high": "high"}
DIR_WORDS = {"north": "N", "south": "S", "east": "E", "west": "W",
             "northeast": "NE", "northwest": "NW", "southeast": "SE", "southwest": "SW"}


def _norm_orient(v: object) -> object:
    if not isinstance(v, str):
        return v
    return ORIENT_MAP.get(v.strip().lower(), v.strip())


def _norm_impact(v: object, title: bool) -> object:
    if not isinstance(v, str):
        return v
    out = IMPACT_MAP.get(v.strip().lower(), v.strip().lower())
    return out.title() if title else out


def _norm_weakest(v: object) -> object:
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    if s == "all":
        return "all"
    prefix = ""
    for p in ("except ", "x "):
        if s.startswith(p):
            prefix, s = "x ", s[len(p):]
            break
    tokens = re.split(r"([/,])", s)
    return prefix + "".join(t if t in "/," else DIR_WORDS.get(t.strip(), t.strip().upper()) for t in tokens)


def _is_null(v: object) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def _collapse(rows: list[dict], key: list[str], cols: list[str]) -> pd.DataFrame:
    """Per key, most recent non-null value of each col (rows arrive oldest first).

    Last-seen wins so recovered values carry the current vocabulary and the
    fixed 'lat, lon' game_loc order; first/last commit are kept for provenance.
    """
    table: dict[tuple, dict] = {}
    for r in rows:
        k = tuple(r[c] for c in key)
        slot = table.setdefault(k, {c: r[c] for c in key})
        for c in cols:
            v = r.get(c)
            if not _is_null(v):
                slot[c] = v
        if "first_seen_sha" not in slot:
            slot["first_seen_sha"] = r["_sha"]
            slot["first_seen_date"] = r["_date"]
        slot["last_seen_sha"] = r["_sha"]
        slot["last_seen_date"] = r["_date"]
    out = pd.DataFrame(list(table.values()))
    order = key + [c for c in cols if c in out.columns] + [
        "first_seen_sha", "first_seen_date", "last_seen_sha", "last_seen_date"]
    return out[order].sort_values(key).reset_index(drop=True)


def _normalise(df: pd.DataFrame, title_impact: bool) -> pd.DataFrame:
    if "orient" in df.columns:
        df["orient"] = df["orient"].map(_norm_orient)
    if "wind_impact" in df.columns:
        df["wind_impact"] = df["wind_impact"].map(lambda v: _norm_impact(v, title_impact))
    if "weakest_wind_effect" in df.columns:
        df["weakest_wind_effect"] = df["weakest_wind_effect"].map(_norm_weakest)
    if "year_built" in df.columns:
        df["year_built"] = df["year_built"].astype("Int64")
    return df


def recover_nfl() -> tuple[pd.DataFrame, pd.DataFrame]:
    stadium_rows: list[dict] = []
    temp_rows: list[dict] = []
    n_blobs = 0
    for snap, blob_file, first in iter_snapshots("nfl_weather.csv", ".csv"):
        if not first:
            continue
        n_blobs += 1
        try:
            df = pd.read_csv(blob_file)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {snap.sha[:7]} nfl: {exc}")
            continue
        if "stadium" not in df.columns or "Game" not in df.columns:
            continue
        date = snap.date.date().isoformat()
        for _, r in df.iterrows():
            teams = _split_game(r["Game"], " vs ")
            if teams is None or not isinstance(r["stadium"], str):
                continue
            away, home = (t.lower() for t in teams)
            row = {"stadium": r["stadium"], "home_team": home, "_sha": snap.sha[:7], "_date": date}
            for c in NFL_STATIC:
                row[c] = r[c] if c in df.columns else None
            stadium_rows.append(row)
            if "home_temp" in df.columns:
                temp_rows.append({"team": home, "avg_temp": r["home_temp"], "_sha": snap.sha[:7], "_date": date})
            if "away_temp" in df.columns:
                temp_rows.append({"team": away, "avg_temp": r["away_temp"], "_sha": snap.sha[:7], "_date": date})
    print(f"  nfl: {n_blobs} distinct blobs, {len(stadium_rows)} game rows")
    stad = _normalise(_collapse(stadium_rows, ["stadium", "home_team"], NFL_STATIC), title_impact=False)
    temps = _collapse(temp_rows, ["team"], ["avg_temp"])
    return stad, temps


def recover_cfb() -> pd.DataFrame:
    rows: list[dict] = []
    n_blobs = 0
    for snap, blob_file, first in iter_snapshots("cfb_weather.xlsx", ".xlsx"):
        if not first:
            continue
        n_blobs += 1
        try:
            df = pd.read_excel(blob_file, sheet_name="FBS")
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {snap.sha[:7]} cfb: {exc}")
            continue
        if "Game" not in df.columns:
            continue
        date = snap.date.date().isoformat()
        for _, r in df.iterrows():
            teams = _split_game(r["Game"], " @ ")
            if teams is None:
                continue
            _, home = teams
            row = {"home_team": home, "_sha": snap.sha[:7], "_date": date}
            for c in CFB_STATIC:
                row[c] = r[c] if c in df.columns else None
            rows.append(row)
    print(f"  cfb: {n_blobs} distinct blobs, {len(rows)} game rows")
    return _normalise(_collapse(rows, ["home_team"], CFB_STATIC), title_impact=True)


def recover_blobs() -> None:
    if ref_exists(LOC_REF) and path_exists_at(LOC_REF, "cfb_locations_updated.csv"):
        data = git("show", f"{LOC_REF}:cfb_locations_updated.csv", binary=True)
        (RAW / "cfb_locations_updated.csv").write_bytes(data)
        n = len(pd.read_csv(RAW / "cfb_locations_updated.csv"))
        print(f"  cfb_locations_updated.csv: {n} rows (from {LOC_REF})")
    else:
        print(f"  WARN: {LOC_REF}:cfb_locations_updated.csv not found")
    if ref_exists(DB_REF):
        LEGACY_DB.mkdir(parents=True, exist_ok=True)
        for name in ("bol_ncaaf.db", "fd_cfb.db"):
            if path_exists_at(DB_REF, name):
                data = git("show", f"{DB_REF}:{name}", binary=True)
                (LEGACY_DB / name).write_bytes(data)
                print(f"  {name}: {len(data)} bytes (from {DB_REF})")
            else:
                print(f"  WARN: {DB_REF}:{name} not found")
    else:
        print(f"  WARN: commit {DB_REF} not found")


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    print("NFL history")
    stad, temps = recover_nfl()
    stad.to_csv(RAW / "nfl_stadium_curated.csv", index=False)
    temps.to_csv(RAW / "nfl_team_temp_curated.csv", index=False)
    print(f"  -> nfl_stadium_curated.csv {len(stad)} rows ({stad['stadium'].nunique()} stadiums)")
    print(f"  -> nfl_team_temp_curated.csv {len(temps)} rows")
    print("CFB history")
    cfb = recover_cfb()
    cfb.to_csv(RAW / "cfb_stadium_curated.csv", index=False)
    print(f"  -> cfb_stadium_curated.csv {len(cfb)} rows")
    print("Static blobs")
    recover_blobs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
