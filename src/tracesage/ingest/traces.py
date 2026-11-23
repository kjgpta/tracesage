"""Shared trace model for AI-app session importers.

Each reader (Claude Code transcripts, Claude Desktop logs, Cursor's local
store, Codex rollouts) turns a session into ``Turn`` objects: one user prompt,
the model calls the agent made, the tools it ran, and any subagents it
spawned. ``turn_events`` converts a turn into RawEvents with stable ids, so
importing the same turn twice writes the same rows.

Run tree for one turn::

    chain (app agent)
    ├── llm (one per model request, named by the model)
    ├── tool
    │   └── chain (subagent spawned by the tool)
    │       ├── llm
    │       └── tool
    └── ...
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracesage.models import EventType, RawEvent

TEXT_LIMIT = 8000


@dataclass
class ModelCall:
    id: str
    model: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    input_text: str = ""
    output_text: str = ""
    thinking: str = ""
    tool_names: list[str] = field(default_factory=list)
    token_input: int | None = None
    token_output: int | None = None
    token_cache_read: int | None = None
    token_cache_write: int | None = None
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    server: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    done: bool = False
    agent: Agent | None = None


@dataclass
class Agent:
    id: str
    name: str
    label: str = ""
    prompt: str = ""
    output: str = ""
    model: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    steps: list[ModelCall | ToolCall] = field(default_factory=list)


@dataclass
class Turn:
    app: str
    session: str
    turn_id: str
    prompt: str = ""
    title: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    final_text: str = ""
    complete: bool = False
    error: str | None = None
    cwd: str | None = None
    model: str | None = None
    steps: list[ModelCall | ToolCall] = field(default_factory=list)


def eid(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def clip(value: Any, limit: int = TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, default=str)[:limit]
    except Exception:
        return str(value)[:limit]


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 strings and epoch seconds or milliseconds. None when unknown."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        if isinstance(value, str) and value:
            text = value.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def split_mcp(name: str) -> tuple[str, str | None]:
    """``mcp__server__tool`` names carry the server. Other names have none."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return parts[2], parts[1]
    return name, None


def turn_fingerprint(turn: Turn) -> str:
    """Changes whenever an import would write different rows for this turn."""
    parts: list[str] = [str(turn.complete), turn.final_text[:64], str(turn.ended)]

    def walk(steps: list[ModelCall | ToolCall]) -> None:
        for step in steps:
            if isinstance(step, ModelCall):
                parts.append(f"m{step.id}{step.ended}{step.token_output}{len(step.output_text)}")
            else:
                parts.append(f"t{step.id}{step.done}{step.ended}")
                if step.agent is not None:
                    walk(step.agent.steps)

    walk(turn.steps)
    return eid(*parts)


