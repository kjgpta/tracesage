"""Developer-ergonomics API: run URLs, session CM, global install, flush, renderers,
the embedded-server end-to-end path, and the sync BackgroundTracer."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import tracesage
from tracesage import TraceSage, TraceSageConfig
from tracesage.models import EventType, Run, RunStatus, StoredEvent
from tracesage.render import TraceView, render_run_tree


def _cfg(tmp_path: Path, **kw) -> TraceSageConfig:
    return TraceSageConfig(data_dir=tmp_path, host="127.0.0.1", print_run_url=False, **kw)


# ----------------------------------------------------------------- run_url


def test_run_url_none_without_server(tmp_path: Path) -> None:
    """No embedded server and no public_url → no link to hand out."""

    async def _go() -> None:
        tl = await TraceSage.create(_cfg(tmp_path), start_server=False)
        try:
            assert tl.run_url("abc") is None
        finally:
            await tl.stop()

    asyncio.run(_go())


def test_run_url_uses_public_url(tmp_path: Path) -> None:
    async def _go() -> None:
        tl = await TraceSage.create(
            _cfg(tmp_path, public_url="https://traces.example.com/"), start_server=False
        )
        try:
            assert tl.run_url("r1") == "https://traces.example.com/ui/#run=r1"
        finally:
            await tl.stop()

    asyncio.run(_go())


# ----------------------------------------------------------- kill switch (enabled)


def test_disabled_is_inert(tmp_path: Path) -> None:
    """enabled=False → no embedded server, no-op handler, no capture, guarded DB."""
    from langchain_core.language_models.fake import FakeListLLM

    async def _go() -> None:
        tl = await TraceSage.create(_cfg(tmp_path, enabled=False, port=7799), start_server=True)
        try:
            assert tl.bound_port is None, "disabled must not bind a server"
            assert tl.run_url("x") is None
            assert tl.handler is not None  # usable no-op handler for callbacks=[...]
            tl.install()  # no-op
            FakeListLLM(responses=["hi"]).invoke("hello")  # nothing should be captured
            await tl.flush()
            with pytest.raises(RuntimeError, match="disabled"):
                _ = tl.db
        finally:
            await tl.stop()

    asyncio.run(_go())


def test_env_var_disables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRACESAGE_ENABLED", "false")
    cfg = TraceSageConfig(data_dir=tmp_path)
    assert cfg.enabled is False


def test_background_tracer_disabled_starts_no_thread(tmp_path: Path) -> None:
    tl = tracesage.start(_cfg(tmp_path, enabled=False, port=7798))
    try:
        assert tl._thread is None, "disabled background tracer must not spin a thread"
        assert tl.run_url("x") is None
    finally:
        tl.stop()


# ----------------------------------------------------- session + global install


def test_session_install_captures_without_callbacks(tmp_path: Path) -> None:
    from langchain_core.language_models.fake import FakeListLLM

    async def _go() -> None:
        async with TraceSage.session(_cfg(tmp_path, port=0), install=True) as tl:
            await FakeListLLM(responses=["hi"]).ainvoke("hello")  # no callbacks=
            await tl.flush()
            runs, _ = await tl.db.list_runs(limit=10, offset=0)
            assert runs, "global install should capture the run"
            url = tl.run_url(runs[0].run_id)
            assert url is not None
            assert url.endswith(f"/ui/#run={runs[0].run_id}")

    asyncio.run(_go())


def test_session_without_install_does_not_capture(tmp_path: Path) -> None:
    """Sanity: uninstall on exit + no install means no global capture leaks."""
    from langchain_core.language_models.fake import FakeListLLM

    async def _go() -> None:
        async with TraceSage.session(_cfg(tmp_path, port=0), install=False) as tl:
            await FakeListLLM(responses=["hi"]).ainvoke("hello")  # not wired
            await tl.flush()
            runs, _ = await tl.db.list_runs(limit=10, offset=0)
            assert runs == [], "no install + no callbacks should capture nothing"

    asyncio.run(_go())


# --------------------------------------------------------------- renderers


def _events() -> list[StoredEvent]:
    t = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def ev(eid, rid, et, parent=None, name=None, tool=None, dur=None, ti=None, to=None, err=None):
        return StoredEvent(
            event_id=eid, run_id=rid, parent_run_id=parent, root_run_id="root",
            event_type=et, timestamp=t, agent_name=name, tool_name=tool,
            summary=err or (name or tool or et.value), duration_ms=dur,
            token_input=ti, token_output=to, error_message=err,
        )

    return [
        ev("e1", "root", EventType.CHAIN_START, name="agent"),
        ev("e2", "llm", EventType.LLM_START, parent="root", name="gpt"),
        ev("e3", "llm", EventType.LLM_END, parent="root", name="gpt", dur=120, ti=5, to=7),
        ev("e4", "tool", EventType.TOOL_START, parent="root", tool="search"),
        ev("e5", "tool", EventType.TOOL_ERROR, parent="root", tool="search", err="boom"),
        ev("e6", "root", EventType.CHAIN_END, name="agent", dur=450),
    ]


def test_render_run_tree_structure() -> None:
    run = Run(
        run_id="root", root_run_id="root", tags=[], status=RunStatus.FAILED,
        started_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        total_steps=3, total_tokens_input=5, total_tokens_output=7,
    )
    out = render_run_tree(run, _events(), use_color=False)
    for token in ("agent", "gpt", "search", "✗", "boom", "↑5/↓7", "├─", "└─", "failed"):
        assert token in out, f"{token!r} missing from tree:\n{out}"


def test_render_run_tree_empty() -> None:
    assert "no events" in render_run_tree(None, [], use_color=False)


def test_render_run_tree_reverse_orders_siblings_newest_first() -> None:
    from datetime import timedelta

    from tracesage.models import EventType, Run, RunStatus, StoredEvent

    t0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def ev(eid, rid, et, secs, name):
        return StoredEvent(
            event_id=eid, run_id=rid, parent_run_id="root", root_run_id="root",
            event_type=et, timestamp=t0 + timedelta(seconds=secs),
            agent_name=name, summary=name,
        )

    events = [
        StoredEvent(
            event_id="r", run_id="root", parent_run_id=None, root_run_id="root",
            event_type=EventType.CHAIN_START, timestamp=t0, agent_name="root", summary="root",
        ),
        ev("a", "a", EventType.LLM_START, 1, "first"),
        ev("b", "b", EventType.LLM_START, 2, "second"),
        ev("c", "c", EventType.LLM_START, 3, "third"),
    ]
    run = Run(
        run_id="root", root_run_id="root", tags=[], status=RunStatus.COMPLETED,
        started_at=t0, total_steps=4,
    )
    asc = render_run_tree(run, events, use_color=False, reverse=False)
    desc = render_run_tree(run, events, use_color=False, reverse=True)
    # ascending: first appears before third; descending: third before first.
    assert asc.index("first") < asc.index("third")
    assert desc.index("third") < desc.index("first")


def test_render_run_tree_tags_mcp_tools_with_server() -> None:
    from tracesage.models import EventType, Run, RunStatus, StoredEvent

    t0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        StoredEvent(
            event_id="r", run_id="root", parent_run_id=None, root_run_id="root",
            event_type=EventType.CHAIN_START, timestamp=t0, agent_name="root", summary="x",
        ),
        StoredEvent(
            event_id="t1", run_id="t1", parent_run_id="root", root_run_id="root",
            event_type=EventType.TOOL_START, timestamp=t0, tool_name="get_weather",
            mcp_server="weather", summary="x",
        ),
        StoredEvent(
            event_id="t2", run_id="t2", parent_run_id="root", root_run_id="root",
            event_type=EventType.TOOL_START, timestamp=t0, tool_name="uppercase",
            mcp_server=None, summary="x",
        ),
    ]
    run = Run(
        run_id="root", root_run_id="root", tags=[], status=RunStatus.COMPLETED,
        started_at=t0, total_steps=3,
    )
    out = render_run_tree(run, events, use_color=False)
    assert "get_weather  mcp:weather" in out  # MCP tool tagged with its server
    # local tool (no server) must NOT get an mcp tag
    upline = next(line for line in out.splitlines() if "uppercase" in line)
    assert "mcp:" not in upline


def test_render_run_tree_colors_distinct_per_kind() -> None:
    from tracesage.models import EventType, Run, RunStatus, StoredEvent
    from tracesage.render import _ANSI

    t0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def ev(eid, rid, et, tool=None, name=None):
        return StoredEvent(
            event_id=eid, run_id=rid, parent_run_id="root", root_run_id="root",
            event_type=et, timestamp=t0, agent_name=name, tool_name=tool, summary="x",
        )

    events = [
        StoredEvent(
            event_id="r", run_id="root", parent_run_id=None, root_run_id="root",
            event_type=EventType.CHAIN_START, timestamp=t0, agent_name="root", summary="x",
        ),
        ev("l", "l", EventType.LLM_START, name="llm"),
        ev("t", "t", EventType.TOOL_START, tool="tool"),
    ]
    run = Run(
        run_id="root", root_run_id="root", tags=[], status=RunStatus.COMPLETED,
        started_at=t0, total_steps=3,
    )
    colored = render_run_tree(run, events, use_color=True)
    # chain (blue), llm (cyan), tool (green) must each emit their colour code.
    for code in ("blue", "cyan", "green"):
        assert _ANSI[code] in colored
    # and no ANSI escapes leak when colour is disabled.
    assert "\033[" not in render_run_tree(run, events, use_color=False)


def test_traceview_repr_html() -> None:
    tv = TraceView("r1", "http://127.0.0.1:7842/ui/#run=r1")
    html = tv._repr_html_()
    assert "iframe" in html
    assert "run=r1" in html
    # No URL → graceful message, no iframe.
    assert "iframe" not in TraceView("r1", None)._repr_html_()


# ---------------------------------------------------------- embedded server E2E


def test_embedded_server_serves_ui_and_api(tmp_path: Path) -> None:
    from langchain_core.language_models.fake import FakeListLLM

    async def _go() -> None:
        async with TraceSage.session(_cfg(tmp_path, port=0), install=True) as tl:
            await FakeListLLM(responses=["hi"]).ainvoke("hello")
            await tl.flush()
            assert tl.bound_port
            base = f"http://127.0.0.1:{tl.bound_port}"
            async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
                # API: the run is queryable.
                r = await c.get("/api/runs")
                assert r.status_code == 200
                assert r.json()["runs"], "run should be visible over REST"
                # UI: the static shell loads and includes the new within-run search box.
                ui = await c.get("/ui/")
                assert ui.status_code == 200
                assert "timeline-search" in ui.text

    asyncio.run(_go())


def test_resolve_bind_port_scans_and_falls_back() -> None:
    """auto=True skips a busy port; auto=False keeps it; port 0 passes through."""
    import socket

    from tracesage.tracer import _resolve_bind_port

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        assert _resolve_bind_port("127.0.0.1", busy, auto=True) != busy  # picks a free one
        assert _resolve_bind_port("127.0.0.1", busy, auto=False) == busy  # verbatim
        assert _resolve_bind_port("127.0.0.1", 0, auto=True) == 0  # ephemeral passthrough
    finally:
        s.close()


@pytest.mark.asyncio
async def test_auto_port_binds_a_free_port_when_busy(tmp_path: Path) -> None:
    """With the configured port occupied, auto-port must bind a *different* free
    port and bring the UI up there (so a second app on the same machine works)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        tl = await TraceSage.create(_cfg(tmp_path, port=busy))  # default port_auto=True
        try:
            assert tl.bound_port is not None
            assert tl.bound_port != busy
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"http://127.0.0.1:{tl.bound_port}/api/health")
            assert r.status_code == 200
        finally:
            await tl.stop()
    finally:
        s.close()


