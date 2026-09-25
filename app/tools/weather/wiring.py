from __future__ import annotations

from app.tools.weather.client import OpenMeteoClient
from app.tools.weather.tool import WeatherTool


def build_weather_tool(
    *,
    default_location: str | None = None,
    cache_ttl_seconds: float = 300.0,
) -> WeatherTool:
    return WeatherTool(
        client=OpenMeteoClient(),
        default_location=default_location,
        cache_ttl_seconds=cache_ttl_seconds,
    )
