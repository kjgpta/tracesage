"""Import Codex sessions from their rollout logs.

Codex (the CLI, the IDE extension and Codex inside the ChatGPT desktop app)
writes ``~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl``. Each line
is ``{timestamp, type, payload}``:

* ``session_meta``: session ``id``, ``cwd``, and for a subagent
  ``parent_thread_id`` / ``source.subagent.thread_spawn``.
* ``turn_context``: the model for the turn.
* ``response_item``: ``message``, ``reasoning``, ``function_call``,
  ``custom_tool_call``, ``local_shell_call``, ``web_search_call`` and the
  matching ``*_output`` items, paired by ``call_id``.
* ``event_msg``: ``task_started`` / ``task_complete`` bound a turn, and
  ``token_count`` carries the usage of the last model request.

A subagent is a separate rollout. The parent's ``spawn_agent`` call returns
its id, and its ``session_meta`` names the parent.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tracesage.ingest.claude_code import read_jsonl
from tracesage.ingest.session_follow import SessionRef, files_signature, import_cutoff
from tracesage.ingest.traces import (
    Agent,
    ModelCall,
    ToolCall,
    Turn,
    as_int,
    bounded,
    clip,
    parse_ts,
)

log = logging.getLogger("tracesage.ingest.codex")

APP = "codex"
_MAX_FILE = 64_000_000
_LARGE_FILE = 8_000_000
_IDLE_COMPLETE = timedelta(minutes=30)
_INPUT_LIMIT = 2000
_SCAFFOLD = ("<environment_context>", "<user_instructions>", "<permissions", "# AGENTS.md",
             "<turn_aborted>", "<subagent_notification>")
_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call", "web_search_call",
                    "tool_search_call"})
_OUTPUTS = frozenset({"function_call_output", "custom_tool_call_output",
                      "local_shell_call_output", "tool_search_output"})


def sessions_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".codex" / "sessions"


def session_meta(path: Path) -> dict[str, Any]:
    """The ``session_meta`` payload from the first lines of a rollout."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "session_meta":
                    payload = obj.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def parent_of(meta: dict[str, Any]) -> str | None:
    parent = meta.get("parent_thread_id")
    if isinstance(parent, str) and parent:
        return parent
    source = meta.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
                return spawn["parent_thread_id"]
    return None


def agent_role(meta: dict[str, Any]) -> tuple[str, str]:
    role = meta.get("agent_role")
    nickname = meta.get("agent_nickname")
    source = meta.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                role = role or spawn.get("agent_role")
                nickname = nickname or spawn.get("agent_nickname")
            other = sub.get("other")
            role = role or other
        elif isinstance(sub, str):
            role = role or sub
    return (str(role) if role else "subagent"), (str(nickname) if nickname else "")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


def _output_text(payload: dict[str, Any]) -> tuple[str, bool]:
    output = payload.get("output")
    failed = False
    if isinstance(output, dict):
        meta = output.get("metadata")
        if isinstance(meta, dict) and as_int(meta.get("exit_code")) not in (None, 0):
            failed = True
        output = output.get("content", output)
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed.get("metadata")
            if isinstance(meta, dict) and as_int(meta.get("exit_code")) not in (None, 0):
                failed = True
            if isinstance(parsed.get("output"), str):
                return parsed["output"], failed
        return output, failed
    return clip(output), failed