def test_embedded_server_port_conflict_does_not_crash(tmp_path: Path) -> None:
    """With port_auto disabled and the configured port taken, the embedded UI must
    fail soft — uvicorn calls sys.exit(1) (SystemExit) on bind failure, which must
    NOT escape and tear down the host application. Tracing must continue."""
    import socket

    from langchain_core.language_models.fake import FakeListLLM

    # Occupy a port, then point a tracer at it with the server enabled.
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]

    async def _go() -> None:
        # port_auto=False pins the busy port; create() must still return cleanly.
        tl = await TraceSage.create(
            _cfg(tmp_path, port=busy_port, port_auto=False), start_server=True
        )
        try:
            assert tl.bound_port is None, "server should not report a port it never bound"
            # Tracing still works end-to-end.
            await FakeListLLM(responses=["hi"]).ainvoke("hello", config={"callbacks": [tl.handler]})
            await tl.flush()
            runs, _ = await tl.db.list_runs(limit=10, offset=0)
            assert runs, "tracing must still capture runs when the UI port is busy"
        finally:
            await tl.stop()

    try:
        asyncio.run(_go())
    finally:
        sock.close()


# ----------------------------------------------------------- sync BackgroundTracer


def test_background_tracer_sync_capture(tmp_path: Path) -> None:
    from langchain_core.language_models.fake import FakeListLLM

    tl = tracesage.start(_cfg(tmp_path, port=0), install=True)
    try:
        FakeListLLM(responses=["sync"]).invoke("hi")  # main-thread sync call
        tl.flush()
        runs = asyncio.run_coroutine_threadsafe(
            tl.tracer.db.list_runs(limit=10, offset=0), tl._loop
        ).result(timeout=5)[0]
        assert runs, "sync background tracer should capture the run"
        assert tl.run_url("x").endswith("/ui/#run=x")
    finally:
        tl.stop()


@pytest.mark.asyncio
async def test_richer_error_traceback_persisted(tmp_path: Path) -> None:
    import uuid

    async with TraceSage.session(_cfg(tmp_path, port=0), install=False) as tl:
        h = tl.handler
        rid = uuid.uuid4()
        h.on_chain_start({"name": "c"}, {"x": 1}, run_id=rid)
        try:
            raise ValueError("explode here")
        except ValueError as e:
            h.on_chain_error(e, run_id=rid)
        await tl.flush()
        journey = await tl.db.get_journey(str(rid))
        errs = [e for e in journey if e.event_type == EventType.CHAIN_ERROR]
        assert errs, "chain error should be captured"
        assert errs[0].blob_path, "error event should be blob-eligible"
        full = await tl.blob_store.read(errs[0].blob_path)
        assert "ValueError: explode here" in (full.get("traceback") or "")
