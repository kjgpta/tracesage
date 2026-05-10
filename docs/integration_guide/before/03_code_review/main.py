"""Run a few sample diffs through the code review pipeline (no tracelens)."""
from __future__ import annotations

import asyncio

from graph import build_graph


SAMPLE_DIFFS = [
    """\
- response = api_client.fetch(url)
+ try:
+     response = api_client.fetch(url)
+ except APIError:
+     response = None
""",
    """\
- def process(d): ...
+ def process(payload): ...
- result = process(d)
+ result = process(payload)
""",
    """\
- DEFAULT_TIMEOUT = 30
+ DEFAULT_TIMEOUT = 5
""",
]


async def main() -> None:
    graph = build_graph()
    for i, diff in enumerate(SAMPLE_DIFFS, start=1):
        result = await graph.ainvoke({"diff": diff})
        first_line_of_review = (result.get("review") or "").splitlines()[0]
        print(f"Diff {i}: attempts={result.get('attempts')} {first_line_of_review}")


if __name__ == "__main__":
    asyncio.run(main())
