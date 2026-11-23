"""Build turns from Anthropic-style message logs.

Claude Code transcripts, Claude Desktop Cowork audit logs and Desktop sidebar
chats all store ``user`` and ``assistant`` records whose ``message.content`` is
a list of text, thinking, tool_use and tool_result blocks. Each assistant
``message.id`` is one model request. Subagent records arrive as separate
streams keyed by the tool_use id that spawned them.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tracesage.ingest.traces import (
    Agent,
    ModelCall,
    ToolCall,
    Turn,
    as_int,
    bounded,
    clip,
    parse_ts,
    split_mcp,
)

log = logging.getLogger("tracesage.ingest.sessions")

_END_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens", "refusal"})
_INPUT_LIMIT = 2000
# Slash commands are logged as user rows. Without model work they are not turns.
_COMMAND_PREFIXES = ("<command-", "<local-command")
IDLE_COMPLETE = timedelta(minutes=30)


@dataclass
class SubStream:
    """Records written by one subagent, plus what the parent called it."""

    records: list[dict[str, Any]] = field(default_factory=list)
    name: str | None = None
    label: str | None = None


def record_time(record: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "_audit_timestamp", "created_at"):
        found = parse_ts(record.get(key))
        if found is not None:
            return found
    return None


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {None, "text"}:
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def prompt_text(record: dict[str, Any]) -> str:
    """User text of a prompt record. Empty for tool results and meta rows."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return ""
    blocks = _blocks(record)
    if any(block.get("type") == "tool_result" for block in blocks):
        return ""
    text = "\n".join(
        block["text"] for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )
    if text.lstrip().startswith("[Request interrupted"):
        return ""
    return text


