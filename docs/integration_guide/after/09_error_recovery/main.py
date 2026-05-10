"""Run URL fetches exercising success and fallback paths, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


URLS = [
    "https://api.example.com/users",
    "https://api.example.com/orders",
    "https://api.example.com/inventory",
]


async def main() -> None:
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for url in URLS:
        result = await graph.ainvoke(
            {"url": url},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        print(f"URL: {url}")
        print(f"  used fallback: {result.get('used_fallback')}")
        print(f"  error:         {result.get('error') or '<none>'}")
        print(f"  processed:     {result.get('processed')}")
        print(f"  summary:       {result.get('summary')}")
        print()

    print("\nOpen http://localhost:7842/ui to see tool_error events on flaky_fetch.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
