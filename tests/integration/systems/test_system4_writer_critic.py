"""System 4 — Writer-Critic Loop (cyclic graph with conditional termination).

A two-node cycle: writer ⇄ critic. The critic decides via state whether to send
the draft back to the writer for revision or to terminate. Bounded by max_iterations.

VALIDATES:
- Cyclic graph traces correctly without confusing the journey ordering.
- Loop terminates when critic decides "done" (or max iterations reached).
- Multiple iterations of the same node appear distinctly in the trace.
- Run still hits COMPLETED (not stuck RUNNING).
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

from tracelens.models import EventType, RunStatus
from tests.integration.conftest import wait_for_drain


class WriterCriticState(TypedDict):
    topic: str
    draft: str
    feedback: str
    iteration: int
    decision: str


def build_writer_critic(max_iterations: int = 3):
    """Writer drafts; critic gives feedback or signals 'done'."""
    writer_llm = FakeListChatModel(
        responses=[
            "Draft v1: short outline.",
            "Draft v2: expanded with examples.",
            "Draft v3: final polished version.",
        ]
    )
    # Critic asks for revision twice, then approves.
    critic_llm = FakeListChatModel(
        responses=["needs_revision", "needs_revision", "done"]
    )

    async def writer_node(state: WriterCriticState) -> dict:
        msg = await writer_llm.ainvoke(
            [HumanMessage(content=f"Topic: {state['topic']}, prev feedback: {state['feedback']}")]
        )
        return {"draft": msg.content, "iteration": state.get("iteration", 0) + 1}

    async def critic_node(state: WriterCriticState) -> dict:
        msg = await critic_llm.ainvoke([HumanMessage(content=f"Review: {state['draft']}")])
        return {
            "feedback": msg.content,
            "decision": msg.content.strip(),
        }

    def should_continue(state: WriterCriticState) -> Literal["writer", "end"]:
        if state.get("iteration", 0) >= max_iterations:
            return "end"
        if state.get("decision", "").strip() == "done":
            return "end"
        return "writer"

    workflow: StateGraph = StateGraph(WriterCriticState)
    workflow.add_node("writer", writer_node)
    workflow.add_node("critic", critic_node)
    workflow.set_entry_point("writer")
    workflow.add_edge("writer", "critic")
    workflow.add_conditional_edges("critic", should_continue, {"writer": "writer", "end": END})
    return workflow.compile()


@pytest.mark.integration
async def test_system_4_loop_iterates_and_terminates(integration_tracer):
    """Loop runs at least 2 iterations and terminates by 'done' decision."""
    graph = build_writer_critic(max_iterations=5)

    result = await graph.ainvoke(
        {
            "topic": "agent observability",
            "draft": "",
            "feedback": "",
            "iteration": 0,
            "decision": "",
        },
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    assert result["draft"], "writer should produce a draft"
    assert result["decision"] == "done", f"critic should approve; got {result['decision']}"
    assert result["iteration"] >= 2, f"expected >=2 iterations, got {result['iteration']}"

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    journey = await integration_tracer.db.get_journey(runs[0].run_id)

    # Writer and critic should each appear multiple times in the journey.
    writer_starts = [
        e for e in journey
        if e.event_type == EventType.CHAIN_START and e.agent_name == "writer"
    ]
    critic_starts = [
        e for e in journey
        if e.event_type == EventType.CHAIN_START and e.agent_name == "critic"
    ]
    assert len(writer_starts) >= 2, f"writer should be invoked >=2 times, got {len(writer_starts)}"
    assert len(critic_starts) >= 2, f"critic should be invoked >=2 times, got {len(critic_starts)}"


@pytest.mark.integration
async def test_system_4_max_iteration_safety(integration_tracer):
    """Loop terminates by max_iterations even if critic never says 'done'."""
    # Critic always says needs_revision — only iteration cap stops it.
    workflow_state = {
        "topic": "infinite loop test",
        "draft": "",
        "feedback": "",
        "iteration": 0,
        "decision": "",
    }

    # Build a graph where critic NEVER approves — only iteration cap saves us.
    writer_llm = FakeListChatModel(responses=["draft"] * 100)
    critic_llm = FakeListChatModel(responses=["needs_revision"] * 100)

    async def writer_node(state):
        msg = await writer_llm.ainvoke([HumanMessage(content="write")])
        return {"draft": msg.content, "iteration": state.get("iteration", 0) + 1}

    async def critic_node(state):
        msg = await critic_llm.ainvoke([HumanMessage(content="review")])
        return {"decision": msg.content}

    def should_continue(state):
        return "end" if state.get("iteration", 0) >= 3 else "writer"

    workflow: StateGraph = StateGraph(WriterCriticState)
    workflow.add_node("writer", writer_node)
    workflow.add_node("critic", critic_node)
    workflow.set_entry_point("writer")
    workflow.add_edge("writer", "critic")
    workflow.add_conditional_edges("critic", should_continue, {"writer": "writer", "end": END})
    graph = workflow.compile()

    result = await graph.ainvoke(
        workflow_state, config={"callbacks": [integration_tracer.handler]}
    )
    await wait_for_drain(integration_tracer, timeout=5.0)

    assert result["iteration"] == 3, f"should hit cap at 3, got {result['iteration']}"

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    final = await integration_tracer.db.get_run(runs[0].run_id)
    assert final is not None
    assert final.status == RunStatus.COMPLETED