class _Clock:
    """Fills a missing timestamp with the last known one.

    Parallel tools and subagents overlap, so time is not forced forward
    globally. ``span`` only keeps each end at or after its own start.
    """

    def __init__(self, start: datetime) -> None:
        self.last = start

    def at(self, value: datetime | None) -> datetime:
        if value is None:
            return self.last
        self.last = value
        return value

    def span(self, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
        begin = self.at(start if start is not None else end)
        finish = self.at(end) if end is not None else begin
        return begin, max(begin, finish)


def turn_events(turn: Turn, *, metadata_only: bool = False) -> list[RawEvent]:
    """RawEvents for one turn, in time order within each span. Never raises."""
    try:
        return _turn_events(turn, metadata_only=metadata_only)
    except Exception:
        return []


def _turn_events(turn: Turn, *, metadata_only: bool) -> list[RawEvent]:
    app = turn.app
    root = eid(app, turn.session, turn.turn_id)
    tags = [f"source:{app}"]
    if turn.cwd:
        project = Path(turn.cwd).name
        if project:
            tags.append(f"project:{project}")
    started = turn.started or _earliest(turn.steps) or turn.ended or datetime.now(UTC)
    clock = _Clock(started)
    events: list[RawEvent] = []

    def add(
        kind: EventType,
        *,
        event_id: str,
        run_id: str,
        parent: str | None,
        when: datetime | None,
        summary: str,
        agent_name: str | None,
        tool_name: str | None = None,
        mcp_server: str | None = None,
        raw: dict[str, Any] | None = None,
        error: str | None = None,
        model: str | None = None,
        tokens: ModelCall | None = None,
    ) -> None:
        payload = {} if metadata_only else (raw or {})
        events.append(RawEvent(
            event_id=event_id,
            event_type=kind,
            run_id=run_id,
            parent_run_id=parent,
            root_run_id=root,
            timestamp=clock.at(when),
            agent_name=agent_name,
            tool_name=tool_name,
            mcp_server=mcp_server,
            summary=(summary or kind.value)[:500],
            full_blob_eligible=kind not in {EventType.RUN_START, EventType.RUN_END},
            raw_payload=payload,
            error_message=error,
            tags=list(tags),
            thread_id=turn.session,
            model=model,
            token_input=tokens.token_input if tokens else None,
            token_output=tokens.token_output if tokens else None,
            token_cache_read=tokens.token_cache_read if tokens else None,
            token_cache_write=tokens.token_cache_write if tokens else None,
        ))

    title = (turn.title or "").strip()
    first_line = turn.prompt.strip().splitlines()[0] if turn.prompt.strip() else ""
    headline = title or first_line
    summary = f"{app}: {headline[:120]}" if headline and not metadata_only else f"{app} prompt"
    if metadata_only and title:
        summary = f"{app}: {title[:120]}"
    add(EventType.RUN_START, event_id=eid(root, "run_start"), run_id=root, parent=None,
        when=started, summary=f"RUN_START {summary}", agent_name=app, raw={"_source": app})
    add(EventType.CHAIN_START, event_id=eid(root, "start"), run_id=root, parent=None,
        when=started, summary=summary, agent_name=app, raw={"input": clip(turn.prompt)})

    def walk(steps: list[ModelCall | ToolCall], parent: str, agent_name: str) -> None:
        for step in steps:
            if isinstance(step, ModelCall):
                model_call(step, parent)
            else:
                tool_call(step, parent, agent_name)

    def model_call(call: ModelCall, parent: str) -> None:
        run = eid(root, "llm", call.id)
        name = call.model or turn.model or "model"
        tools = ", ".join(call.tool_names[:6])
        begin, finish = clock.span(call.started, call.ended)
        add(EventType.CHAT_MODEL_START, event_id=eid(run, "start"), run_id=run, parent=parent,
            when=begin, summary=f"{name} request", agent_name=name,
            model=name, raw={"input": clip(call.input_text)})
        if call.output_text.strip():
            out_summary = call.output_text.strip()[:160]
        elif tools:
            out_summary = f"calls {tools}"
        elif call.thinking.strip():
            out_summary = call.thinking.strip()[:160]
        else:
            out_summary = f"{name} response"
        raw: dict[str, Any] = {"output": clip(call.output_text)}
        if call.thinking:
            raw["thinking"] = clip(call.thinking)
        if call.tool_names:
            raw["tool_calls"] = call.tool_names[:50]
        if call.stop_reason:
            raw["stop_reason"] = call.stop_reason
        add(EventType.LLM_ERROR if call.error else EventType.LLM_END,
            event_id=eid(run, "end"), run_id=run, parent=parent,
            when=finish, summary=out_summary, agent_name=name,
            model=name, raw=raw, error=call.error, tokens=call)

    def tool_call(tool: ToolCall, parent: str, agent_name: str) -> None:
        run = eid(root, "tool", tool.id)
        sub = tool.agent
        tool_end = tool.ended
        if sub is not None and sub.ended is not None and (tool_end is None or sub.ended > tool_end):
            tool_end = sub.ended
        begin, finish = clock.span(tool.started, tool_end)
        add(EventType.TOOL_START, event_id=eid(run, "start"), run_id=run, parent=parent,
            when=begin, summary=f"{tool.name} start", agent_name=agent_name,
            tool_name=tool.name, mcp_server=tool.server, raw={"input": _jsonable(tool.input)})
        if sub is not None:
            sub_run = eid(root, "agent", sub.id)
            label = sub.label or sub.name
            sub_begin, sub_finish = clock.span(sub.started or begin, sub.ended or tool_end)
            add(EventType.CHAIN_START, event_id=eid(sub_run, "start"), run_id=sub_run,
                parent=run, when=sub_begin, summary=f"{sub.name}: {label[:120]}",
                agent_name=sub.name, raw={"input": clip(sub.prompt)})
            walk(sub.steps, sub_run, sub.name)
            if tool.done or sub.ended:
                add(EventType.CHAIN_END, event_id=eid(sub_run, "end"), run_id=sub_run,
                    parent=run, when=sub_finish,
                    summary=(sub.output.strip()[:160] or f"{sub.name} end"),
                    agent_name=sub.name, model=sub.model, raw={"output": clip(sub.output)})
        if tool.done:
            failed = bool(tool.error)
            add(EventType.TOOL_ERROR if failed else EventType.TOOL_END,
                event_id=eid(run, "end"), run_id=run, parent=parent,
                when=finish, summary=f"{tool.name} {'error' if failed else 'end'}",
                agent_name=agent_name, tool_name=tool.name, mcp_server=tool.server,
                raw={"input": _jsonable(tool.input), "output": _jsonable(tool.output)},
                error=clip(tool.error, 500) if failed else None)

    walk(turn.steps, root, app)

    if turn.complete:
        failed = bool(turn.error)
        end_summary = turn.final_text.strip()[:160] or f"{app} end"
        end_at = max(started, clock.at(turn.ended))
        add(EventType.CHAIN_ERROR if failed else EventType.CHAIN_END,
            event_id=eid(root, "end"), run_id=root, parent=None, when=end_at,
            summary=end_summary, agent_name=app, model=turn.model,
            raw={"output": clip(turn.final_text)}, error=clip(turn.error, 500) if failed else None)
        add(EventType.RUN_END, event_id=eid(root, "run_end"), run_id=root, parent=None,
            when=end_at, summary=f"RUN_END {app}", agent_name=app,
            raw={"_source": app}, error=clip(turn.error, 500) if failed else None)
    return events


def _earliest(steps: list[ModelCall | ToolCall]) -> datetime | None:
    found = [step.started or step.ended for step in steps]
    known = [stamp for stamp in found if stamp is not None]
    return min(known) if known else None


def bounded(value: Any) -> Any:
    """A tool input or output small enough to keep in memory for a long session."""
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:TEXT_LIMIT]
    text = clip(value)
    if len(text) >= TEXT_LIMIT:
        return text
    try:
        return json.loads(text)
    except Exception:
        return text
