"""OpenTelemetry span -> tracesage RawEvent mapping.

One finished span becomes two events: a ``*_start`` stamped with the span's start
time and a ``*_end`` / ``*_error`` stamped with its end time, emitted back to back
so the worker's existing start/end pairing computes ``duration_ms`` unchanged.

Identity::

    root_run_id   = trace_id
    run_id        = span_id
    parent_run_id = parent_span_id            (root span: trace_id)

The first span seen for a trace also yields a synthetic ``RUN_START`` with
``run_id = trace_id`` (the run row the UI lists); the trace's root span yields a
``RUN_END`` so the worker marks that run terminal. Children usually export before
their parents, so arrival order must not matter — only ids are used for linking.

Two attribute vocabularies are understood: the OpenTelemetry GenAI semantic
conventions (``gen_ai.*``: Pydantic AI, OpenAI Agents SDK, the ``genai-openai`` /
``genai-anthropic`` instrumentations) and OpenInference (``openinference.span.kind``,
``llm.*``, ``tool.*``: CrewAI, LlamaIndex, …). Anything else lands as a chain step
named after the span, so unknown sources still show up in the timeline.

Pure: no IO, no tracer access. Thread-safe (the OTel SDK may call the exporter from
any thread).
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tracesage.models import BLOB_ELIGIBLE_EVENTS, EventType, RawEvent

# Marker placed in every ingested event's raw_payload. exporters/otel.py skips these
# so an ``otlp_endpoint`` pointing back at tracesage cannot loop.
SOURCE_MARKER = "otlp"

_ZERO_IDS = frozenset({"", "0" * 16, "0" * 32})


@dataclass(slots=True)
class SpanRecord:
    """Transport-neutral view of one finished span (OTLP/JSON, protobuf, or an SDK
    ``ReadableSpan`` all decode into this)."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    kind: str = "INTERNAL"  # SpanKind name: INTERNAL/SERVER/CLIENT/PRODUCER/CONSUMER
    status_code: str = "UNSET"  # UNSET / OK / ERROR
    status_message: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    resource: dict[str, Any] = field(default_factory=dict)
    scope_name: str | None = None

    @property
    def is_root(self) -> bool:
        return self.parent_span_id is None or self.parent_span_id in _ZERO_IDS


@dataclass(frozen=True, slots=True)
class _Kind:
    family: str  # llm | tool | agent | chain | retriever
    start: EventType
    end: EventType
    error: EventType


_LLM_CHAT = _Kind("llm", EventType.CHAT_MODEL_START, EventType.LLM_END, EventType.LLM_ERROR)
_LLM_TEXT = _Kind("llm", EventType.LLM_START, EventType.LLM_END, EventType.LLM_ERROR)
_TOOL = _Kind("tool", EventType.TOOL_START, EventType.TOOL_END, EventType.TOOL_ERROR)
_AGENT = _Kind("agent", EventType.CHAIN_START, EventType.CHAIN_END, EventType.CHAIN_ERROR)
_CHAIN = _Kind("chain", EventType.CHAIN_START, EventType.CHAIN_END, EventType.CHAIN_ERROR)
_RETRIEVER = _Kind(
    "retriever", EventType.RETRIEVER_START, EventType.RETRIEVER_END, EventType.RETRIEVER_ERROR
)

# 1. OpenTelemetry GenAI semantic conventions: gen_ai.operation.name
_GENAI_OPS: dict[str, _Kind] = {
    "chat": _LLM_CHAT,
    "text_completion": _LLM_TEXT,
    "generate_content": _LLM_TEXT,
    "embeddings": _LLM_TEXT,
    "execute_tool": _TOOL,
    "invoke_agent": _AGENT,
    "invoke_workflow": _AGENT,
    "create_agent": _AGENT,
    "retrieval": _RETRIEVER,
}

