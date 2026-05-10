"""Run documents through the map-reduce summarizer, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


DOCUMENTS = [
    (
        "Multi-agent systems split work across specialized roles. "
        "A supervisor coordinates the workers. "
        "Each worker has its own toolbox and LLM. "
        "Outputs are merged at the end of the pipeline. "
        "Observability is critical for understanding what each agent did. "
        "Tracelens captures every callback event without changing the workflow. "
        "The result is a complete picture of execution, decisions, and tool calls. "
        "This makes debugging multi-agent systems tractable."
    ),
    (
        "Retrieval-augmented generation grounds an LLM in external context. "
        "First, a retriever fetches candidate documents. "
        "Optionally a reranker trims the candidates. "
        "Then the LLM answers using the trimmed context."
    ),
    (
        "LangChain expression language composes prompts, models, and parsers. "
        "Pipe operators chain together discrete primitives. "
        "Each segment is observable independently. "
        "LangGraph adds explicit state machines on top. "
        "Together they cover most agent design patterns."
    ),
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for i, doc in enumerate(DOCUMENTS, start=1):
        result = await graph.ainvoke(
            {"document": doc},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"Doc {i} ({len(doc)} chars):")
        print(f"  chunks:    {len(result.get('chunks', []))}")
        print(f"  summaries: {len(result.get('summaries', []))}")
        print(f"  final:     {result.get('final', '')}")
        print()

    print("\nOpen http://localhost:7842/ui to see dynamic fan-out from Send.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
