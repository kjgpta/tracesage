"""18 — Personal Assistant over MCP (plain LangGraph).

A ReAct agent whose tools come from THREE sources: an in-code `current_time` tool plus
two local FastMCP stdio servers (`notes` with add/list, `tasks` with add/list) loaded via
langchain-mcp-adapters' MultiServerMCPClient. The agent juggles all of them to handle a
short productivity request.

Needs the MCP extras (guarded below):
    pip install mcp langchain-mcp-adapters

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:  # pragma: no cover
    sys.exit("This example needs MCP support. Install: pip install mcp langchain-mcp-adapters")

HERE = Path(__file__).resolve().parent


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


@tool
def current_time() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def make_client() -> MultiServerMCPClient:
    # Two local MCP servers over stdio (started as subprocesses by the client).
    return MultiServerMCPClient(
        {
            "notes": {
                "command": sys.executable,
                "args": [str(HERE / "notes_server.py")],
                "transport": "stdio",
            },
            "tasks": {
                "command": sys.executable,
                "args": [str(HERE / "tasks_server.py")],
                "transport": "stdio",
            },
        }
    )


def build_agent(tools: list) -> Runnable:
    return create_react_agent(make_llm(), tools)


async def main() -> None:
    client = make_client()
    request = (
        "Add a note 'water the plants', add a task 'book dentist', then tell me the "
        "current time and list everything you saved."
    )
    print(f"Q: {request}\n")

    mcp_tools = await client.get_tools()
    tools = [current_time, *mcp_tools]
    agent = build_agent(tools)
    result = await agent.ainvoke(
        {"messages": [("user", request)]},
        config={"recursion_limit": 12},
    )
    print("A:", result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