class _Stream:
    """Fold one agent's records into model and tool steps."""

    def __init__(self, subs: dict[str, SubStream], model: str | None, depth: int) -> None:
        self.subs = subs
        self.model = model
        self.depth = depth
        self.steps: list[ModelCall | ToolCall] = []
        self.calls: dict[str, ModelCall] = {}
        self.tools: dict[str, ToolCall] = {}
        self.output_tokens: dict[str, int] = {}
        self.prev: datetime | None = None
        self.pending = ""
        self.last: datetime | None = None
        self.last_call: ModelCall | None = None

    def start(self, prompt: str, when: datetime | None) -> None:
        self.pending = prompt[:_INPUT_LIMIT]
        self.prev = when
        self.last = when

    def feed(self, record: dict[str, Any]) -> None:
        when = record_time(record)
        kind = record.get("type")
        if kind == "assistant":
            self._assistant(record, when)
        elif kind == "user":
            self._results(record, when)
        if when is not None:
            self.last = when if self.last is None else max(self.last, when)

    def _assistant(self, record: dict[str, Any], when: datetime | None) -> None:
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        blocks = _blocks(record)
        call_id = message.get("id") if isinstance(message.get("id"), str) else None
        call_id = call_id or str(record.get("uuid") or len(self.calls))
        model = message.get("model") if isinstance(message.get("model"), str) else None
        if model == "<synthetic>":
            model = None
        if model:
            self.model = model
        starts = [t for t in (parse_ts(b.get("start_timestamp")) for b in blocks) if t]
        stops = [t for t in (parse_ts(b.get("stop_timestamp")) for b in blocks) if t]
        call = self.calls.get(call_id)
        if call is None:
            begin = min(starts) if starts else (self.prev or when)
            call = ModelCall(id=call_id, model=model or self.model, started=begin,
                             input_text=self.pending)
            self.pending = ""
            self.calls[call_id] = call
            self.steps.append(call)
        end = max(stops) if stops else when
        if end is not None:
            call.ended = end if call.ended is None else max(call.ended, end)
        if model:
            call.model = model
        stop = message.get("stop_reason")
        if isinstance(stop, str):
            call.stop_reason = stop
        if record.get("isApiErrorMessage") or record.get("error"):
            call.error = clip(_text_of(message.get("content")) or record.get("error"), 500) or "API error"
        self._usage(call, message.get("usage"))
        for block in blocks:
            kind = block.get("type")
            if kind == "thinking":
                thought = block.get("thinking") or block.get("text")
                if isinstance(thought, str) and thought.strip():
                    call.thinking = (call.thinking + "\n" + thought).strip()
            elif kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    call.output_text = (call.output_text + "\n" + text).strip()
            elif kind in {"tool_use", "server_tool_use"}:
                self._tool_use(block, call, when)
        self.last_call = call
        self.prev = call.ended or when

    def _usage(self, call: ModelCall, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        output = as_int(usage.get("output_tokens")) or 0
        if output < self.output_tokens.get(call.id, -1):
            return
        self.output_tokens[call.id] = output
        call.token_input = as_int(usage.get("input_tokens"))
        call.token_output = as_int(usage.get("output_tokens"))
        call.token_cache_read = as_int(usage.get("cache_read_input_tokens"))
        call.token_cache_write = as_int(usage.get("cache_creation_input_tokens"))

    def _tool_use(self, block: dict[str, Any], call: ModelCall, when: datetime | None) -> None:
        raw_name = block.get("name") if isinstance(block.get("name"), str) else "tool"
        tool_id = block.get("id") if isinstance(block.get("id"), str) else f"{call.id}:{len(self.tools)}"
        if tool_id in self.tools:
            return
        name, server = split_mcp(raw_name)
        integration = block.get("integration_name")
        if server is None and isinstance(integration, str) and integration:
            server = integration[:60]
        tool = ToolCall(
            id=tool_id, name=name, server=server, input=bounded(block.get("input")),
            started=parse_ts(block.get("start_timestamp")) or when,
        )
        call.tool_names.append(name)
        self.tools[tool_id] = tool
        self.steps.append(tool)
        sub = self.subs.get(tool_id)
        if sub is not None and self.depth < 6:
            tool.agent = build_agent(tool_id, sub, block.get("input"), self.subs, self.model,
                                     self.depth + 1)

    def _results(self, record: dict[str, Any], when: datetime | None) -> None:
        results = [b for b in _blocks(record) if b.get("type") == "tool_result"]
        texts: list[str] = []
        for result in results:
            tool_id = result.get("tool_use_id")
            tool = self.tools.get(tool_id) if isinstance(tool_id, str) else None
            output = _text_of(result.get("content"))
            texts.append(output[:600])
            if tool is None:
                continue
            tool.done = True
            tool.ended = parse_ts(result.get("stop_timestamp")) or when
            tool.output = clip(output) if output else _meta_output(record)
            if result.get("is_error"):
                tool.error = clip(output, 500) or "tool error"
        if results:
            self.pending = "\n\n".join(texts)[:_INPUT_LIMIT]
            self.prev = when

    def open_tools(self) -> bool:
        return any(not tool.done for tool in self.tools.values())

    def final_text(self) -> str:
        for step in reversed(self.steps):
            if isinstance(step, ModelCall) and step.output_text and not step.tool_names:
                return step.output_text
        for step in reversed(self.steps):
            if isinstance(step, ModelCall) and step.output_text:
                return step.output_text
        return ""


def _meta_output(record: dict[str, Any]) -> Any:
    extra = record.get("toolUseResult")
    return clip(extra) if extra else None


def build_agent(
    agent_id: str,
    sub: SubStream,
    tool_input: Any,
    subs: dict[str, SubStream],
    model: str | None,
    depth: int = 1,
) -> Agent:
    """One subagent's steps from its own records."""
    params = tool_input if isinstance(tool_input, dict) else {}
    kind = params.get("subagent_type") or params.get("agent_type")
    name = sub.name or (kind if isinstance(kind, str) and kind else "subagent")
    label = sub.label or params.get("description") or ""
    stream = _Stream(subs, model, depth)
    prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else ""
    agent = Agent(id=agent_id, name=str(name)[:60], label=str(label)[:200])
    for record in sub.records:
        text = prompt_text(record)
        if text and not stream.steps:
            prompt = prompt or text
            stream.start(text, record_time(record))
            agent.started = agent.started or record_time(record)
            continue
        if agent.started is None:
            agent.started = record_time(record)
        stream.feed(record)
    agent.prompt = clip(prompt)
    agent.steps = stream.steps
    agent.output = stream.final_text()
    agent.model = stream.model
    agent.ended = stream.last
    return agent


def build_turns(
    records: Iterable[dict[str, Any]],
    *,
    app: str,
    session: str | Callable[[dict[str, Any]], str],
    subs: dict[str, SubStream] | None = None,
    always_complete: bool = False,
    now: datetime | None = None,
    cwd: str | None = None,
) -> list[Turn]:
    """Split a main-agent record stream into turns. Bad records are skipped."""
    subs = subs or {}
    now = now or datetime.now(UTC)
    turns: list[Turn] = []
    turn: Turn | None = None
    stream: _Stream | None = None
    model: str | None = None

    def close(complete: bool) -> None:
        if turn is None or stream is None:
            return
        turn.steps = stream.steps
        turn.final_text = turn.final_text or stream.final_text()
        turn.model = turn.model or stream.model
        turn.ended = turn.ended or stream.last
        if complete or always_complete:
            turn.complete = True
        elif not stream.open_tools():
            last = stream.last_call
            if last is not None and last.stop_reason in _END_REASONS and not last.tool_names:
                turn.complete = True
        if not turn.complete and turn.ended is not None and now - turn.ended > IDLE_COMPLETE:
            turn.complete = True
        if not turn.steps and turn.prompt.lstrip().startswith(_COMMAND_PREFIXES):
            return
        turns.append(turn)

    for record in records:
        try:
            if not isinstance(record, dict) or record.get("isReplay") is True:
                continue
            kind = record.get("type")
            text = prompt_text(record)
            if text:
                close(True)
                when = record_time(record)
                uid = record.get("uuid")
                sid = session(record) if callable(session) else session
                turn = Turn(
                    app=app,
                    session=sid,
                    turn_id=uid if isinstance(uid, str) and uid else f"{sid}:{len(turns)}",
                    prompt=clip(text),
                    started=when,
                    cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else cwd,
                )
                title = record.get("title")
                if isinstance(title, str) and title.strip():
                    turn.title = title.strip()
                stream = _Stream(subs, model, 0)
                stream.start(text, when)
                continue
            if turn is None or stream is None:
                continue
            if kind in {"assistant", "user"}:
                stream.feed(record)
                model = stream.model or model
            elif kind == "result":
                turn.ended = record_time(record) or stream.last
                result = record.get("result")
                if isinstance(result, str) and result.strip():
                    turn.final_text = clip(result)
                named = record.get("modelUsage")
                if isinstance(named, dict) and named:
                    first = next(iter(named))
                    if isinstance(first, str) and first:
                        turn.model = first
                if record.get("is_error"):
                    turn.error = clip(result, 500) if isinstance(result, str) and result else "session error"
                close(True)
                turn = None
                stream = None
        except Exception as exc:
            log.debug("session record skipped: %s", exc)
            continue
    close(False)
    return turns
