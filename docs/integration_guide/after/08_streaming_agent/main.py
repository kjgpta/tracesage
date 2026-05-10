"""Run streaming questions through the LCEL chain + follow-up node, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


QUESTIONS = [
    "Should we use multi-agent systems?",
    "What does observability give me?",
    "How do LCEL chains compose?",
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
        print(f"  chunks streamed: {result.get('chunk_count')}")
        print(f"  final:           {result.get('final', '').splitlines()[0]}")
        print()

    print("\nOpen http://localhost:7842/ui to see streaming telemetry on LLM_END events.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
