"""Run a few customer support queries through the graph (no tracelens)."""
from __future__ import annotations

import asyncio

from graph import build_graph


SAMPLE_QUERIES = [
    "I want a refund for my last payment, it was a duplicate charge",
    "The checkout API has been timing out for 20 minutes",
    "I'd like to file a formal complaint about my service experience",
    "Why is my service still slow after restart? Logs please",
]


async def main() -> None:
    graph = build_graph()
    for q in SAMPLE_QUERIES:
        result = await graph.ainvoke({"query": q})
        print(f"Q: {q}")
        print(f"  category: {result.get('category')}")
        print(f"  tool:     {result.get('tool_used')}")
        print(f"  reply:    {result.get('resolution')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
