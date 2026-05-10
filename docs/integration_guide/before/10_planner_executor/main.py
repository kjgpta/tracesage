"""Run a few research tasks through the planner-executor loop."""
from __future__ import annotations

import asyncio

from graph import build_graph


TASKS = [
    "summarize the latest news on multi-agent systems",
    "find recent posts on tracing tools",
    "skim the documentation for LangGraph state machines",
]


async def main() -> None:
    graph = build_graph()
    for task in TASKS:
        result = await graph.ainvoke({"task": task})
        print(f"Task: {task}")
        print(f"  steps run: {result.get('completed')}")
        print(f"  final:     {result.get('final')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
