"""MCP server tools, in-process, against a seeded data dir."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tracesage.mcp_server import build_server, mcp_installed
from tracesage.models import EventType, Run, RunStatus, StoredEvent
from tracesage.storage import SQLiteBackend


def _text(result) -> str:
    if isinstance(result, dict):
        return json.dumps(result)
    parts = []
    for block in result:
        parts.append(getattr(block, "text", None) or str(block))
    return "\n".join(parts)


async def _seed(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "traces.db")
    await db.init()
    now = datetime.now(timezone.utc)
    await db.upsert_run(Run(
        run_id="r1", root_run_id="r1", status=RunStatus.FAILED, started_at=now,
        thread_id="thr", error_message="boom", tags=["demo"],
    ))
    await db.upsert_event(StoredEvent(
        event_id="e1", run_id="r1", root_run_id="r1", event_type=EventType.CHAIN_START,
        timestamp=now, agent_name="agent", summary="a very long prompt " * 50,
        search_text="needle in a haystack",
    ))
    await db.close()


@pytest.mark.skipif(not mcp_installed(), reason="mcp extra not installed")
async def test_tools_truncation_readonly_and_delete(tmp_path) -> None:
    await _seed(tmp_path)
    server = build_server(tmp_path, read_only=False, allow_delete=False)
    runs = _text(await server.call_tool("list_runs", {}))
    assert "r1" in runs
    got = _text(await server.call_tool("get_run", {"run_id": "r1"}))
    assert "boom" in got
    spans = _text(await server.call_tool("get_run_spans", {"run_id": "r1"}))
    assert "agent" in spans
    step = _text(await server.call_tool("get_step", {"event_id": "e1", "max_chars": 80}))
    assert "truncated" in step
    found = _text(await server.call_tool("search", {"q": "needle"}))
    assert "e1" in found
    errors = _text(await server.call_tool("list_errors", {}))
    assert "r1" in errors
    threads = _text(await server.call_tool("list_threads", {}))
    assert "thr" in threads
    thread = _text(await server.call_tool("get_thread", {"thread_id": "thr"}))
    assert "r1" in thread
    stats = _text(await server.call_tool("get_stats", {}))
    assert "failed" in stats
    topo = _text(await server.call_tool("get_topology", {"scope": "run:r1"}))
    assert "agent" in topo
    compared = _text(await server.call_tool("compare_runs", {"run_a": "r1", "run_b": "r1"}))
    assert "status" in compared
    noted = _text(await server.call_tool("add_note", {"run_id": "r1", "text": "look here"}))
    assert "look here" in noted
    tagged = _text(await server.call_tool("set_tags", {"run_id": "r1", "add": ["bug"]}))
    assert "bug" in tagged
    denied = _text(await server.call_tool("delete_run", {"run_id": "r1", "confirm": True}))
    assert "allow-delete" in denied

    readonly = build_server(tmp_path, read_only=True, allow_delete=True)
    refused = _text(await readonly.call_tool("add_note", {"run_id": "r1", "text": "nope"}))
    assert "read-only" in refused

    deleting = build_server(tmp_path, read_only=False, allow_delete=True)
    unconfirmed = _text(await deleting.call_tool("delete_run", {"run_id": "r1"}))
    assert "confirm" in unconfirmed
    gone = _text(await deleting.call_tool("delete_run", {"run_id": "r1", "confirm": True}))
    assert "deleted" in gone


def test_missing_mcp_hint(monkeypatch) -> None:
    monkeypatch.setattr("tracesage.mcp_server.mcp_installed", lambda: False)
    from tracesage.mcp_server import mcp_installed as check

    assert check() is False
