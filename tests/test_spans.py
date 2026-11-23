"""Waterfall span pairing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from tracesage.config import TraceSageConfig
from tracesage.models import EventType, Run, RunStatus, Stats, StoredEvent
from tracesage.server import WebSocketManager, create_app
from tracesage.spans import build_spans
from tracesage.storage import SQLiteBackend
from tracesage.storage.blob_store import BlobStore


def _ev(eid, rid, et, ms, *, parent=None, root="root", name=None, tool=None) -> StoredEvent:
    return StoredEvent(
        event_id=eid, run_id=rid, parent_run_id=parent, root_run_id=root,
        event_type=et, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms),
        agent_name=name, tool_name=tool, summary=eid,
    )


def test_nesting_and_durations() -> None:
    events = [
        _ev("a0", "root", EventType.CHAIN_START, 0, name="agent"),
        _ev("b0", "llm", EventType.LLM_START, 10, parent="root", name="gpt"),
        _ev("b1", "llm", EventType.LLM_END, 110, parent="root", name="gpt"),
        _ev("c0", "tool", EventType.TOOL_START, 120, parent="root", tool="search"),
        _ev("c1", "tool", EventType.TOOL_ERROR, 140, parent="root", tool="search"),
        _ev("a1", "root", EventType.CHAIN_END, 200, name="agent"),
    ]
    rows = {row.run_id: row for row in build_spans(events)}
    assert rows["llm"].duration_ms == 100
    assert rows["llm"].depth == 1
    assert rows["tool"].status == "error"
    assert rows["root"].status == "ok"
    assert "llm" in rows["root"].children


def test_interrupt_and_running() -> None:
    events = [
        _ev("a0", "root", EventType.CHAIN_START, 0, name="agent"),
        _ev("a1", "root", EventType.CHAIN_INTERRUPT, 50, name="agent"),
        _ev("b0", "open", EventType.TOOL_START, 10, parent="root", tool="ask"),
    ]
    rows = {row.run_id: row for row in build_spans(events, running=True)}
    assert rows["root"].status == "interrupted"
    assert rows["open"].status == "running"
    assert rows["open"].end_ms >= rows["open"].start_ms


def test_out_of_order_otlp_start() -> None:
    events = [
        _ev("e", "span", EventType.LLM_END, 5, name="m"),
        _ev("s", "span", EventType.LLM_START, 40, name="m"),
    ]
    row = build_spans(events)[0]
    assert row.duration_ms == 35
    assert row.status == "ok"


async def test_spans_rest(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    now = datetime.now(timezone.utc)
    await db.upsert_run(Run(run_id="root", root_run_id="root", status=RunStatus.COMPLETED, started_at=now))
    await db.upsert_event(_ev("a0", "root", EventType.CHAIN_START, 0, name="agent"))
    await db.upsert_event(_ev("a1", "root", EventType.CHAIN_END, 20, name="agent"))
    app = create_app(
        db=db, blob_store=BlobStore(tmp_path / "blobs"), ws_manager=WebSocketManager(),
        config=TraceSageConfig(data_dir=tmp_path), stats=Stats(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/runs/root/spans")
        assert resp.status_code == 200
        assert resp.json()[0]["name"] == "agent"
    finally:
        await db.close()
