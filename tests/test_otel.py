"""Tests for the optional OpenTelemetry (OTLP) span exporter."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tracesage.models import EventType, StoredEvent

# Skip the whole module if the optional OTel extra isn't installed.
pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from tracesage.exporters.otel import OTelSpanExporter

_T0 = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


def _ev(eid, rid, et, parent, secs, **kw) -> StoredEvent:
    return StoredEvent(
        event_id=eid, run_id=rid, parent_run_id=parent, root_run_id="root",
        event_type=et, timestamp=_T0 + timedelta(seconds=secs), summary="x", **kw,
    )


def _exporter() -> tuple[OTelSpanExporter, InMemorySpanExporter]:
    mem = InMemorySpanExporter()
    return OTelSpanExporter(span_exporter=mem), mem


def test_builds_parent_child_span_tree_with_attrs() -> None:
    exp, mem = _exporter()
    exp.handle(_ev("1", "root", EventType.CHAIN_START, None, 0, agent_name="LangGraph"))
    exp.handle(_ev("2", "t1", EventType.TOOL_START, "root", 1, tool_name="get_weather", mcp_server="weather"))
    exp.handle(_ev("3", "t1", EventType.TOOL_END, "root", 2, tool_name="get_weather", token_output=5))
    exp.handle(_ev("4", "root", EventType.CHAIN_END, None, 3))
    exp.shutdown()

    spans = {s.name: s for s in mem.get_finished_spans()}
    assert set(spans) == {"chain LangGraph", "tool get_weather"}
    chain, tool = spans["chain LangGraph"], spans["tool get_weather"]
    # Same trace, correct parent linkage.
    assert chain.context.trace_id == tool.context.trace_id
    assert tool.parent.span_id == chain.context.span_id
    # Attributes + token usage.
    assert tool.attributes["tracesage.tool"] == "get_weather"
    assert tool.attributes["tracesage.mcp_server"] == "weather"
    assert tool.attributes["gen_ai.usage.output_tokens"] == 5
    # Durations come from the event timestamps.
    assert tool.end_time - tool.start_time == 1_000_000_000
    assert chain.end_time - chain.start_time == 3_000_000_000


def test_error_event_sets_error_status() -> None:
    from opentelemetry.trace import StatusCode

    exp, mem = _exporter()
    exp.handle(_ev("1", "t1", EventType.TOOL_START, "root", 0, tool_name="boom"))
    exp.handle(_ev("2", "t1", EventType.TOOL_ERROR, "root", 1, tool_name="boom", error_message="kaboom"))
    exp.shutdown()

    span = mem.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["tracesage.error"] == "kaboom"


def test_handle_never_raises_on_bad_event() -> None:
    exp, _mem = _exporter()
    # Unknown/odd shapes must be swallowed, not raised.
    exp.handle(_ev("1", "x", EventType.RETRY, None, 0))  # not a start/end type
    exp.shutdown()  # no spans, no error


@pytest.mark.asyncio
async def test_tracer_with_unreachable_otlp_endpoint_does_not_crash(tmp_path: Path) -> None:
    """Configuring an OTLP endpoint that nothing is listening on must not break
    tracing or the host app — export is best-effort and async."""
    from langchain_core.language_models.fake import FakeListLLM

    import tracesage
    from tracesage.config import TraceSageConfig

    cfg = TraceSageConfig(
        data_dir=tmp_path, print_run_url=False, start_server=False,
        otlp_endpoint="http://127.0.0.1:9",  # nothing listens here
    )
    tl = await tracesage.TraceSage.create(cfg)
    try:
        assert tl._otel is not None, "exporter should be constructed when endpoint set"
        await FakeListLLM(responses=["hi"]).ainvoke("hello", config={"callbacks": [tl.handler]})
        await tl.flush()
        runs, _ = await tl.db.list_runs(limit=10, offset=0)
        assert runs, "tracing must still capture runs even if the collector is down"
    finally:
        await tl.stop()
