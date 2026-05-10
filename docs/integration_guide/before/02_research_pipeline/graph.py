"""LangGraph wiring with parallel fan-out for analysis."""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from nodes import (
    ResearchState,
    entities,
    fact_extractor,
    ingest,
    retrieve,
    sentiment,
    synthesize,
)


def build_graph() -> Any:
    sg: StateGraph = StateGraph(ResearchState)
    sg.add_node("ingest", ingest)
    sg.add_node("retrieve", retrieve)
    sg.add_node("fact_extractor", fact_extractor)
    sg.add_node("sentiment", sentiment)
    sg.add_node("entities", entities)
    sg.add_node("synthesize", synthesize)

    sg.set_entry_point("ingest")
    sg.add_edge("ingest", "retrieve")

    # Fan out: retrieve feeds three parallel analyzers.
    sg.add_edge("retrieve", "fact_extractor")
    sg.add_edge("retrieve", "sentiment")
    sg.add_edge("retrieve", "entities")

    # Fan in: all three feed into synthesize. LangGraph runs the three branches
    # concurrently because they share a common downstream node, and merges
    # state automatically (each branch writes to a different state key).
    sg.add_edge("fact_extractor", "synthesize")
    sg.add_edge("sentiment", "synthesize")
    sg.add_edge("entities", "synthesize")

    sg.add_edge("synthesize", END)
    return sg.compile()
