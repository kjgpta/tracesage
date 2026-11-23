"""Tests for the manual tracing API: tracer.span() / tracer.trace_fn()."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

import tracesage
from tracesage.config import TraceSageConfig
from tracesage.manual import Span, current_run_id, current_span
from tracesage.models import EventType, RunStatus
from tracesage.tracer import TraceSage, _DisabledTraceSage


@pytest_asyncio.fixture
async def tl(tmp_path: Path):
    cfg = TraceSageConfig(data_dir=tmp_path, worker_batch_timeout=0.02, print_run_url=False)
    tracer = await TraceSage.create(cfg, start_server=False)
    try:
        yield tracer
    finally:
        await tracer.stop()


async def _journey(tl: TraceSage, run_id: str):
    await tl.flush()
    return await tl.db.get_journey(run_id)


async def test_sync_nested_spans(tl: TraceSage) -> None:
    with tl.span("checkout", kind="agent", inputs={"cart": 7}, tags=["manual"]) as root:
        assert current_span() is root
        assert root.is_root
        with tl.span("lookup_prices", kind="tool", inputs={"sku": "A"}) as tool:
            assert tool.parent_run_id == root.run_id
            assert tool.root_run_id == root.run_id
            tool.set_output({"A": 9.5})
        with tl.span("gpt", kind="llm", model="gpt-4o") as llm:
            llm.set_tokens(input=12, output=3).set_output("ok")
        root.set_output("done").set_attributes(customer="c1")
    assert current_span() is None

    run = await tl.db.get_run(root.run_id)
    assert run is None or run.status == RunStatus.RUNNING  # not yet flushed
    events = await _journey(tl, root.run_id)
    types = [e.event_type for e in events]
    assert types.count(EventType.RUN_START) == 1
    assert types.count(EventType.CHAIN_START) == 1
    assert types.count(EventType.TOOL_START) == types.count(EventType.TOOL_END) == 1
    assert types.count(EventType.CHAT_MODEL_START) == types.count(EventType.LLM_END) == 1
    assert types.count(EventType.CHAIN_END) == 1
    assert len(types) == 7  # RUN_START + 3 start/end pairs

    run = await tl.db.get_run(root.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.tags == ["manual"]
    assert (run.total_tokens_input, run.total_tokens_output) == (12, 3)

    by_type = {e.event_type: e for e in events}
    assert by_type[EventType.CHAIN_START].agent_name == "checkout"
    assert by_type[EventType.CHAIN_START].parent_run_id is None
    assert "cart" in by_type[EventType.CHAIN_START].summary
    assert by_type[EventType.TOOL_START].tool_name == "lookup_prices"
    assert by_type[EventType.TOOL_START].parent_run_id == root.run_id
    assert by_type[EventType.TOOL_END].duration_ms is not None
    assert by_type[EventType.TOOL_END].summary == 'lookup_prices end: output={"A": 9.5}'
    assert by_type[EventType.CHAT_MODEL_START].agent_name == "gpt-4o"
    assert by_type[EventType.CHAIN_END].duration_ms is not None
    assert by_type[EventType.CHAIN_END].blob_path is not None
    payload = await tl.blob_store.read(by_type[EventType.CHAIN_END].blob_path)
    assert payload["_source"] == "manual"
    assert payload["outputs"] == "done"
    assert payload["attributes"] == {"customer": "c1"}

    topo = await tl.db.get_topology(scope=f"run:{root.run_id}")
    ids = {n.id for n in topo.nodes}
    assert ids == {"agent:checkout", "tool:lookup_prices", "llm:gpt-4o"}
    assert {(e.source, e.target) for e in topo.edges} == {
        ("agent:checkout", "tool:lookup_prices"),
        ("agent:checkout", "llm:gpt-4o"),
    }


async def test_async_spans_and_concurrent_children(tl: TraceSage) -> None:
    async with tl.span("pipeline", thread_id="t-1") as root:

        async def step(i: int) -> str:
            async with tl.span(f"step{i}", kind="tool") as sp:
                await asyncio.sleep(0.01)
                sp.set_output(i)
                assert sp.parent_run_id == root.run_id
                assert sp.thread_id == "t-1"  # inherited
                return f"s{i}"

        results = await asyncio.gather(step(1), step(2), step(3))
        root.set_output(results)
    events = await _journey(tl, root.run_id)
    tools = [e for e in events if e.event_type == EventType.TOOL_START]
    assert sorted(e.tool_name for e in tools) == ["step1", "step2", "step3"]
    assert all(e.parent_run_id == root.run_id for e in tools)
    run = await tl.db.get_run(root.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.thread_id == "t-1"


async def test_exception_marks_error_and_fails_run(tl: TraceSage) -> None:
    root = tl.span("job")

    def _job() -> None:
        with root:
            with tl.span("inner", kind="tool") as sp:
                sp.set_output("partial")
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _job()
    events = await _journey(tl, root.run_id)
    err = next(e for e in events if e.event_type == EventType.CHAIN_ERROR)
    assert err.error_message == "ValueError: boom"
    assert err.summary.startswith("job error: ValueError: boom")
    payload = await tl.blob_store.read(err.blob_path)
    assert payload["type"] == "ValueError"
    assert "Traceback" in payload["traceback"]
    assert EventType.TOOL_END in {e.event_type for e in events}  # inner completed
    run = await tl.db.get_run(root.run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error_message == "ValueError: boom"


async def test_nested_error_does_not_fail_run(tl: TraceSage) -> None:
    def _inner() -> None:
        with tl.span("inner", kind="tool"):
            raise KeyError("k")

    with tl.span("job") as root, pytest.raises(KeyError):
        _inner()
    events = await _journey(tl, root.run_id)
    assert EventType.TOOL_ERROR in {e.event_type for e in events}
    run = await tl.db.get_run(root.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED


async def test_trace_fn_sync_and_async(tl: TraceSage) -> None:
    @tl.trace_fn(kind="tool")
    def add(a: int, b: int = 1) -> int:
        return a + b

    @tl.trace_fn()
    async def pipeline(x: int) -> int:
        return add(x, b=2) + add(1)

    assert pipeline.__name__ == "pipeline"
    assert await pipeline(3) == 7
    runs, total = await _flushed_runs(tl)
    assert total == 1
    run = runs[0]
    assert run.status == RunStatus.COMPLETED
    events = await tl.db.get_journey(run.run_id)
    root = next(e for e in events if e.event_type == EventType.CHAIN_START)
    assert root.agent_name == "pipeline"  # no "<locals>" noise for nested functions
    assert '"x": 3' in root.summary
    tools = [e for e in events if e.event_type == EventType.TOOL_START]
    assert len(tools) == 2
    assert all(t.parent_run_id == root.run_id for t in tools)
    assert all(t.tool_name == "add" for t in tools)
    ends = [e for e in events if e.event_type == EventType.TOOL_END]
    assert sorted(e.summary.split("output=")[1] for e in ends) == ["2", "5"]
    chain_end = next(e for e in events if e.event_type == EventType.CHAIN_END)
    assert chain_end.summary.endswith("output=7")


async def test_trace_fn_error_propagates(tl: TraceSage) -> None:
    @tl.trace_fn(name="explode")
    def explode() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        explode()
    runs, _ = await _flushed_runs(tl)
    assert runs[0].status == RunStatus.FAILED
    assert runs[0].error_message == "RuntimeError: nope"


async def _flushed_runs(tl: TraceSage):
    await tl.flush()
    return await tl.db.list_runs()


def test_invalid_kind_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown span kind"):
        Span(None, "x", kind="widget")
    disabled = _DisabledTraceSage(TraceSageConfig(enabled=False))
    with pytest.raises(ValueError, match="unknown span kind"):
        disabled.trace_fn(kind="widget")


def test_disabled_tracer_spans_are_inert() -> None:
    disabled = _DisabledTraceSage(TraceSageConfig(enabled=False))
    with disabled.span("outer", inputs={"a": 1}) as outer:
        assert current_span() is outer  # nesting still tracked
        with disabled.span("inner", kind="tool") as inner:
            assert inner.parent_run_id == outer.run_id
            inner.set_output(1).set_tokens(1, 2).set_attributes(k="v")
    assert current_span() is None

    def f(x: int) -> int:
        return x

    assert disabled.trace_fn()(f) is f  # zero-overhead passthrough


def test_background_tracer_span_from_trace(tmp_path: Path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path, start_server=False, print_run_url=False)
    with tracesage.trace(cfg, install=False) as bg:
        with bg.span("sync-job", kind="agent") as root:
            with bg.span("tool", kind="tool") as sp:
                sp.set_output("x")

            @bg.trace_fn(kind="llm", model="m")
            def call_model(prompt: str) -> str:
                return prompt.upper()

            call_model("hi")
        bg.flush()
        run = asyncio.run_coroutine_threadsafe(bg.tracer.db.get_run(root.run_id), bg._loop).result(5)
        events = asyncio.run_coroutine_threadsafe(
            bg.tracer.db.get_journey(root.run_id), bg._loop
        ).result(5)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    types = {e.event_type for e in events}
    assert {EventType.TOOL_END, EventType.LLM_END, EventType.CHAIN_END} <= types


def test_current_run_id_only_for_live_spans() -> None:
    assert current_run_id() is None
    sp = Span(None, "x")
    assert current_run_id() is None  # not started
    with sp:
        assert current_run_id() == sp.run_id
    assert current_run_id() is None
    # start/end are idempotent
    sp.start()
    sp.end()
    assert current_run_id() is None


async def test_langchain_root_nests_under_manual_span(tl: TraceSage) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: x * 2, name="doubler")
    with tl.span("wrapper", kind="agent") as root:
        out = chain.invoke(3, config={"callbacks": [tl.handler]})
    assert out == 6
    events = await _journey(tl, root.run_id)
    chain_starts = [e for e in events if e.event_type == EventType.CHAIN_START]
    names = {e.agent_name: e for e in chain_starts}
    assert "wrapper" in names
    assert "doubler" in names
    assert names["doubler"].parent_run_id == root.run_id
    assert names["doubler"].root_run_id == root.run_id
    # Only ONE run (the manual root); LangChain did not start its own.
    assert next(e for e in events if e.event_type == EventType.RUN_START).run_id == root.run_id
    runs, total = await tl.db.list_runs()
    assert total == 1
    run = runs[0]
    assert run.status == RunStatus.COMPLETED
    topo = await tl.db.get_topology(scope=f"run:{root.run_id}")
    assert ("agent:wrapper", "chain:doubler") in {(e.source, e.target) for e in topo.edges}


async def test_langchain_without_manual_span_is_its_own_root(tl: TraceSage) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: x + 1, name="inc")
    chain.invoke(1, config={"callbacks": [tl.handler]})
    await tl.flush()
    runs, total = await tl.db.list_runs()
    assert total == 1
    events = await tl.db.get_journey(runs[0].run_id)
    run_start = next(e for e in events if e.event_type == EventType.RUN_START)
    assert run_start.agent_name == "inc"
    assert run_start.run_id == runs[0].run_id
