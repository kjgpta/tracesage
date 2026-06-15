"""A tiny flights MCP server (stdio transport) exposing 2 tools.

Run indirectly by before.py / after.py via MultiServerMCPClient — you do not run
this file yourself. Requires `pip install mcp`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flights")


@mcp.tool()
def search_flights(origin: str, destination: str) -> str:
    """Find the cheapest direct flight between two cities."""
    return f"{origin}->{destination}: 1 direct, $420, departs 09:15"


@mcp.tool()
def baggage_policy(airline: str) -> str:
    """Return the carry-on baggage allowance for an airline."""
    return f"{airline}: 1 carry-on (8kg) + 1 personal item included"


if __name__ == "__main__":
    mcp.run(transport="stdio")
