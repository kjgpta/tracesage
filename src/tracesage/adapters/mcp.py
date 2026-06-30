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


def _server_connection(client: Any, server: str) -> Any | None:
    """Best-effort extraction of one server's connection config from the client."""
    for attr in ("connections", "_connections", "server_name_to_config"):
        conns = getattr(client, attr, None)
        if isinstance(conns, dict) and server in conns:
            return conns[server]
    return None


async def _load_server_tools(
    client: Any, server: str, *, handle_tool_errors: bool = True
) -> list[Any]:
    """Load just one server's tools, across langchain-mcp-adapters API variants.

    When ``handle_tool_errors`` is False, an MCP tool that errors server-side
    raises (a ``ToolException``) instead of returning the error as tool content —
    so the failure propagates and fails the run. ``MultiServerMCPClient.get_tools``
    can't pass that flag, so we go straight to the session loader in that case.
    """
    # Newer API: get_tools(server_name=...). Only usable when we're happy to let
    # tool errors come back as content (the adapter default).
    get_tools = getattr(client, "get_tools", None)
    if handle_tool_errors and get_tools is not None:
        try:
            return list(await get_tools(server_name=server))
        except TypeError:
            pass  # older signature without server_name — fall through to connection()
    from langchain_mcp_adapters.tools import load_mcp_tools

    # Load tools bound to the server's *connection config* (not an open session),
    # so each tool call opens its own fresh session — exactly what get_tools does.
    # Binding to a session from `client.session(...)` would close it on exit and
    # every later tool call would raise ClosedResourceError.
    connection = _server_connection(client, server)
    if connection is not None:
        try:
            return list(
                await load_mcp_tools(
                    None, connection=connection, handle_tool_errors=handle_tool_errors
                )
            )
        except TypeError:
            return list(await load_mcp_tools(None, connection=connection))
    # Last resort (no discoverable connection config): a per-server session. Tools
    # are only valid while the session is open, but it keeps older clients working.
    async with client.session(server) as session:
        try:
            return list(
                await load_mcp_tools(session, handle_tool_errors=handle_tool_errors)
            )
        except TypeError:
            return list(await load_mcp_tools(session))


async def register_mcp_client(
    tracer: TraceSage, client: Any, *, handle_tool_errors: bool = True
) -> list[Any]:
    """Load every server's tools from a langchain-mcp-adapters ``MultiServerMCPClient``
    and attribute each tool to its originating server.

    Returns the aggregated list of LangChain tools (bind these to your agent). Raises
    ImportError with an install hint if langchain-mcp-adapters is not installed. A
    single server that fails to load is logged and skipped (others still register).

    ``handle_tool_errors`` (default True) matches langchain-mcp-adapters' behaviour:
    a tool that errors server-side returns the error as tool content, which the model
    sees and can recover from. Pass False to make MCP tool errors *raise* instead, so
    a broken tool call fails the run — surfaced in the UI as a red error node on the
    exact tool call. (Mirrors ``ToolNode(handle_tool_errors=...)`` for local tools.)
    """
    try:
        import langchain_mcp_adapters  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(_INSTALL_HINT) from e

    all_tools: list[Any] = []
    for server in _server_names(client):
        try:
            tools = await _load_server_tools(
                client, server, handle_tool_errors=handle_tool_errors
            )
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
