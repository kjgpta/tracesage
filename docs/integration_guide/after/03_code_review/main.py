"""Run sample diffs through the code review pipeline, with tracelens."""
from __future__ import annotations

import asyncio

from graph import build_graph
from tracelens_setup import DEFAULT_TAGS, init_tracer


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
    tracer = await init_tracer()
    print("tracelens UI: http://localhost:7842/ui")

    graph = build_graph()
    for i, diff in enumerate(SAMPLE_DIFFS, start=1):
        result = await graph.ainvoke(
            {"diff": diff},
            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
        )
        first_line_of_review = (result.get("review") or "").splitlines()[0]
        print(f"Diff {i}: attempts={result.get('attempts')} {first_line_of_review}")

    print("\nOpen http://localhost:7842/ui to see LCEL chain decomposition + retry loop.")
    print("Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
