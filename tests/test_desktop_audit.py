"""Claude Desktop Cowork audit logs become traces with model calls and subagents."""
from __future__ import annotations

import json
from pathlib import Path

from tracesage.ingest.anthropic_log import build_turns
from tracesage.ingest.desktop_audit import CoworkSource, load_audit, split_streams
from tracesage.ingest.session_follow import SessionImporter
from tracesage.ingest.traces import turn_events


def _turn() -> list[dict]:
    return [
        {
            "type": "user", "uuid": "u1", "session_id": "sess",
            "cwd": "/work/invoices", "_audit_timestamp": "2026-06-19T14:16:28.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "find open invoices"}]},
        },
        {
            "type": "user", "uuid": "u1", "isReplay": True, "session_id": "sess",
            "message": {"role": "user", "content": "find open invoices"},
        },
        {
            "type": "assistant", "uuid": "a1", "session_id": "turn-id",
            "_audit_timestamp": "2026-06-19T14:16:30.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus", "id": "msg1",
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "content": [
                    {"type": "thinking", "thinking": "look it up"},
                    {
                        "type": "tool_use", "id": "t1",
                        "name": "mcp__vectorbridge__search_invoices",
                        "input": {"status": "open"},
                    },
                    {
                        "type": "tool_use", "id": "t2", "name": "Agent",
                        "input": {"subagent_type": "Explore", "description": "check ledger",
                                  "prompt": "look at the ledger"},
                    },
                ],
            },
        },
        {
            "type": "system", "subtype": "task_started", "tool_use_id": "t2",
            "subagent_type": "Explore", "description": "check ledger",
        },
        {
            "type": "assistant", "uuid": "s1", "parent_tool_use_id": "t2",
            "_audit_timestamp": "2026-06-19T14:16:31.000Z",
            "message": {
                "role": "assistant", "model": "claude-haiku", "id": "sub-msg",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "content": [{"type": "text", "text": "ledger is clean"}],
            },
        },
        {
            "type": "user", "uuid": "u2", "session_id": "sess",
            "_audit_timestamp": "2026-06-19T14:16:32.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "3 invoices"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "ledger is clean"},
                ],
            },
        },
        {
            "type": "assistant", "uuid": "a2", "session_id": "sess",
            "_audit_timestamp": "2026-06-19T14:16:34.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus", "id": "msg2",
                "usage": {"input_tokens": 9, "output_tokens": 4},
                "content": [{"type": "text", "text": "Found three."}],
            },
        },
        {
            "type": "result", "uuid": "r1", "session_id": "turn-id",
            "_audit_timestamp": "2026-06-19T14:16:35.000Z",
            "modelUsage": {"claude-opus": {}},
        },
    ]


def _events() -> list:
    main, subs = split_streams(_turn())
    turns = build_turns(main, app="claude-desktop", session=lambda r: r.get("session_id") or "x",
                        subs=subs)
    assert len(turns) == 1
    return turn_events(turns[0])


def test_one_desktop_turn_is_one_thread_with_model_calls() -> None:
    events = _events()
    assert {event.thread_id for event in events} == {"sess"}
    assert len({event.root_run_id for event in events}) == 1
    starts = [e for e in events if e.event_type.value == "chat_model_start"]
    assert {e.agent_name for e in starts} == {"claude-opus", "claude-haiku"}
    ends = [e for e in events if e.event_type.value == "llm_end"]
    assert sum(e.token_output or 0 for e in ends) == 8
    assert "look it up" in json.dumps([e.raw_payload for e in ends])
    tool = next(e for e in events if e.tool_name == "search_invoices")
    assert tool.mcp_server == "vectorbridge"
    assert "project:invoices" in tool.tags
    end = next(e for e in events if e.event_type.value == "chain_end" and e.parent_run_id is None)
    assert end.model == "claude-opus"
    assert end.token_output is None
    assert "Found three." in json.dumps(end.raw_payload)


def test_subagent_is_nested_under_the_tool_that_spawned_it() -> None:
    events = _events()
    agent_tool = next(e for e in events if e.tool_name == "Agent" and e.event_type.value == "tool_start")
    sub = next(e for e in events if e.event_type.value == "chain_start" and e.agent_name == "Explore")
    assert sub.parent_run_id == agent_tool.run_id
    assert "check ledger" in sub.summary
    sub_llm = next(e for e in events if e.agent_name == "claude-haiku")
    assert sub_llm.parent_run_id == sub.run_id


def test_source_imports_a_log_once_and_skips_a_large_one(tmp_path: Path) -> None:
    root = tmp_path / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    small = root / "org" / "local_small"
    small.mkdir(parents=True)
    (small / "audit.jsonl").write_text(
        "\n".join(json.dumps(line) for line in _turn()) + "\n", encoding="utf-8",
    )
    big = root / "org" / "big"
    big.mkdir()
    (big / "audit.jsonl").write_bytes(b'{"type":"user","message":{"content":"secret"}}\n' + b"x" * 4000)

    source = CoworkSource(home=tmp_path, max_bytes=3000)
    source.root = root
    refs = source.sessions()
    assert len(refs) == 1
    importer = SessionImporter(tmp_path / "state.json")
    got: list = []
    importer.apply(refs[0], refs[0].load(), got.append)
    assert {event.thread_id for event in got} == {"sess"}
    again: list = []
    importer.apply(refs[0], refs[0].load(), again.append)
    assert again == []
    assert load_audit(big / "audit.jsonl", max_bytes=3000) == []
