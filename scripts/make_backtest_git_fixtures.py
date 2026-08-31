"""Build tests/fixtures/git_archive/ for tests/test_backtest_git.py.

Takes real blobs out of the legacy archive's git history -- one NFL week and one CFB week of
2025 at leads ~7 d / 5 d / 3 d / 2 d / 1 d / 12 h / 4 h -- trims each to at most MAX_GAMES rows,
and writes the schedule / result / ERA5 slices those games need so the historical backtest runs
end to end offline::

    python scripts/make_backtest_git_fixtures.py [--out tests/fixtures/git_archive]

Layout produced::

    git_archive/manifest.json          [{sport, sha, commit_date, file}, ...]
    git_archive/nfl/<sha>.csv          trimmed nfl_weather.csv blobs
    git_archive/cfb/<sha>.xlsx         trimmed cfb_weather.xlsx blobs (FBS + Other)
    git_archive/nflverse_games.csv     the fixture week's rows of nflverse games.csv
    git_archive/cfbd_games_2025.json   the fixture week's CFBD /games entries
    git_archive/era5/era5h_*.json      hourly ERA5 around each fixture kickoff

The nflverse / CFBD / ERA5 slices come from the caches a normal ``--from-git`` run leaves under
``data/backtest`` (run that first); nothing here touches the network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import backtest as bt  # noqa: E402
from pipeline import backtest_git as bg  # noqa: E402
from pipeline.stadiums.loader import load_stadium_book  # noqa: E402
from utils.timeutil import ensure_utc, parse_iso, utc_iso  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "tests" / "fixtures" / "git_archive"
MAX_GAMES = 15
OTHER_ROWS = 3
SEASON = 2025
# (reference kickoff, the game date the blobs are trimmed to)
TARGETS = {
    "nfl": (datetime(2025, 11, 23, 18, 0, tzinfo=bg.UTC), ("11/23", "11/24")),
    "cfb": (datetime(2025, 11, 15, 17, 0, tzinfo=bg.UTC), ("11/15",)),
}
LEADS_H = (168.0, 120.0, 72.0, 48.0, 24.0, 12.0, 4.0)
ERA5_PAD_H = 6


def _blobs(sport: str) -> list[tuple[str, datetime, Path]]:
    gh = bg._git_history_module()
    out: list[tuple[str, datetime, Path]] = []
    for snap, blob_file, first in gh.iter_snapshots(bg.ARCHIVE_FILE[sport], bg.ARCHIVE_SUFFIX[sport]):
        if first:
            out.append((snap.sha[:7], ensure_utc(snap.date), Path(blob_file)))
    return out


def pick_blobs(sport: str) -> list[tuple[str, datetime, Path]]:
    """One blob per target lead, nearest the reference kickoff (deduped, oldest first)."""
    kick, _ = TARGETS[sport]
    blobs = _blobs(sport)
    chosen: dict[str, tuple[str, datetime, Path]] = {}
    for lead in LEADS_H:
        want = kick - timedelta(hours=lead)
        cands = [b for b in blobs if b[1] <= kick]
        if not cands:
            continue
        best = min(cands, key=lambda b: abs((b[1] - want).total_seconds()))
        chosen.setdefault(best[0], best)
    return sorted(chosen.values(), key=lambda b: b[1])


def _keep(df: pd.DataFrame, dates: tuple[str, ...], limit: int) -> pd.DataFrame:
    if "Date" not in df.columns:
        return df.head(limit)
    mask = df["Date"].astype(str).str.contains("|".join(d.replace("/", "/") for d in dates), regex=True, na=False)
    kept = df[mask]
    return (kept if len(kept) else df).head(limit)


def write_blobs(sport: str, out: Path) -> list[dict[str, Any]]:
    _, dates = TARGETS[sport]
    entries: list[dict[str, Any]] = []
    dest = out / sport
    dest.mkdir(parents=True, exist_ok=True)
    for sha, when, path in pick_blobs(sport):
        if sport == "nfl":
            target = dest / f"{sha}.csv"
            _keep(pd.read_csv(path), dates, MAX_GAMES).to_csv(target, index=False)
        else:
            target = dest / f"{sha}.xlsx"
            sheets = pd.read_excel(path, sheet_name=None)
            with pd.ExcelWriter(target, engine="openpyxl") as writer:
                for name, df in sheets.items():
                    _keep(df, dates, MAX_GAMES if name == "FBS" else OTHER_ROWS).to_excel(writer, sheet_name=name, index=False)
        entries.append({"sport": sport, "sha": sha, "commit_date": when.isoformat(), "file": f"{sport}/{target.name}"})
        print(f"  {sport} {sha} {when.isoformat()} -> {target.name}")
    return entries


def fixture_rows(out: Path, sport: str, entries: list[dict[str, Any]]) -> pd.DataFrame:
    paths = [(e["sha"], e["commit_date"], out / e["file"]) for e in entries if e["sport"] == sport]
    return pd.DataFrame(bg.extract_from_files(paths, sport), columns=list(bg.SNAP_COLUMNS))


def write_nflverse(out: Path, wanted: set[tuple[str, str]], cache: Path) -> None:
    src = cache / "nflverse_games.csv"
    df = pd.read_csv(src, dtype=str)
    keep = df[(df["season"] == str(SEASON)) & df.apply(lambda r: (str(r["away_team"]).lower(), str(r["home_team"]).lower()) in wanted, axis=1)]
    keep.to_csv(out / "nflverse_games.csv", index=False)
    print(f"  nflverse_games.csv: {len(keep)} row(s)")


def write_cfbd(out: Path, wanted: set[tuple[str, str]], cache: Path, book: Any) -> None:
    payload = json.loads((cache / f"cfbd_games_{SEASON}.json").read_text(encoding="utf-8"))
    from pipeline.stadiums.loader import slug

    def key(g: dict) -> tuple[str, str]:
        away = book.resolve_team("cfb", g.get("awayTeam") or "", fuzzy=False) or slug(str(g.get("awayTeam") or ""))
        home = book.resolve_team("cfb", g.get("homeTeam") or "", fuzzy=False) or slug(str(g.get("homeTeam") or ""))
        return away, home

    keep = [g for g in payload if key(g) in wanted]
    (out / f"cfbd_games_{SEASON}.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")
    print(f"  cfbd_games_{SEASON}.json: {len(keep)} game(s)")


def write_era5(out: Path, rows: list[bt.GameRow], cache: Path) -> None:
    """One trimmed hourly file per stadium: only the hours around the fixture kickoffs."""
    dest = out / "era5"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    index = bg.era5_index(cache)
    by_stadium: dict[str, list[bt.GameRow]] = {}
    for r in rows:
        if r.stadium_id and r.kickoff_utc:
            by_stadium.setdefault(r.stadium_id, []).append(r)
    n = 0
    for sid, members in sorted(by_stadium.items()):
        entries = index.get(sid) or []
        kicks = [ensure_utc(parse_iso(r.kickoff_utc)) for r in members]
        path = next((p for k in kicks for p in [bg.era5_covers(entries, k.date())] if p), None)
        if path is None:
            print(f"  WARN: no ERA5 cache for {sid}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        hourly = payload.get("hourly") or {}
        times = [str(t) for t in hourly.get("time") or []]
        keep_idx = [
            i for i, t in enumerate(times)
            if any(abs((datetime.fromisoformat(t).replace(tzinfo=bg.UTC) - k).total_seconds()) <= ERA5_PAD_H * 3600 for k in kicks)
        ]
        if not keep_idx:
            continue
        payload["hourly"] = {k: [v[i] for i in keep_idx if i < len(v)] for k, v in hourly.items() if isinstance(v, list)}
        lo, hi = min(kicks).date(), max(kicks).date()
        (dest / f"era5h_{lo.replace(day=1)}_{hi}_{sid}.json").write_text(json.dumps(payload), encoding="utf-8")
        n += 1
    print(f"  era5: {n} stadium file(s)")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=bg.DEFAULT_GIT_CACHE, help="nflverse / CFBD cache from a --from-git run")
    ap.add_argument("--era5-cache", type=Path, default=bg.DEFAULT_ERA5_CACHE)
    a = ap.parse_args(argv)
    out: Path = a.out
    out.mkdir(parents=True, exist_ok=True)
    book = load_stadium_book()

    entries: list[dict[str, Any]] = []
    for sport in ("nfl", "cfb"):
        print(sport)
        entries.extend(write_blobs(sport, out))
    (out / "manifest.json").write_text(json.dumps(entries, indent=1), encoding="utf-8")

    # which games the trimmed blobs reference -> the schedule / result slices they need
    rows: list[bt.GameRow] = []
    for sport in ("nfl", "cfb"):
        snaps = fixture_rows(out, sport, entries)
        sched = bg.load_schedule(sport, [SEASON], cache_dir=a.cache, book=book, no_network=True)
        index = bg.GameIndex(sport, [g for g in sched if g.season == SEASON])
        wanted: set[tuple[str, str]] = set()
        for rec in snaps.to_dict(orient="records"):
            run_ts = ensure_utc(parse_iso(str(rec["run_ts"])))
            g = index.match(str(rec["away_raw"]), str(rec["home_raw"]), bg.game_date(rec.get("date_label"), run_ts), run_ts)
            if g is not None:
                wanted.add((g.away_id, g.home_id))
        print(f"  {sport}: {len(snaps)} rows -> {len(wanted)} game(s)")
        if sport == "nfl":
            write_nflverse(out, wanted, a.cache)
        else:
            write_cfbd(out, wanted, a.cache, book)
        sp_rows, _, _, _ = bg.build_rows(sport, [SEASON], snapshots=snaps, sched=sched, log=lambda _m: None)
        rows.extend(sp_rows)

    write_era5(out, rows, a.era5_cache)
    print(f"fixtures in {out}: {len(entries)} blob(s), {len(rows)} game(s); "
          f"graded {sum(1 for r in rows if r.close_result)} at {utc_iso()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
