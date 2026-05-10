"""Run sample analytical questions through the supervisor pipeline."""
from __future__ import annotations

import asyncio

from graph import build_graph


QUESTIONS = [
    "How many users signed up last month?",
    "Show me the revenue trend over the last 6 months",
    "Write a quarterly performance summary by region",
]


async def main() -> None:
    graph = build_graph()
    for q in QUESTIONS:
        result = await graph.ainvoke({"question": q})
        print(f"Q: {q}")
        print(f"  visited: {result.get('visited')}")
        ans = (result.get("final_answer") or "")[:120]
        print(f"  answer:  {ans}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
