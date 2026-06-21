"""Scenario: a SINGLE agent that calls tools from MULTIPLE MCP servers.

One `researcher` agent calls tools from BOTH the weather server and the math
server (plus you could add local tools). In the topology you see one agent node
fanning out to tools coloured by their MCP server, and both `mcp:weather` and
`mcp:math` nodes connected to the tools they provide. In a run trace, selecting
the run shows the single agent -> tools AND the two MCP servers that backed them.

Contrast with `mcp_only.py` / `main.py`, which use one agent PER server.

Run:
    pip install 'tracesage[mcp]'
    python examples/mcp/single_agent_multi_mcp.py            # open http://localhost:7842/ui
    python examples/mcp/single_agent_multi_mcp.py --check     # run once, print inventory, exit
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
DATA_DIR = Path.home() / ".tracesage" / "multi-mcp"


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

    llm = FakeListChatModel(responses=["Researching across services...", "Done."])

    async def researcher(state: State, config) -> dict:
        # ONE agent, interleaving calls to BOTH MCP servers' tools.
        await llm.ainvoke("research plan", config=config)
        notes = []
        for name, args in (
            ("get_weather", {"city": "Tokyo"}),
            ("add", {"a": 10, "b": 5}),
            ("get_forecast", {"city": "Tokyo"}),
            ("multiply", {"a": 3, "b": 7}),
            ("severe_alerts", {"region": "JP"}),
        ):
            if name in by_name:
                notes.append(str(await by_name[name].ainvoke(args, config=config)))
        return {"notes": notes}

    g = StateGraph(State)
    g.add_node("researcher", researcher)
    g.add_edge(START, "researcher")
    g.add_edge("researcher", END)
    graph = g.compile()

    await graph.ainvoke({"notes": []}, config={"callbacks": [tracer.handler], "tags": ["single-agent-multi-mcp"]})

    await asyncio.sleep(0.5)
    inv = await tracer.db.get_tool_inventory()
    print("\nOne agent, tools by source:")
    for s in inv["sources"]:
        kind = "MCP" if s["kind"] == "mcp" else "Local"
        print(f"  {kind:5s} {s['source']:8s} -> {s['tool_count']} tools  {[t['name'] for t in s['tools']]}")

    if check:
        await tracer.stop()
        return
    print(f"\nOpen {tracer.ui_url} — one 'researcher' agent fans out to both MCP servers.")
    print("Select the run to see the same in the run-trace view. Ctrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main(check="--check" in sys.argv))
    except KeyboardInterrupt:
        print("\nstopped.")