class _Stream:
    """Fold one session's response items into model and tool steps."""

    def __init__(self, children: dict[str, Path], depth: int) -> None:
        self.children = children
        self.depth = depth
        self.steps: list[ModelCall | ToolCall] = []
        self.call: ModelCall | None = None
        self.tools: dict[str, ToolCall] = {}
        self.model: str | None = None
        self.pending = ""
        self.prev: datetime | None = None
        self.last: datetime | None = None
        self.final = ""
        self.count = 0

    def start(self, prompt: str, when: datetime | None) -> None:
        self.pending = prompt[:_INPUT_LIMIT]
        self.prev = when
        self.call = None

    def _ensure_call(self, when: datetime | None) -> ModelCall:
        if self.call is None:
            self.count += 1
            self.call = ModelCall(id=f"codex:{self.count}:{when}", model=self.model,
                                  started=self.prev or when, input_text=self.pending)
            self.pending = ""
            self.steps.append(self.call)
        if when is not None:
            self.call.ended = when
        return self.call

    def item(self, payload: dict[str, Any], when: datetime | None) -> None:
        kind = payload.get("type")
        if when is not None:
            self.last = when
        if kind == "reasoning":
            call = self._ensure_call(when)
            summary = payload.get("summary")
            text = _content_text(summary) or _content_text(payload.get("content"))
            if text.strip():
                call.thinking = (call.thinking + "\n" + text).strip()
        elif kind == "message" and payload.get("role") == "assistant":
            call = self._ensure_call(when)
            text = _content_text(payload.get("content"))
            if text.strip():
                call.output_text = (call.output_text + "\n" + text).strip()
                self.final = text
        elif kind in _CALLS:
            self._call(payload, when)
        elif kind in _OUTPUTS:
            self._output(payload, when)

    def _call(self, payload: dict[str, Any], when: datetime | None) -> None:
        call = self._ensure_call(when)
        name = payload.get("name")
        kind = str(payload.get("type"))
        if not isinstance(name, str) or not name:
            name = {"local_shell_call": "shell", "web_search_call": "web_search"}.get(kind, kind)
        raw_args = payload.get("arguments", payload.get("input", payload.get("action")))
        args: Any = raw_args
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                args = raw_args
        call_id = payload.get("call_id") or payload.get("id") or f"{call.id}:{len(self.tools)}"
        server = None
        if "." in name and payload.get("namespace"):
            server = str(payload.get("namespace"))
        tool = ToolCall(id=str(call_id), name=name[:120], server=server, input=bounded(args),
                        started=when)
        if kind == "web_search_call":
            tool.done = True
            tool.ended = when
        call.tool_names.append(tool.name)
        self.tools[tool.id] = tool
        self.steps.append(tool)

    def _output(self, payload: dict[str, Any], when: datetime | None) -> None:
        call_id = payload.get("call_id")
        tool = self.tools.get(str(call_id)) if call_id else None
        text, failed = _output_text(payload)
        self.call = None
        self.prev = when
        self.pending = (self.pending + "\n\n" + text[:600]).strip()[:_INPUT_LIMIT]
        if tool is None:
            return
        tool.done = True
        tool.ended = when
        tool.output = clip(text)
        if failed:
            tool.error = clip(text, 500) or "tool error"
        if tool.name == "spawn_agent" and self.depth < 4:
            tool.agent = self._spawned(tool, text)

    def _spawned(self, tool: ToolCall, text: str) -> Agent | None:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        child = parsed.get("agent_id") if isinstance(parsed, dict) else None
        path = self.children.get(child) if isinstance(child, str) else None
        if path is None:
            return None
        return load_subagent(path, child, tool.input, self.depth + 1)

    def usage(self, info: Any) -> None:
        if not isinstance(info, dict) or (self.call is None and not self.steps):
            return
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            return
        target = self.call
        if target is None:
            target = next((s for s in reversed(self.steps) if isinstance(s, ModelCall)), None)
        if target is None:
            return
        cached = as_int(last.get("cached_input_tokens")) or 0
        total_in = as_int(last.get("input_tokens"))
        target.token_input = None if total_in is None else max(0, total_in - cached)
        target.token_cache_read = cached or None
        target.token_output = as_int(last.get("output_tokens"))


def _children_index(root: Path) -> dict[str, dict[str, Path]]:
    """parent session id -> {child session id: rollout path}."""
    index: dict[str, dict[str, Path]] = {}
    if not root.is_dir():
        return index
    for path in root.rglob("rollout-*.jsonl"):
        meta = session_meta(path)
        parent = parent_of(meta)
        child = meta.get("id")
        if parent and isinstance(child, str):
            index.setdefault(parent, {})[child] = path
    return index


def load_subagent(path: Path, agent_id: str, tool_input: Any, depth: int) -> Agent:
    meta = session_meta(path)
    role, nickname = agent_role(meta)
    params = tool_input if isinstance(tool_input, dict) else {}
    prompt = params.get("message") or params.get("prompt") or ""
    agent = Agent(id=agent_id, name=role[:60], label=nickname or str(params.get("agent_type") or ""),
                  prompt=clip(prompt))
    root = path.parent
    while root.name != "sessions" and root.parent != root:
        root = root.parent
    children = _children_index(root).get(agent_id, {}) if depth < 4 else {}
    stream = _Stream(children, depth)
    for record in read_jsonl(path, _MAX_FILE):
        when = parse_ts(record.get("timestamp"))
        agent.started = agent.started or when
        _feed(stream, record, when)
    agent.steps = stream.steps
    agent.output = stream.final
    agent.model = stream.model
    agent.ended = stream.last
    return agent


