from __future__ import annotations

# WMO (World Meteorological Organization) weather interpretation codes, as
# used by Open-Meteo's `weather_code` field. A short human-readable label
# plus a single well-supported Unicode symbol (Miscellaneous Symbols block,
# not an emoji font glyph) per code -- so the GUI never has to guess.
# https://open-meteo.com/en/docs -- WMO Weather interpretation codes (WW)
_WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "☀"),  # ☀
    1: ("Mainly clear", "☀"),
    2: ("Partly cloudy", "⛅"),  # ⛅
    3: ("Overcast", "☁"),  # ☁
    45: ("Fog", "▒"),  # ▒ (no widely-supported fog glyph; simple texture stand-in)
    48: ("Depositing rime fog", "▒"),
    51: ("Light drizzle", "☂"),  # ☂
    53: ("Moderate drizzle", "☂"),
    55: ("Dense drizzle", "☂"),
    56: ("Light freezing drizzle", "☂"),
    57: ("Dense freezing drizzle", "☂"),
    61: ("Slight rain", "☂"),
    63: ("Moderate rain", "☂"),
    65: ("Heavy rain", "☂"),
    66: ("Light freezing rain", "☂"),
    67: ("Heavy freezing rain", "☂"),
    71: ("Slight snow fall", "❄"),  # ❄
    73: ("Moderate snow fall", "❄"),
    75: ("Heavy snow fall", "❄"),
    77: ("Snow grains", "❄"),
    80: ("Slight rain showers", "☂"),
    81: ("Moderate rain showers", "☂"),
    82: ("Violent rain showers", "☂"),
    85: ("Slight snow showers", "❄"),
    86: ("Heavy snow showers", "❄"),
    95: ("Thunderstorm", "⚡"),  # ⚡
    96: ("Thunderstorm with slight hail", "⚡"),
    99: ("Thunderstorm with heavy hail", "⚡"),
}

_DEFAULT = ("Unknown", "?")


def describe_weather_code(code: int | None) -> tuple[str, str]:
    """Returns (condition_text, symbol) for a WMO weather code."""
    if code is None:
        return _DEFAULT
    return _WMO_CODES.get(int(code), _DEFAULT)
