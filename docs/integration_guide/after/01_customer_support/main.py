"""Run a few customer support queries through the graph, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


SAMPLE_QUERIES = [
    "I want a refund for my last payment, it was a duplicate charge",
    "The checkout API has been timing out for 20 minutes",
    "I'd like to file a formal complaint about my service experience",
    "Why is my service still slow after restart? Logs please",
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for q in SAMPLE_QUERIES:
        result = await graph.ainvoke(
            {"query": q},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"Q: {q}")
        print(f"  category: {result.get('category')}")
        print(f"  tool:     {result.get('tool_used')}")
        print(f"  reply:    {result.get('resolution')}")
        print()

    print("Open http://localhost:7842/ui to inspect runs. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