def _feed(stream: _Stream, record: dict[str, Any], when: datetime | None) -> None:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    kind = record.get("type")
    if kind == "turn_context" and isinstance(payload.get("model"), str):
        stream.model = payload["model"]
    elif kind == "response_item":
        if payload.get("type") == "message" and payload.get("role") == "user":
            text = _content_text(payload.get("content"))
            if text and not text.lstrip().startswith(_SCAFFOLD):
                stream.start(text, when)
            return
        stream.item(payload, when)
    elif kind == "event_msg" and payload.get("type") == "token_count":
        stream.usage(payload.get("info"))


def load_rollout(path: Path, children: dict[str, Path] | None = None,
                 now: datetime | None = None) -> list[Turn]:
    """Turns of one Codex session, with subagents nested. Never raises."""
    try:
        return _load_rollout(path, children or {}, now or datetime.now(UTC))
    except Exception as exc:
        log.warning("codex session %s skipped: %s", path.name, exc)
        return []


def _load_rollout(path: Path, children: dict[str, Path], now: datetime) -> list[Turn]:
    records = read_jsonl(path, _MAX_FILE)
    meta: dict[str, Any] = {}
    turns: list[Turn] = []
    turn: Turn | None = None
    stream = _Stream(children, 0)
    model: str | None = None

    def close(complete: bool, when: datetime | None = None) -> None:
        if turn is None:
            return
        turn.steps = stream.steps
        turn.final_text = turn.final_text or stream.final
        turn.ended = when or stream.last
        turn.model = turn.model or stream.model or model
        turn.complete = complete
        if not complete and turn.ended is not None and now - turn.ended > _IDLE_COMPLETE:
            turn.complete = True
        turns.append(turn)

    session = path.stem
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        kind = record.get("type")
        when = parse_ts(record.get("timestamp"))
        if kind == "session_meta":
            meta = payload
            if isinstance(payload.get("id"), str):
                session = payload["id"]
            continue
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
            stream.model = model
            continue
        if kind == "event_msg" and payload.get("type") in {"task_complete", "turn_complete"}:
            if turn is not None:
                message = payload.get("last_agent_message")
                if isinstance(message, str) and message.strip():
                    turn.final_text = clip(message)
                close(True, when)
                turn = None
            continue
        if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            text = _content_text(payload.get("content"))
            if not text.strip() or text.lstrip().startswith(_SCAFFOLD):
                continue
            close(True)
            turn = Turn(
                app=APP,
                session=session,
                turn_id=f"{session}:{when.isoformat() if when else len(turns)}",
                prompt=clip(text),
                started=when,
                cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
                model=model,
            )
            stream = _Stream(children, 0)
            stream.model = model
            stream.start(text, when)
            continue
        if turn is None:
            continue
        _feed(stream, record, when)
    close(False)
    return turns


class CodexSource:
    name = APP

    def __init__(self, home: Path | None = None) -> None:
        self.root = sessions_dir(home)

    def available(self) -> bool:
        return self.root.is_dir()

    def sessions(self) -> list[SessionRef]:
        if not self.root.is_dir():
            return []
        cutoff = import_cutoff()
        index = _children_index(self.root)
        child_ids = {child for kids in index.values() for child in kids}
        refs: list[SessionRef] = []
        for path in sorted(self.root.rglob("rollout-*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff or stat.st_size > _MAX_FILE:
                continue
            meta = session_meta(path)
            sid = meta.get("id") if isinstance(meta.get("id"), str) else path.stem
            if sid in child_ids or parent_of(meta):
                continue
            kids = index.get(sid, {})
            refs.append(SessionRef(
                key=f"{APP}:{path}",
                signature=files_signature([path, *kids.values()]),
                load=lambda p=path, k=kids: load_rollout(p, k),
                large=stat.st_size > _LARGE_FILE,
            ))
        return refs
