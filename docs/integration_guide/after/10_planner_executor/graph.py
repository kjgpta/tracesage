"""LangGraph wiring for the planner-executor loop.

Layout:
    planner → executor → router
                            ├ done    → END
                            └ next    → executor (loop until plan empty)
"""
from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, StateGraph

from agents import PlanState, executor_node, planner_node


def route_after_executor(state: PlanState) -> Literal["next", "done"]:
    if state.get("plan"):
        return "next"
    return "done"


def build_graph() -> Any:
    sg: StateGraph = StateGraph(PlanState)
    sg.add_node("planner", planner_node)
    sg.add_node("executor", executor_node)

    sg.set_entry_point("planner")
    sg.add_edge("planner", "executor")
    sg.add_conditional_edges(
        "executor",
        route_after_executor,
        {"next": "executor", "done": END},
    )
    return sg.compile()
