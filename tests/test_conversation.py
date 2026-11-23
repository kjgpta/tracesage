"""Conversation turns from several payload shapes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from tracesage.config import TraceSageConfig
from tracesage.conversation import build_turns, extract_assistant_text, extract_user_text
from tracesage.models import EventType, Run, RunStatus, Stats, StoredEvent
from tracesage.server import WebSocketManager, create_app
from tracesage.storage import SQLiteBackend
from tracesage.storage.blob_store import BlobStore


def test_extractors() -> None:
    assert extract_user_text("plain question") == "plain question"
    assert extract_user_text({"input": "where to?"}) == "where to?"
    assert extract_user_text({"question": "why"}) == "why"
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert extract_user_text({"messages": messages}) == "hi"
    assert extract_assistant_text({"messages": messages}) == "hello"
    assert extract_assistant_text({"output": "done"}) == "done"
    assert extract_assistant_text([{"role": "assistant", "content": "ok"}]) == "ok"
    wrapped = "<timestamp>Fri</timestamp>\n<user_query>\nwhy did it fail?\n</user_query>"
    assert extract_user_text({"input": wrapped}) == "why did it fail?"
    assert extract_assistant_text({"output": "Use **this**.\n\nThen stop."}) == "Use **this**.\n\nThen stop."


def test_turns_order_failure_and_interrupt() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runs = [
        Run(run_id="a", root_run_id="a", status=RunStatus.COMPLETED, started_at=t0,
            thread_id="thr", total_tokens_input=1, total_tokens_output=2),
        Run(run_id="b", root_run_id="b", status=RunStatus.FAILED, started_at=t0 + timedelta(seconds=1),
            thread_id="thr", error_message="boom"),
        Run(run_id="c", root_run_id="c", status=RunStatus.INTERRUPTED, started_at=t0 + timedelta(seconds=2),
            thread_id="thr"),
    ]
    events = [
        StoredEvent(event_id="a1", run_id="a", root_run_id="a", event_type=EventType.CHAIN_START,
                    timestamp=t0, summary="s"),
        StoredEvent(event_id="a2", run_id="tool-1", parent_run_id="a", root_run_id="a",
                    event_type=EventType.TOOL_START, timestamp=t0, tool_name="search",
                    mcp_server="flights", summary="s"),
        StoredEvent(event_id="a2e", run_id="tool-1", parent_run_id="a", root_run_id="a",
                    event_type=EventType.TOOL_END, timestamp=t0, tool_name="search",
                    mcp_server="flights", summary="s", duration_ms=4),
        StoredEvent(event_id="a3", run_id="a", root_run_id="a", event_type=EventType.CHAIN_END,
                    timestamp=t0, summary="s", duration_ms=10),
    ]
    payloads = {
        "a1": {"input": "find a flight"},
        "a2": {"input": {"query": "SFO to BOS"}},
        "a2e": {"output": "one nonstop"},
        "a3": {"output": "booked"},
    }
    turns = build_turns(runs, events, payloads)
    assert [t.run_id for t in turns] == ["a", "b", "c"]
    assert turns[0].user_text == "find a flight"
    assert turns[0].assistant_text == "booked"
    assert turns[0].tools[0].mcp_server == "flights"
    assert turns[0].steps[0].summary == "SFO to BOS"
    assert turns[0].steps[0].output == "one nonstop"
    assert turns[0].steps[0].duration_ms == 4
    events.append(StoredEvent(
        event_id="a4", run_id="llm-1", parent_run_id="a", root_run_id="a",
        event_type=EventType.LLM_END, timestamp=t0, model="claude", summary="s",
    ))
    payloads["a4"] = {"input": "system: be brief", "output": "checking flights"}
    again = build_turns(runs, events, payloads)
    model = again[0].steps[-1]
    assert model.kind == "llm"
    assert model.name == "claude"
    assert model.input == "system: be brief"
    assert model.output == "checking flights"
    assert turns[1].status == "failed"
    assert turns[1].error == "boom"
    assert turns[2].interrupted is True


async def test_thread_rest_and_blob_guard(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    blob = BlobStore(tmp_path / "blobs")
    await db.init()
    t0 = datetime.now(timezone.utc)
    await db.upsert_run(Run(
        run_id="a", root_run_id="a", status=RunStatus.COMPLETED, started_at=t0, thread_id="thr"
    ))
    path = await blob.write("a", "a1", {"input": "hello"})
    await db.upsert_event(StoredEvent(
        event_id="a1", run_id="a", root_run_id="a", event_type=EventType.CHAIN_START,
        timestamp=t0, summary="s", blob_path=path,
    ))
    app = create_app(
        db=db, blob_store=blob, ws_manager=WebSocketManager(),
        config=TraceSageConfig(data_dir=tmp_path), stats=Stats(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get("/api/threads")
            detail = await client.get("/api/threads/thr")
            missing = await client.get("/api/threads/nope")
            one = await client.get("/api/threads/thr", params={"run": "a"})
            other = await client.get("/api/threads/thr", params={"run": "b"})
        assert one.json()["turns"][0]["run_id"] == "a"
        assert other.status_code == 404
        assert listed.status_code == 200
        assert listed.json()["threads"][0]["thread_id"] == "thr"
        assert detail.json()["turns"][0]["user_text"] == "hello"
        assert missing.status_code == 404
        # A blob path that escapes the store is refused.
        try:
            await blob.read("../secrets.txt")
        except ValueError:
            pass
        else:
            raise AssertionError("blob read should reject a parent path")
    finally:
        await db.close()
