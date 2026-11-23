"""Sidebar chats saved by Claude Desktop become traces."""
from __future__ import annotations

import json
import struct

from tracesage.ingest.desktop_chats import (
    _conversations_in,
    _decode_v8,
    chat_turns,
    claude_support_dir,
    load_indexeddb_chats,
    poll_desktop_chats,
)
from tracesage.ingest.traces import turn_events

_UUID = "22222222-2222-2222-2222-222222222222"


def _chat() -> dict:
    return {
        "uuid": _UUID,
        "name": "Verizon migration questions",
        "model": "claude-sonnet",
        "messages": [
            {
                "uuid": "m1", "sender": "human", "index": 0,
                "content": [{"type": "text", "text": "where is the cutover"}],
            },
            {
                "uuid": "m2", "sender": "assistant", "index": 1,
                "created_at": "2026-11-01T10:00:00Z",
                "content": [
                    {"type": "thinking", "thinking": "check the plan",
                     "start_timestamp": "2026-11-01T10:00:01Z",
                     "stop_timestamp": "2026-11-01T10:00:02Z"},
                    {
                        "type": "tool_use", "id": "t1", "name": "search",
                        "integration_name": "docs", "input": {"q": "cutover"},
                        "start_timestamp": "2026-11-01T10:00:02Z",
                        "stop_timestamp": "2026-11-01T10:00:03Z",
                    },
                    {
                        "type": "tool_result", "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "friday"}],
                        "start_timestamp": "2026-11-01T10:00:03Z",
                        "stop_timestamp": "2026-11-01T10:00:05Z",
                    },
                    {"type": "text", "text": "Friday.",
                     "start_timestamp": "2026-11-01T10:00:06Z",
                     "stop_timestamp": "2026-11-01T10:00:07Z"},
                ],
            },
        ],
    }


def _events(chat: dict) -> list:
    return [event for turn in chat_turns(chat) for event in turn_events(turn)]


def test_cached_chat_is_one_thread_named_from_the_title() -> None:
    events = _events(_chat())
    assert {event.thread_id for event in events} == {_UUID}
    assert len({event.root_run_id for event in events}) == 1
    start = next(event for event in events if event.event_type.value == "chain_start")
    assert "Verizon migration questions" in start.summary
    assert "where is the cutover" in json.dumps(start.raw_payload)
    tool = next(event for event in events if event.tool_name)
    assert tool.tool_name == "search"
    assert tool.mcp_server == "docs"
    end = next(event for event in events if event.event_type.value == "chain_end")
    assert end.model == "claude-sonnet"
    assert "Friday." in json.dumps(end.raw_payload)


def test_cached_chat_splits_model_requests_at_tool_results() -> None:
    events = _events(_chat())
    requests = [e for e in events if e.event_type.value == "chat_model_start"]
    assert len(requests) == 2
    assert {e.agent_name for e in requests} == {"claude-sonnet"}
    tool_start = next(e for e in events if e.event_type.value == "tool_start")
    tool_end = next(e for e in events if e.event_type.value == "tool_end")
    assert (tool_end.timestamp - tool_start.timestamp).total_seconds() == 3


def test_cache_file_imports_once(tmp_path) -> None:
    try:
        from compression import zstd
    except ImportError:
        import zstandard

        def compress(payload: bytes) -> bytes:
            return zstandard.ZstdCompressor().compress(payload)
    else:
        compress = zstd.compress
    cache = claude_support_dir(tmp_path) / "Cache" / "Cache_Data"
    cache.mkdir(parents=True)
    body = compress(json.dumps(_chat() | {"chat_messages": _chat()["messages"]}).encode())
    key = (
        f"1/0/https://claude.ai/api/organizations/org/chat_conversations/{_UUID}?tree=True"
    ).encode()
    (cache / "entry_0").write_bytes(key + body)
    emitted: list = []
    state: dict[str, int] = {}
    poll_desktop_chats(tmp_path, state, emitted.append)
    assert emitted
    assert state[_UUID] == 2
    again: list = []
    poll_desktop_chats(tmp_path, state, again.append)
    assert again == []


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _text(value: str) -> bytes:
    raw = value.encode("latin1")
    return b'"' + _varint(len(raw)) + raw


def _object(pairs: list[tuple[str, bytes]]) -> bytes:
    body = b"".join(_text(key) + value for key, value in pairs)
    return b"o" + body + b"{" + _varint(len(pairs))


def _array(items: list[bytes]) -> bytes:
    return b"A" + _varint(len(items)) + b"".join(items) + b"$" + _varint(0) + _varint(len(items))


def test_indexeddb_tree_keeps_the_sidebar_title(tmp_path) -> None:
    message = _object([
        ("sender", _text("human")),
        ("content", _array([
            _object([("type", _text("text")), ("text", _text("hello there"))]),
        ])),
    ])
    blob = b"\xff\x10" + _object([
        ("label", _object([("uuid", _text(_UUID)), ("name", _text("Last run failure"))])),
        ("tree", _object([
            ("conversationUuid", _text(_UUID)),
            ("messages", _array([message])),
        ])),
    ])
    decoded = _decode_v8(blob)
    chats, names = _conversations_in(decoded)
    assert names[_UUID] == "Last run failure"
    assert len(chats) == 1
    assert chats[0]["messages"][0]["sender"] == "human"
    record = struct.pack("<IHB", 0, len(blob), 1) + blob
    folder = tmp_path / "indexeddb"
    folder.mkdir()
    (folder / "000003.log").write_bytes(record)
    loaded = load_indexeddb_chats(folder)
    assert loaded[0]["name"] == "Last run failure"
    assert loaded[0]["uuid"] == _UUID
