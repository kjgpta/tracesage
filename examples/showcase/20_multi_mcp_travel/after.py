"""20 — Multi-MCP Travel Planner (with tracesage).

Identical to before.py except for the tracesage lines marked below. `register_mcp_client`
records which MCP server each tool came from, so the trace shows ONE agent fanning out to
TWO MCP servers — flight tool calls attributed to `flights`, weather tool calls to
`weather`. Open the printed link for the multi-MCP visualization.

Needs the MCP extras (guarded below):
    pip install 'tracesage[mcp]' mcp langchain-mcp-adapters

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.prebuilt import create_react_agent

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:  # pragma: no cover
    sys.exit("This example needs MCP support. Install: pip install mcp langchain-mcp-adapters")

from tracesage import TraceSage  # ← tracesage
from tracesage.adapters.mcp import register_mcp_client  # ← tracesage

HERE = Path(__file__).resolve().parent


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def make_client() -> MultiServerMCPClient:
    # Two local MCP servers over stdio (started as subprocesses by the client).
    return MultiServerMCPClient(
        {
            "flights": {
                "command": sys.executable,
                "args": [str(HERE / "flights_server.py")],
                "transport": "stdio",
            },
            "weather": {
                "command": sys.executable,
                "args": [str(HERE / "weather_server.py")],
                "transport": "stdio",
            },
        }
    )


def build_agent(tools: list) -> Runnable:
    return create_react_agent(make_llm(), tools)


async def main() -> None:
    client = make_client()
    request = (
        "I'm flying London to Tokyo. Find a flight, the carry-on baggage rule, and "
        "Tokyo's weather, then sum it up in two lines."
    )
    print(f"Q: {request}\n")

    async with TraceSage.session(install=True) as tl:  # ← tracesage
        mcp_tools = await register_mcp_client(tl, client)  # ← tracesage: attribute tools to their server
        agent = build_agent(mcp_tools)
        result = await agent.ainvoke(
            {"messages": [("user", request)]},
            config={"recursion_limit": 12},
        )
        print("A:", result["messages"][-1].content)
        await tl.flush()  # ← tracesage: ensure events persist
        if sys.stdin.isatty():
            await asyncio.to_thread(input, "\n[trace] Open the printed link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
