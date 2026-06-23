"""Weather MCP server (stdio transport) for the trip-planner demo.

Seven tools: get_weather, get_7day_forecast, get_hourly_forecast, get_air_quality,
get_uv_index, get_sun_times, get_travel_advisory.
Data is realistic-looking but fully hardcoded — no external API calls.
Run indirectly by demo.py via MultiServerMCPClient.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

_CURRENT: dict[str, str] = {
    "tokyo": "Tokyo — 24°C, partly cloudy, humidity 68%, UV index 6 (high), wind 12 km/h E",
    "london": "London — 14°C, overcast, humidity 82%, light drizzle, wind 20 km/h SW",
    "new york": "New York — 21°C, clear skies, humidity 55%, UV index 5 (moderate), wind 9 km/h NW",
    "nyc": "New York — 21°C, clear skies, humidity 55%, UV index 5 (moderate), wind 9 km/h NW",
    "paris": "Paris — 18°C, mostly sunny, humidity 60%, UV index 4 (moderate), wind 14 km/h W",
}

_FORECAST: dict[str, str] = {
    "tokyo": (
        "Tokyo 7-day forecast:\n"
        "  Mon   24°C  ⛅  partly cloudy\n"
        "  Tue   26°C  ☀   sunny\n"
        "  Wed   22°C  🌧  showers (70% chance)\n"
        "  Thu   25°C  ☀   sunny\n"
        "  Fri   27°C  ☀   sunny — warmest day\n"
        "  Sat   23°C  ⛅  partly cloudy\n"
        "  Sun   21°C  🌧  rain likely (60% chance)\n"
        "Best days to be outside: Thu-Sat. Pack a light rain jacket."
    ),
    "london": (
        "London 7-day forecast:\n"
        "  Mon   14°C  🌧  rain\n"
        "  Tue   15°C  ⛅  cloudy\n"
        "  Wed   16°C  ⛅  partly cloudy\n"
        "  Thu   18°C  ☀   sunny spell\n"
        "  Fri   17°C  ⛅  partly cloudy\n"
        "  Sat   13°C  🌧  heavy rain\n"
        "  Sun   12°C  🌧  rain\n"
        "Classic London — an umbrella is non-negotiable."
    ),
}


@mcp.tool()
def get_weather(city: str) -> str:
    """Get current weather conditions for a city."""
    return _CURRENT.get(city.lower(), f"{city} — 20°C, partly cloudy, humidity 65%, conditions normal")


@mcp.tool()
def get_7day_forecast(city: str) -> str:
    """Get a 7-day weather forecast for a city."""
    return _FORECAST.get(
        city.lower(),
        (
            f"{city} 7-day forecast:\n"
            "  Mon-Wed  22-25°C  ☀   sunny to partly cloudy\n"
            "  Thu      20°C     ⛅  chance of showers\n"
            "  Fri-Sun  23-26°C  ☀   mostly clear\n"
            "Good travel weather overall."
        ),
    )


@mcp.tool()
def get_hourly_forecast(city: str) -> str:
    """Get an hour-by-hour forecast for the next 12 hours in a city."""
    return (
        f"{city} — next 12 hours:\n"
        "  09:00  22°C  ☀   clear\n"
        "  12:00  25°C  ☀   sunny, UV high\n"
        "  15:00  26°C  ⛅  partly cloudy\n"
        "  18:00  23°C  ⛅  cloudy\n"
        "  21:00  20°C  🌙  clear night\n"
        "Best window for outdoor plans: 09:00-15:00."
    )


@mcp.tool()
def get_air_quality(city: str) -> str:
    """Get the current air quality index (AQI) and pollutant breakdown for a city."""
    return (
        f"{city} — air quality:\n"
        "  AQI:    42 (Good)\n"
        "  PM2.5:  10 µg/m³   PM10: 18 µg/m³\n"
        "  Ozone:  moderate\n"
        "No health precautions needed for outdoor activity."
    )


@mcp.tool()
def get_uv_index(city: str) -> str:
    """Get the current and peak UV index for a city, with sun-protection advice."""
    return (
        f"{city} — UV index:\n"
        "  Current:  6 (High)\n"
        "  Peak:     8 (Very High) around 13:00\n"
        "Advice: SPF 30+, sunglasses, and a hat midday. Seek shade 11:00-15:00."
    )


@mcp.tool()
def get_sun_times(city: str) -> str:
    """Get sunrise, sunset, and daylight-hours for a city today."""
    return (
        f"{city} — sun times today:\n"
        "  Sunrise:  05:42\n"
        "  Sunset:   18:31\n"
        "  Daylight: 12h 49m\n"
        "  Golden hour: 17:45-18:31 (great for photos)"
    )


@mcp.tool()
def get_travel_advisory(city: str) -> str:
    """Get weather-related travel advisories or alerts for a city."""
    return (
        f"{city} — travel advisory:\n"
        "  No active severe-weather alerts.\n"
        "  Seasonal note: light afternoon showers possible — pack a compact umbrella.\n"
        "  Transport running normally."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
