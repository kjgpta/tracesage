"""Run a few RAG questions through the two-stage retrieval pipeline."""
from __future__ import annotations

import asyncio

from graph import build_graph


QUESTIONS = [
    "what are multi-agent systems?",
    "how does retrieval work in RAG?",
    "what is LCEL good for?",
]


async def main() -> None:
    graph = build_graph()
    for q in QUESTIONS:
        result = await graph.ainvoke({"question": q})
        print(f"Q: {q}")
        print(f"  candidates: {len(result.get('candidates', []))}")
        print(f"  reranked:   {len(result.get('reranked', []))}")
        cited = (result.get("cited") or "").splitlines()[0]
        print(f"  answer:     {cited}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
