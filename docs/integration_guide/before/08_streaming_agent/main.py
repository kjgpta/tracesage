"""Run streaming questions through the LCEL chain + follow-up node."""
from __future__ import annotations

import asyncio

from graph import build_graph


QUESTIONS = [
    "Should we use multi-agent systems?",
    "What does observability give me?",
    "How do LCEL chains compose?",
]


async def main() -> None:
    graph = build_graph()
    for q in QUESTIONS:
        result = await graph.ainvoke({"question": q})
        print(f"Q: {q}")
        print(f"  chunks streamed: {result.get('chunk_count')}")
        print(f"  final:           {result.get('final', '').splitlines()[0]}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
