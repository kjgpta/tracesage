"""Scenario: ONLY MCP tools (no hardcoded tools).

Loads tools from two local MCP servers (weather provides 4 tools, 3 called; math: 2)
and runs a graph that uses only those. The "Tools by source" panel + graph show two
MCP groups and NO "Local" group; every tool node is ringed and chipped with its
server colour (weather's uncalled air_quality still appears under the weather group).

Run:
    pip install 'tracesage[mcp]'
    python examples/mcp/mcp_only.py            # then open http://localhost:7842/ui
    python examples/mcp/mcp_only.py --check     # run once, print inventory, exit
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracesage import TraceSage, TraceSageConfig  # noqa: E402
from tracesage.adapters.mcp import register_mcp_client  # noqa: E402

HERE = Path(__file__).resolve().parent
# Dedicated data dir per example so applications stay isolated (topology/tools
# are computed per data dir = per application).
DATA_DIR = Path.home() / ".tracesage" / "mcp-only"


class State(TypedDict):
    notes: list[str]


async def main(check: bool = False) -> None:
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))
    print(f"tracesage UI: {tracer.ui_url}")
    print(f"Data dir:     {DATA_DIR}")
    print(f"Inspect CLI:  tracesage runs -d {DATA_DIR}")

    client = MultiServerMCPClient(
        {
            "weather": {"command": sys.executable, "args": [str(HERE / "weather_server.py")], "transport": "stdio"},
            "math": {"command": sys.executable, "args": [str(HERE / "math_server.py")], "transport": "stdio"},
        }
    )
    mcp_tools = await register_mcp_client(tracer, client)
    by_name = {t.name: t for t in mcp_tools}
    print(f"Loaded {len(mcp_tools)} MCP tools: {sorted(by_name)}")

    llm = FakeListChatModel(responses=["Planning...", "Done."])

    async def planner(state: State, config) -> dict:
        await llm.ainvoke("plan", config=config)
        return {"notes": []}

    async def weather_agent(state: State, config) -> dict:
        notes = list(state["notes"])
        for name, args in (("get_weather", {"city": "Paris"}), ("get_forecast", {"city": "Paris"}), ("severe_alerts", {"region": "EU"})):
            if name in by_name:
                notes.append(str(await by_name[name].ainvoke(args, config=config)))
        return {"notes": notes}

    async def math_agent(state: State, config) -> dict:
        notes = list(state["notes"])
        for name, args in (("add", {"a": 7, "b": 8}), ("multiply", {"a": 6, "b": 9})):
            if name in by_name:
                notes.append(str(await by_name[name].ainvoke(args, config=config)))
        return {"notes": notes}

    g = StateGraph(State)
    g.add_node("planner", planner)
    g.add_node("weather_agent", weather_agent)
    g.add_node("math_agent", math_agent)
    g.add_edge(START, "planner")
    g.add_edge("planner", "weather_agent")
    g.add_edge("weather_agent", "math_agent")
    g.add_edge("math_agent", END)
    graph = g.compile()

    await graph.ainvoke({"notes": []}, config={"callbacks": [tracer.handler], "tags": ["mcp-only"]})

    await asyncio.sleep(0.5)
    inv = await tracer.db.get_tool_inventory()
    print("\nTools by source:")
    for s in inv["sources"]:
        kind = "MCP" if s["kind"] == "mcp" else "Local"
        print(f"  {kind:5s} {s['source']:8s} -> {s['tool_count']} tools  {[t['name'] for t in s['tools']]}")

    if check:
        await tracer.stop()
        return
    print(f"\nOpen {tracer.ui_url} — every tool node is ringed/chipped by MCP server. Ctrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main(check="--check" in sys.argv))
    except KeyboardInterrupt:
        print("\nstopped.")
