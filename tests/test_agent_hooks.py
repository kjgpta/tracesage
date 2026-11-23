"""Hook ingest from Claude Code, Codex and Cursor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tracesage.config import TraceSageConfig
from tracesage.ingest.agent_hooks import drain_spool, normalize
from tracesage.models import RawEvent, Stats
from tracesage.server import WebSocketManager, create_app
from tracesage.storage import SQLiteBackend
from tracesage.storage.blob_store import BlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_prompt_tool_and_stop_make_one_run_and_one_thread() -> None:
    events: list[RawEvent] = []
    for name in ("claude_prompt.json", "claude_tool.json", "claude_stop.json"):
        events.extend(normalize("claude-code", _load(name)))
    roots = {e.root_run_id for e in events}
    threads = {e.thread_id for e in events}
    assert len(roots) == 1
    assert threads == {"sess-1"}
    tool = next(e for e in events if e.tool_name)
    assert tool.tool_name == "search"
    assert tool.mcp_server == "flights"
    again = normalize("claude-code", _load("claude_prompt.json"))
    assert again[0].event_id == events[0].event_id


def test_failed_tool() -> None:
    events = normalize("claude-code", _load("claude_tool_fail.json"))
    assert events[0].event_type.value == "tool_error"
    assert events[0].error_message


def test_metadata_only_drops_text() -> None:
    events = normalize("claude-code", _load("claude_prompt.json"), metadata_only=True)
    start = next(e for e in events if e.event_type.value == "chain_start")
    assert "station" not in json.dumps(start.raw_payload)


def test_cursor_names_map() -> None:
    events = normalize("cursor", _load("cursor_prompt.json"))
    assert events
    assert events[0].tags[0] == "source:cursor"


def test_transcript_enrichment_and_missing(tmp_path) -> None:
    good = tmp_path / "ok.jsonl"
    good.write_text(
        json.dumps({
            "message": {
                "model": "claude-sonnet",
                "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 1},
            }
        }) + "\n",
        encoding="utf-8",
    )
    payload = _load("claude_stop.json")
    payload["transcript_path"] = str(good)
    events = normalize("claude-code", payload)
    end = next(e for e in events if e.event_type.value == "chain_end")
    assert end.model == "claude-sonnet"
    assert end.token_input == 3
    payload["transcript_path"] = str(tmp_path / "missing.jsonl")
    assert normalize("claude-code", payload)
    garbled = tmp_path / "bad.jsonl"
    garbled.write_text("not json\n", encoding="utf-8")
    payload["transcript_path"] = str(garbled)
    assert normalize("claude-code", payload)


def test_prompt_text_reaches_the_event_for_redaction() -> None:
    events = normalize("codex", _load("claude_prompt.json"))
    blob = json.dumps(events[1].raw_payload)
    assert "station" in blob


@pytest.fixture
def sink() -> list:
    return []


async def test_route_always_200_and_auth(tmp_path, sink) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    cfg = TraceSageConfig(data_dir=tmp_path, auth_token="sekret")
    app = create_app(
        db=db, blob_store=BlobStore(tmp_path / "blobs"), ws_manager=WebSocketManager(),
        config=cfg, stats=Stats(), emit=sink.append,
    )
    body = json.dumps(_load("claude_prompt.json"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post("/v1/hooks/claude-code", content=body)
            ok = await client.post(
                "/v1/hooks/claude-code",
                content=body,
                headers={"authorization": "Bearer sekret"},
            )
            bad = await client.post(
                "/v1/hooks/claude-code",
                content=b"not-json",
                headers={"authorization": "Bearer sekret"},
            )
        assert denied.status_code == 401
        assert ok.status_code == 200
        assert ok.json() == {}
        assert bad.status_code == 200
        assert bad.json() == {}
        assert sink
    finally:
        await db.close()


def test_spool_drain(tmp_path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "claude-code.jsonl").write_text(
        json.dumps(_load("claude_prompt.json")) + "\n", encoding="utf-8"
    )
    got: list = []
    n = drain_spool(spool, got.append)
    assert n >= 1
    assert list(spool.glob("*.jsonl")) == []
