"""LangGraph wiring for the streaming agent + follow-up tool node.

Layout:
    streamed_answer (LCEL astream) → followup (tools) → END
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from chains import streaming_chain
from tools import add_disclaimer, shorten


class StreamState(TypedDict, total=False):
    question: str
    streamed: str
    chunk_count: int
    final: str


async def streamed_answer(state: StreamState) -> dict:
    """Consume the streaming chain — accumulates chunks AND counts them.

    Counting chunks here is illustrative; tracelens captures the same
    information automatically via on_llm_new_token (visible in the LLM_END
    event's `_stream` payload).
    """
    parts: list[str] = []
    count = 0
    async for chunk in streaming_chain.astream({"question": state["question"]}):
        parts.append(chunk)
        if chunk:
            count += 1
    return {"streamed": "".join(parts), "chunk_count": count}


async def followup(state: StreamState) -> dict:
    """Post-process the streamed text with two tools."""
    short = await shorten.ainvoke({"text": state.get("streamed", ""), "limit": 80})
    final = await add_disclaimer.ainvoke({"text": short})
    return {"final": final}


def build_graph() -> Any:
    sg: StateGraph = StateGraph(StreamState)
    sg.add_node("streamed_answer", streamed_answer)
    sg.add_node("followup", followup)
    sg.set_entry_point("streamed_answer")
    sg.add_edge("streamed_answer", "followup")
    sg.add_edge("followup", END)
    return sg.compile()
