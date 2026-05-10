"""Run several URL fetches, exercising both success and error/fallback paths."""
from __future__ import annotations

import asyncio

from graph import build_graph


URLS = [
    "https://api.example.com/users",       # call #1: success
    "https://api.example.com/orders",      # call #2: success
    "https://api.example.com/inventory",   # call #3: ERROR -> fallback
]


async def main() -> None:
    graph = build_graph()
    for url in URLS:
        result = await graph.ainvoke({"url": url})
        print(f"URL: {url}")
        print(f"  used fallback: {result.get('used_fallback')}")
        print(f"  error:         {result.get('error') or '<none>'}")
        print(f"  processed:     {result.get('processed')}")
        print(f"  summary:       {result.get('summary')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
