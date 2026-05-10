"""Run a few research tasks through the planner-executor loop, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


TASKS = [
    "summarize the latest news on multi-agent systems",
    "find recent posts on tracing tools",
    "skim the documentation for LangGraph state machines",
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for task in TASKS:
        result = await graph.ainvoke(
            {"task": task},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"Task: {task}")
        print(f"  steps run: {result.get('completed')}")
        print(f"  final:     {result.get('final')}")
        print()

    print("\nOpen http://localhost:7842/ui to see the iterative executor loop.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
