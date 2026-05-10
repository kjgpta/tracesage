"""System 3 — Parallel Code Review (concurrent branches → aggregator).

A LangGraph that fans out from a START node to three parallel reviewers (lint,
security, test) which all converge on an aggregator. This stresses concurrent
agent execution within a single run.

VALIDATES:
- Concurrent agents do not mix events across branches.
- All three parallel branches' events end up in the same root_run_id journey.
- Aggregator runs only after all three parallel branches complete.
"""
from __future__ import annotations

from typing import TypedDict

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracelens.models import EventType, RunStatus
from tests.integration.conftest import wait_for_drain


class ReviewState(TypedDict, total=False):
    code: str
    lint: list[str]
    security: list[str]
    tests: list[str]
    aggregated: str


def build_parallel_review():
    """Three reviewers fan-out from START; aggregator joins them."""
    lint_llm = FakeListChatModel(responses=["lint: 0 issues"])
    security_llm = FakeListChatModel(responses=["security: 1 finding (low)"])
    test_llm = FakeListChatModel(responses=["tests: all green"])
    agg_llm = FakeListChatModel(responses=["overall: ok with one low-risk finding"])

    async def lint_node(state: ReviewState) -> dict:
        msg = await lint_llm.ainvoke([HumanMessage(content=state["code"])])
        return {"lint": [msg.content]}

    async def security_node(state: ReviewState) -> dict:
        msg = await security_llm.ainvoke([HumanMessage(content=state["code"])])
        return {"security": [msg.content]}

    async def test_node(state: ReviewState) -> dict:
        msg = await test_llm.ainvoke([HumanMessage(content=state["code"])])
        return {"tests": [msg.content]}

    async def aggregate_node(state: ReviewState) -> dict:
        ctx = (
            f"lint={state.get('lint')}, "
            f"security={state.get('security')}, "
            f"tests={state.get('tests')}"
        )
        msg = await agg_llm.ainvoke([HumanMessage(content=ctx)])
        return {"aggregated": msg.content}

    workflow: StateGraph = StateGraph(ReviewState)
    workflow.add_node("lint", lint_node)
    workflow.add_node("security", security_node)
    workflow.add_node("tests", test_node)
    workflow.add_node("aggregate", aggregate_node)

    # Fan-out: START → all three reviewers (in parallel)
    workflow.add_edge(START, "lint")
    workflow.add_edge(START, "security")
    workflow.add_edge(START, "tests")
    # Fan-in: all three → aggregate
    workflow.add_edge("lint", "aggregate")
    workflow.add_edge("security", "aggregate")
    workflow.add_edge("tests", "aggregate")
    workflow.add_edge("aggregate", END)
    return workflow.compile()


@pytest.mark.integration
async def test_system_3_parallel_branches_no_mixing(integration_tracer):
    """All three parallel branches show up; events stay attached to their own run_id."""
    graph = build_parallel_review()

    result = await graph.ainvoke(
        {"code": "def foo(): pass"},
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    assert result["aggregated"], "aggregator should produce output"

    runs, total = await integration_tracer.db.list_runs(limit=10)
    assert total >= 1
    journey = await integration_tracer.db.get_journey(runs[0].run_id)

    # Three parallel reviewer agents should each appear.
    agent_names = {e.agent_name for e in journey if e.agent_name}
    expected = {"lint", "security", "tests", "aggregate"}
    overlap = agent_names & expected
    assert len(overlap) >= 4, (
        f"expected all 4 nodes in journey; got {agent_names}"
    )

    # All branches share single root_run_id.
    root_ids = {e.root_run_id for e in journey}
    assert len(root_ids) == 1, f"branches leaked across roots: {root_ids}"


@pytest.mark.integration
async def test_system_3_aggregator_runs_after_branches(integration_tracer):
    """The aggregate node's chain_start should occur after all three reviewers' chain_end."""
    graph = build_parallel_review()

    await graph.ainvoke(
        {"code": "def bar(): return 42"},
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    journey = await integration_tracer.db.get_journey(runs[0].run_id)

    # Find timestamps of each reviewer's chain_end and the aggregator's chain_start.
    reviewer_ends = [
        e for e in journey
        if e.event_type == EventType.CHAIN_END
        and e.agent_name in {"lint", "security", "tests"}
    ]
    agg_starts = [
        e for e in journey
        if e.event_type == EventType.CHAIN_START and e.agent_name == "aggregate"
    ]

    assert len(reviewer_ends) >= 3, f"expected 3 reviewer ends, got {len(reviewer_ends)}"
    assert len(agg_starts) >= 1, "aggregator did not start"

    last_reviewer_end = max(reviewer_ends, key=lambda e: e.timestamp)
    first_agg_start = min(agg_starts, key=lambda e: e.timestamp)
    assert first_agg_start.timestamp >= last_reviewer_end.timestamp, (
        "aggregator started before all reviewers finished — fan-in broken"
    )


@pytest.mark.integration
async def test_system_3_run_completes(integration_tracer):
    graph = build_parallel_review()
    await graph.ainvoke(
        {"code": "x = 1"},
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    final = await integration_tracer.db.get_run(runs[0].run_id)
    assert final is not None
    assert final.status == RunStatus.COMPLETED