# 2. OpenInference: openinference.span.kind
_OPENINFERENCE_KINDS: dict[str, _Kind] = {
    "LLM": _LLM_CHAT,
    "TOOL": _TOOL,
    "AGENT": _AGENT,
    "CHAIN": _CHAIN,
    "RETRIEVER": _RETRIEVER,
    "EMBEDDING": _LLM_TEXT,
    "RERANKER": _CHAIN,
    "GUARDRAIL": _CHAIN,
    "EVALUATOR": _CHAIN,
}

# 3. Span-name prefixes (older instrumentations that set no operation attribute).
_NAME_PREFIXES: tuple[tuple[str, _Kind], ...] = (
    ("chat ", _LLM_CHAT),
    ("text_completion ", _LLM_TEXT),
    ("generate_content ", _LLM_TEXT),
    ("embeddings ", _LLM_TEXT),
    ("execute_tool ", _TOOL),
    ("invoke_agent ", _AGENT),
    ("invoke_workflow ", _AGENT),
    ("create_agent ", _AGENT),
    ("retrieval ", _RETRIEVER),
    ("retrieve ", _RETRIEVER),
    ("ChatCompletion", _LLM_CHAT),
    ("openai.chat", _LLM_CHAT),
    ("anthropic.messages", _LLM_CHAT),
    ("Messages.create", _LLM_CHAT),
)
_NAME_EXACT: dict[str, _Kind] = {
    "chat": _LLM_CHAT,
    "text_completion": _LLM_TEXT,
    "execute_tool": _TOOL,
    "invoke_agent": _AGENT,
}

_MODEL_KEYS = ("gen_ai.request.model", "gen_ai.response.model", "llm.model_name")
_TOOL_KEYS = ("gen_ai.tool.name", "tool.name")
_AGENT_KEYS = ("gen_ai.agent.name", "graph.node.id", "agent.name")
_THREAD_KEYS = ("gen_ai.conversation.id", "session.id", "thread.id", "thread_id")
_IN_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt")
_OUT_TOKEN_KEYS = (
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.token_count.completion",
)
_CACHE_READ_KEYS = (
    "gen_ai.usage.cache_read.input_tokens",
    "llm.token_count.prompt_details.cache_read",
)
_CACHE_WRITE_KEYS = (
    "gen_ai.usage.cache_creation.input_tokens",
    "llm.token_count.prompt_details.cache_write",
)
_PROVIDER_KEYS = ("gen_ai.provider.name", "gen_ai.system", "llm.provider")
_MCP_SERVER_KEYS = ("mcp.server.name", "anthropic_mcp_server_name")

_PREVIEW_CHARS = 200
# Message-structure keys that hold the text we want in a summary, in preference
# order, and metadata keys whose values are never that text (role names, ids …).
_TEXT_KEYS = ("content", "text", "parts", "value", "result", "arguments", "query", "input", "output")
_SKIP_KEYS = frozenset(
    {"role", "type", "id", "name", "finish_reason", "tool_call_id", "mime_type", "call_id"}
)


