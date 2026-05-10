"""LangGraph wiring with a writer ↔ critic cycle.

Layout:
    writer → critic → router
                        ├ PASS  → finalize → END
                        └ REVISE (and attempts < 3) → writer  (loop)
"""
from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, StateGraph

from agents import WriterCriticState, critic_node, finalize_node, writer_node


_MAX_ATTEMPTS = 3


def route_after_critic(state: WriterCriticState) -> Literal["revise", "pass"]:
    verdict = (state.get("verdict") or "").strip().upper()
    if verdict.startswith("REVISE") and state.get("attempts", 0) < _MAX_ATTEMPTS:
        return "revise"
    return "pass"


def build_graph() -> Any:
    sg: StateGraph = StateGraph(WriterCriticState)
    sg.add_node("writer", writer_node)
    sg.add_node("critic", critic_node)
    sg.add_node("finalize", finalize_node)

    sg.set_entry_point("writer")
    sg.add_edge("writer", "critic")
    sg.add_conditional_edges(
        "critic",
        route_after_critic,
        {"revise": "writer", "pass": "finalize"},
    )
    sg.add_edge("finalize", END)
    return sg.compile()
