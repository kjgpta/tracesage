"""System 2 — Research Supervisor (multi-agent LangGraph with conditional edges).

A supervisor agent routes between three sub-agents (web, doc, summary) via conditional
edges. Multiple iterations through the supervisor produce nested run hierarchies.

VALIDATES:
- root_run_id propagates correctly through deeply nested LangGraph runs.
- Multiple sub-agents appear distinctly in the journey (no name collisions).
- Conditional edges don't confuse the trace (path through graph is visible).
- Run completes successfully even with cyclic-like structure (supervisor revisited).
"""
from __future__ import annotations

from typing import Literal, TypedDict

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tests.integration.conftest import wait_for_drain
from tracelens.models import EventType, RunStatus


class ResearchState(TypedDict):
    query: str
    next: str
    web_results: str
    doc_results: str
    summary: str


def build_research_supervisor():
    """Build a supervisor graph that visits web → doc → summary → end."""
    # Supervisor cycles through routing decisions deterministically
    supervisor_llm = FakeListChatModel(responses=["web", "doc", "summary", "done"])
    web_llm = FakeListChatModel(responses=["Web result: tracelens observability"])
    doc_llm = FakeListChatModel(responses=["Doc result: callback handler protocol"])
    summary_llm = FakeListChatModel(
        responses=["Final summary: tracelens traces LangChain agents."]
    )

    async def supervisor_node(state: ResearchState) -> dict:
        msg = await supervisor_llm.ainvoke(
            [HumanMessage(content=f"Route research for: {state['query']}")]
        )
        return {"next": msg.content.strip()}

    async def web_researcher_node(state: ResearchState) -> dict:
        msg = await web_llm.ainvoke([HumanMessage(content=state["query"])])
        return {"web_results": msg.content}

    async def doc_researcher_node(state: ResearchState) -> dict:
        msg = await doc_llm.ainvoke([HumanMessage(content=state["query"])])
        return {"doc_results": msg.content}

    async def summarizer_node(state: ResearchState) -> dict:
        ctx = (
            f"web={state.get('web_results', '')} | "
            f"doc={state.get('doc_results', '')}"
        )
        msg = await summary_llm.ainvoke([HumanMessage(content=ctx)])
        return {"summary": msg.content}

    def route(state: ResearchState) -> Literal["web", "doc", "summary", "end"]:
        nxt = (state.get("next") or "").strip()
        if nxt == "done":
            return "end"
        if nxt in {"web", "doc", "summary"}:
            return nxt  # type: ignore[return-value]
        # Defensive default
        return "end"

    workflow: StateGraph = StateGraph(ResearchState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("web_researcher", web_researcher_node)
    workflow.add_node("doc_researcher", doc_researcher_node)
    workflow.add_node("summarizer", summarizer_node)

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        route,
        {
            "web": "web_researcher",
            "doc": "doc_researcher",
            "summary": "summarizer",
            "end": END,
        },
    )
    workflow.add_edge("web_researcher", "supervisor")
    workflow.add_edge("doc_researcher", "supervisor")
    workflow.add_edge("summarizer", END)
    return workflow.compile()


@pytest.mark.integration
async def test_system_2_root_run_id_propagation(integration_tracer):
    """All events from nested runs should share the top-level root_run_id."""
    graph = build_research_supervisor()

    initial: ResearchState = {
        "query": "What is tracelens?",
        "next": "",
        "web_results": "",
        "doc_results": "",
        "summary": "",
    }
    result = await graph.ainvoke(
        initial,
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    assert result["summary"], "summarizer should have produced output"

    runs, total = await integration_tracer.db.list_runs(limit=10)
    assert total >= 1
    root_run = runs[0]

    journey = await integration_tracer.db.get_journey(root_run.run_id)
    assert len(journey) > 5, f"expected substantial journey, got {len(journey)} events"

    # All events share single root_run_id even across deeply nested runs.
    root_ids = {e.root_run_id for e in journey}
    assert len(root_ids) == 1, (
        f"nested runs should share root_run_id; found multiple: {root_ids}"
    )


@pytest.mark.integration
async def test_system_2_multiple_sub_agents_visible(integration_tracer):
    """At least 3 of the 4 sub-agent nodes should appear distinctly in the journey."""
    graph = build_research_supervisor()

    await graph.ainvoke(
        {
            "query": "What is tracelens?",
            "next": "",
            "web_results": "",
            "doc_results": "",
            "summary": "",
        },
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    journey = await integration_tracer.db.get_journey(runs[0].run_id)

    # Each node's name should appear as agent_name on at least one event.
    agent_names = {e.agent_name for e in journey if e.agent_name}
    expected = {"supervisor", "web_researcher", "doc_researcher", "summarizer"}
    overlap = agent_names & expected
    assert len(overlap) >= 3, (
        f"expected at least 3 sub-agents in journey; got {agent_names} "
        f"(overlap with expected: {overlap})"
    )


@pytest.mark.integration
async def test_system_2_run_marked_completed(integration_tracer):
    """The top-level run should reach COMPLETED status after the graph finishes."""
    graph = build_research_supervisor()

    await graph.ainvoke(
        {
            "query": "Test",
            "next": "",
            "web_results": "",
            "doc_results": "",
            "summary": "",
        },
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    final = await integration_tracer.db.get_run(runs[0].run_id)
    assert final is not None
    assert final.status == RunStatus.COMPLETED, (
        f"run did not complete cleanly: status={final.status}"
    )
