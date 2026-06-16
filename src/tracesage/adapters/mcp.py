"""MCP tool-source attribution helpers.

langchain-mcp-adapters does not reliably expose which MCP server a tool came from
on the returned LangChain tools (see langchain-mcp-adapters issue #484), so tracesage
records the mapping explicitly. Register tools at setup (before invoking your graph);
the callback handler then tags each tool event with its MCP server, and the UI groups
tools by source.

Typical usage::

    from langchain_mcp_adapters.client import MultiServerMCPClient
    from tracesage.adapters.mcp import register_mcp_client

    client = MultiServerMCPClient({"weather": {...}, "math": {...}})
    tools = await register_mcp_client(tracer, client)   # tools attributed per server
    # ... add your own @tool functions (left as "local"), build the agent with `tools`

Nothing here is imported when you ``import tracesage`` — langchain-mcp-adapters is an
optional dependency (``pip install tracesage[mcp]``) imported lazily on first use.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracesage.tracer import TraceSage

log = logging.getLogger("tracesage.adapters.mcp")

_INSTALL_HINT = (
    "MCP support requires langchain-mcp-adapters. Install with: pip install 'tracesage[mcp]'"
)


def register_mcp_tools(tracer: TraceSage, tools: list[Any], server: str) -> list[Any]:
    """Attribute an explicit list of (already-loaded) LangChain tools to one MCP server.

    Use this when you load tools per-server yourself. Returns the same list for
    convenient chaining. Tools without a ``.name`` are skipped.
    """
    registered = list(tools or [])
    for tool in registered:
        name = getattr(tool, "name", None)
        if name:
            tracer.register_tool_source(name, server)
    return registered


def _server_names(client: Any) -> list[str]:
    """Best-effort extraction of configured server names from a MultiServerMCPClient."""
    for attr in ("connections", "_connections", "server_name_to_config"):
        conns = getattr(client, attr, None)
        if isinstance(conns, dict) and conns:
            return list(conns.keys())
    raise RuntimeError(
        "Could not determine MCP server names from the client; pass tools per server "
        "to register_mcp_tools(tracer, tools, server) instead."
    )


async def _load_server_tools(client: Any, server: str) -> list[Any]:
    """Load just one server's tools, across langchain-mcp-adapters API variants."""
    # Newer API: get_tools(server_name=...).
    get_tools = getattr(client, "get_tools", None)
    if get_tools is not None:
        try:
            return list(await get_tools(server_name=server))
        except TypeError:
            pass  # older signature without server_name — fall through to session()
    # Stable path: open a per-server session and load its tools.
    from langchain_mcp_adapters.tools import load_mcp_tools

    async with client.session(server) as session:
        return list(await load_mcp_tools(session))


async def register_mcp_client(tracer: TraceSage, client: Any) -> list[Any]:
    """Load every server's tools from a langchain-mcp-adapters ``MultiServerMCPClient``
    and attribute each tool to its originating server.

    Returns the aggregated list of LangChain tools (bind these to your agent). Raises
    ImportError with an install hint if langchain-mcp-adapters is not installed. A
    single server that fails to load is logged and skipped (others still register).
    """
    try:
        import langchain_mcp_adapters  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(_INSTALL_HINT) from e

    all_tools: list[Any] = []
    for server in _server_names(client):
        try:
            tools = await _load_server_tools(client, server)
        except Exception as e:
            log.warning("tracesage: failed to load MCP tools for server %r: %s", server, e)
            continue
        all_tools.extend(register_mcp_tools(tracer, tools, server))
        # Persist the full tool list for this server so the topology/inventory can
        # show every tool it provides, even ones the run never invokes.
        names: list[str] = [
            str(n) for n in (getattr(t, "name", None) for t in tools) if n
        ]
        if names:
            try:
                await tracer.db.upsert_mcp_tools(server, names)
            except Exception as e:
                log.warning("tracesage: failed to persist MCP tools for %r: %s", server, e)
    return all_tools
