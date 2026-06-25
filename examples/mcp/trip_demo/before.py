"""Trip Planner MCP demo — WITHOUT tracesage (the "before" picture).

A single ReAct agent plans a trip across three MCP servers (flights, weather,
hotels) plus one local formatting tool, then prints the final travel brief.

That's all you get: the answer at the end. This is the everyday reality of
running an agent over MCP servers with no observability — and the whole point of
the demo. While it runs you're staring at a silent terminal, and when it finishes
you have a paragraph of text and a pile of questions:

  • Which tools actually fired? In what order? Did it call all three servers, or
    silently skip one?
  • What did each tool RETURN? If the brief is wrong, was it a bad flight result,
    a stale forecast, or the LLM ignoring a correct tool result?
  • How many LLM round-trips did this take? How many tokens did it burn? Where did
    the time go — the model, or a slow MCP server?
  • Which server does `get_hotel_details` even come from when something errors?
  • A tool raised — where, and with what inputs? (Re-run and hope it repeats?)

Your options today: bolt on `print()`s and re-run, crank up LangChain debug
logging and drown in noise, or wire up a heavyweight tracing stack. Run `demo.py`
next to see the same agent with tracesage added — five lines — answering every
question above in a live local UI.

Run it (same setup as demo.py):
    pip install 'tracesage[mcp]'
    export ANTHROPIC_API_KEY=...              # default: Anthropic claude-haiku-4-5
    python examples/mcp/trip_demo/before.py

    (A .env file in the repo root is loaded automatically, so ANTHROPIC_API_KEY /
    OPENAI_API_KEY can live there instead of being exported.)

Switch to OpenAI:
    export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...
    python examples/mcp/trip_demo/before.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    sys.exit("MCP support missing. Install: pip install 'tracesage[mcp]'")

from langgraph.prebuilt import create_react_agent

HERE = Path(__file__).resolve().parent

# Identical query to demo.py — the only difference between the two files is that
# demo.py adds tracesage. Everything else is byte-for-byte the same.
QUERY = (
    "I'm planning a weekend trip from NYC to Tokyo next month. "
    "Search for a good flight and get its baggage policy. "
    "Check Tokyo's current weather and 7-day forecast. "
    "Search for hotels and get details on the best-value pick. "
    "Finally, call format_travel_brief with a concise summary covering flight, weather, and hotel."
)


# ── Local tool ────────────────────────────────────────────────────────────────

@tool
def format_travel_brief(summary: str) -> str:
    """Format and present the final travel brief. Call this last with your complete summary."""
    border = "=" * 54
    return f"\n{border}\n  TRAVEL BRIEF — NYC → Tokyo\n{border}\n{summary}\n{border}"


# ── Setup helpers (identical to demo.py) ───────────────────────────────────────

_PROVIDER_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def make_llm() -> Runnable:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
    key_var = _PROVIDER_KEY.get(provider, f"{provider.upper()}_API_KEY")
    if not os.environ.get(key_var):
        sys.exit(
            f"\nNo LLM API key found. This demo defaults to '{provider}' and needs ${key_var}.\n\n"
            f"    export {key_var}=...\n"
            "    python examples/mcp/trip_demo/before.py\n\n"
            "(Or add it to a .env file in the repo root — this script loads .env automatically.)\n"
            "Use a different provider: export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...\n"
        )
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

async def main() -> None:
    llm = make_llm()  # preflight: exits with setup steps if no LLM API key is set

    client = make_mcp_client()
    tools = await client.get_tools()        # no tracesage — plain MCP tools
    all_tools = [*tools, format_travel_brief]
    agent = create_react_agent(llm, all_tools)

    print(f"Q: {QUERY}\n")
    print("…running (no trace, no tool log — just wait for the answer)…\n")
    result = await agent.ainvoke(
        {"messages": [("user", QUERY)]},
        config={"recursion_limit": 20},      # no callbacks=[tracer.handler]
    )
    print(result["messages"][-1].content)

    # That's it. Final answer only — no idea which of the 22 tools fired, what they
    # returned, how many LLM calls/tokens it took, or where the time went.
    print(
        "\n"
        "↑ That's everything you get without tracesage: the final text.\n"
        "  No tool call log, no per-server attribution, no token counts, no timeline.\n"
        "  Run `python examples/mcp/trip_demo/demo.py` to see the same run, traced.\n"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
