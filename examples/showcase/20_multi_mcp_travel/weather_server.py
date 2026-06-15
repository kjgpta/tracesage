"""A tiny weather MCP server (stdio transport) exposing 2 tools.

Run indirectly by before.py / after.py via MultiServerMCPClient — you do not run
this file yourself. Requires `pip install mcp`.
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
