"""Run a few research topics through the pipeline, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


TOPICS = [
    "multi-agent systems",
    "observability for LLM apps",
    "agent tool use patterns",
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for topic in TOPICS:
        result = await graph.ainvoke(
            {"topic": topic},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"Topic: {topic}")
        print(f"  facts:     {result.get('facts')}")
        print(f"  sentiment: {result.get('sentiment')}")
        print(f"  entities:  {result.get('entities')}")
        summary_first_line = (result.get("summary") or "").splitlines()[0:1]
        print(f"  summary:   {summary_first_line[0] if summary_first_line else ''}")
        print()

    print("\nOpen http://localhost:7842/ui to see the parallel branches in the topology.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
