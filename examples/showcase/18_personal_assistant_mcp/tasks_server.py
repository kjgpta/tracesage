"""A tiny tasks MCP server (stdio transport) exposing 2 tools.

Run indirectly by before.py / after.py via MultiServerMCPClient — you do not run
this file yourself. Requires `pip install mcp langchain-mcp-adapters`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tasks")

_TASKS: list[str] = ["Renew passport"]


@mcp.tool()
def add_task(title: str) -> str:
    """Add a to-do task."""
    _TASKS.append(title)
    return f"added task #{len(_TASKS)}: {title}"


@mcp.tool()
def list_tasks() -> str:
    """List all open to-do tasks."""
    return "\n".join(f"{i}. {t}" for i, t in enumerate(_TASKS, 1))


if __name__ == "__main__":
    mcp.run(transport="stdio")
