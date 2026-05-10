"""Run RAG questions through the two-stage retrieval pipeline, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


QUESTIONS = [
    "what are multi-agent systems?",
    "how does retrieval work in RAG?",
    "what is LCEL good for?",
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
        print(f"  candidates: {len(result.get('candidates', []))}")
        print(f"  reranked:   {len(result.get('reranked', []))}")
        cited = (result.get("cited") or "").splitlines()[0]
        print(f"  answer:     {cited}")
        print()

    print("\nOpen http://localhost:7842/ui to see two-stage retrieval in the topology.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
