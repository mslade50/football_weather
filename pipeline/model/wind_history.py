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


def stadium_wind_history(card: dict, payload: dict | None, *, now: datetime) -> dict:
    sport = card.get("sport")
    stadium = (card.get("stadium") or {}).get("stadium_id")
    result = {"status": "unavailable", "signal": f"{str(sport).upper()} Wind",
              "wins": 0, "losses": 0, "pushes": 0, "n": 0, "seasons": [],
              "as_of": ((payload or {}).get("meta") or {}).get("generated_at")}
    if not stadium or not payload or not isinstance(payload.get("games"), list):
        return result
    result["status"] = "ok"
    seen, seasons = set(), set()
    for row in payload["games"]:
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
        # Closing forecasts are what a bettor could know before the game.
        flags = combined_flags(sport, _number(row.get("wind_fc")), _number(row.get("temp_fc")),
                               _number(row.get("spread_open")), None, None, None)
        if result["signal"] not in flags:
            continue
        seen.add(gid)
        result[{"W": "wins", "L": "losses", "P": "pushes"}[row["under_result"]]] += 1
        season = _number(row.get("season"))
        seasons.add(int(season) if season is not None else kickoff.year)
    result["n"] = len(seen)
    result["seasons"] = sorted(seasons)
    return result
