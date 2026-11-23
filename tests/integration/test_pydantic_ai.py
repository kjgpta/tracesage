"""End-to-end: a real Pydantic AI agent (TestModel, no API key) traced through the
in-process OpenTelemetry bridge (`tracesage.instrument("pydantic_ai")`).

Skipped when pydantic_ai or the OTel SDK is not installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("pydantic_ai")
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

import tracesage
from tracesage.config import TraceSageConfig
from tracesage.models import EventType, RunStatus
from tracesage.tracer import TraceSage


def _agent() -> Agent:
    agent = Agent(TestModel(), name="weather_agent", instructions="Be brief.")

    @agent.tool_plain
    def get_weather(city: str) -> str:
        """Weather for a city."""
        return f"Sunny in {city}"

    return agent


@pytest.mark.asyncio
async def test_pydantic_ai_agent_run_is_traced(tmp_path: Path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path, worker_batch_timeout=0.02, print_run_url=False)
    tl = await TraceSage.create(cfg, start_server=False)
    try:
        # instrument_all() binds every Agent to OUR provider explicitly, so a global
        # provider set by another test cannot steal the spans.
        provider = tl.instrument("pydantic_ai")
        assert provider is not None

        result = await _agent().run("Weather in Paris?")
        assert result.output
        await tl.flush()

        runs, total = await tl.db.list_runs()
        assert total == 1
        run = runs[0]
        assert run.status == RunStatus.COMPLETED
        assert run.tags[0] == "otlp"
        assert run.total_tokens_input > 0
        assert run.total_tokens_output > 0

        journey = await tl.db.get_journey(run.run_id)
        types = [e.event_type for e in journey]
        assert types.count(EventType.RUN_START) == 1
        assert types.count(EventType.RUN_END) == 1
        assert types.count(EventType.CHAIN_START) == 1  # invoke_agent
        assert types.count(EventType.TOOL_START) == 1  # execute_tool get_weather
        assert types.count(EventType.CHAT_MODEL_START) == 2  # two model requests
        root = next(e for e in journey if e.event_type == EventType.CHAIN_START)
        assert root.agent_name == "weather_agent"
        assert root.parent_run_id == run.run_id
        tool = next(e for e in journey if e.event_type == EventType.TOOL_START)
        assert tool.tool_name == "get_weather"
        assert tool.parent_run_id == root.run_id
        llm_ends = [e for e in journey if e.event_type == EventType.LLM_END]
        assert all(e.duration_ms is not None for e in llm_ends)
        assert all(e.token_input and e.token_output for e in llm_ends)
        chain_end = next(e for e in journey if e.event_type == EventType.CHAIN_END)
        assert chain_end.duration_ms is not None
        assert "Weather in Paris?" in root.summary  # prompt from pydantic_ai.all_messages

        topo = await tl.db.get_topology(scope=f"run:{run.run_id}")
        ids = {n.id for n in topo.nodes}
        assert {"agent:weather_agent", "tool:get_weather", "llm:test"} <= ids
        edges = {(e.source, e.target) for e in topo.edges}
        assert ("agent:weather_agent", "tool:get_weather") in edges
        assert ("agent:weather_agent", "llm:test") in edges
    finally:
        await tl.stop()


def test_pydantic_ai_sync_via_trace_instrument(tmp_path: Path) -> None:
    """The one-liner path: tracesage.trace(instrument=[...]) from synchronous code."""
    import asyncio

    cfg = TraceSageConfig(data_dir=tmp_path, start_server=False, print_run_url=False)
    # run_sync() reuses the current loop if there is one and otherwise creates one it
    # never closes; own it here so the suite stays ResourceWarning-clean.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with tracesage.trace(cfg, install=False, instrument=["pydantic_ai"]) as tl:
            result = _agent().run_sync("Weather in Rome?")
            assert result.output
            tl.flush()
            runs, total = asyncio.run_coroutine_threadsafe(
                tl.tracer.db.list_runs(), tl._loop
            ).result(10)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    assert total >= 1
    assert any(r.status == RunStatus.COMPLETED and "otlp" in r.tags for r in runs)
