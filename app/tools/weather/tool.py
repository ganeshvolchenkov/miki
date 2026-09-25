from __future__ import annotations

import logging
from typing import Any

from app.tools.base import ACCESS_READ, Tool, ToolError, ToolOperation, ToolResult
from app.tools.weather.cache import TTLCache
from app.tools.weather.client import WeatherApiError, WeatherClientProtocol
from app.tools.weather.models import GeocodedLocation, WeatherCurrent, WeatherDailyEntry, WeatherForecast, WeatherHourlyEntry
from app.tools.weather.wmo_codes import describe_weather_code

logger = logging.getLogger(__name__)

_TIMEFRAMES = {"current", "today", "tomorrow", "this_week"}

_LOCATION_PARAM = {
    "type": "string",
    "description": "City name, e.g. 'Amsterdam' or 'London'. Defaults to the configured default location if omitted.",
}

_CURRENT_WEATHER_PARAMETERS = {
    "type": "object",
    "properties": {"location": _LOCATION_PARAM},
    "required": [],
}

_HOURLY_FORECAST_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": _LOCATION_PARAM,
        "hours": {"type": "integer", "description": "How many hours ahead to include (1-48). Defaults to 12."},
    },
    "required": [],
}

_DAILY_FORECAST_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": _LOCATION_PARAM,
        "days": {"type": "integer", "description": "How many days ahead to include (1-7). Defaults to 5."},
    },
    "required": [],
}

_GET_WEATHER_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": _LOCATION_PARAM,
        "timeframe": {
            "type": "string",
            "enum": sorted(_TIMEFRAMES),
            "description": "Which part of the forecast to answer about. Defaults to 'current'.",
        },
    },
    "required": [],
}


def _hour_prefix(timestamp: str) -> str:
    return timestamp[:13] if timestamp else ""


def _find_hourly_index_for_time(hourly_raw: dict[str, Any], target_time: str) -> int | None:
    target_prefix = _hour_prefix(target_time)
    if not target_prefix:
        return None
    for index, entry_time in enumerate(hourly_raw.get("time") or []):
        if _hour_prefix(entry_time) == target_prefix:
            return index
    return None


def _parse_forecast(location: GeocodedLocation, raw: dict[str, Any]) -> WeatherForecast:
    """Normalizes Open-Meteo's raw JSON into Miki's internal models. This is
    the only place that knows Open-Meteo's response shape (spec section 5:
    normalize provider-specific formats, never pass raw JSON onward)."""
    current_raw = raw.get("current") or {}
    hourly_raw = raw.get("hourly") or {}
    daily_raw = raw.get("daily") or {}

    current: WeatherCurrent | None = None
    if current_raw:
        condition, symbol = describe_weather_code(current_raw.get("weather_code"))
        hourly_index = _find_hourly_index_for_time(hourly_raw, current_raw.get("time", ""))
        precipitation_probability = None
        visibility = None
        if hourly_index is not None:
            precip_list = hourly_raw.get("precipitation_probability") or []
            visibility_list = hourly_raw.get("visibility") or []
            if hourly_index < len(precip_list):
                precipitation_probability = precip_list[hourly_index]
            if hourly_index < len(visibility_list):
                visibility = visibility_list[hourly_index]

        daily_sunrise = daily_raw.get("sunrise") or []
        daily_sunset = daily_raw.get("sunset") or []

        current = WeatherCurrent(
            location=location.display_name,
            timestamp=current_raw.get("time", ""),
            temperature=current_raw.get("temperature_2m"),
            feels_like=current_raw.get("apparent_temperature"),
            condition=condition,
            condition_symbol=symbol,
            precipitation_probability=precipitation_probability,
            precipitation_amount=current_raw.get("precipitation"),
            humidity=current_raw.get("relative_humidity_2m"),
            wind_speed=current_raw.get("wind_speed_10m"),
            wind_direction=current_raw.get("wind_direction_10m"),
            cloud_cover=current_raw.get("cloud_cover"),
            visibility=visibility,
            sunrise=daily_sunrise[0] if daily_sunrise else None,
            sunset=daily_sunset[0] if daily_sunset else None,
        )

    hourly: list[WeatherHourlyEntry] = []
    times = hourly_raw.get("time") or []
    temperatures = hourly_raw.get("temperature_2m") or []
    precip_probs = hourly_raw.get("precipitation_probability") or []
    codes = hourly_raw.get("weather_code") or []
    for i, timestamp in enumerate(times):
        condition, symbol = describe_weather_code(codes[i] if i < len(codes) else None)
        hourly.append(
            WeatherHourlyEntry(
                timestamp=timestamp,
                temperature=temperatures[i] if i < len(temperatures) else None,
                condition=condition,
                condition_symbol=symbol,
                precipitation_probability=precip_probs[i] if i < len(precip_probs) else None,
            )
        )

    daily: list[WeatherDailyEntry] = []
    d_times = daily_raw.get("time") or []
    d_codes = daily_raw.get("weather_code") or []
    d_max = daily_raw.get("temperature_2m_max") or []
    d_min = daily_raw.get("temperature_2m_min") or []
    d_precip = daily_raw.get("precipitation_probability_max") or []
    d_sunrise = daily_raw.get("sunrise") or []
    d_sunset = daily_raw.get("sunset") or []
    for i, date in enumerate(d_times):
        condition, symbol = describe_weather_code(d_codes[i] if i < len(d_codes) else None)
        daily.append(
            WeatherDailyEntry(
                date=date,
                condition=condition,
                condition_symbol=symbol,
                temperature_high=d_max[i] if i < len(d_max) else None,
                temperature_low=d_min[i] if i < len(d_min) else None,
                precipitation_probability=d_precip[i] if i < len(d_precip) else None,
                sunrise=d_sunrise[i] if i < len(d_sunrise) else None,
                sunset=d_sunset[i] if i < len(d_sunset) else None,
            )
        )

    return WeatherForecast(location=location, current=current, hourly=hourly, daily=daily)


