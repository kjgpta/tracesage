"""Tests for the in-process OTel bridge (tracesage.ingest.exporter / frameworks)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Status, StatusCode

import tracesage
from tracesage.config import TraceSageConfig
from tracesage.ingest import TraceSageSpanExporter, install_otel
from tracesage.ingest.exporter import span_to_record
from tracesage.models import EventType, RawEvent, RunStatus
from tracesage.tracer import TraceSage, _DisabledTraceSage


class _Sink:
    """Minimal tracer stand-in: records emitted events."""

    def __init__(self, fail: bool = False) -> None:
        self.events: list[RawEvent] = []
        self.fail = fail
        self._config = TraceSageConfig(summary_max_chars=40)

    def emit(self, event: RawEvent) -> None:
        if self.fail:
            raise RuntimeError("sink down")
        self.events.append(event)


def _provider(sink: object) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceSageSpanExporter(sink)))
    return provider


def test_sdk_spans_become_events_with_parent_links() -> None:
    sink = _Sink()
    tracer = _provider(sink).get_tracer("t")
    with tracer.start_as_current_span(
        "invoke_agent planner", attributes={"gen_ai.operation.name": "invoke_agent",
                                            "gen_ai.agent.name": "planner"}
    ) as root:
        with tracer.start_as_current_span(
            "execute_tool search", attributes={"gen_ai.operation.name": "execute_tool",
                                               "gen_ai.tool.name": "search",
                                               "gen_ai.tool.call.arguments": '{"q": "x"}'}
        ):
            pass
        root.set_status(Status(StatusCode.OK))

    types = [e.event_type for e in sink.events]
    # child ends (and exports) first: RUN_START + tool pair, then root pair + RUN_END
    assert types == [
        EventType.RUN_START, EventType.TOOL_START, EventType.TOOL_END,
        EventType.CHAIN_START, EventType.CHAIN_END, EventType.RUN_END,
    ]
    tool_start = sink.events[1]
    root_start = sink.events[3]
    trace_id = sink.events[0].run_id
    assert len(trace_id) == 32
    assert tool_start.parent_run_id == root_start.run_id
    assert root_start.parent_run_id == trace_id
    assert root_start.agent_name == "planner"
    assert tool_start.tool_name == "search"
    assert tool_start.raw_payload["messages"] == {"q": "x"}
    assert all(len(e.summary) <= 40 for e in sink.events)  # tracer's summary_max_chars
    assert sink.events[0].tags[0] == "otlp"


def test_span_to_record_fields() -> None:
    captured: list = []

    class _Grab:
        def export(self, spans):
            captured.extend(spans)
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 0) -> bool:
            return True

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Grab()))
    tracer = provider.get_tracer("scope-x")
    with tracer.start_as_current_span("chat m", kind=otel_trace.SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.usage.input_tokens", 3)
        span.add_event("exception", {"exception.type": "E", "exception.message": "m"})
        span.set_status(Status(StatusCode.ERROR, "bad"))
    rec = span_to_record(captured[0])
    assert rec.name == "chat m"
    assert rec.kind == "CLIENT"
    assert rec.status_code == "ERROR"
    assert rec.status_message == "bad"
    assert rec.scope_name == "scope-x"
    assert rec.parent_span_id is None
    assert rec.attributes["gen_ai.usage.input_tokens"] == 3
    assert rec.events[0]["attributes"]["exception.message"] == "m"
    assert rec.events[0]["time_ns"] > 0
    assert rec.end_ns >= rec.start_ns > 0
    assert "service.name" in rec.resource


def test_export_never_raises() -> None:
    exporter = TraceSageSpanExporter(_Sink(fail=True))
    tracer = _provider(_Sink()).get_tracer("t")
    with tracer.start_as_current_span("x"):
        pass
    # garbage input and a failing sink both end in SUCCESS, no exception
    from opentelemetry.sdk.trace.export import SpanExportResult

    assert exporter.export([object(), None]) is SpanExportResult.SUCCESS
    assert exporter.force_flush() is True
    exporter.shutdown()


async def test_install_otel_end_to_end(tmp_path: Path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path, worker_batch_timeout=0.02)
    tl = await TraceSage.create(cfg, start_server=False)
    try:
        provider = install_otel(tl, set_global=False)
        assert isinstance(provider, TracerProvider)
        assert install_otel(tl, set_global=False) is provider  # idempotent
        assert tl.install_otel(set_global=False) is provider

        tracer = provider.get_tracer("app")
        with tracer.start_as_current_span(
            "invoke_agent bot", attributes={"gen_ai.operation.name": "invoke_agent"}
        ) as root, tracer.start_as_current_span(
            "chat gpt", attributes={"gen_ai.operation.name": "chat",
                                    "gen_ai.request.model": "gpt",
                                    "gen_ai.usage.input_tokens": 11,
                                    "gen_ai.usage.output_tokens": 4}
        ):
            pass
        trace_id = format(root.get_span_context().trace_id, "032x")
        await tl.flush()

        run = await tl.db.get_run(trace_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert (run.total_tokens_input, run.total_tokens_output) == (11, 4)
        journey = await tl.db.get_journey(trace_id)
        assert {e.event_type for e in journey} >= {
            EventType.RUN_START, EventType.CHAIN_START, EventType.CHAT_MODEL_START,
            EventType.LLM_END, EventType.CHAIN_END, EventType.RUN_END,
        }
        llm_end = next(e for e in journey if e.event_type == EventType.LLM_END)
        assert llm_end.duration_ms is not None
        topo = await tl.db.get_topology(scope=f"run:{trace_id}")
        assert {n.id for n in topo.nodes} == {"agent:bot", "llm:gpt"}
    finally:
        await tl.stop()


def test_install_otel_piggybacks_on_existing_sdk_provider(monkeypatch) -> None:
    """If the app already configured an SDK TracerProvider globally, we add our
    processor to it rather than fighting over the global."""
    existing = TracerProvider()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: existing)
    calls: list = []
    monkeypatch.setattr(otel_trace, "set_tracer_provider", lambda p: calls.append(p))
    sink = _Sink()
    assert install_otel(sink) is existing
    assert calls == []
    with existing.get_tracer("t").start_as_current_span("hello"):
        pass
    assert [e.event_type for e in sink.events][-1] == EventType.RUN_END


def test_install_otel_sets_global_when_unset(monkeypatch) -> None:
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: object())  # proxy/non-SDK
    calls: list = []
    monkeypatch.setattr(otel_trace, "set_tracer_provider", lambda p: calls.append(p))
    provider = install_otel(_Sink())
    assert isinstance(provider, TracerProvider)
    assert calls == [provider]


def test_disabled_tracer_is_a_noop() -> None:
    disabled = _DisabledTraceSage(TraceSageConfig(enabled=False))
    assert install_otel(disabled) is None
    assert disabled.install_otel() is None
    assert disabled.instrument("pydantic_ai") is None
    assert tracesage.instrument("pydantic_ai", tracer=disabled) is None


def test_instrument_validates_names() -> None:
    with pytest.raises(ValueError, match="at least one"):
        tracesage.instrument(tracer=_Sink())
    with pytest.raises(ValueError, match="unknown framework"):
        tracesage.instrument("langflow", tracer=_Sink(), set_global=False)


def test_instrument_missing_framework_gives_install_hint(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("openinference"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(ImportError, match=r'tracesage\[crewai\]'):
        tracesage.instrument("crewai", tracer=_Sink(), set_global=False)


def test_background_tracer_delegates(tmp_path: Path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path, start_server=False)
    with tracesage.trace(cfg, install=False) as bg:
        provider = bg.install_otel(set_global=False)
        assert isinstance(provider, TracerProvider)
        exporter = bg.otel_exporter()
        assert isinstance(exporter, TraceSageSpanExporter)
        with provider.get_tracer("t").start_as_current_span("job") as root:
            pass
        trace_id = format(root.get_span_context().trace_id, "032x")
        bg.flush()
        import asyncio

        run = asyncio.run_coroutine_threadsafe(bg.tracer.db.get_run(trace_id), bg._loop).result(5)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
