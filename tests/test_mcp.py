"""Tests for MCP tool-source attribution (registry + adapter + helper)."""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from tracesage.adapters.langchain import TraceSageCallbackHandler
from tracesage.adapters.mcp import register_mcp_client, register_mcp_tools
from tracesage.config import TraceSageConfig
from tracesage.models import EventType, RawEvent


class _StubTracer:
    """Duck-typed tracer with the MCP registry surface, recording emit() calls."""

    def __init__(self) -> None:
        self._config = TraceSageConfig()
        self.events: list[RawEvent] = []
        self._root_map: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._tool_sources: dict[str, str] = {}
        # Optional DB stub; tests that assert persistence attach a recorder.
        self.db: object | None = None

    def emit(self, event: RawEvent) -> None:
        with self._lock:
            self.events.append(event)

    def get_or_set_root(self, run_id: str, parent_run_id: str | None) -> str:
        if parent_run_id is None:
            self._root_map.setdefault(run_id, run_id)
            return self._root_map[run_id]
        root = self._root_map.get(parent_run_id, parent_run_id)
        self._root_map[run_id] = root
        return root

    def register_tool_source(self, name: str, server: str) -> None:
        if name and server:
            self._tool_sources[name] = server

    def register_tool_sources(self, mapping: dict[str, str]) -> None:
        for n, s in (mapping or {}).items():
            self.register_tool_source(n, s)

    def tool_source(self, name: str | None) -> str | None:
        return self._tool_sources.get(name) if name else None


def _fake_tool(name: str) -> object:
    return type("FakeTool", (), {"name": name})()


def _starts(tracer: _StubTracer) -> list[RawEvent]:
    return [e for e in tracer.events if e.event_type == EventType.TOOL_START]


def test_register_tool_source_and_lookup() -> None:
    t = _StubTracer()
    t.register_tool_sources({"get_weather": "weather", "add": "math"})
    assert t.tool_source("get_weather") == "weather"
    assert t.tool_source("add") == "math"
    assert t.tool_source("unknown") is None
    assert t.tool_source(None) is None


def test_register_mcp_tools_attributes_list() -> None:
    t = _StubTracer()
    tools = [_fake_tool("a"), _fake_tool("b"), type("NoName", (), {})()]
    out = register_mcp_tools(t, tools, "srv")
    assert len(out) == 3  # returns all (incl. the nameless one), but only named registered
    assert t.tool_source("a") == "srv"
    assert t.tool_source("b") == "srv"


def test_handler_stamps_mcp_server_from_registry() -> None:
    t = _StubTracer()
    t.register_tool_source("get_weather", "weather")
    h = TraceSageCallbackHandler(t)
    rid = uuid.uuid4()
    h.on_tool_start({"name": "get_weather"}, "London", run_id=rid)
    h.on_tool_end("sunny", run_id=rid)
    starts = _starts(t)
    ends = [e for e in t.events if e.event_type == EventType.TOOL_END]
    assert starts
    assert starts[0].mcp_server == "weather"
    assert ends
    assert ends[0].mcp_server == "weather"


def test_handler_auto_detects_server_from_metadata() -> None:
    t = _StubTracer()
    h = TraceSageCallbackHandler(t)
    rid = uuid.uuid4()
    h.on_tool_start({"name": "x"}, "in", run_id=rid, metadata={"mcp_server_name": "github"})
    starts = _starts(t)
    assert starts
    assert starts[0].mcp_server == "github"


def test_local_tool_has_no_source() -> None:
    t = _StubTracer()
    h = TraceSageCallbackHandler(t)
    rid = uuid.uuid4()
    h.on_tool_start({"name": "local_calc"}, "in", run_id=rid)
    starts = _starts(t)
    assert starts
    assert starts[0].mcp_server is None