class WeatherTool(Tool):
    """Current conditions and forecasts, via Open-Meteo (no API key needed).
    Every operation is read-only -- there is nothing to confirm or undo."""

    name = "weather"
    description = "Get current weather conditions and hourly/daily forecasts for any location."

    def __init__(
        self,
        *,
        client: WeatherClientProtocol,
        default_location: str | None = None,
        cache_ttl_seconds: float = 300.0,
        geocode_cache_ttl_seconds: float = 86400.0,
    ) -> None:
        super().__init__(name=self.name, description=self.description)
        self._client = client
        self.default_location = (default_location or "").strip() or None
        self._forecast_cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self._geocode_cache = TTLCache(ttl_seconds=geocode_cache_ttl_seconds)
        self.operations = [
            ToolOperation("get_current_weather", "Get current weather conditions for a location.", _CURRENT_WEATHER_PARAMETERS, ACCESS_READ),
            ToolOperation("get_hourly_forecast", "Get an hourly forecast for a location.", _HOURLY_FORECAST_PARAMETERS, ACCESS_READ),
            ToolOperation("get_daily_forecast", "Get a multi-day forecast for a location.", _DAILY_FORECAST_PARAMETERS, ACCESS_READ),
            ToolOperation(
                "get_weather",
                "Answer a general weather question for a location and timeframe (current, today, tomorrow, this_week). "
                "Use this for vaguer questions; use the more specific operations when the user wants precise hourly/daily detail.",
                _GET_WEATHER_PARAMETERS,
                ACCESS_READ,
            ),
        ]

    def is_available(self) -> bool:
        return True  # Open-Meteo needs no credentials -- always usable once the tool is enabled.

    def status(self) -> dict[str, Any]:
        return {"available": True, "provider": "open-meteo", "default_location": self.default_location}

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        handler = {
            "get_current_weather": self._get_current_weather,
            "get_hourly_forecast": self._get_hourly_forecast,
            "get_daily_forecast": self._get_daily_forecast,
            "get_weather": self._get_weather,
        }.get(operation)
        if handler is None:
            return ToolResult(success=False, operation=operation, error_type="operation_not_found", message=f"Unknown weather operation '{operation}'.")

        try:
            return handler(arguments)
        except ToolError as exc:
            return ToolResult(success=False, operation=operation, error_type=exc.error_type, message=exc.message)

    # -- operations ---------------------------------------------------

    def _get_current_weather(self, arguments: dict[str, Any]) -> ToolResult:
        location = self._resolve_location(arguments.get("location"))
        forecast = self._get_forecast_bundle(location)
        if forecast.current is None:
            raise ToolError("api_error", "Current weather data was not available for that location.")
        return ToolResult(success=True, operation="get_current_weather", data={"location": location.display_name, "current": forecast.current.to_dict()})

    def _get_hourly_forecast(self, arguments: dict[str, Any]) -> ToolResult:
        location = self._resolve_location(arguments.get("location"))
        hours = self._bounded_int(arguments.get("hours"), default=12, low=1, high=48, field="hours")
        forecast = self._get_forecast_bundle(location)
        entries = forecast.hourly[:hours]
        return ToolResult(success=True, operation="get_hourly_forecast", data={"location": location.display_name, "hourly": [entry.to_dict() for entry in entries]})

    def _get_daily_forecast(self, arguments: dict[str, Any]) -> ToolResult:
        location = self._resolve_location(arguments.get("location"))
        days = self._bounded_int(arguments.get("days"), default=5, low=1, high=7, field="days")
        forecast = self._get_forecast_bundle(location)
        entries = forecast.daily[:days]
        return ToolResult(success=True, operation="get_daily_forecast", data={"location": location.display_name, "daily": [entry.to_dict() for entry in entries]})

    def _get_weather(self, arguments: dict[str, Any]) -> ToolResult:
        location = self._resolve_location(arguments.get("location"))
        timeframe = (arguments.get("timeframe") or "current").strip().lower()
        if timeframe not in _TIMEFRAMES:
            raise ToolError("invalid_arguments", f"Unknown timeframe '{timeframe}'. Use current, today, tomorrow, or this_week.")

        forecast = self._get_forecast_bundle(location)

        if timeframe == "current":
            if forecast.current is None:
                raise ToolError("api_error", "Current weather data was not available for that location.")
            return ToolResult(success=True, operation="get_weather", data={"timeframe": "current", "location": location.display_name, "current": forecast.current.to_dict()})

        if timeframe in {"today", "tomorrow"}:
            index = 0 if timeframe == "today" else 1
            if index >= len(forecast.daily):
                raise ToolError("api_error", f"No forecast data available for {timeframe}.")
            day = forecast.daily[index]
            hourly_for_day = [entry.to_dict() for entry in forecast.hourly if entry.timestamp[:10] == day.date]
            return ToolResult(
                success=True, operation="get_weather",
                data={"timeframe": timeframe, "location": location.display_name, "summary": day.to_dict(), "hourly": hourly_for_day},
            )

        return ToolResult(success=True, operation="get_weather", data={"timeframe": "this_week", "location": location.display_name, "daily": [entry.to_dict() for entry in forecast.daily]})

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _bounded_int(value: Any, *, default: int, low: int, high: int, field: str) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ToolError("invalid_arguments", f"{field} must be a number.")
        return max(low, min(parsed, high))

    def _resolve_location(self, location_arg: Any) -> GeocodedLocation:
        location_name = (str(location_arg).strip() if location_arg else "") or (self.default_location or "")
        if not location_name:
            raise ToolError("invalid_arguments", "No location was provided and no default location is configured.")

        cache_key = location_name.lower()
        cached = self._geocode_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            raw = self._client.geocode(location_name)
        except WeatherApiError as exc:
            raise ToolError(exc.error_type, exc.message) from exc

        if raw is None:
            raise ToolError("invalid_location", f"I couldn't find a location called '{location_name}'.")

        geocoded = GeocodedLocation(
            name=raw.get("name", location_name),
            country=raw.get("country"),
            latitude=raw["latitude"],
            longitude=raw["longitude"],
            timezone=raw.get("timezone"),
        )
        self._geocode_cache.set(cache_key, geocoded)
        return geocoded

    def _get_forecast_bundle(self, location: GeocodedLocation) -> WeatherForecast:
        cache_key = (round(location.latitude, 4), round(location.longitude, 4))
        cached = self._forecast_cache.get(cache_key)
        if cached is not None:
            logger.info("Weather cache hit for %s", location.display_name)
            return cached

        logger.info("Weather cache miss for %s -- calling provider", location.display_name)
        try:
            raw = self._client.get_forecast(latitude=location.latitude, longitude=location.longitude, timezone=location.timezone or "auto", forecast_days=7)
        except WeatherApiError as exc:
            raise ToolError(exc.error_type, exc.message) from exc

        forecast = _parse_forecast(location, raw)
        self._forecast_cache.set(cache_key, forecast)
        return forecast
