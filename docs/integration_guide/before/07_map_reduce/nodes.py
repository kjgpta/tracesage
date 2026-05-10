"""LangGraph nodes for map-reduce summarization.

Pattern:
    split → fan out via Send → N parallel summarize_chunk → reduce → END

The fan-out uses LangGraph's `Send` API so the number of parallel branches
is determined dynamically by the chunk count, not hardcoded.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage

from llm import get_llm
from tools import join_summaries, split_text


class MapReduceState(TypedDict, total=False):
    document: str
    chunks: list[str]
    # Annotated with operator.add so each parallel summarizer's output appends
    # rather than overwrites. This is what makes Send-based fan-out work.
    summaries: Annotated[list[str], operator.add]
    final: str


# Module-level fake LLMs — responses cycle across all chunks of all documents.
_summary_llm = get_llm(
    responses=[
        "chunk summary 1",
        "chunk summary 2",
        "chunk summary 3",
        "chunk summary 4",
        "chunk summary 5",
        "chunk summary 6",
        "chunk summary 7",
        "chunk summary 8",
        "chunk summary 9",
        "chunk summary 10",
    ]
)
_reduce_llm = get_llm(
    responses=[
        "Combined: 4 chunks summarized into one paragraph.",
        "Combined: 3 chunks merged.",
        "Combined: full doc summary across all chunks.",
    ]
)


async def split_node(state: MapReduceState) -> dict:
    """Split the document via the `split_text` tool."""
    raw = await split_text.ainvoke({"text": state["document"], "chunk_size": 200})
    chunks = [c.strip() for c in raw.split("|||") if c.strip()]
    return {"chunks": chunks}


async def summarize_chunk(state: dict) -> dict:
    """Summarize a single chunk.

    The `state` here is the per-Send payload (a dict with one chunk), NOT the
    full MapReduceState. The returned dict is merged into MapReduceState via
    the `operator.add` reducer on `summaries`.
    """
    chunk = state.get("chunk", "")
    msg = await _summary_llm.ainvoke([HumanMessage(content=f"Summarize: {chunk}")])
    return {"summaries": [msg.content]}


async def reduce_node(state: MapReduceState) -> dict:
    """Combine all chunk summaries into a final summary."""
    summaries = state.get("summaries") or []
    joined = await join_summaries.ainvoke({"summaries": "|||".join(summaries)})
    msg = await _reduce_llm.ainvoke(
        [HumanMessage(content=f"Final summary based on:\n{joined}")]
    )
    return {"final": msg.content}
