"""A tiny notes MCP server (stdio transport) exposing 2 tools.

Run indirectly by before.py / after.py via MultiServerMCPClient — you do not run
this file yourself. Requires `pip install mcp langchain-mcp-adapters`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("notes")

_NOTES: list[str] = ["Buy oat milk", "Cancel the unused gym membership"]


@mcp.tool()
def add_note(text: str) -> str:
    """Save a short personal note."""
    _NOTES.append(text)
    return f"saved note #{len(_NOTES)}: {text}"


@mcp.tool()
def list_notes() -> str:
    """List all saved personal notes."""
    return "\n".join(f"{i}. {n}" for i, n in enumerate(_NOTES, 1))


if __name__ == "__main__":
    mcp.run(transport="stdio")
