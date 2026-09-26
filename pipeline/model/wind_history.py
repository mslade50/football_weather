"""Stadium under records for the board's named wind signals, from graded games."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pipeline.model.signals import combined_flags
from utils.timeutil import parse_iso


def _number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def stadium_wind_history(card: dict, payload: dict | None, *, now: datetime,
                         archive: dict | None = None) -> dict:
    archive_ok = bool(archive and archive.get("schema_version") == 1
                      and isinstance(archive.get("games"), list) and isinstance(archive.get("actual_games"), list))
    live_rows = (payload or {}).get("games")
    archive = archive if archive_ok else {}
    stamp = ((payload or {}).get("meta") or {}).get("generated_at")
    if not isinstance(live_rows, list):
        live_rows = []
        stamp = (archive.get("meta") or {}).get("generated_at")
    result = _record(card, live_rows + archive.get("games", []), now=now, actual=False)
    result.update(as_of=stamp, archive_available=archive_ok,
                  status="ok" if archive_ok or isinstance((payload or {}).get("games"), list) else "unavailable")
    result["actual"] = _record(card, archive.get("actual_games", []), now=now, actual=True)
    result["actual"]["status"] = "ok" if archive_ok else "unavailable"
    if not (card.get("stadium") or {}).get("stadium_id"):
        result["status"] = result["actual"]["status"] = "unavailable"
    return result


def _record(card: dict, rows: list, *, now: datetime, actual: bool) -> dict:
    sport = card.get("sport")
    stadium = (card.get("stadium") or {}).get("stadium_id")
    result = {"status": "unavailable", "signal": f"{str(sport).upper()} Wind",
              "wins": 0, "losses": 0, "pushes": 0, "n": 0, "seasons": [],
              "as_of": None}
    if not stadium:
        return result
    result["status"] = "ok"
    seen, seasons = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        gid = row.get("game_id")
        if (not gid or gid in seen or gid == card.get("game_id") or row.get("sport") != sport
                or row.get("stadium_id") != stadium or row.get("roof_state") in ("closed", "dome")
                or row.get("under_result") not in ("W", "L", "P") or _number(row.get("total_close")) is None):
            continue
        try:
            kickoff = parse_iso(row["kickoff_utc"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if kickoff >= now:
            continue
        seen.add(gid)
        if actual:
            # Descriptive observed-wind band, not the forecast signal's temp/spread rule.
            wind = _number(row.get("wind_act"))
            if wind is None or wind < 15:
                continue
        else:
            flags = combined_flags(sport, _number(row.get("wind_fc")), _number(row.get("temp_fc")),
                                   _number(row.get("spread_open")), None, None, None)
            if result["signal"] not in flags:
                continue
        result[{"W": "wins", "L": "losses", "P": "pushes"}[row["under_result"]]] += 1
        season = _number(row.get("season"))
        seasons.add(int(season) if season is not None else kickoff.year)
    result["n"] = result["wins"] + result["losses"] + result["pushes"]
    result["seasons"] = sorted(seasons)
    return result
