"""Scenario: ONLY hardcoded tools (no MCP servers at all).

A graph whose tools are plain @tool functions defined in this file. The "Tools by
source" panel shows a single "Local" group, the graph tool nodes have no server
ring/chip, and the legend shows no MCP section — i.e. tracesage degrades cleanly
when there is no MCP in play. Needs only `tracesage[langchain]` + langgraph.

Run:
    python examples/mcp/local_only.py            # then open http://localhost:7842/ui
    python examples/mcp/local_only.py --check     # run once, print inventory, exit
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracesage import TraceSage, TraceSageConfig  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "mcp_demo_data"


@tool
def uppercase(text: str) -> str:
    """Uppercase a string."""
    return text.upper()


@tool
def reverse(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


@tool
def word_count(text: str) -> int:
    """Count whitespace-separated words."""
    return len(text.split())


LOCAL = {t.name: t for t in (uppercase, reverse, word_count)}


class State(TypedDict):
    text: str
    notes: list[str]


async def main(check: bool = False) -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))
    print("tracesage UI: http://localhost:7842/ui")

    llm = FakeListChatModel(responses=["Working...", "Done."])

    async def planner(state: State, config) -> dict:
        await llm.ainvoke("plan", config=config)
        return {"notes": []}

    async def worker(state: State, config) -> dict:
        notes = []
        notes.append(str(await LOCAL["uppercase"].ainvoke({"text": state["text"]}, config=config)))
        notes.append(str(await LOCAL["reverse"].ainvoke({"text": state["text"]}, config=config)))
        notes.append(str(await LOCAL["word_count"].ainvoke({"text": state["text"]}, config=config)))
        return {"notes": notes}

    g = StateGraph(State)
    g.add_node("planner", planner)
    g.add_node("worker", worker)
    g.add_edge(START, "planner")
    g.add_edge("planner", "worker")
    g.add_edge("worker", END)
    graph = g.compile()

    await graph.ainvoke(
        {"text": "the quick brown fox", "notes": []},
        config={"callbacks": [tracer.handler], "tags": ["local-only"]},
    )

    await asyncio.sleep(0.5)
    inv = await tracer.db.get_tool_inventory()
    print("\nTools by source (expect a single 'local' group):")
    for s in inv["sources"]:
        kind = "MCP" if s["kind"] == "mcp" else "Local"
        print(f"  {kind:5s} {s['source']:8s} -> {s['tool_count']} tools  {[t['name'] for t in s['tools']]}")

    if check:
        await tracer.stop()
        return
    print("\nOpen http://localhost:7842/ui — one 'Local' group, no MCP rings/legend. Ctrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main(check="--check" in sys.argv))
    except KeyboardInterrupt:
        print("\nstopped.")
