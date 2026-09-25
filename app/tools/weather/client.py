from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_CURRENT_PARAMS = "temperature_2m,apparent_temperature,weather_code,precipitation,relative_humidity_2m,wind_speed_10m,wind_direction_10m,cloud_cover"
_HOURLY_PARAMS = "temperature_2m,precipitation_probability,weather_code,visibility"
_DAILY_PARAMS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"


class WeatherApiError(Exception):
    """Clean, normalized failure from the weather client -- never a raw
    urllib/HTTP exception. `tool.py` only ever needs to catch this one type."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class WeatherClientProtocol(Protocol):
    """What WeatherTool needs from a weather backend. Satisfied by
    OpenMeteoClient in production and by a fake in tests -- neither side
    imports the other."""

    def geocode(self, location: str) -> dict[str, Any] | None:
        ...

    def get_forecast(self, *, latitude: float, longitude: float, timezone: str, forecast_days: int = 7) -> dict[str, Any]:
        ...


class OpenMeteoClient:
    """Thin wrapper around the free Open-Meteo API (no API key required).
    All HTTP/urllib specifics live here -- nothing above this layer makes a
    network call directly (spec section 2/6)."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def geocode(self, location: str) -> dict[str, Any] | None:
        params = urllib.parse.urlencode({"name": location, "count": 1, "language": "en", "format": "json"})
        payload = self._get(f"{GEOCODING_URL}?{params}")
        results = payload.get("results") or []
        if not results:
            return None
        return results[0]

    def get_forecast(self, *, latitude: float, longitude: float, timezone: str, forecast_days: int = 7) -> dict[str, Any]:
        params = urllib.parse.urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": _CURRENT_PARAMS,
                "hourly": _HOURLY_PARAMS,
                "daily": _DAILY_PARAMS,
                "timezone": timezone,
                "forecast_days": forecast_days,
            }
        )
        return self._get(f"{FORECAST_URL}?{params}")

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "Miki-Weather/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise WeatherApiError("rate_limited", "Weather API rate limit reached; try again shortly.") from exc
            if exc.code in (401, 403):
                raise WeatherApiError("authentication_failed", "Weather API rejected the request credentials.") from exc
            if exc.code == 400:
                raise WeatherApiError("invalid_location", "That location could not be understood by the weather service.") from exc
            logger.warning("Weather API HTTP error (status=%s)", exc.code)
            raise WeatherApiError("api_error", f"Weather API error (status {exc.code}).") from exc
        except urllib.error.URLError as exc:
            logger.warning("Could not reach the weather API: %s", exc)
            raise WeatherApiError("network_error", "Could not reach the weather service.") from exc
        except TimeoutError as exc:
            logger.warning("Weather API request timed out")
            raise WeatherApiError("timeout", "The weather service took too long to respond.") from exc

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Weather API returned malformed JSON")
            raise WeatherApiError("malformed_response", "The weather service returned an unreadable response.") from exc
