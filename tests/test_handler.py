"""Tests for TraceLensCallbackHandler.

Strategy: use a stub tracer that records emit() calls. Avoids any dependency on
the real TraceLens, queue, worker, storage, or server.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tracelens.adapters.langchain import TraceLensCallbackHandler
from tracelens.config import TraceLensConfig
from tracelens.models import EventType, RawEvent


class _StubTracer:
    """Minimal duck-typed tracer for handler unit tests."""

    def __init__(self, *, summary_max_chars: int = 500) -> None:
        self._config = TraceLensConfig(summary_max_chars=summary_max_chars)
        self.events: list[RawEvent] = []
        self._root_map: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def emit(self, event: RawEvent) -> None:
        with self._lock:
            self.events.append(event)

    def get_or_set_root(self, run_id: str, parent_run_id: str | None) -> str:
        with self._lock:
            if parent_run_id is None:
                if run_id not in self._root_map:
                    self._root_map[run_id] = run_id
                return self._root_map[run_id]
            parent_root = self._root_map.get(parent_run_id, parent_run_id)
            self._root_map[run_id] = parent_root
            return parent_root


def _make_handler(**kwargs: Any) -> tuple[TraceLensCallbackHandler, _StubTracer]:
    tracer = _StubTracer(**kwargs)
    handler = TraceLensCallbackHandler(tracer)
    return handler, tracer


# ---------------------------------------------------------------------- 1
def test_handler_never_raises() -> None:
    """If tracer.emit raises, the handler must still return None silently."""
    handler, tracer = _make_handler()

    def boom(_event: RawEvent) -> None:
        raise RuntimeError("simulated emit failure")

    tracer.emit = boom  # type: ignore[assignment]

    rid = uuid.uuid4()
    pid = uuid.uuid4()

    # All 16 methods must absorb the failure.
    handler.on_chain_start({"name": "a"}, {"x": 1}, run_id=rid)
    handler.on_chain_end({"out": 2}, run_id=rid)
    handler.on_chain_error(ValueError("e"), run_id=rid)
    handler.on_agent_action(type("A", (), {"tool": "t", "tool_input": "i"})(), run_id=rid)
    handler.on_agent_finish(type("F", (), {"return_values": {"x": 1}, "log": ""})(), run_id=rid)
    handler.on_tool_start({"name": "t"}, "in", run_id=rid)
    handler.on_tool_end("out", run_id=rid)
    handler.on_tool_error(ValueError("e"), run_id=rid)
    handler.on_llm_start({"name": "llm"}, ["p"], run_id=rid)
    handler.on_chat_model_start({"name": "chat"}, [[]], run_id=rid)
    handler.on_llm_end(type("R", (), {"generations": [[]], "llm_output": {}})(), run_id=rid)
    handler.on_llm_error(ValueError("e"), run_id=rid)
    handler.on_retriever_start({"name": "r"}, "q", run_id=rid)
    handler.on_retriever_end([{"x": 1}], run_id=rid)
    handler.on_retriever_error(ValueError("e"), run_id=rid)
    # And with parent_run_id set.
    handler.on_chain_start({"name": "a"}, {"x": 1}, run_id=rid, parent_run_id=pid)


# ---------------------------------------------------------------------- 2
def test_run_id_converted_to_string() -> None:
    """LangChain passes UUID objects; handler must stringify."""
    handler, tracer = _make_handler()
    run_id = uuid.uuid4()
    handler.on_chain_start({"name": "agent"}, {"x": 1}, run_id=run_id)

    assert tracer.events, "no events emitted"
    for evt in tracer.events:
        assert isinstance(evt.run_id, str)
        assert evt.run_id == str(run_id)


# ---------------------------------------------------------------------- 3
def test_parent_to_root_propagation() -> None:
    """A child event's root_run_id must equal its parent's root."""
    handler, tracer = _make_handler()
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()

    handler.on_chain_start({"name": "root"}, {"x": 1}, run_id=root_id)
    handler.on_chain_start({"name": "child"}, {"y": 2}, run_id=child_id, parent_run_id=root_id)

    # First two events are RUN_START + CHAIN_START for root, then a CHAIN_START for child.
    chain_starts = [e for e in tracer.events if e.event_type == EventType.CHAIN_START]
    assert len(chain_starts) == 2
    root_chain = chain_starts[0]
    child_chain = chain_starts[1]

    assert root_chain.run_id == str(root_id)
    assert root_chain.root_run_id == str(root_id)
    assert child_chain.run_id == str(child_id)
    assert child_chain.parent_run_id == str(root_id)
    assert child_chain.root_run_id == str(root_id)


# ---------------------------------------------------------------------- 4
def test_summary_truncation() -> None:
    """Summary must respect the configured max length even with absurd inputs."""
    handler, tracer = _make_handler(summary_max_chars=100)
    huge = "X" * 10_000
    handler.on_chain_start({"name": "agent"}, {"input": huge}, run_id=uuid.uuid4())

    chain = next(e for e in tracer.events if e.event_type == EventType.CHAIN_START)
    assert len(chain.summary) <= 100


# ---------------------------------------------------------------------- 5
async def test_concurrent_runs_no_mixing() -> None:
    """50 concurrent asyncio tasks each invoke a chain through the handler.

    Every event recorded must trace back to its source task's run_id.
    """
    handler, tracer = _make_handler()

    async def one(run_id: uuid.UUID) -> None:
        handler.on_chain_start({"name": "agent"}, {"i": str(run_id)}, run_id=run_id)
        await asyncio.sleep(0)
        handler.on_chain_end({"out": str(run_id)}, run_id=run_id)

    ids = [uuid.uuid4() for _ in range(50)]
    await asyncio.gather(*[one(rid) for rid in ids])

    # Each id should have produced exactly one CHAIN_START + one CHAIN_END.
    for rid in ids:
        rid_s = str(rid)
        starts = [e for e in tracer.events if e.event_type == EventType.CHAIN_START and e.run_id == rid_s]
        ends = [e for e in tracer.events if e.event_type == EventType.CHAIN_END and e.run_id == rid_s]
        assert len(starts) == 1, f"missing start for {rid}"
        assert len(ends) == 1, f"missing end for {rid}"


# ---------------------------------------------------------------------- 6
def test_thread_safe_emit() -> None:
    """Calling the handler from a thread pool must record all events."""
    handler, tracer = _make_handler()

    def emit_one() -> None:
        handler.on_chain_start({"name": "x"}, {"v": 1}, run_id=uuid.uuid4())

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: emit_one(), range(64)))

    # Each chain_start at root also emits a RUN_START synthetic, so 64*2 = 128 events.
    assert len(tracer.events) == 128


# ---------------------------------------------------------------------- 7
def test_on_chat_model_start_emits_event() -> None:
    handler, tracer = _make_handler()
    handler.on_chat_model_start({"name": "chat"}, [[]], run_id=uuid.uuid4())

    types = [e.event_type for e in tracer.events]
    assert EventType.CHAT_MODEL_START in types


# ---------------------------------------------------------------------- 8
def test_on_retriever_events_captured() -> None:
    handler, tracer = _make_handler()
    rid = uuid.uuid4()
    handler.on_retriever_start({"name": "r"}, "what is x?", run_id=rid)
    handler.on_retriever_end([{"page_content": "..."}, {"page_content": "..."}], run_id=rid)

    types = [e.event_type for e in tracer.events]
    assert EventType.RETRIEVER_START in types
    assert EventType.RETRIEVER_END in types


# ---------------------------------------------------------------------- 9
def test_synthetic_run_start_emitted_on_root_chain_start() -> None:
    handler, tracer = _make_handler()
    handler.on_chain_start({"name": "root"}, {"x": 1}, run_id=uuid.uuid4())

    types = [e.event_type for e in tracer.events]
    assert EventType.RUN_START in types
    assert EventType.CHAIN_START in types
    # RUN_START first, CHAIN_START second.
    assert types[0] == EventType.RUN_START
    assert types[1] == EventType.CHAIN_START


# ---------------------------------------------------------------------- 10
def test_no_run_start_for_nested_chain() -> None:
    """A non-root chain_start must NOT emit a synthetic RUN_START."""
    handler, tracer = _make_handler()
    parent = uuid.uuid4()
    child = uuid.uuid4()
    handler.on_chain_start({"name": "p"}, {}, run_id=parent)
    tracer.events.clear()
    handler.on_chain_start({"name": "c"}, {}, run_id=child, parent_run_id=parent)

    types = [e.event_type for e in tracer.events]
    assert EventType.RUN_START not in types
    assert EventType.CHAIN_START in types


# ---------------------------------------------------------------------- 11
def test_handler_emits_event_with_unstringifiable_input() -> None:
    """Pathological input (object whose __repr__ raises) must NOT silently drop the event.

    Regression for the audit finding that _stringify lacked a double-catch around
    str(value), so json+str failures escaped and the outer try/except dropped events.
    """

    class BadRepr:
        def __repr__(self) -> str:
            raise RuntimeError("bad repr")

        def __str__(self) -> str:
            raise RuntimeError("bad str")

    handler, tracer = _make_handler()
    handler.on_chain_start({"name": "x"}, {"weird": BadRepr()}, run_id=uuid.uuid4())

    # Both the synthetic RUN_START and the CHAIN_START must reach the tracer despite
    # the bad object — _stringify falls back to a placeholder.
    types = [e.event_type for e in tracer.events]
    assert EventType.RUN_START in types
    assert EventType.CHAIN_START in types


# ---------------------------------------------------------------------- 12
def test_on_chain_error_recalls_agent_name() -> None:
    """on_chain_error must populate agent_name from the cache set at chain_start.

    Regression for the audit finding that error events had agent_name=None even
    though the start event captured the name.
    """
    handler, tracer = _make_handler()
    run_id = uuid.uuid4()
    handler.on_chain_start({"name": "MyAgent"}, {}, run_id=run_id)
    tracer.events.clear()
    handler.on_chain_error(RuntimeError("oops"), run_id=run_id)

    assert len(tracer.events) == 1
    assert tracer.events[0].event_type == EventType.CHAIN_ERROR
    assert tracer.events[0].agent_name == "MyAgent"


# ---------------------------------------------------------------------- 13
def test_token_usage_from_usage_metadata() -> None:
    """Modern langchain puts token usage on message.usage_metadata.

    Regression for adapter-1: non-OpenAI chat models (Anthropic, Bedrock,
    Vertex, Ollama) and modern langchain report tokens via usage_metadata,
    NOT llm_output. The handler must surface those on the LLM_END event.
    """
    from tracelens.adapters.langchain import _extract_token_usage

    msg = type("Msg", (), {"usage_metadata": {"input_tokens": 10, "output_tokens": 5}})()
    gen = type("Gen", (), {"message": msg})()
    response = type("R", (), {"generations": [[gen]], "llm_output": None})()

    assert _extract_token_usage(response) == (10, 5)

    # And end-to-end through on_llm_end so token_input/token_output land on the event.
    handler, tracer = _make_handler()
    handler.on_llm_end(response, run_id=uuid.uuid4())
    llm_end = next(e for e in tracer.events if e.event_type == EventType.LLM_END)
    assert llm_end.token_input == 10
    assert llm_end.token_output == 5


# ---------------------------------------------------------------------- 14
def test_on_retry_never_raises_and_emits_retry_event() -> None:
    """on_retry must absorb a pathological retry_state and emit one RETRY event.

    The RETRY event is informational: no tool_name / agent_name (so it produces
    no topology node).
    """
    handler, tracer = _make_handler()

    class BadRetryState:
        attempt_number = 3

        @property
        def outcome(self) -> Any:
            raise RuntimeError("no outcome yet")

        def __str__(self) -> str:
            raise RuntimeError("bad str")

    rid = uuid.uuid4()
    # Must not raise even though every attribute access / str() is hostile.
    handler.on_retry(BadRetryState(), run_id=rid)

    retries = [e for e in tracer.events if e.event_type == EventType.RETRY]
    assert len(retries) == 1
    evt = retries[0]
    assert evt.event_type == EventType.RETRY
    assert evt.tool_name is None
    assert evt.agent_name is None
    assert evt.run_id == str(rid)

    # And a benign retry_state with attempt info populates the summary/payload.
    handler2, tracer2 = _make_handler()
    state = type("S", (), {"attempt_number": 2, "outcome": "RetryError"})()
    handler2.on_retry(state, run_id=uuid.uuid4())
    retry2 = next(e for e in tracer2.events if e.event_type == EventType.RETRY)
    assert "2" in retry2.summary
    assert retry2.raw_payload["attempt"] == 2


# ---------------------------------------------------------------------- 15
def test_llm_end_stream_payload_has_ttft_ms() -> None:
    """on_llm_start → on_llm_new_token → on_llm_end yields a numeric ttft_ms >= 0.

    True TTFT is the delta between the recorded llm_start timestamp and the
    first streamed token, surfaced on the LLM_END raw_payload._stream dict.
    """
    handler, tracer = _make_handler()
    rid = uuid.uuid4()

    handler.on_llm_start({"name": "llm"}, ["hello"], run_id=rid)
    handler.on_llm_new_token("Hi", run_id=rid)
    handler.on_llm_new_token(" there", run_id=rid)
    response = type("R", (), {"generations": [[]], "llm_output": {}})()
    handler.on_llm_end(response, run_id=rid)

    llm_end = next(e for e in tracer.events if e.event_type == EventType.LLM_END)
    stream = llm_end.raw_payload.get("_stream")
    assert stream is not None, "no _stream telemetry on LLM_END"
    ttft = stream.get("ttft_ms")
    assert isinstance(ttft, int)
    assert ttft >= 0
    # Start-ts cache must be drained after on_llm_end (no leak).
    assert str(rid) not in handler._llm_start_ts


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
