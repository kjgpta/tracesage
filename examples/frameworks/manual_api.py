"""Manual spans: trace plain Python with no framework — runs with NO API key.

``tl.span(...)`` and ``@tl.trace_fn(...)`` build the same run / step / topology
structure the LangChain handler produces, for code you wrote yourself. Spans nest by
Python context (works across ``await``), errors are recorded and re-raised, and the
five kinds (chain / agent / tool / llm / retriever) map onto the topology colours.

    pip install tracesage
    python examples/frameworks/manual_api.py
"""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tracesage
from tracesage import TraceSageConfig

DATA_DIR = Path.home() / ".tracesage" / "manual-api-demo"


async def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="manual-api-demo")
    tl = await tracesage.TraceSage.create(cfg)

    @tl.trace_fn(kind="tool", tool_name="web_search")
    async def web_search(query: str) -> list[str]:
        await asyncio.sleep(0.05)
        return [f"result {i} for {query}" for i in range(3)]

    @tl.trace_fn(kind="retriever", name="vector_store")
    async def retrieve(query: str) -> list[str]:
        await asyncio.sleep(0.02)
        return [f"doc about {query}"]

    async def fake_llm(prompt: str) -> str:
        """Stand-in for a model call; a real one would be instrumented or wrapped too."""
        await asyncio.sleep(0.03)
        if random.random() < 0.25:  # noqa: S311 - demo
            raise RuntimeError("model overloaded (simulated)")
        return f"Answer based on {len(prompt)} chars of context."

    async def answer(question: str, thread_id: str) -> str:
        async with tl.span(
            "research_agent", kind="agent", inputs={"question": question}, thread_id=thread_id
        ) as root:
            # Two tools in parallel: both nest under research_agent.
            hits, docs = await asyncio.gather(web_search(question), retrieve(question))
            async with tl.span("synthesise", kind="llm", model="fake-llm-1") as llm:
                text = await fake_llm("\n".join(hits + docs))
                llm.set_output(text).set_tokens(input=len(hits) * 40, output=12)
            root.set_output(text)
            return text

    try:
        for i, q in enumerate(["solar sails", "tidal power", "geothermal heat pumps"]):
            try:
                print(f"  {q!r:<28s} -> {await answer(q, thread_id=f'thread-{i % 2}')}")
            except RuntimeError as e:
                print(f"  {q!r:<28s} -> FAILED: {e}  (red node in the UI)")
        await tl.flush()
        print(f"\nOpen {tl.ui_url}: research_agent → web_search / vector_store / synthesise.")
        print("Ctrl+C to stop.")
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await tl.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("tracesage stopped.")
