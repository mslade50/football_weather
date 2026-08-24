"""Phase 0: extract golden fixtures for the v1 impact model from git history.

Outputs:
  tests/fixtures/golden_v1.parquet      deduped (inputs -> outputs) rows for model/impact.py
  tests/fixtures/golden_fair_2024.parquet FBS rows from the 2024 projection-feed commits
                                          (Spread/Total_proj/My_total/Edge/My_spread/Edge_s populated)

Usage: python scripts/extract_golden.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_history import REPO, git, iter_snapshots, path_exists_at, ref_exists  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
FAIR_COMMITS = ["d6f4fe6", "047f4be", "fac1c2e", "2726b5b", "2af9168"]

INPUTS = ["sport", "month", "temp_fg", "wind_fg", "rain_fg", "travel_alt", "home_temp", "away_temp"]
OUTPUTS = ["gs_fg", "away_fg"]
META = ["game", "date", "timestamp", "sheet", "sha"]


def _month(date_str: object, fallback: int) -> int:
    if isinstance(date_str, str) and "/" in date_str:
        try:
            return int(date_str.split()[-1].split("/")[0])
        except ValueError:
            pass
    return fallback


def _rows(df: pd.DataFrame, sport: str, sheet: str, sha: str, fallback_month: int) -> list[dict]:
    if "gs_fg" not in df.columns:
        return []
    out: list[dict] = []
    ts_col = "Timestamp" if "Timestamp" in df.columns else None
    for _, r in df.iterrows():
        row: dict = {
            "sport": sport,
            "month": _month(r.get("Date"), fallback_month),
            "game": r.get("Game"),
            "date": r.get("Date"),
            "timestamp": str(r[ts_col]) if ts_col else None,
            "sheet": sheet,
            "sha": sha,
        }
        for c in ["temp_fg", "wind_fg", "rain_fg", "travel_alt", "home_temp", "away_temp", "gs_fg", "away_fg"]:
            row[c] = pd.to_numeric(r.get(c), errors="coerce") if c in df.columns else float("nan")
        out.append(row)
    return out


def extract_golden() -> pd.DataFrame:
    rows: list[dict] = []
    n = 0
    for snap, blob_file, first in iter_snapshots("nfl_weather.csv", ".csv"):
        if not first:
            continue
        n += 1
        try:
            df = pd.read_csv(blob_file)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {snap.sha[:7]}: {exc}")
            continue
        rows.extend(_rows(df, "nfl", "csv", snap.sha[:7], snap.date.month))
    print(f"  nfl: {n} blobs -> {len(rows)} rows")
    m = len(rows)
    n = 0
    for snap, blob_file, first in iter_snapshots("cfb_weather.xlsx", ".xlsx"):
        if not first:
            continue
        n += 1
        try:
            sheets = pd.read_excel(blob_file, sheet_name=None)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {snap.sha[:7]}: {exc}")
            continue
        for name, df in sheets.items():
            rows.extend(_rows(df, "cfb", name, snap.sha[:7], snap.date.month))
    print(f"  cfb: {n} blobs -> {len(rows) - m} rows")

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["gs_fg", "temp_fg", "wind_fg"])
    df["month"] = df["month"].astype("int16")
    # dedupe on the model's inputs + outputs; keep first occurrence (oldest) for provenance
    df = df.drop_duplicates(subset=INPUTS + OUTPUTS, keep="first").reset_index(drop=True)
    return df[INPUTS + OUTPUTS + META]


def extract_fair() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    scratch = FIXTURES / "raw" / "_tmp_fair"
    scratch.mkdir(parents=True, exist_ok=True)
    for sha in FAIR_COMMITS:
        if not (ref_exists(sha) and path_exists_at(sha, "cfb_weather.xlsx")):
            print(f"  WARN: {sha}:cfb_weather.xlsx missing")
            continue
        p = scratch / f"{sha}.xlsx"
        p.write_bytes(git("show", f"{sha}:cfb_weather.xlsx", binary=True))
        df = pd.read_excel(p, sheet_name="FBS")
        df.insert(0, "sha", sha)
        frames.append(df)
        p.unlink()
    try:
        scratch.rmdir()
    except OSError:
        pass
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    keep = [c for c in ["Spread", "Total_proj", "My_total", "Edge", "My_spread", "Edge_s"] if c in out.columns]
    out = out.dropna(subset=keep, how="all").reset_index(drop=True)
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].astype(str).where(out[c].notna(), None)
    return out


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    print("golden_v1")
    g = extract_golden()
    g.to_parquet(FIXTURES / "golden_v1.parquet", index=False)
    print(f"  -> golden_v1.parquet {len(g)} rows ({(g.sport == 'nfl').sum()} nfl, {(g.sport == 'cfb').sum()} cfb)")
    print("golden_fair_2024")
    f = extract_fair()
    f.to_parquet(FIXTURES / "golden_fair_2024.parquet", index=False)
    print(f"  -> golden_fair_2024.parquet {len(f)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
