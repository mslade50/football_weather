"""Display-only whole-point retention in the existing wx_last state object."""
from dataclasses import fields, replace
from datetime import datetime
from typing import Any

from pipeline.contracts import Game, Stadium, WeatherForecast, WeatherPoint
from pipeline.model.config import WEATHER_RETENTION_MAX_AGE_H
from pipeline.weather.screening import finite


def usable(forecast: WeatherForecast | None) -> bool:
    return forecast is not None and all(finite(getattr(forecast, key)) for key in ("temp_fg", "wind_fg", "rain_fg_mm"))


def identity(game: Game, stadium: Stadium) -> list[Any]:
    # Reschedules, venue moves and orientation/roof corrections require new data.
    return [game.kickoff_utc.isoformat(), stadium.stadium_id, stadium.lat, stadium.lon,
            stadium.orientation_deg, stadium.roof_type]


def remember(state: dict, game: Game, stadium: Stadium, forecast: WeatherForecast, payload: dict) -> None:
    if usable(forecast) and not forecast.retained and not forecast.point_aged and forecast.run_time is not None:
        state.setdefault("forecasts", {})[game.game_id] = {"identity": identity(game, stadium), "forecast": payload}


def restore(state: dict, game: Game, stadium: Stadium, roof_state: str | None,
            now: datetime, reason: str) -> WeatherForecast | None:
    try:
        entry = state["forecasts"][game.game_id]
        if entry["identity"] != identity(game, stadium):
            return None
        payload = dict(entry["forecast"])
        # Old/incomplete snapshots are not enough to preserve the weather contract.
        if set(payload) != {field.name for field in fields(WeatherForecast)}:
            return None
        stamp = datetime.fromisoformat(payload["run_time"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= WEATHER_RETENTION_MAX_AGE_H * 3600:
            return None
        payload["run_time"] = stamp
        payload["hourly"] = [WeatherPoint(**{**point, "t": datetime.fromisoformat(point["t"].replace("Z", "+00:00"))})
                             for point in payload["hourly"]]
        payload["ecmwf_apart_fields"] = tuple(payload["ecmwf_apart_fields"])
        forecast = WeatherForecast(**payload)
        if (not usable(forecast) or forecast.retained or forecast.point_aged
                or forecast.game_id != game.game_id or forecast.roof_state != roof_state):
            return None
        return replace(forecast, point_aged=True, retained=True, retained_from=entry["forecast"]["run_time"],
                       retained_reason=reason)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