def _ns_to_dt(ns: int) -> datetime:
    """Nanoseconds since the epoch -> aware UTC datetime (microsecond precision)."""
    secs, rem = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(secs, UTC) + timedelta(microseconds=rem // 1000)


def _first(attrs: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _maybe_json(value: Any) -> Any:
    """Decode a JSON-encoded string attribute; leave anything else untouched."""
    if isinstance(value, str):
        stripped = value.strip()
        looks_json = stripped[:1] in ("{", "[") or (
            len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"'
        )
        if looks_json:
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


def _text_of(value: Any, limit: int = _PREVIEW_CHARS) -> str:
    """Human preview of a message/args structure: the first text-ish string found,
    else a compact JSON dump. Never raises."""
    found = _first_text(value, 0)
    if found is None:
        try:
            found = value if isinstance(value, str) else json.dumps(value, default=str)
        except Exception:
            found = str(value)
    found = " ".join(found.split())
    return found if len(found) <= limit else found[:limit]


def _first_text(value: Any, depth: int) -> str | None:
    if depth > 8:
        return None
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if key in value:
                hit = _first_text(value[key], depth + 1)
                if hit:
                    return hit
        for k, v in value.items():
            if k in _SKIP_KEYS:
                continue
            hit = _first_text(v, depth + 1)
            if hit:
                return hit
        return None
    if isinstance(value, list):
        for item in value:
            hit = _first_text(item, depth + 1)
            if hit:
                return hit
    return None


def _openinference_messages(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]] | None:
    """Rebuild ``llm.input_messages.N.message.role`` / ``.content`` flattened keys."""
    head = f"{prefix}."
    collected: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        if not key.startswith(head):
            continue
        rest = key[len(head):].split(".", 2)  # ["0", "message", "role"]
        if len(rest) != 3 or rest[1] != "message":
            continue
        try:
            idx = int(rest[0])
        except ValueError:
            continue
        collected.setdefault(idx, {})[rest[2]] = value
    if not collected:
        return None
    return [collected[i] for i in sorted(collected)]


def _user_message(messages: Any, *, first: bool = False) -> Any:
    """The last (or first) user-role message in a GenAI/OpenInference message list —
    the current turn for a model request, the prompt for a whole run — else the
    whole value."""
    if isinstance(messages, list):
        ordered = messages if first else reversed(messages)
        for msg in ordered:
            if isinstance(msg, dict) and str(msg.get("role", "")).lower() == "user":
                return msg
    return messages


class SpanMapper:
    """Stateful span -> events mapper. State is only the bounded set of traces for
    which ``RUN_START`` has already been emitted."""

    def __init__(self, *, summary_max_chars: int = 500, seen_cap: int = 100_000) -> None:
        self._summary_max = max(1, int(summary_max_chars))
        self._seen_cap = max(1, int(seen_cap))
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- public

    def map(self, span: SpanRecord) -> list[RawEvent]:
        """Translate one finished span into its RawEvents, in emit order."""
        trace_id = span.trace_id
        kind = classify(span)
        attrs = span.attributes or {}
        agent_name, tool_name = self._names(span, kind, attrs)
        thread_id = _str_or_none(_first(attrs, _THREAD_KEYS))
        mcp_server = self._mcp_server(attrs)
        start_dt = _ns_to_dt(span.start_ns)
        end_dt = _ns_to_dt(max(span.end_ns, span.start_ns))
        failed = span.status_code.upper() == "ERROR"
        error_message, error_type = self._error(span) if failed else (None, None)
        tags = self._tags(span)

        display = tool_name or agent_name or span.name
        req_messages, resp_messages = self._messages(attrs, kind)
        tokens_in = _as_int(_first(attrs, _IN_TOKEN_KEYS))
        tokens_out = _as_int(_first(attrs, _OUT_TOKEN_KEYS))
        cache_read = _as_int(_first(attrs, _CACHE_READ_KEYS))
        cache_write = _as_int(_first(attrs, _CACHE_WRITE_KEYS))
        model = _str_or_none(_first(attrs, _MODEL_KEYS))
        provider = _str_or_none(_first(attrs, _PROVIDER_KEYS))
        if kind in (_LLM_CHAT, _LLM_TEXT) and model and not agent_name:
            agent_name = model

        events: list[RawEvent] = []
        if self._first_seen(trace_id):
            events.append(
                RawEvent(
                    event_id=f"{trace_id}-run_start",
                    event_type=EventType.RUN_START,
                    run_id=trace_id,
                    parent_run_id=None,
                    root_run_id=trace_id,
                    timestamp=start_dt,
                    agent_name=None,
                    summary=self._clip(f"RUN_START {trace_id}"),
                    full_blob_eligible=False,
                    raw_payload={"_source": SOURCE_MARKER, "resource": dict(span.resource or {})},
                    tags=tags,
                    thread_id=thread_id,
                )
            )

        parent_run_id = trace_id if span.is_root else span.parent_span_id
        span_info = {
            "name": span.name,
            "kind": span.kind,
            "trace_id": trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "scope": span.scope_name,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
        }
        start_payload: dict[str, Any] = {
            "_source": SOURCE_MARKER,
            "span": span_info,
            "attributes": dict(attrs),
            "resource": dict(span.resource or {}),
        }
        if req_messages is not None:
            start_payload["messages"] = req_messages
        events.append(
            RawEvent(
                event_id=f"{span.span_id}-start",
                event_type=kind.start,
                run_id=span.span_id,
                parent_run_id=parent_run_id,
                root_run_id=trace_id,
                timestamp=start_dt,
                agent_name=agent_name,
                tool_name=tool_name,
                mcp_server=mcp_server,
                model=model,
                provider=provider,
                summary=self._clip(f"{display}: input={_text_of(req_messages) if req_messages is not None else ''}"),
                full_blob_eligible=kind.start in BLOB_ELIGIBLE_EVENTS,
                raw_payload=start_payload,
                tags=tags,
                thread_id=thread_id,
            )
        )

        end_type = kind.error if failed else kind.end
        end_payload: dict[str, Any] = {
            "_source": SOURCE_MARKER,
            "span": span_info,
            "attributes": dict(attrs),
            "events": list(span.events or []),
            "status": {"code": span.status_code, "message": span.status_message},
        }
        if resp_messages is not None:
            end_payload["messages"] = resp_messages
        if failed:
            end_payload["error"] = error_message
            if error_type:
                end_payload["type"] = error_type
            summary = f"{display} error: {error_message or 'error'}"
        else:
            out = _text_of(resp_messages) if resp_messages is not None else ""
            summary = f"{display} end: output={out}"
        events.append(
            RawEvent(
                event_id=f"{span.span_id}-end",
                event_type=end_type,
                run_id=span.span_id,
                parent_run_id=parent_run_id,
                root_run_id=trace_id,
                timestamp=end_dt,
                agent_name=agent_name,
                tool_name=tool_name,
                mcp_server=mcp_server,
                model=model,
                provider=provider,
                summary=self._clip(summary),
                full_blob_eligible=end_type in BLOB_ELIGIBLE_EVENTS,
                raw_payload=end_payload,
                token_input=tokens_in,
                token_output=tokens_out,
                token_cache_read=cache_read,
                token_cache_write=cache_write,
                error_message=error_message,
                tags=tags,
                thread_id=thread_id,
            )
        )

        if span.is_root:
            events.append(
                RawEvent(
                    event_id=f"{trace_id}-run_end",
                    event_type=EventType.RUN_END,
                    run_id=trace_id,
                    parent_run_id=None,
                    root_run_id=trace_id,
                    timestamp=end_dt,
                    agent_name=None,
                    summary=self._clip(
                        f"RUN_END {display}" + (f" failed: {error_message}" if failed else "")
                    ),
                    full_blob_eligible=False,
                    raw_payload={"_source": SOURCE_MARKER, "root_span": span.span_id},
                    error_message=error_message,
                    tags=tags,
                    thread_id=thread_id,
                )
            )
        return events

    # --------------------------------------------------------------- helpers

    def _first_seen(self, trace_id: str) -> bool:
        with self._lock:
            if trace_id in self._seen:
                self._seen.move_to_end(trace_id)
                return False
            self._seen[trace_id] = None
            while len(self._seen) > self._seen_cap:
                self._seen.popitem(last=False)
            return True

    def _clip(self, text: str) -> str:
        return text if len(text) <= self._summary_max else text[: self._summary_max]

    @staticmethod
    def _names(span: SpanRecord, kind: _Kind, attrs: dict[str, Any]) -> tuple[str | None, str | None]:
        remainder = _name_remainder(span.name)
        if kind.family == "llm":
            model = _str_or_none(_first(attrs, _MODEL_KEYS)) or remainder or span.name
            return model, None
        if kind.family == "tool":
            tool = _str_or_none(_first(attrs, _TOOL_KEYS)) or remainder or span.name
            return None, tool
        if kind.family == "agent":
            return _str_or_none(_first(attrs, _AGENT_KEYS)) or remainder or span.name, None
        if kind.family == "retriever":
            return _str_or_none(_first(attrs, _AGENT_KEYS)) or remainder or span.name, None
        return span.name or "span", None

    @staticmethod
    def _mcp_server(attrs: dict[str, Any]) -> str | None:
        server = _str_or_none(_first(attrs, _MCP_SERVER_KEYS))
        if server:
            return server
        if attrs.get("mcp.method.name") == "tools/call":
            return _str_or_none(attrs.get("server.address")) or "mcp"
        return None

    @staticmethod
    def _error(span: SpanRecord) -> tuple[str, str | None]:
        err_type: str | None = None
        message = _str_or_none(span.status_message)
        for ev in span.events or []:
            if not isinstance(ev, dict) or ev.get("name") != "exception":
                continue
            ev_attrs = ev.get("attributes") or {}
            err_type = err_type or _str_or_none(ev_attrs.get("exception.type"))
            message = message or _str_or_none(ev_attrs.get("exception.message"))
        err_type = err_type or _str_or_none(span.attributes.get("error.type"))
        return (message or err_type or "error")[:4000], err_type

    @staticmethod
    def _tags(span: SpanRecord) -> list[str]:
        tags = [SOURCE_MARKER]
        service = _str_or_none((span.resource or {}).get("service.name"))
        # The SDK default ("unknown_service:python") carries no information.
        if service and not service.startswith("unknown_service"):
            tags.append(service)
        return tags

    @staticmethod
    def _messages(attrs: dict[str, Any], kind: _Kind) -> tuple[Any, Any]:
        """(request-side, response-side) content for the start/end payloads."""
        request: Any = None
        response: Any = None
        if kind.family == "tool":
            request = _maybe_json(
                _first(attrs, ("gen_ai.tool.call.arguments", "tool.parameters", "input.value"))
            )
            response = _maybe_json(_first(attrs, ("gen_ai.tool.call.result", "output.value")))
            return request, response

        inp = _first(attrs, ("gen_ai.input.messages", "gen_ai.prompt"))
        if inp is not None:
            request = _user_message(_maybe_json(inp))
        else:
            request = _openinference_messages(attrs, "llm.input_messages")
            if request is not None:
                request = _user_message(request)
            elif attrs.get("pydantic_ai.all_messages") is not None:
                # Agent-run span: the whole conversation; the prompt is the first user turn.
                request = _user_message(_maybe_json(attrs["pydantic_ai.all_messages"]), first=True)
            else:
                request = _maybe_json(attrs.get("input.value"))
        out = _first(attrs, ("gen_ai.output.messages", "gen_ai.completion"))
        if out is not None:
            response = _maybe_json(out)
        else:
            response = _openinference_messages(attrs, "llm.output_messages")
            if response is None:
                response = _maybe_json(_first(attrs, ("final_result", "output.value")))
        return request, response


def classify(span: SpanRecord) -> _Kind:
    """Pick the tracesage event family for a span (first match wins)."""
    attrs = span.attributes or {}
    op = attrs.get("gen_ai.operation.name")
    if isinstance(op, str) and op in _GENAI_OPS:
        return _GENAI_OPS[op]
    oi_kind = attrs.get("openinference.span.kind")
    if isinstance(oi_kind, str):
        hit = _OPENINFERENCE_KINDS.get(oi_kind.upper())
        if hit is not None:
            return hit
    name = span.name or ""
    if name in _NAME_EXACT:
        return _NAME_EXACT[name]
    for prefix, kind in _NAME_PREFIXES:
        if name.startswith(prefix):
            return kind
    # Any span carrying a model + token usage is an LLM call even without an op name.
    if _first(attrs, _MODEL_KEYS) is not None and (
        _first(attrs, _IN_TOKEN_KEYS) is not None or _first(attrs, _OUT_TOKEN_KEYS) is not None
    ):
        return _LLM_CHAT
    return _CHAIN


def _name_remainder(name: str) -> str | None:
    """``"chat gpt-4o"`` -> ``"gpt-4o"``; a name without a space has no remainder."""
    if not name:
        return None
    _head, sep, rest = name.partition(" ")
    if not sep:
        return None
    return rest.strip() or None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return str(value)
    except Exception:
        return None


__all__ = ["SOURCE_MARKER", "SpanMapper", "SpanRecord", "classify"]
