"""Run the writer-critic loop on a few topics, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


TOPICS = [
    "multi-agent systems",
    "observability for AI apps",
    "LangChain expression language",
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
        print(f"  attempts: {result.get('attempts')}")
        print(f"  final:    {result.get('final', '').splitlines()[0] if result.get('final') else ''}")
        print()

    print("\nOpen http://localhost:7842/ui to see the writer <-> critic cycle.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
