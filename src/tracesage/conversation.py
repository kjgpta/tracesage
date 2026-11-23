"""Chat-style turns built from the runs of one thread.

Pure functions over stored events and (already redacted) payloads.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from tracesage.models import Run, StoredEvent

_USER_ROLES = frozenset({"human", "user"})
_AI_ROLES = frozenset({"ai", "assistant"})
_TEXT_KEYS = ("content", "text", "input", "output", "question", "query", "result")
_PREVIEW_LIMIT = 800
_SUMMARY_KEYS = ("command", "cmd", "path", "file_path", "query", "pattern", "url", "description")
_WRAPPER_BLOCKS = (
    "timestamp",
    "system_reminder",
    "system-reminder",
    "open_and_recently_viewed_files",
    "agent_transcripts",
    "agent_skills",
    "available_skills",
    "rules",
    "user_info",
    "communication",
    "git_status",
    "attached_files",
    "image_files",
    "code_selection",
)
_BLOCK_RE = re.compile(
    r"<(" + "|".join(re.escape(name) for name in _WRAPPER_BLOCKS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL | re.IGNORECASE,
)
_LEFTOVER_TAG_RE = re.compile(
    r"</?(?:user_query|timestamp|" + "|".join(re.escape(name) for name in _WRAPPER_BLOCKS) + r")\b[^>]*>",
    re.IGNORECASE,
)


class ToolChip(BaseModel):
    name: str
    mcp_server: str | None = None


class ToolStep(BaseModel):
    """One tool or model call inside a turn, in the order it happened."""

    name: str
    kind: str = "tool"
    mcp_server: str | None = None
    summary: str | None = None
    input: str | None = None
    output: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    event_id: str | None = None


class Turn(BaseModel):
    run_id: str
    user_text: str | None = None
    assistant_text: str | None = None
    tools: list[ToolChip] = Field(default_factory=list)
    steps: list[ToolStep] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    status: str = "running"
    error: str | None = None
    interrupted: bool = False


def _role(message: dict[str, Any]) -> str:
    raw = message.get("role") or message.get("type") or ""
    return str(raw).lower()


def _keep_lines(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return cleaned


def clean_display_text(text: str) -> str | None:
    """Drop app prompt wrappers and keep the question or answer text."""
    queries = [part.strip() for part in _USER_QUERY_RE.findall(text) if part.strip()]
    body = "\n\n".join(queries) if queries else _BLOCK_RE.sub("", text)
    body = _LEFTOVER_TAG_RE.sub("", body)
    cleaned = _keep_lines(body)
    return cleaned or None


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str | None:
    if value is None or value == "" or value == {} or value == []:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = str(value)
    text = _keep_lines(text)
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text or None


def _summary_line(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in _SUMMARY_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip().splitlines()[0][:180]
    preview = _preview(value, 180)
    if not preview:
        return None
    return preview.splitlines()[0][:180]


def _text_of(value: Any, depth: int = 0) -> str | None:
    if depth > 8 or value is None:
        return None
    if isinstance(value, str):
        text = _keep_lines(value)
        return text or None
    if isinstance(value, dict):
        if _role(value) and "content" in value:
            return _text_of(value.get("content"), depth + 1)
        for key in _TEXT_KEYS:
            if key in value:
                hit = _text_of(value[key], depth + 1)
                if hit:
                    return hit
        return None
    if isinstance(value, list):
        parts = [hit for item in value if (hit := _text_of(item, depth + 1))]
        return "\n".join(parts) or None
    return None


def _message_list(value: Any) -> list[dict[str, Any]] | None:
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) for item in value)
        and any(_role(item) for item in value)
    ):
        return value
    return None


def _from_messages(messages: list[dict[str, Any]], roles: frozenset[str]) -> str | None:
    chosen = [msg for msg in messages if _role(msg) in roles]
    if not chosen:
        return None
    return _text_of(chosen[-1])


def _finish(text: str | None) -> str | None:
    if not text:
        return None
    return clean_display_text(text)


def extract_user_text(payload: Any) -> str | None:
    """Best-effort user text from a start payload. Never raises."""
    try:
        if isinstance(payload, str):
            return _finish(payload)
        if isinstance(payload, list):
            return _finish(_from_messages(payload, _USER_ROLES) or _text_of(payload))
        if not isinstance(payload, dict):
            return None
        messages = _message_list(payload.get("messages"))
        if messages:
            return _finish(_from_messages(messages, _USER_ROLES))
        for key in ("input", "inputs", "question", "query", "prompt"):
            if key in payload and payload[key] not in (None, "", {}, []):
                nested = payload[key]
                nested_messages = _message_list(nested)
                if nested_messages:
                    return _finish(_from_messages(nested_messages, _USER_ROLES))
                return _finish(_text_of(nested))
        return _finish(_text_of(payload.get("messages")))
    except Exception:
        return None


def extract_assistant_text(payload: Any) -> str | None:
    """Best-effort assistant text from an end payload. Never raises."""
    try:
        if isinstance(payload, str):
            return _finish(payload)
        if isinstance(payload, list):
            return _finish(_from_messages(payload, _AI_ROLES) or _text_of(payload))
        if not isinstance(payload, dict):
            return None
        messages = _message_list(payload.get("messages")) or _message_list(
            payload.get("output")
        )
        if messages:
            hit = _from_messages(messages, _AI_ROLES)
            if hit:
                return _finish(hit)
        for key in ("output", "outputs", "result", "content"):
            if key in payload and payload[key] not in (None, "", {}, []):
                nested = payload[key]
                nested_messages = _message_list(nested)
                if nested_messages:
                    hit = _from_messages(nested_messages, _AI_ROLES)
                    if hit:
                        return _finish(hit)
                return _finish(_text_of(nested))
        return None
    except Exception:
        return None


def _payload_field(payload: Any, key: str) -> Any:
    if isinstance(payload, dict) and key in payload:
        return payload[key]
    return None


def build_turns(
    runs: list[Run],
    events: list[StoredEvent],
    payloads: dict[str, Any] | None = None,
) -> list[Turn]:
    """One turn per run, in the order given. ``payloads`` maps event_id to blob JSON."""
    payloads = payloads or {}
    by_root: dict[str, list[StoredEvent]] = {}
    for event in events:
        by_root.setdefault(event.root_run_id, []).append(event)

    turns: list[Turn] = []
    for run in runs:
        group = by_root.get(run.run_id, [])
        user_text: str | None = None
        assistant_text: str | None = None
        tools: list[ToolChip] = []
        seen_tools: set[tuple[str, str | None]] = set()
        steps: list[ToolStep] = []
        open_steps: dict[str, ToolStep] = {}
        duration: int | None = None
        for event in group:
            payload = payloads.get(event.event_id)
            et = event.event_type.value
            if user_text is None and et.endswith("_start") and event.run_id == run.run_id:
                user_text = extract_user_text(payload) if payload is not None else None
            if event.run_id == run.run_id and (
                et.endswith("_end") or et in {"agent_finish", "chain_interrupt"}
            ):
                text = extract_assistant_text(payload) if payload is not None else None
                if text:
                    assistant_text = text
                if event.duration_ms is not None:
                    duration = event.duration_ms
            if et in {"tool_start", "tool_end", "tool_error"} and event.tool_name:
                key = (event.tool_name, event.mcp_server)
                if key not in seen_tools:
                    seen_tools.add(key)
                    tools.append(ToolChip(name=event.tool_name, mcp_server=event.mcp_server))
            if et == "tool_start":
                incoming = _payload_field(payload, "input")
                step = ToolStep(
                    name=event.tool_name or "tool",
                    mcp_server=event.mcp_server,
                    summary=_summary_line(incoming),
                    input=_preview(incoming),
                    event_id=event.event_id,
                )
                steps.append(step)
                open_steps[event.run_id] = step
            elif et in {"llm_start", "chat_model_start", "llm_end", "llm_error"}:
                incoming = _payload_field(payload, "input")
                if incoming in (None, ""):
                    incoming = _payload_field(payload, "prompt")
                outgoing = _payload_field(payload, "output")
                step = open_steps.get(event.run_id) if et in {"llm_end", "llm_error"} else None
                if step is None:
                    spoken = outgoing if et in {"llm_end", "llm_error"} else incoming
                    label = event.model or "Model"
                    if event.agent_name and label == event.agent_name:
                        label = "Model"
                    step = ToolStep(
                        name=label,
                        kind="llm",
                        summary=_summary_line(spoken) or _summary_line(incoming),
                        input=_preview(incoming),
                        output=_preview(outgoing, limit=None) if et in {"llm_end", "llm_error"} else None,
                        event_id=event.event_id,
                    )
                    steps.append(step)
                    open_steps[event.run_id] = step
                else:
                    if step.input is None:
                        step.input = _preview(incoming)
                    step.output = _preview(outgoing, limit=None) or step.output
                    step.summary = step.summary or _summary_line(outgoing)
                    step.duration_ms = event.duration_ms
                    if event.model and event.model != event.agent_name:
                        step.name = event.model
                if et == "llm_error":
                    step.error = event.error_message or _preview(outgoing)
            elif et in {"tool_end", "tool_error"}:
                step = open_steps.get(event.run_id)
                if step is None:
                    incoming = _payload_field(payload, "input")
                    step = ToolStep(
                        name=event.tool_name or "tool",
                        mcp_server=event.mcp_server,
                        summary=_summary_line(incoming),
                        input=_preview(incoming),
                        event_id=event.event_id,
                    )
                    steps.append(step)
                outgoing = _payload_field(payload, "output")
                if step.input is None:
                    incoming = _payload_field(payload, "input")
                    step.input = _preview(incoming)
                    step.summary = step.summary or _summary_line(incoming)
                step.output = _preview(outgoing)
                step.duration_ms = event.duration_ms
                if et == "tool_error":
                    step.error = event.error_message or _preview(outgoing)
        if run.completed_at and run.started_at and duration is None:
            duration = max(0, int((run.completed_at - run.started_at).total_seconds() * 1000))
        turns.append(
            Turn(
                run_id=run.run_id,
                user_text=user_text,
                assistant_text=assistant_text,
                tools=tools,
                steps=steps,
                tokens_in=run.total_tokens_input,
                tokens_out=run.total_tokens_output,
                cost_usd=run.total_cost_usd,
                duration_ms=duration,
                status=run.status.value,
                error=run.error_message,
                interrupted=run.status.value == "interrupted",
            )
        )
    return turns
