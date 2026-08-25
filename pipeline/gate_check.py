"""Cheap pre-build gate for CI (shape from golf_scraping/board/gate_check.py).

Decides whether ``pipeline.yml`` should run the build at all, using only httpx +
stdlib (never imports ``pipeline.build`` or pandas): SKIP when the requested
sport(s) have no kickoff inside ``[now - LOOKBACK_HOURS, now + HORIZON_DAYS]``
(CFB dark Jan-Jul, NFL dark Mar-Jul; ARCH §9.3), otherwise SCRAPE.

``HORIZON_DAYS`` is the ODDS horizon (``pipeline.build.ODDS_WINDOW_AFTER_D``, pinned
equal by tests/test_gate_check.py), not the 10-day weather window: books post NFL
week 1+ and CFB weeks ahead, and a run that only records openers / line history is
still a run worth having.

Fail-open: any network/parse/credential problem -> ``scrape`` (a wasted run is
cheap; a missed run is not). ``--force`` / ``PIPELINE_FORCE=1`` bypasses.

Prints ``run=skip|scrape`` and ``need_playwright=true|false`` (plus per-sport
``run_nfl=`` / ``run_cfb=``) to stdout and appends the same lines to
``$GITHUB_OUTPUT`` when set. Phase 1 has no Playwright books, so
``need_playwright`` is always ``false``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from utils.env import load_repo_dotenv

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

HORIZON_DAYS = 45      # == pipeline.build.ODDS_WINDOW_AFTER_D (odds horizon, not the 10-day weather window)
LOOKBACK_HOURS = 6
TIMEOUT_S = 20

NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CFBD_GAMES_URL = "https://api.collegefootballdata.com/games"
USER_AGENT = "football_weather gate_check (mckinleyslade@gmail.com)"

SPORTS = ("nfl", "cfb")


# ---- pure helpers (tested) -------------------------------------------------------

def decide(kickoffs: Iterable[datetime], now: datetime, horizon_days: int = HORIZON_DAYS,
           lookback_hours: int = LOOKBACK_HOURS) -> str:
    """'scrape' when any kickoff falls inside the window, else 'skip'."""
    lo = now - timedelta(hours=lookback_hours)
    hi = now + timedelta(days=horizon_days)
    for k in kickoffs:
        if k.tzinfo is None:
            k = k.replace(tzinfo=UTC)
        if lo <= k <= hi:
            return "scrape"
    return "skip"


def candidate_seasons(now: datetime) -> list[int]:
    """Seasons whose games could be near ``now`` (Jan/Feb postseason belongs to the prior year)."""
    y = now.astimezone(ET).year
    return [y - 1, y] if now.astimezone(ET).month <= 2 else [y]


def parse_nflverse_kickoffs(text: str, seasons: Sequence[int]) -> list[datetime]:
    out: list[datetime] = []
    wanted = {str(s) for s in seasons}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") not in wanted:
            continue
        day = (row.get("gameday") or "").strip()
        if not day:
            continue
        t = (row.get("gametime") or "").strip() or "13:00"
        try:
            local = datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        except ValueError:
            continue
        out.append(local.astimezone(UTC))
    return out


def parse_cfbd_kickoffs(payload: Any) -> list[datetime]:
    out: list[datetime] = []
    if isinstance(payload, dict):
        payload = payload.get("games") or payload.get("data") or []
    for g in payload or []:
        s = g.get("startDate") or g.get("start_date")
        if not s:
            continue
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out.append(dt.astimezone(UTC))
    return out


# ---- fetchers (fail-open) --------------------------------------------------------

def _get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
    import httpx  # local import: keep module importable without httpx for pure tests

    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    r = httpx.get(url, params=params, headers=h, timeout=TIMEOUT_S, follow_redirects=True)
    r.raise_for_status()
    return r.text


def kickoffs_nfl(now: datetime) -> list[datetime]:
    return parse_nflverse_kickoffs(_get(NFLVERSE_GAMES_URL), candidate_seasons(now))


def kickoffs_cfb(now: datetime) -> list[datetime]:
    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CFBD_API_KEY not set")
    out: list[datetime] = []
    for season in candidate_seasons(now):
        for season_type in ("regular", "postseason"):
            text = _get(
                CFBD_GAMES_URL,
                params={"year": season, "seasonType": season_type, "division": "fbs"},
                headers={"Authorization": f"Bearer {key}"},
            )
            out.extend(parse_cfbd_kickoffs(json.loads(text)))
    return out


FETCHERS = {"nfl": kickoffs_nfl, "cfb": kickoffs_cfb}


def decide_sport(sport: str, now: datetime, horizon_days: int = HORIZON_DAYS) -> str:
    try:
        kicks = FETCHERS[sport](now)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        print(f"gate: {sport} fetch failed ({type(exc).__name__}: {exc}) -> scrape", file=sys.stderr)
        return "scrape"
    if not kicks:
        print(f"gate: {sport} no kickoffs parsed -> scrape", file=sys.stderr)
        return "scrape"
    return decide(kicks, now, horizon_days)


def gate(sports: Sequence[str], now: datetime | None = None, force: bool = False,
         horizon_days: int = HORIZON_DAYS) -> dict[str, str]:
    now = now or datetime.now(UTC)
    per: dict[str, str] = {}
    for s in sports:
        per[f"run_{s}"] = "scrape" if force else decide_sport(s, now, horizon_days)
    run = "scrape" if any(v == "scrape" for v in per.values()) else "skip"
    out = {"run": run, "need_playwright": "false"}
    out.update(per)
    return out


def emit(result: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in result.items()]
    for line in lines:
        print(line)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    load_repo_dotenv()
    p = argparse.ArgumentParser(prog="python -m pipeline.gate_check")
    p.add_argument("--sport", choices=("nfl", "cfb", "all"), default="all")
    p.add_argument("--force", action="store_true")
    p.add_argument("--horizon-days", type=int, default=HORIZON_DAYS)
    a = p.parse_args(argv)
    sports = list(SPORTS) if a.sport == "all" else [a.sport]
    force = a.force or os.environ.get("PIPELINE_FORCE") == "1"
    try:
        result = gate(sports, force=force, horizon_days=a.horizon_days)
    except Exception as exc:  # noqa: BLE001
        print(f"gate: unexpected failure ({exc}) -> scrape", file=sys.stderr)
        result = {"run": "scrape", "need_playwright": "false"}
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
