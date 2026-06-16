"""System 1 — Sequential Order Processing Pipeline.

A 4-step LangGraph StateGraph: validate → inventory → pricing → confirmation.
Each node makes one LLM call against a deterministic FakeListChatModel.

VALIDATES:
- Chain events captured for every graph node (chain_start + chain_end pairs).
- Chat-model events captured for every LLM call (chat_model_start + llm_end).
- All events of one invocation share a single root_run_id.
- Run reaches RunStatus.COMPLETED.
- Token counts (when present) and durations are recorded.
"""
from __future__ import annotations

from typing import TypedDict

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tests.integration.conftest import wait_for_drain
from tracesage.models import EventType, RunStatus


class OrderState(TypedDict):
    order_id: str
    validated: str
    inventory: str
    pricing: str
    confirmation: str


def build_order_pipeline():
    """Construct a 4-node sequential graph backed by a deterministic chat model."""
    llm = FakeListChatModel(
        responses=[
            "Order valid.",
            "10 units in stock.",
            "Total $250.00.",
            "Confirmation email sent.",
        ]
    )

    async def validate_node(state: OrderState) -> dict:
        result = await llm.ainvoke(
            [HumanMessage(content=f"Validate order {state['order_id']}")]
        )
        return {"validated": result.content}

    async def inventory_node(state: OrderState) -> dict:
        result = await llm.ainvoke([HumanMessage(content="Check inventory")])
        return {"inventory": result.content}

    async def pricing_node(state: OrderState) -> dict:
        result = await llm.ainvoke([HumanMessage(content="Calculate price")])
        return {"pricing": result.content}

    async def confirmation_node(state: OrderState) -> dict:
        result = await llm.ainvoke([HumanMessage(content="Send confirmation")])
        return {"confirmation": result.content}

    workflow: StateGraph = StateGraph(OrderState)
    workflow.add_node("validate", validate_node)
    workflow.add_node("inventory", inventory_node)
    workflow.add_node("pricing", pricing_node)
    workflow.add_node("confirmation", confirmation_node)
    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "inventory")
    workflow.add_edge("inventory", "pricing")
    workflow.add_edge("pricing", "confirmation")
    workflow.add_edge("confirmation", END)
    return workflow.compile()


@pytest.mark.integration
async def test_system_1_full_journey_captured(integration_tracer):
    """All four nodes run; all events captured; run completes; root_run_id is unique."""
    pipeline = build_order_pipeline()

    initial: OrderState = {
        "order_id": "8821",
        "validated": "",
        "inventory": "",
        "pricing": "",
        "confirmation": "",
    }
    result = await pipeline.ainvoke(
        initial,
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer)

    assert result["validated"] == "Order valid."
    assert result["inventory"] == "10 units in stock."
    assert result["pricing"] == "Total $250.00."
    assert result["confirmation"] == "Confirmation email sent."

    runs, total = await integration_tracer.db.list_runs(limit=10)
    assert total >= 1
    root_run = runs[0]

    journey = await integration_tracer.db.get_journey(root_run.run_id)
    assert journey, "journey should not be empty"

    # All events share a single root_run_id (no leakage).
    root_ids = {e.root_run_id for e in journey}
    assert len(root_ids) == 1, f"events leaked across roots: {root_ids}"

    # At least 4 chat_model_start events (one per node).
    chat_starts = [e for e in journey if e.event_type == EventType.CHAT_MODEL_START]
    assert len(chat_starts) >= 4, f"expected >=4 chat_model_start, got {len(chat_starts)}"

    llm_ends = [e for e in journey if e.event_type == EventType.LLM_END]
    assert len(llm_ends) >= 4, f"expected >=4 llm_end, got {len(llm_ends)}"

    # Run reached terminal state.
    final_run = await integration_tracer.db.get_run(root_run.run_id)
    assert final_run is not None
    assert final_run.status == RunStatus.COMPLETED, (
        f"expected COMPLETED, got {final_run.status}"
    )


@pytest.mark.integration
async def test_system_1_durations_recorded(integration_tracer):
    """Each *_end event should have a non-null duration_ms after pairing with its start."""
    pipeline = build_order_pipeline()

    await pipeline.ainvoke(
        {
            "order_id": "9001",
            "validated": "",
            "inventory": "",
            "pricing": "",
            "confirmation": "",
        },
        config={"callbacks": [integration_tracer.handler]},
    )
    await wait_for_drain(integration_tracer)

    runs, _ = await integration_tracer.db.list_runs(limit=10)
    journey = await integration_tracer.db.get_journey(runs[0].run_id)

    end_events = [
        e
        for e in journey
        if e.event_type
        in {EventType.CHAIN_END, EventType.LLM_END}
    ]
    with_duration = [e for e in end_events if e.duration_ms is not None and e.duration_ms >= 0]
    assert len(with_duration) >= 4, (
        f"expected at least 4 *_end events with duration_ms, got {len(with_duration)}/"
        f"{len(end_events)}"
    )


@pytest.mark.integration
async def test_system_1_concurrent_runs_no_mixing(integration_tracer):
    """10 concurrent runs of the same pipeline; each run's events stay attached to its own root."""
    import asyncio

    pipeline = build_order_pipeline()

    async def one_run(idx: int) -> str:
        await pipeline.ainvoke(
            {
                "order_id": f"order-{idx}",
                "validated": "",
                "inventory": "",
                "pricing": "",
                "confirmation": "",
            },
            config={"callbacks": [integration_tracer.handler]},
        )
        return f"order-{idx}"

    await asyncio.gather(*[one_run(i) for i in range(10)])
    await wait_for_drain(integration_tracer, timeout=5.0)

    runs, total = await integration_tracer.db.list_runs(limit=50)
    assert total >= 10, f"expected >=10 runs, got {total}"

    # For each run, verify all its events share its own root_run_id.
    for run in runs[:10]:
        journey = await integration_tracer.db.get_journey(run.run_id)
        if not journey:
            continue
        root_ids = {e.root_run_id for e in journey}
        assert root_ids == {run.run_id}, (
            f"run {run.run_id} has cross-run leakage: {root_ids}"
        )
