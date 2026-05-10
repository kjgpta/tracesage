"""LangGraph wiring with dynamic fan-out via Send."""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

# `Send` lives in different submodules across langgraph versions.
try:
    from langgraph.types import Send
except ImportError:  # pragma: no cover - old langgraph
    from langgraph.constants import Send  # type: ignore[no-redef]

from nodes import MapReduceState, reduce_node, split_node, summarize_chunk


def map_to_chunks(state: MapReduceState) -> list:
    """Conditional-edge function — emits one Send per chunk for parallel summarization."""
    return [Send("summarize_chunk", {"chunk": c}) for c in (state.get("chunks") or [])]


def build_graph() -> Any:
    sg: StateGraph = StateGraph(MapReduceState)
    sg.add_node("split", split_node)
    sg.add_node("summarize_chunk", summarize_chunk)
    sg.add_node("reduce", reduce_node)

    sg.set_entry_point("split")
    # Dynamic dispatch: split's output gets fanned out to N copies of
    # summarize_chunk, where N = len(chunks).
    sg.add_conditional_edges("split", map_to_chunks, ["summarize_chunk"])
    sg.add_edge("summarize_chunk", "reduce")
    sg.add_edge("reduce", END)
    return sg.compile()
