"""LangGraph state machine wiring for the customer support system.

Layout:
    triage → router → { billing_agent | tech_agent | escalation_agent } → END

The router is a conditional edge: `triage` writes the category into state,
and `route()` picks the next node from the category string.
"""
from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from agents import SupportState, billing_agent, escalation_agent, tech_agent
from llm import get_llm


# Triage classifications, one per query in the demo's order.
# With a real LLM, this list is unused.
_TRIAGE_RESPONSES = ["billing", "tech", "escalate", "tech"]
_triage_llm = get_llm(responses=_TRIAGE_RESPONSES)


async def triage(state: SupportState) -> dict:
    """Classify the incoming query into one of {billing, tech, escalate}."""
    msg = await _triage_llm.ainvoke(
        [HumanMessage(content=f"Classify (billing/tech/escalate): {state['query']}")]
    )
    return {"category": msg.content.strip().lower()}


def route(state: SupportState) -> Literal["billing", "tech", "escalate"]:
    """Pick the next node based on the triaged category."""
    cat = (state.get("category") or "").strip().lower()
    if cat in {"billing", "tech", "escalate"}:
        return cat  # type: ignore[return-value]
    return "escalate"


def build_graph() -> Any:
    """Assemble and compile the customer support graph."""
    sg: StateGraph = StateGraph(SupportState)
    sg.add_node("triage", triage)
    sg.add_node("billing_agent", billing_agent)
    sg.add_node("tech_agent", tech_agent)
    sg.add_node("escalation_agent", escalation_agent)

    sg.set_entry_point("triage")
    sg.add_conditional_edges(
        "triage",
        route,
        {
            "billing": "billing_agent",
            "tech": "tech_agent",
            "escalate": "escalation_agent",
        },
    )
    sg.add_edge("billing_agent", END)
    sg.add_edge("tech_agent", END)
    sg.add_edge("escalation_agent", END)
    return sg.compile()
