"""Run a few research topics through the pipeline (no tracelens)."""
from __future__ import annotations

import asyncio

from graph import build_graph


TOPICS = [
    "multi-agent systems",
    "observability for LLM apps",
    "agent tool use patterns",
]


async def main() -> None:
    graph = build_graph()
    for topic in TOPICS:
        result = await graph.ainvoke({"topic": topic})
        print(f"Topic: {topic}")
        print(f"  facts:     {result.get('facts')}")
        print(f"  sentiment: {result.get('sentiment')}")
        print(f"  entities:  {result.get('entities')}")
        summary_first_line = (result.get("summary") or "").splitlines()[0:1]
        print(f"  summary:   {summary_first_line[0] if summary_first_line else ''}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
