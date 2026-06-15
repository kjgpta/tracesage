"""Example 1: Smart Search Agent — one agent, four tools, picks one per query.

Demonstrates the most important tracelens use case:

    The same agent can route to different tools depending on input. tracelens
    captures every tool that the agent COULD use, then shows you which one was
    actually used in each run — others are dimmed in the graph.

This example issues four queries. The fake LLM picks a different tool each time:
    "find user data"   → search_database
    "latest news"      → search_web
    "fastapi reference"→ search_docs
    "previous result"  → cache_lookup

After all four runs:
    - Topology shows ONE agent connected to FOUR tools.
    - Click each run in the UI: only the tool used is highlighted; the other
      three are faded (dashed border, low opacity).

Run:
    python examples/getting_started/01_smart_search_agent.py
    # Open http://localhost:7842/ui
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracelens import TraceLens  # noqa: E402


# ---- The four tools the agent can choose from ----------------------------- #


@tool
def search_database(query: str) -> str:
    """Search the internal user database for matching records."""
    return f"DB: 3 records matching '{query}'"


@tool
def search_web(query: str) -> str:
    """Search the public web for recent information."""
    return f"WEB: top result for '{query}' is example.com/{query.replace(' ', '-')}"


@tool
def search_docs(query: str) -> str:
    """Search internal product documentation."""
    return f"DOCS: section 4.2 references '{query}'"


@tool
def cache_lookup(key: str) -> str:
    """Look up a previously computed result in the cache."""
    return f"CACHE: hit for '{key}', value cached 12s ago"


TOOLS_BY_NAME = {
    "database": search_database,
    "web": search_web,
    "docs": search_docs,
    "cache": cache_lookup,
}


class SearchState(TypedDict):
    query: str
    tool_used: str
    result: str


# Pre-program the FakeListChatModel: one router decision per run.
ROUTING_RESPONSES = ["database", "web", "docs", "cache"]


async def main() -> None:
    tracer = await TraceLens.create()
    print("tracelens at http://localhost:7842/ui")

    router_llm = FakeListChatModel(responses=ROUTING_RESPONSES)
    answer_llm = FakeListChatModel(
        responses=[
            "Found user records.",
            "Latest news summary.",
            "FastAPI documentation excerpt.",
            "Cached value reused.",
        ]
    )

    async def route_and_search(state: SearchState) -> dict:
        # 1. Router LLM picks a tool name.
        choice = await router_llm.ainvoke(
            [HumanMessage(content=f"Pick search tool for: {state['query']}")]
        )
        chosen = choice.content.strip().lower()

        # 2. Invoke the chosen tool — fires on_tool_start / on_tool_end.
        tool_fn = TOOLS_BY_NAME.get(chosen)
        if tool_fn is None:
            return {"tool_used": "none", "result": "no tool matched"}
        tool_input = {"query": state["query"]} if chosen != "cache" else {"key": state["query"]}
        tool_result = await tool_fn.ainvoke(tool_input)

        # 3. Synthesize a final answer with another LLM call.
        await answer_llm.ainvoke([HumanMessage(content=f"Wrap: {tool_result}")])

        return {"tool_used": chosen, "result": tool_result}

    workflow: StateGraph = StateGraph(SearchState)
    workflow.add_node("smart_search", route_and_search)
    workflow.set_entry_point("smart_search")
    workflow.add_edge("smart_search", END)
    graph = workflow.compile()

    queries = [
        "find user data",
        "latest news on AI",
        "fastapi reference",
        "previous result",
    ]
    for q in queries:
        result = await graph.ainvoke(
            {"query": q, "tool_used": "", "result": ""},
            config={"callbacks": [tracer.handler], "tags": ["smart-search"]},
        )
        print(f"  query={q!r:<30s} tool={result['tool_used']:<10s} result={result['result']}")

    print("\nLeaving server up. Open http://localhost:7842/ui to see the topology")
    print("and click each run to see which tool was actually used.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()
        print("tracelens stopped.")


if __name__ == "__main__":
    asyncio.run(main())
