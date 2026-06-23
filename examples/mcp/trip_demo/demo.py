"""Trip Planner MCP demo — three MCP servers, one agent, full tracesage attribution.

A single ReAct agent queries three MCP servers (flights, weather, hotels) plus one
local formatting tool. tracesage records which tool call came from which server,
visible in the topology graph and "Tools by source" panel.

Demo arc (3 steps for the recording):
  Step 1 — Two lines of setup: TraceSage.create() + register_mcp_client()
  Step 2 — Run it: watch the agent reason and tool calls fire across 3 servers
  Step 3 — Explore the UI: topology, tools-by-source panel, MCP server drawer

Run:
    pip install 'tracesage[mcp]'
    export ANTHROPIC_API_KEY=...              # default: Anthropic claude-haiku-4-5
    python examples/mcp/trip_demo/demo.py
    python examples/mcp/trip_demo/demo.py --open   # auto-open browser

Switch to OpenAI:
    export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...
    python examples/mcp/trip_demo/demo.py

Smoke test (run agent then exit — useful for CI):
    python examples/mcp/trip_demo/demo.py --check
"""
from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    sys.exit("MCP support missing. Install: pip install 'tracesage[mcp]'")

from langgraph.prebuilt import create_react_agent

from tracesage import TraceSage, TraceSageConfig  # ← tracesage (line 1 of 2)
from tracesage.adapters.mcp import register_mcp_client  # ← tracesage (line 2 of 2)

HERE = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".tracesage" / "trip-demo"

QUERY = (
    "I'm planning a weekend trip from NYC to Tokyo next month. "
    "Search for a good flight and get its baggage policy. "
    "Check Tokyo's current weather and 7-day forecast. "
    "Search for hotels and get details on the best-value pick. "
    "Finally, call format_travel_brief with a concise summary covering flight, weather, and hotel."
)


# ── Local tool (shows up as "Local" in the Tools by source panel) ────────────

@tool
def format_travel_brief(summary: str) -> str:
    """Format and present the final travel brief. Call this last with your complete summary."""
    border = "=" * 54
    return f"\n{border}\n  TRAVEL BRIEF — NYC → Tokyo\n{border}\n{summary}\n{border}"


# ── Setup helpers ─────────────────────────────────────────────────────────────

def make_llm() -> Runnable:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
    return init_chat_model(model, model_provider=provider, temperature=0.0)


def make_mcp_client() -> MultiServerMCPClient:
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
            "hotels": {
                "command": sys.executable,
                "args": [str(HERE / "hotels_server.py")],
                "transport": "stdio",
            },
        }
    )


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(*, check: bool = False, open_browser: bool = False) -> None:
    # ── tracesage: two lines, that's it ──────────────────────────────────────
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))
    mcp_tools = await register_mcp_client(tracer, make_mcp_client())
    # ─────────────────────────────────────────────────────────────────────────

    all_tools = [*mcp_tools, format_travel_brief]
    agent = create_react_agent(make_llm(), all_tools)

    print(f"Q: {QUERY}\n")
    result = await agent.ainvoke(
        {"messages": [("user", QUERY)]},
        config={"callbacks": [tracer.handler], "recursion_limit": 20},
    )
    print(result["messages"][-1].content)

    await asyncio.sleep(0.5)  # let the worker batch drain to DB

    # Print the same breakdown the UI shows in "Tools by source"
    inv = await tracer.db.get_tool_inventory()
    print("\nTools attributed by tracesage:")
    for s in inv["sources"]:
        kind = "MCP  " if s["kind"] == "mcp" else "Local"
        names = [t["name"] for t in s["tools"]]
        print(f"  {kind}  {s['source']:<12} → {s['tool_count']} tools   {names}")

    if check:
        await tracer.stop()
        return

    url = "http://localhost:7842/ui"
    print(f"\ntracesage UI → {url}")
    print("  Topology tab   — 1 agent node fanning out to 3 coloured MCP server nodes")
    print("  Tools panel    — flights(2)  weather(2)  hotels(2)  Local(1)")
    print("  MCP server node — click any server to see its full tool list + call history")

    if open_browser:
        webbrowser.open(url)

    print("\nCtrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(
            main(
                check="--check" in sys.argv,
                open_browser="--open" in sys.argv or "-o" in sys.argv,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped.")
