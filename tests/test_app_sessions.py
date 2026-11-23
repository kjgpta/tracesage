"""Local session stores of Claude Code, Cursor and Codex become full traces."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tracesage.ingest.agent_hooks import follow_locally, normalize
from tracesage.ingest.claude_code import ClaudeCodeSource, load_session
from tracesage.ingest.codex_sessions import CodexSource, load_rollout
from tracesage.ingest.cursor_store import load_composer
from tracesage.ingest.session_follow import SessionImporter, SessionRef
from tracesage.ingest.traces import ModelCall, ToolCall, Turn, turn_events


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _claude_session(root: Path) -> Path:
    session = root / "proj" / "sess-1.jsonl"
    _write_jsonl(session, [
        {"type": "user", "uuid": "p1", "sessionId": "sess-1", "cwd": "/w/laya",
         "timestamp": "2026-11-01T10:00:00Z",
         "message": {"role": "user", "content": "review the repo"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-01T10:00:02Z",
         "message": {"id": "m1", "model": "claude-opus-5", "role": "assistant",
                     "usage": {"input_tokens": 0, "output_tokens": 0},
                     "content": [{"type": "thinking", "thinking": "split the work"}]}},
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-11-01T10:00:03Z",
         "message": {"id": "m1", "model": "claude-opus-5", "role": "assistant",
                     "stop_reason": "tool_use",
                     "usage": {"input_tokens": 12, "output_tokens": 30,
                               "cache_read_input_tokens": 100},
                     "content": [{"type": "tool_use", "id": "toolu_A", "name": "Agent",
                                  "input": {"subagent_type": "Explore",
                                            "description": "map tests", "prompt": "find tests"}}]}},
        {"type": "user", "uuid": "r1", "timestamp": "2026-11-01T10:00:20Z",
         "toolUseResult": {"agentId": "abc"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_A", "content": "tests live in tests/"}]}},
        {"type": "assistant", "uuid": "a3", "timestamp": "2026-11-01T10:00:25Z",
         "message": {"id": "m2", "model": "claude-opus-5", "role": "assistant",
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 5, "output_tokens": 9},
                     "content": [{"type": "text", "text": "Tests are in tests/."}]}},
        {"type": "ai-title", "aiTitle": "Repo review"},
    ])
    sub = root / "proj" / "sess-1" / "subagents" / "agent-abc.jsonl"
    _write_jsonl(sub, [
        {"type": "user", "uuid": "s0", "isSidechain": True, "agentId": "abc",
         "timestamp": "2026-11-01T10:00:04Z",
         "message": {"role": "user", "content": "find tests"}},
        {"type": "assistant", "uuid": "s1", "isSidechain": True, "timestamp": "2026-11-01T10:00:06Z",
         "message": {"id": "sm1", "model": "claude-haiku-5", "role": "assistant",
                     "usage": {"input_tokens": 3, "output_tokens": 4},
                     "content": [{"type": "tool_use", "id": "toolu_G", "name": "Glob",
                                  "input": {"pattern": "tests/**"}}]}},
        {"type": "user", "uuid": "s2", "isSidechain": True, "timestamp": "2026-11-01T10:00:07Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_G", "content": "tests/test_a.py"}]}},
        {"type": "assistant", "uuid": "s3", "isSidechain": True, "timestamp": "2026-11-01T10:00:09Z",
         "message": {"id": "sm2", "model": "claude-haiku-5", "role": "assistant",
                     "usage": {"input_tokens": 2, "output_tokens": 5},
                     "content": [{"type": "text", "text": "tests live in tests/"}]}},
    ])
    sub.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "map tests", "toolUseId": "toolu_A"}),
        encoding="utf-8",
    )
    return session


def test_claude_code_transcript_has_model_calls_tools_and_subagents(tmp_path: Path) -> None:
    session = _claude_session(tmp_path)
    turns = load_session(session)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.complete
    assert turn.title == "Repo review"
    events = turn_events(turn)
    requests = [e for e in events if e.event_type.value == "chat_model_start"]
    assert [e.agent_name for e in requests] == ["claude-opus-5", "claude-haiku-5",
                                                "claude-haiku-5", "claude-opus-5"]
    first = next(e for e in events if e.event_type.value == "llm_end")
    assert (first.token_input, first.token_output, first.token_cache_read) == (12, 30, 100)
    agent_tool = next(e for e in events if e.tool_name == "Agent" and e.event_type.value == "tool_start")
    explore = next(e for e in events if e.event_type.value == "chain_start" and e.agent_name == "Explore")
    assert explore.parent_run_id == agent_tool.run_id
    glob = next(e for e in events if e.tool_name == "Glob" and e.event_type.value == "tool_start")
    assert glob.parent_run_id == explore.run_id
    assert glob.agent_name == "Explore"
    assert "project:laya" in glob.tags
    assert {e.thread_id for e in events} == {"sess-1"}


def test_claude_code_source_lists_recent_sessions(tmp_path: Path) -> None:
    _claude_session(tmp_path / ".claude" / "projects")
    refs = ClaudeCodeSource(home=tmp_path).sessions()
    assert len(refs) == 1
    assert refs[0].load()[0].turn_id == "p1"


def _cursor_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.execute(
        "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, "
        "createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
        "recency INTEGER, checkpointAt INTEGER, value TEXT, subagentTypeName TEXT)"
    )
    rows = {
        "composerData:c1": {"name": "Fix login", "modelConfig": {"modelName": "claude-4.5-sonnet"},
                            "status": "completed", "fullConversationHeadersOnly": [
                                {"bubbleId": b} for b in ("u1", "k1", "t1", "t2", "x1")]},
        "bubbleId:c1:u1": {"bubbleId": "u1", "type": 1, "text": "fix the login bug",
                           "createdAt": "2026-11-01T10:00:00Z"},
        "bubbleId:c1:k1": {"bubbleId": "k1", "type": 2, "thinking": {"text": "look at auth"},
                           "thinkingDurationMs": 1500, "createdAt": "2026-11-01T10:00:01Z"},
        "bubbleId:c1:t1": {"bubbleId": "t1", "type": 2, "createdAt": "2026-11-01T10:00:03Z",
                           "completedAtMs": 1793527205000,
                           "toolFormerData": {"name": "read_file_v2", "toolCallId": "call_1",
                                              "status": "completed",
                                              "params": json.dumps({"path": "auth.py"}),
                                              "result": json.dumps({"contents": "def login"})}},
        "bubbleId:c1:t2": {"bubbleId": "t2", "type": 2, "createdAt": "2026-11-01T10:00:08Z",
                           "completedAtMs": 1793527212000,
                           "toolFormerData": {"name": "task_v2", "toolCallId": "call_2",
                                              "status": "completed",
                                              "params": json.dumps({"subagent_type": "explore",
                                                                    "description": "find callers",
                                                                    "prompt": "who calls login"}),
                                              "result": json.dumps({"agentId": "c2"}),
                                              "additionalData": {"subagentComposerId": "c2"}}},
        "bubbleId:c1:x1": {"bubbleId": "x1", "type": 2, "text": "Fixed the bug.",
                           "createdAt": "2026-11-01T10:00:13Z"},
        "composerData:c2": {"name": "find callers", "modelConfig": {"modelName": "composer-2"}},
        "bubbleId:c2:v1": {"bubbleId": "v1", "type": 1, "text": "who calls login",
                           "createdAt": "2026-11-01T10:00:08.500Z"},
        "bubbleId:c2:v2": {"bubbleId": "v2", "type": 2, "createdAt": "2026-11-01T10:00:09Z",
                           "toolFormerData": {"name": "ripgrep_raw_search", "toolCallId": "call_3",
                                              "status": "completed",
                                              "params": json.dumps({"pattern": "login("})}},
    }
    for key, value in rows.items():
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
    con.execute("INSERT INTO composerHeaders (composerId, lastUpdatedAt, isSubagent, value) "
                "VALUES ('c1', 1, 0, '{}')")
    con.execute("INSERT INTO composerHeaders (composerId, lastUpdatedAt, isSubagent, value, "
                "subagentTypeName) VALUES ('c2', 1, 1, '{}', 'explore')")
    con.commit()
    con.close()


def test_cursor_chat_has_inferred_model_calls_tools_and_subagent(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    _cursor_db(db)
    turns = load_composer(db, "c1", cwd="/w/app")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.complete
    assert turn.title == "Fix login"
    assert turn.final_text == "Fixed the bug."
    kinds = ["llm" if isinstance(s, ModelCall) else s.name for s in turn.steps]
    assert kinds == ["llm", "read_file_v2", "llm", "task_v2", "llm"]
    first = turn.steps[0]
    assert isinstance(first, ModelCall)
    assert first.thinking == "look at auth"
    assert first.model == "claude-4.5-sonnet"
    task = turn.steps[3]
    assert isinstance(task, ToolCall)
    assert task.agent is not None
    assert task.agent.name == "explore"
    assert [s.name for s in task.agent.steps if isinstance(s, ToolCall)] == ["ripgrep_raw_search"]
    events = turn_events(turn)
    sub = next(e for e in events if e.event_type.value == "chain_start" and e.agent_name == "explore")
    assert "find callers" in sub.summary
    assert {e.thread_id for e in events} == {"c1"}


def test_cursor_turn_without_timestamps_uses_chat_time(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    _cursor_db(db)
    con = sqlite3.connect(db)
    rows = {
        "composerData:c3": {"name": "old chat", "createdAt": 1790139893023,
                            "fullConversationHeadersOnly": [
                                {"bubbleId": b} for b in ("a1", "a2", "b1", "b2")]},
        "bubbleId:c3:a1": {"bubbleId": "a1", "type": 1, "text": "first"},
        "bubbleId:c3:a2": {"bubbleId": "a2", "type": 2, "text": "one"},
        "bubbleId:c3:b1": {"bubbleId": "b1", "type": 1, "text": "second"},
        "bubbleId:c3:b2": {"bubbleId": "b2", "type": 2, "text": "two",
                           "createdAt": "2026-10-01T09:00:00Z"},
    }
    for key, value in rows.items():
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
    con.commit()
    con.close()
    first, second = load_composer(db, "c3")
    created = datetime.fromtimestamp(1790139893.023, UTC)
    assert first.started == first.ended == created
    assert second.started == datetime(2026, 10, 1, 9, tzinfo=UTC)
    start = next(e for e in turn_events(first) if e.event_type.value == "run_start")
    assert start.timestamp == created


def test_cursor_simulated_message_continues_the_turn(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    _cursor_db(db)
    con = sqlite3.connect(db)
    rows = {
        "composerData:c4": {"name": "bg"},
        "bubbleId:c4:a1": {"bubbleId": "a1", "type": 1, "text": "run the tests",
                           "createdAt": "2026-11-01T10:00:00Z"},
        "bubbleId:c4:a2": {"bubbleId": "a2", "type": 2, "text": "started in background",
                           "createdAt": "2026-11-01T10:00:01Z"},
        "bubbleId:c4:a3": {"bubbleId": "a3", "type": 1, "text": "task 7 finished: 3 passed",
                           "isSimulatedMsg": True, "createdAt": "2026-11-01T10:00:30Z"},
        "bubbleId:c4:a4": {"bubbleId": "a4", "type": 2, "text": "All 3 tests pass.",
                           "createdAt": "2026-11-01T10:00:31Z"},
    }
    for key, value in rows.items():
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
    con.commit()
    con.close()
    (turn,) = load_composer(db, "c4")
    calls = [s for s in turn.steps if isinstance(s, ModelCall)]
    assert len(calls) == 2
    assert calls[1].input_text == "task 7 finished: 3 passed"
    assert turn.final_text == "All 3 tests pass."


def _codex_tree(root: Path) -> Path:
    day = root / ".codex" / "sessions" / "2026" / "11" / "01"
    parent = day / "rollout-2026-11-01T10-00-00-p1.jsonl"
    child = day / "rollout-2026-11-01T10-00-05-k1.jsonl"
    _write_jsonl(parent, [
        {"timestamp": "2026-11-01T10:00:00Z", "type": "session_meta",
         "payload": {"id": "p1", "cwd": "/w/svc", "source": "cli"}},
        {"timestamp": "2026-11-01T10:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5.5"}},
        {"timestamp": "2026-11-01T10:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
        {"timestamp": "2026-11-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "why does CI fail"}]}},
        {"timestamp": "2026-11-01T10:00:03Z", "type": "response_item",
         "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "check logs"}]}},
        {"timestamp": "2026-11-01T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "c1",
                     "arguments": json.dumps({"agent_type": "explorer", "message": "read CI"})}},
        {"timestamp": "2026-11-01T10:00:04Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                   "output_tokens": 8}}}},
        {"timestamp": "2026-11-01T10:00:09Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": json.dumps({"agent_id": "k1", "nickname": "Hubble"})}},
        {"timestamp": "2026-11-01T10:00:10Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c2",
                     "arguments": json.dumps({"cmd": "pytest"})}},
        {"timestamp": "2026-11-01T10:00:12Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c2",
                     "output": json.dumps({"output": "1 failed", "metadata": {"exit_code": 1}})}},
        {"timestamp": "2026-11-01T10:00:14Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "A test fails."}]}},
        {"timestamp": "2026-11-01T10:00:15Z", "type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "A test fails."}},
    ])
    _write_jsonl(child, [
        {"timestamp": "2026-11-01T10:00:05Z", "type": "session_meta",
         "payload": {"id": "k1", "parent_thread_id": "p1",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p1",
                                                              "agent_role": "explorer",
                                                              "agent_nickname": "Hubble"}}}}},
        {"timestamp": "2026-11-01T10:00:05Z", "type": "turn_context", "payload": {"model": "gpt-5.5-mini"}},
        {"timestamp": "2026-11-01T10:00:06Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "k-c1",
                     "arguments": json.dumps({"cmd": "cat ci.yml"})}},
        {"timestamp": "2026-11-01T10:00:07Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "k-c1", "output": "jobs: test"}},
        {"timestamp": "2026-11-01T10:00:08Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "CI runs pytest"}]}},
    ])
    return parent


def test_codex_rollout_has_model_calls_tools_and_spawned_agent(tmp_path: Path) -> None:
    _codex_tree(tmp_path)
    refs = CodexSource(home=tmp_path).sessions()
    assert len(refs) == 1
    turns = refs[0].load()
    assert len(turns) == 1
    turn = turns[0]
    assert turn.complete
    assert turn.final_text == "A test fails."
    assert turn.model == "gpt-5.5"
    events = turn_events(turn)
    first = next(e for e in events if e.event_type.value == "llm_end")
    assert (first.token_input, first.token_cache_read, first.token_output) == (30, 20, 8)
    assert "check logs" in json.dumps(first.raw_payload)
    spawn = next(e for e in events if e.tool_name == "spawn_agent" and e.event_type.value == "tool_start")
    explorer = next(e for e in events if e.event_type.value == "chain_start" and e.agent_name == "explorer")
    assert explorer.parent_run_id == spawn.run_id
    child_llm = next(e for e in events if e.agent_name == "gpt-5.5-mini")
    assert child_llm.parent_run_id == explorer.run_id
    failed = next(e for e in events if e.event_type.value == "tool_error")
    assert failed.tool_name == "exec_command"


def test_codex_loader_skips_missing_file(tmp_path: Path) -> None:
    assert load_rollout(tmp_path / "missing.jsonl") == []


def test_importer_emits_only_what_changed(tmp_path: Path) -> None:
    turn = Turn(app="cursor", session="s", turn_id="t", prompt="hi")
    turn.steps.append(ModelCall(id="m1", model="m", output_text="partial"))
    ref = SessionRef(key="k", signature="1", load=lambda: [turn])
    importer = SessionImporter(tmp_path / "state.json")
    first: list = []
    importer.apply(ref, [turn], first.append)
    assert first
    assert not any(e.event_type.value == "chain_end" for e in first)
    turn.steps.append(ToolCall(id="x", name="Read", done=True))
    turn.complete = True
    second: list = []
    importer.apply(ref, [turn], second.append)
    assert {e.event_type.value for e in second} >= {"tool_start", "tool_end", "chain_end"}
    assert not any(e.event_type.value == "chat_model_start" for e in second)
    importer.save()
    reloaded = SessionImporter(tmp_path / "state.json")
    third: list = []
    reloaded.apply(ref, [turn], third.append)
    assert third == []


def test_hooks_are_skipped_for_apps_read_from_disk() -> None:
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"}
    try:
        follow_locally({"cursor"})
        assert normalize("cursor", payload) == []
        assert normalize("codex", payload)
    finally:
        follow_locally(set())
    assert normalize("cursor", payload)
