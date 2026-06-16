"""A tiny weather MCP server (stdio transport) exposing 4 tools (one, air_quality,
is deliberately left uncalled by the example graphs).

Run indirectly by examples/mcp/main.py via MultiServerMCPClient — you do
not run this file yourself. Requires `pip install 'tracesage[mcp]'`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")


@mcp.tool()
def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"{city}: 18C, partly cloudy"


@mcp.tool()
def get_forecast(city: str) -> str:
    """Return a short multi-day forecast for a city."""
    return f"{city}: sunny / rain / sunny"


@mcp.tool()
def severe_alerts(region: str) -> str:
    """Return active severe-weather alerts for a region."""
    return f"{region}: no active alerts"


@mcp.tool()
def air_quality(city: str) -> str:
    """Return the air-quality index for a city.

    The example graphs deliberately do NOT call this one — it demonstrates that
    tracesage still shows a server's tools in the topology even when uninvoked.
    """
    return f"{city}: AQI 42 (good)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
