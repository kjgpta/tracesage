"""A tiny math MCP server (stdio transport) exposing 2 tools.

Run indirectly by examples/mcp/main.py via MultiServerMCPClient — you do
not run this file yourself. Requires `pip install 'tracelens[mcp]'`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("math")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


if __name__ == "__main__":
    mcp.run(transport="stdio")
