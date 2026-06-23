"""Weather MCP server (stdio transport) for the trip-planner demo.

Two tools: get_weather and get_7day_forecast.
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
