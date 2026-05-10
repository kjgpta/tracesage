"""Run the writer-critic loop on a few topics."""
from __future__ import annotations

import asyncio

from graph import build_graph


TOPICS = [
    "multi-agent systems",
    "observability for AI apps",
    "LangChain expression language",
]


async def main() -> None:
    graph = build_graph()
    for topic in TOPICS:
        result = await graph.ainvoke({"topic": topic})
        print(f"Topic: {topic}")
        print(f"  attempts: {result.get('attempts')}")
        print(f"  final:    {result.get('final', '').splitlines()[0] if result.get('final') else ''}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
