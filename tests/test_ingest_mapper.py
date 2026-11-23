"""Tests for tracesage.ingest.mapper: OTel spans -> RawEvents.

Uses the recorded OTLP/JSON fixtures under tests/fixtures/otlp (one per supported
framework) plus a few hand-built SpanRecords for edge cases, and runs the mapped
events through the real StorageWorker + SQLite to prove the worker's pairing,
run-status, and topology logic hold for ingested traces.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracesage.config import TraceSageConfig
from tracesage.ingest import SOURCE_MARKER, SpanMapper, SpanRecord, decode_json
from tracesage.ingest.mapper import classify
from tracesage.models import EventType, RawEvent, RunStatus, Stats
from tracesage.storage.blob_store import BlobStore
from tracesage.storage.sqlite_backend import SQLiteBackend
from tracesage.worker import StorageWorker

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"


def _load(name: str) -> list[SpanRecord]:
    return decode_json((FIXTURES / name).read_bytes())


def _map_all(spans: list[SpanRecord], **kw: Any) -> list[RawEvent]:
    mapper = SpanMapper(**kw)
    out: list[RawEvent] = []
    for span in spans:
        out.extend(mapper.map(span))
    return out


def _by_type(events: list[RawEvent], et: EventType) -> list[RawEvent]:
    return [e for e in events if e.event_type == et]


def _span(
    name: str,
    *,
    attrs: dict[str, Any] | None = None,
    parent: str | None = None,
    span_id: str = "1111111111111111",
    trace_id: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    status: str = "UNSET",
    status_message: str | None = None,
    events: list[dict[str, Any]] | None = None,
    resource: dict[str, Any] | None = None,
    start_ns: int = 1_700_000_000_000_000_000,
    end_ns: int = 1_700_000_000_250_000_000,
) -> SpanRecord:
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        attributes=attrs or {},
        status_code=status,
        status_message=status_message,
        events=events or [],
        resource=resource or {},
    )


# ------------------------------------------------------------- Pydantic AI


class TestPydanticAI:
    TRACE = "0af7651916cd43dd8448eb211c80319c"
    ROOT = "a1b2c3d4e5f60718"

    def test_structure(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        # 4 spans -> 8 start/end events + RUN_START + RUN_END
        assert len(events) == 10
        types = [e.event_type for e in events]
        assert types.count(EventType.RUN_START) == 1
        assert types.count(EventType.RUN_END) == 1
        assert types.count(EventType.CHAT_MODEL_START) == 2
        assert types.count(EventType.LLM_END) == 2
        assert types.count(EventType.TOOL_START) == 1
        assert types.count(EventType.TOOL_END) == 1
        assert types.count(EventType.CHAIN_START) == 1
        assert types.count(EventType.CHAIN_END) == 1
        assert all(e.root_run_id == self.TRACE for e in events)

    def test_run_start_is_first_and_synthetic(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        first = events[0]
        assert first.event_type == EventType.RUN_START
        assert first.run_id == self.TRACE
        assert first.parent_run_id is None
        assert first.agent_name is None  # not a topology node
        assert first.tags == ["otlp", "weather-app"]
        assert first.thread_id == "conv-1"
        assert first.event_id == f"{self.TRACE}-run_start"
        assert first.raw_payload["_source"] == SOURCE_MARKER

    def test_root_span_links_to_trace_and_emits_run_end(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        root_start = _by_type(events, EventType.CHAIN_START)[0]
        assert root_start.run_id == self.ROOT
        assert root_start.parent_run_id == self.TRACE
        assert root_start.agent_name == "weather_agent"
        run_end = events[-1]
        assert run_end.event_type == EventType.RUN_END
        assert run_end.run_id == self.TRACE
        assert run_end.parent_run_id is None
        assert run_end.error_message is None
        assert run_end.event_id == f"{self.TRACE}-run_end"

    def test_children_link_to_root_span(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        tool = _by_type(events, EventType.TOOL_START)[0]
        assert tool.parent_run_id == self.ROOT
        assert tool.run_id == "c8be7c8270314442"
        assert tool.tool_name == "get_weather"
        assert tool.agent_name is None
        for llm in _by_type(events, EventType.CHAT_MODEL_START):
            assert llm.parent_run_id == self.ROOT
            assert llm.agent_name == "test"  # gen_ai.request.model

    def test_tokens_only_on_llm_end_not_aggregated(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        ends = _by_type(events, EventType.LLM_END)
        assert [(e.token_input, e.token_output) for e in ends] == [(50, 10), (20, 5)]
        # The agent span's gen_ai.aggregated_usage.* must NOT be counted (double count).
        chain_end = _by_type(events, EventType.CHAIN_END)[0]
        assert chain_end.token_input is None
        assert chain_end.token_output is None
        for e in events:
            if e.event_type not in (EventType.LLM_END,):
                assert e.token_input is None

    def test_deterministic_ids(self) -> None:
        a = _map_all(_load("pydantic_ai.json"))
        b = _map_all(_load("pydantic_ai.json"))
        assert [e.event_id for e in a] == [e.event_id for e in b]
        assert f"{self.ROOT}-start" in {e.event_id for e in a}
        assert f"{self.ROOT}-end" in {e.event_id for e in a}

    def test_messages_and_summary(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        llm_start = _by_type(events, EventType.CHAT_MODEL_START)[0]
        assert "What is the weather in Paris?" in llm_start.summary
        assert llm_start.summary.startswith("test: input=")
        # gen_ai.input.messages (JSON string) is decoded; the current user turn kept.
        assert llm_start.raw_payload["messages"]["role"] == "user"
        llm_end = _by_type(events, EventType.LLM_END)[1]
        assert "It is sunny and 22C in Paris." in llm_end.summary
        assert llm_end.raw_payload["messages"][0]["role"] == "assistant"
        tool_start = _by_type(events, EventType.TOOL_START)[0]
        assert tool_start.raw_payload["messages"] == {"city": "Paris"}
        assert "Paris" in tool_start.summary
        tool_end = _by_type(events, EventType.TOOL_END)[0]
        assert tool_end.raw_payload["messages"] == "Sunny, 22C"
        assert tool_end.summary == "get_weather end: output=Sunny, 22C"

    def test_payload_shape(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        start = _by_type(events, EventType.CHAIN_START)[0]
        assert start.full_blob_eligible is True
        assert start.raw_payload["_source"] == "otlp"
        assert start.raw_payload["attributes"]["gen_ai.agent.name"] == "weather_agent"
        assert start.raw_payload["resource"]["service.name"] == "weather-app"
        assert start.raw_payload["span"]["name"] == "invoke_agent weather_agent"
        end = _by_type(events, EventType.CHAIN_END)[0]
        assert end.raw_payload["status"] == {"code": "UNSET", "message": None}
        assert "events" in end.raw_payload

    def test_timestamps(self) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        start = _by_type(events, EventType.CHAIN_START)[0]
        end = _by_type(events, EventType.CHAIN_END)[0]
        assert start.timestamp.tzinfo is not None
        assert (end.timestamp - start.timestamp).total_seconds() == pytest.approx(0.65)
        assert start.timestamp.isoformat() == "2025-11-21T08:00:00+00:00"


# ---------------------------------------------------------- OpenAI Agents


class TestOpenAIAgents:
    def test_nested_tool_error_does_not_fail_run(self) -> None:
        events = _map_all(_load("openai_agents.json"))
        errs = _by_type(events, EventType.TOOL_ERROR)
        assert len(errs) == 1
        err = errs[0]
        assert err.tool_name == "get_time"
        assert err.error_message == "tz is required"
        assert err.raw_payload["type"] == "ValueError"
        assert err.summary.startswith("get_time error: tz is required")
        assert err.full_blob_eligible is True
        run_end = _by_type(events, EventType.RUN_END)[0]
        assert run_end.error_message is None
        assert _by_type(events, EventType.TOOL_END) == []

    def test_workflow_and_agent_names(self) -> None:
        events = _map_all(_load("openai_agents.json"))
        chains = _by_type(events, EventType.CHAIN_START)
        assert [c.agent_name for c in chains] == ["Agent workflow", "Triage Agent"]
        workflow, agent = chains
        assert workflow.parent_run_id == "1bf7651916cd43dd8448eb211c80319d"
        assert agent.parent_run_id == workflow.run_id

    def test_llm_model_and_thread(self) -> None:
        events = _map_all(_load("openai_agents.json"))
        llm = _by_type(events, EventType.CHAT_MODEL_START)[0]
        assert llm.agent_name == "gpt-4o-mini"
        assert llm.raw_payload["attributes"]["gen_ai.response.finish_reasons"] == ["tool_calls"]
        assert llm.raw_payload["attributes"]["gen_ai.request.temperature"] == 0.2
        # Thread id comes from the span that carries it (the workflow root).
        run_start = events[0]
        assert run_start.thread_id == "thread-abc"
        ends = _by_type(events, EventType.LLM_END)
        assert [(e.token_input, e.token_output) for e in ends] == [(120, 18), (150, 30)]


# ------------------------------------------------------- CrewAI / OpenInference


class TestCrewAIOpenInference:
    def test_kinds(self) -> None:
        events = _map_all(_load("crewai_openinference.json"))
        types = [
            e.event_type
            for e in events
            if e.event_type.value.endswith("_start") and e.event_type != EventType.RUN_START
        ]
        assert types == [
            EventType.CHAIN_START,  # Crew.kickoff (CHAIN)
            EventType.CHAIN_START,  # Task (AGENT)
            EventType.TOOL_START,  # ToolUsage (TOOL)
            EventType.CHAT_MODEL_START,  # ChatCompletion (LLM)
        ]

    def test_names(self) -> None:
        events = _map_all(_load("crewai_openinference.json"))
        chains = _by_type(events, EventType.CHAIN_START)
        assert chains[0].agent_name == "Crew.kickoff"
        assert chains[1].agent_name == "Researcher"  # graph.node.id beats span name
        tool = _by_type(events, EventType.TOOL_START)[0]
        assert tool.tool_name == "search"
        llm = _by_type(events, EventType.CHAT_MODEL_START)[0]
        assert llm.agent_name == "gpt-4o"  # llm.model_name
        assert llm.parent_run_id == chains[1].run_id

    def test_openinference_messages_and_tokens(self) -> None:
        events = _map_all(_load("crewai_openinference.json"))
        llm_start = _by_type(events, EventType.CHAT_MODEL_START)[0]
        assert llm_start.raw_payload["messages"] == {
            "role": "user",
            "content": "Summarize the search results.",
        }
        assert "Summarize the search results." in llm_start.summary
        llm_end = _by_type(events, EventType.LLM_END)[0]
        assert llm_end.raw_payload["messages"] == [
            {"role": "assistant", "content": "Quantum computing uses qubits..."}
        ]
        assert (llm_end.token_input, llm_end.token_output) == (300, 40)
        # input.value JSON strings are decoded for chain/tool payloads.
        crew = _by_type(events, EventType.CHAIN_START)[0]
        assert crew.raw_payload["messages"] == {"topic": "quantum computing"}
        tool = _by_type(events, EventType.TOOL_START)[0]
        assert tool.raw_payload["messages"] == {"query": "quantum computing 2025"}
        tool_end = _by_type(events, EventType.TOOL_END)[0]
        assert tool_end.raw_payload["messages"] == "10 results..."

    def test_tags_carry_service_name(self) -> None:
        events = _map_all(_load("crewai_openinference.json"))
        assert events[0].tags == ["otlp", "research-crew"]
        assert all(e.tags == ["otlp", "research-crew"] for e in events)


# ---------------------------------------------------------- raw SDK clients


class TestClients:
    def test_openai_single_span_trace(self) -> None:
        events = _map_all(_load("openai_client.json"))
        assert [e.event_type for e in events] == [
            EventType.RUN_START,
            EventType.CHAT_MODEL_START,
            EventType.LLM_END,
            EventType.RUN_END,
        ]
        start, end = events[1], events[2]
        trace = "3df7651916cd43dd8448eb211c80319f"
        assert start.parent_run_id == trace  # root span hangs under the run row
        assert start.agent_name == "gpt-4o"
        assert (end.token_input, end.token_output) == (12, 9)
        assert "Say hi" in start.summary
        assert "Hi there!" in end.summary

    def test_anthropic_root_error_fails_run(self) -> None:
        events = _map_all(_load("anthropic_client.json"))
        failed = [e for e in events if e.root_run_id == "4ef7651916cd43dd8448eb211c803190"]
        assert [e.event_type for e in failed] == [
            EventType.RUN_START,
            EventType.CHAT_MODEL_START,
            EventType.LLM_ERROR,
            EventType.RUN_END,
        ]
        err, run_end = failed[2], failed[3]
        assert err.error_message == "Error code: 429 - rate limit exceeded"
        assert err.raw_payload["type"] == "anthropic.RateLimitError"
        assert run_end.error_message == "Error code: 429 - rate limit exceeded"
        assert "failed" in run_end.summary
        ok = [e for e in events if e.root_run_id == "5ff7651916cd43dd8448eb211c803191"]
        assert ok[0].event_type == EventType.RUN_START  # second trace gets its own run
        assert ok[-1].error_message is None


# ------------------------------------------------------------- edge cases


class TestClassification:
    def test_genai_operation_wins(self) -> None:
        k = classify(_span("whatever", attrs={"gen_ai.operation.name": "execute_tool",
                                               "openinference.span.kind": "LLM"}))
        assert k.start == EventType.TOOL_START

    @pytest.mark.parametrize(
        ("op", "start"),
        [
            ("chat", EventType.CHAT_MODEL_START),
            ("text_completion", EventType.LLM_START),
            ("generate_content", EventType.LLM_START),
            ("embeddings", EventType.LLM_START),
            ("execute_tool", EventType.TOOL_START),
            ("invoke_agent", EventType.CHAIN_START),
            ("invoke_workflow", EventType.CHAIN_START),
            ("create_agent", EventType.CHAIN_START),
            ("retrieval", EventType.RETRIEVER_START),
        ],
    )
    def test_genai_ops(self, op: str, start: EventType) -> None:
        assert classify(_span("x", attrs={"gen_ai.operation.name": op})).start == start

    @pytest.mark.parametrize(
        ("kind", "start"),
        [
            ("LLM", EventType.CHAT_MODEL_START),
            ("TOOL", EventType.TOOL_START),
            ("AGENT", EventType.CHAIN_START),
            ("CHAIN", EventType.CHAIN_START),
            ("RETRIEVER", EventType.RETRIEVER_START),
            ("EMBEDDING", EventType.LLM_START),
            ("RERANKER", EventType.CHAIN_START),
            ("GUARDRAIL", EventType.CHAIN_START),
        ],
    )
    def test_openinference_kinds(self, kind: str, start: EventType) -> None:
        assert classify(_span("x", attrs={"openinference.span.kind": kind})).start == start

    @pytest.mark.parametrize(
        ("name", "start"),
        [
            ("chat gpt-4o", EventType.CHAT_MODEL_START),
            ("execute_tool search", EventType.TOOL_START),
            ("invoke_agent bob", EventType.CHAIN_START),
            ("ChatCompletion", EventType.CHAT_MODEL_START),
            ("anthropic.messages.create", EventType.CHAT_MODEL_START),
            ("retrieval docs", EventType.RETRIEVER_START),
        ],
    )
    def test_name_prefixes(self, name: str, start: EventType) -> None:
        assert classify(_span(name)).start == start

    def test_model_plus_tokens_is_llm(self) -> None:
        k = classify(_span("vendor.call", attrs={"gen_ai.request.model": "m",
                                                 "gen_ai.usage.input_tokens": 3}))
        assert k.start == EventType.CHAT_MODEL_START

    def test_fallback_is_chain_named_after_span(self) -> None:
        events = SpanMapper().map(_span("my custom step", attrs={"foo": "bar"}))
        start = _by_type(events, EventType.CHAIN_START)[0]
        assert start.agent_name == "my custom step"
        assert start.tool_name is None


class TestFields:
    def test_name_fallback_chain(self) -> None:
        # No attributes: remainder of the span name.
        ev = SpanMapper().map(_span("chat gpt-4.1"))
        assert _by_type(ev, EventType.CHAT_MODEL_START)[0].agent_name == "gpt-4.1"
        ev = SpanMapper().map(_span("execute_tool lookup"))
        assert _by_type(ev, EventType.TOOL_START)[0].tool_name == "lookup"
        ev = SpanMapper().map(_span("invoke_agent"))  # no remainder -> whole name
        assert _by_type(ev, EventType.CHAIN_START)[0].agent_name == "invoke_agent"
        ev = SpanMapper().map(_span("chat", attrs={"gen_ai.response.model": "resp-model"}))
        assert _by_type(ev, EventType.CHAT_MODEL_START)[0].agent_name == "resp-model"

    def test_thread_id_fallbacks(self) -> None:
        ev = SpanMapper().map(_span("x", attrs={"session.id": "s-1"}))
        assert all(e.thread_id == "s-1" for e in ev)
        ev = SpanMapper().map(_span("x", attrs={"gen_ai.conversation.id": "c-1", "session.id": "s"}))
        assert ev[0].thread_id == "c-1"
        ev = SpanMapper().map(_span("x"))
        assert ev[0].thread_id is None

    def test_mcp_server(self) -> None:
        ev = SpanMapper().map(
            _span("execute_tool fetch", attrs={"gen_ai.operation.name": "execute_tool",
                                                "gen_ai.tool.name": "fetch",
                                                "mcp.server.name": "docs-server"})
        )
        assert _by_type(ev, EventType.TOOL_START)[0].mcp_server == "docs-server"
        ev = SpanMapper().map(
            _span("execute_tool fetch", attrs={"gen_ai.operation.name": "execute_tool",
                                                "mcp.method.name": "tools/call",
                                                "server.address": "localhost"})
        )
        assert _by_type(ev, EventType.TOOL_START)[0].mcp_server == "localhost"
        ev = SpanMapper().map(_span("execute_tool fetch"))
        assert _by_type(ev, EventType.TOOL_START)[0].mcp_server is None

    def test_token_fallback_keys(self) -> None:
        ev = SpanMapper().map(
            _span("chat m", attrs={"gen_ai.usage.prompt_tokens": "7",
                                   "gen_ai.usage.completion_tokens": 3.0})
        )
        end = _by_type(ev, EventType.LLM_END)[0]
        assert (end.token_input, end.token_output) == (7, 3)

    def test_error_message_sources(self) -> None:
        # status message wins
        ev = SpanMapper().map(_span("chat m", status="ERROR", status_message="boom"))
        assert _by_type(ev, EventType.LLM_ERROR)[0].error_message == "boom"
        # exception event next
        ev = SpanMapper().map(
            _span("chat m", status="ERROR",
                  events=[{"name": "exception", "time_ns": 0,
                           "attributes": {"exception.type": "KeyError",
                                          "exception.message": "missing"}}])
        )
        err = _by_type(ev, EventType.LLM_ERROR)[0]
        assert err.error_message == "missing"
        assert err.raw_payload["type"] == "KeyError"
        # nothing -> error.type -> "error"
        ev = SpanMapper().map(_span("chat m", status="ERROR", attrs={"error.type": "Timeout"}))
        assert _by_type(ev, EventType.LLM_ERROR)[0].error_message == "Timeout"
        ev = SpanMapper().map(_span("chat m", status="ERROR"))
        assert _by_type(ev, EventType.LLM_ERROR)[0].error_message == "error"

    def test_summary_clipped(self) -> None:
        ev = SpanMapper(summary_max_chars=12).map(
            _span("chat m", attrs={"gen_ai.prompt": "a" * 500})
        )
        assert all(len(e.summary) <= 12 for e in ev)

    def test_end_before_start_clamped(self) -> None:
        ev = SpanMapper().map(_span("x", start_ns=10_000_000_000, end_ns=5_000_000_000))
        start = _by_type(ev, EventType.CHAIN_START)[0]
        end = _by_type(ev, EventType.CHAIN_END)[0]
        assert end.timestamp == start.timestamp

    def test_run_start_once_per_trace_and_lru(self) -> None:
        mapper = SpanMapper(seen_cap=1)
        a1 = mapper.map(_span("a", trace_id="a" * 32, span_id="1" * 16, parent="9" * 16))
        a2 = mapper.map(_span("a", trace_id="a" * 32, span_id="2" * 16, parent="9" * 16))
        assert _by_type(a1, EventType.RUN_START)
        assert not _by_type(a2, EventType.RUN_START)
        b = mapper.map(_span("b", trace_id="b" * 32, span_id="3" * 16))
        assert _by_type(b, EventType.RUN_START)
        # 'a' was evicted (cap=1): a later span for it re-emits RUN_START, which the
        # worker upserts idempotently.
        a3 = mapper.map(_span("a", trace_id="a" * 32, span_id="4" * 16, parent="9" * 16))
        assert _by_type(a3, EventType.RUN_START)

    def test_zero_parent_is_root(self) -> None:
        ev = SpanMapper().map(_span("x", parent="0" * 16))
        assert _by_type(ev, EventType.RUN_END)
        assert _by_type(ev, EventType.CHAIN_START)[0].parent_run_id == "a" * 32

    def test_non_root_span_emits_no_run_end(self) -> None:
        ev = SpanMapper().map(_span("x", parent="9" * 16))
        assert not _by_type(ev, EventType.RUN_END)
        assert _by_type(ev, EventType.CHAIN_START)[0].parent_run_id == "9" * 16


# ------------------------------------------------------- through the worker


class _WS:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def broadcast(self, run_id: str, message: Any) -> None:
        self.messages.append(message)

    async def broadcast_all(self, message: Any) -> None:
        self.messages.append(message)


async def _ingest(tmp_path: Path, events: list[RawEvent]) -> tuple[SQLiteBackend, StorageWorker]:
    cfg = TraceSageConfig(data_dir=tmp_path)
    cfg.ensure_data_dirs()
    db = SQLiteBackend(cfg.db_path)
    await db.init()
    import asyncio

    worker = StorageWorker(asyncio.Queue(), db, BlobStore(cfg.blob_dir), _WS(), cfg, Stats())
    await worker._process_batch(events)
    return db, worker


class TestThroughWorker:
    async def test_pydantic_ai_run_completed_with_durations(self, tmp_path: Path) -> None:
        events = _map_all(_load("pydantic_ai.json"))
        db, _ = await _ingest(tmp_path, events)
        try:
            run = await db.get_run(TestPydanticAI.TRACE)
            assert run is not None
            assert run.status == RunStatus.COMPLETED
            assert run.total_tokens_input == 70
            assert run.total_tokens_output == 15
            assert run.thread_id == "conv-1"
            assert run.tags == ["otlp", "weather-app"]
            runs, total = await db.list_runs(limit=10)
            assert total == 1
            assert [r.run_id for r in runs] == [TestPydanticAI.TRACE]  # sub-runs hidden

            journey = await db.get_journey(TestPydanticAI.TRACE)
            by_id = {e.event_id: e for e in journey}
            assert by_id[f"{TestPydanticAI.ROOT}-end"].duration_ms == 650
            assert by_id["c8be7c8270314442-end"].duration_ms == 50
            assert by_id["b7ad6b7169203331-end"].duration_ms == 250
            assert by_id[f"{TestPydanticAI.ROOT}-start"].blob_path is not None

            topo = await db.get_topology(scope=f"run:{TestPydanticAI.TRACE}")
            kinds = {(n.type, n.name) for n in topo.nodes}
            assert ("agent", "weather_agent") in kinds
            assert ("tool", "get_weather") in kinds
            assert ("llm", "test") in kinds
            edges = {(e.source, e.target) for e in topo.edges}
            assert ("agent:weather_agent", "tool:get_weather") in edges
            assert ("agent:weather_agent", "llm:test") in edges
        finally:
            await db.close()

    async def test_root_error_marks_run_failed(self, tmp_path: Path) -> None:
        events = _map_all(_load("anthropic_client.json"))
        db, _ = await _ingest(tmp_path, events)
        try:
            failed = await db.get_run("4ef7651916cd43dd8448eb211c803190")
            assert failed is not None
            assert failed.status == RunStatus.FAILED
            assert failed.error_message == "Error code: 429 - rate limit exceeded"
            ok = await db.get_run("5ff7651916cd43dd8448eb211c803191")
            assert ok is not None
            assert ok.status == RunStatus.COMPLETED
        finally:
            await db.close()

    async def test_nested_error_keeps_run_completed(self, tmp_path: Path) -> None:
        events = _map_all(_load("openai_agents.json"))
        db, _ = await _ingest(tmp_path, events)
        try:
            run = await db.get_run("1bf7651916cd43dd8448eb211c80319d")
            assert run is not None
            assert run.status == RunStatus.COMPLETED
            topo = await db.get_topology(scope="run:1bf7651916cd43dd8448eb211c80319d")
            tool = next(n for n in topo.nodes if n.id == "tool:get_time")
            assert tool.error_count == 1
        finally:
            await db.close()

    async def test_replayed_batch_is_idempotent(self, tmp_path: Path) -> None:
        """OTLP retries re-send the same spans; deterministic ids make that an upsert."""
        events = _map_all(_load("openai_client.json"))
        db, worker = await _ingest(tmp_path, events)
        try:
            await worker._process_batch(_map_all(_load("openai_client.json")))
            journey = await db.get_journey("3df7651916cd43dd8448eb211c80319f")
            assert len(journey) == 4
        finally:
            await db.close()


def test_fixtures_are_valid_otlp_json() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text())
        assert "resourceSpans" in doc, path.name
        assert decode_json(path.read_bytes()), path.name
