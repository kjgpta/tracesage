"""LangGraph wiring for the supervisor pattern.

Layout:
    supervisor → router → { sql_agent | chart_agent | narrative_agent | finalize }
                                        ↓ (each worker returns to supervisor)
                                    supervisor (loop until done)
"""
from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, StateGraph

from agents import (
    AnalystState,
    chart_agent,
    finalize,
    narrative_agent,
    sql_agent,
    supervisor,
)


def route_after_supervisor(
    state: AnalystState,
) -> Literal["sql_agent", "chart_agent", "narrative_agent", "finalize"]:
    nxt = (state.get("next_worker") or "").strip().lower()
    if nxt in {"sql", "sql_agent"}:
        return "sql_agent"
    if nxt in {"chart", "chart_agent"}:
        return "chart_agent"
    if nxt in {"narrative", "narrative_agent"}:
        return "narrative_agent"
    return "finalize"


def build_graph() -> Any:
    sg: StateGraph = StateGraph(AnalystState)
    sg.add_node("supervisor", supervisor)
    sg.add_node("sql_agent", sql_agent)
    sg.add_node("chart_agent", chart_agent)
    sg.add_node("narrative_agent", narrative_agent)
    sg.add_node("finalize", finalize)

    sg.set_entry_point("supervisor")
    sg.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "sql_agent": "sql_agent",
            "chart_agent": "chart_agent",
            "narrative_agent": "narrative_agent",
            "finalize": "finalize",
        },
    )
    # Workers loop back to the supervisor.
    sg.add_edge("sql_agent", "supervisor")
    sg.add_edge("chart_agent", "supervisor")
    sg.add_edge("narrative_agent", "supervisor")
    sg.add_edge("finalize", END)
    return sg.compile()
