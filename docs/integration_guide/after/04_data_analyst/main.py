"""Run sample questions through the data analyst supervisor, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


QUESTIONS = [
    "How many users signed up last month?",
    "Show me the revenue trend over the last 6 months",
    "Write a quarterly performance summary by region",
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for q in QUESTIONS:
        result = await graph.ainvoke(
            {"question": q},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"Q: {q}")
        print(f"  visited: {result.get('visited')}")
        ans = (result.get("final_answer") or "")[:120]
        print(f"  answer:  {ans}")
        print()

    print("\nOpen http://localhost:7842/ui to see the supervisor loop in the topology.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
