"""Run a few documents through the map-reduce summarizer."""
from __future__ import annotations

import asyncio

from graph import build_graph


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
    graph = build_graph()
    for i, doc in enumerate(DOCUMENTS, start=1):
        result = await graph.ainvoke({"document": doc})
        print(f"Doc {i} ({len(doc)} chars):")
        print(f"  chunks:    {len(result.get('chunks', []))}")
        print(f"  summaries: {len(result.get('summaries', []))}")
        print(f"  final:     {result.get('final', '')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
