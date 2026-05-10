"""LangGraph wiring with an error edge to a fallback node."""
from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, StateGraph

from nodes import (
    RecoveryState,
    fallback_node,
    fetch_node,
    process_node,
    summarize_node,
)


def route_after_fetch(state: RecoveryState) -> Literal["fallback", "process"]:
    """If the primary fetch errored, take the fallback path."""
    if state.get("error"):
        return "fallback"
    return "process"


def build_graph() -> Any:
    sg: StateGraph = StateGraph(RecoveryState)
    sg.add_node("fetch", fetch_node)
    sg.add_node("fallback", fallback_node)
    sg.add_node("process", process_node)
    sg.add_node("summarize", summarize_node)

    sg.set_entry_point("fetch")
    sg.add_conditional_edges(
        "fetch",
        route_after_fetch,
        {"fallback": "fallback", "process": "process"},
    )
    sg.add_edge("fallback", "process")
    sg.add_edge("process", "summarize")
    sg.add_edge("summarize", END)
    return sg.compile()
