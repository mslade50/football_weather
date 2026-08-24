"""Weather stack (ARCH §6): Open-Meteo forecast client, NWS fallback, per-game merge."""

from pipeline.weather.merge import MergeResult, build_forecast

__all__ = ["MergeResult", "build_forecast"]