class _FakeMcpClient:
    """Stands in for langchain-mcp-adapters MultiServerMCPClient."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.connections = {server: {} for server in mapping}
        self._mapping = mapping

    async def get_tools(self, server_name: str | None = None) -> list[object]:
        if server_name is None:
            raise TypeError("server_name required")
        return [_fake_tool(n) for n in self._mapping[server_name]]


async def test_register_mcp_client_attributes_per_server() -> None:
    # Requires the optional extra (langchain-mcp-adapters) for the lazy import guard.
    import pytest

    pytest.importorskip("langchain_mcp_adapters")
    t = _StubTracer()
    client = _FakeMcpClient({"weather": ["get_weather", "get_forecast"], "math": ["add"]})
    tools = await register_mcp_client(t, client)
    assert len(tools) == 3
    assert t.tool_source("get_weather") == "weather"
    assert t.tool_source("get_forecast") == "weather"
    assert t.tool_source("add") == "math"


class _RecordingDb:
    """Records upsert_mcp_tools(server, names) calls so the register->DB wiring
    (which persists uncalled tools for the topology) is actually asserted."""

    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {}

    async def upsert_mcp_tools(self, server: str, names: list[str]) -> None:
        self.calls[server] = list(names)


async def test_register_mcp_client_persists_full_tool_list() -> None:
    """The register->db.upsert_mcp_tools wiring must fire with EVERY tool a server
    provides (including ones the run never invokes), not just the called ones."""
    import pytest

    pytest.importorskip("langchain_mcp_adapters")
    t = _StubTracer()
    t.db = _RecordingDb()
    client = _FakeMcpClient(
        {"weather": ["get_weather", "get_forecast", "air_quality"], "math": ["add"]}
    )
    await register_mcp_client(t, client)
    assert t.db.calls["weather"] == ["get_weather", "get_forecast", "air_quality"]
    assert t.db.calls["math"] == ["add"]


async def test_load_server_tools_session_fallback(monkeypatch) -> None:
    """Older langchain-mcp-adapters lack get_tools(server_name=...); _load_server_tools
    must fall back to opening a per-server session() + load_mcp_tools."""
    import pytest

    pytest.importorskip("langchain_mcp_adapters")
    import langchain_mcp_adapters.tools as lmt

    from tracesage.adapters.mcp import _load_server_tools

    captured = {"session_opened": False}

    async def _fake_load(session: object) -> list[object]:
        return [_fake_tool("alpha"), _fake_tool("beta")]

    monkeypatch.setattr(lmt, "load_mcp_tools", _fake_load)

    class _Ctx:
        async def __aenter__(self) -> object:
            captured["session_opened"] = True
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _FallbackClient:
        def __init__(self) -> None:
            self.connections = {"srv": {}}

        async def get_tools(self, server_name: str | None = None) -> list[object]:
            raise TypeError("got an unexpected keyword argument 'server_name'")

        def session(self, server: str) -> _Ctx:
            return _Ctx()

    tools = await _load_server_tools(_FallbackClient(), "srv")
    assert captured["session_opened"] is True
    assert {getattr(t, "name", None) for t in tools} == {"alpha", "beta"}


async def test_register_mcp_client_real_stdio_server() -> None:
    """End-to-end against a REAL stdio MCP server (the example weather server),
    a real MultiServerMCPClient, and the real register_mcp_client path."""
    import sys
    from pathlib import Path

    import pytest

    pytest.importorskip("langchain_mcp_adapters")
    pytest.importorskip("mcp")
    from langchain_mcp_adapters.client import MultiServerMCPClient

    server = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "mcp"
        / "weather_server.py"
    )
    if not server.exists():
        pytest.skip("example MCP server not found")

    t = _StubTracer()
    client = MultiServerMCPClient(
        {"weather": {"command": sys.executable, "args": [str(server)], "transport": "stdio"}}
    )
    tools = await register_mcp_client(t, client)
    names = {getattr(x, "name", None) for x in tools}
    assert {"get_weather", "get_forecast", "severe_alerts"} <= names
    assert t.tool_source("get_weather") == "weather"
    assert t.tool_source("severe_alerts") == "weather"
