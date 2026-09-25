from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GeocodedLocation:
    """A resolved location -- Miki's own shape, never the raw geocoder payload."""

    name: str
    country: str | None
    latitude: float
    longitude: float
    timezone: str | None = None

    @property
    def display_name(self) -> str:
        return f"{self.name}, {self.country}" if self.country else self.name

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "country": self.country, "latitude": self.latitude, "longitude": self.longitude}


@dataclass
class WeatherCurrent:
    """Current conditions. Fields the provider doesn't reliably give (e.g.
    sunrise/sunset, which Open-Meteo only reports per-day) stay None rather
    than being guessed."""

    location: str
    timestamp: str
    temperature: float
    feels_like: float
    condition: str
    condition_symbol: str
    precipitation_probability: float | None = None
    precipitation_amount: float | None = None
    humidity: float | None = None
    wind_speed: float | None = None
    wind_direction: float | None = None
    cloud_cover: float | None = None
    visibility: float | None = None
    sunrise: str | None = None
    sunset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "timestamp": self.timestamp,
            "temperature": self.temperature,
            "feels_like": self.feels_like,
            "condition": self.condition,
            "condition_symbol": self.condition_symbol,
            "precipitation_probability": self.precipitation_probability,
            "precipitation_amount": self.precipitation_amount,
            "humidity": self.humidity,
            "wind_speed": self.wind_speed,
            "wind_direction": self.wind_direction,
            "cloud_cover": self.cloud_cover,
            "visibility": self.visibility,
            "sunrise": self.sunrise,
            "sunset": self.sunset,
        }


@dataclass
class WeatherHourlyEntry:
    timestamp: str
    temperature: float
    condition: str
    condition_symbol: str
    precipitation_probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "temperature": self.temperature,
            "condition": self.condition,
            "condition_symbol": self.condition_symbol,
            "precipitation_probability": self.precipitation_probability,
        }


@dataclass
class WeatherDailyEntry:
    date: str
    condition: str
    condition_symbol: str
    temperature_high: float
    temperature_low: float
    precipitation_probability: float | None = None
    sunrise: str | None = None
    sunset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "condition": self.condition,
            "condition_symbol": self.condition_symbol,
            "temperature_high": self.temperature_high,
            "temperature_low": self.temperature_low,
            "precipitation_probability": self.precipitation_probability,
            "sunrise": self.sunrise,
            "sunset": self.sunset,
        }


@dataclass
class WeatherForecast:
    """The full bundle for a location -- current + hourly + daily -- fetched
    and cached together in one API call, then sliced per-operation by the
    Tool. Never handed to the Brain or GUI as raw provider JSON."""

    location: GeocodedLocation
    current: WeatherCurrent | None
    hourly: list[WeatherHourlyEntry] = field(default_factory=list)
    daily: list[WeatherDailyEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location.display_name,
            "current": self.current.to_dict() if self.current else None,
            "hourly": [entry.to_dict() for entry in self.hourly],
            "daily": [entry.to_dict() for entry in self.daily],
        }
