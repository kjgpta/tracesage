"""LangChain BaseCallbackHandler that emits RawEvents into the tracer queue.

Hard requirement: every method MUST NEVER raise. If anything fails, log to stderr and
return None silently.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import traceback
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tracesage.models import (
    BLOB_ELIGIBLE_EVENTS,
    EventType,
    RawEvent,
)

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as _ie:  # pragma: no cover
    raise ImportError(
        "tracesage requires langchain-core for the LangChain adapter. "
        'Install with: pip install "tracesage[langchain]"'
    ) from _ie

if TYPE_CHECKING:
    from tracesage.tracer import TraceSage

log = logging.getLogger("tracesage.handler")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _traceback_str(error: BaseException, max_len: int = 4000) -> str | None:
    """Best-effort formatted traceback for an exception (for richer error debugging),
    or None if the exception carries no traceback. Never raises."""
    tb = getattr(error, "__traceback__", None)
    if tb is None:
        return None
    try:
        formatted = "".join(traceback.format_exception(type(error), error, tb))
        return _safe_str(formatted, max_len)
    except Exception:
        return None


def _safe_str(obj: Any, max_len: int) -> str:
    try:
        s = obj if isinstance(obj, str) else str(obj)
    except Exception:
        s = "<unprintable>"
    if len(s) > max_len:
        return s[:max_len]
    return s


def _safe_dict(obj: Any) -> dict[str, Any]:
    """Convert any object to a JSON-serializable dict for raw_payload storage.

    Strategy: try Pydantic dump → try dict() → fall back to {"value": str(obj)}.
    The blob serializer also tolerates non-serializable, but we normalize here so
    StoredEvent's raw_payload field accepts cleanly.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        # Verify it round-trips through JSON; if not, stringify problem values
        # (and keys, in case the dict is keyed by non-string objects).
        try:
            json.dumps(obj, default=str)
            return obj
        except Exception:
            try:
                return {str(k): _stringify(v) for k, v in obj.items()}
            except Exception:
                return {"_unstringifiable_dict": f"<{type(obj).__name__}>"}
    # Pydantic model.
    if hasattr(obj, "model_dump"):
        with contextlib.suppress(Exception):
            return obj.model_dump()  # type: ignore[no-any-return]
    if hasattr(obj, "dict") and callable(obj.dict):  # legacy pydantic v1
        with contextlib.suppress(Exception):
            return obj.dict()  # type: ignore[no-any-return]
    return {"value": _stringify(obj)}


def _stringify(value: Any) -> Any:
    """Coerce an arbitrary value into a JSON-friendly form.

    Three-step fallback: keep value if json-encodable, else str(), else a placeholder.
    The third level matters for objects whose ``__repr__``/``__str__`` raise, which
    LangChain users hit with custom Pydantic models or arbitrary tool args.
    """
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        try:
            return str(value)
        except Exception:
            return f"<unstringifiable {type(value).__name__}>"


def _extract_name(
    serialized: dict | None,
    kwargs: dict | None = None,
    default: str | None = None,
) -> str | None:
    """Extract the human-readable name for a chain/agent/tool/retriever event.

    LangGraph passes the node name via ``kwargs["name"]`` rather than serialized,
    so we check kwargs first. AgentExecutor + LCEL place it in ``serialized["name"]``
    or as the last element of ``serialized["id"]``.
    """
    if kwargs:
        name = kwargs.get("name")
        if name:
            return str(name)
    if serialized:
        name = serialized.get("name")
        if name:
            return str(name)
        ident = serialized.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1])
    return default


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion that never raises (a bad value yields None rather
    than discarding the complementary count)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present(d: dict[str, Any], *keys: str) -> Any:
    """First value present (not None) among `keys`.

    Uses an explicit ``is not None`` test rather than ``a or b`` so a legitimate
    ``0`` (e.g. a cached/empty prompt with ``prompt_tokens == 0``) is preserved
    instead of being treated as falsy and dropped.
    """
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _extract_token_usage(response: Any) -> tuple[int | None, int | None]:
    """Pull (token_input, token_output) from an LLMResult-like object.

    Fills the input and output counts INDEPENDENTLY, taking the first non-None for
    each across locations: (1) generation message usage_metadata (modern, all
    providers), (2) legacy llm_output token_usage/usage, (3) message
    response_metadata. This merges complementary counts that live in different
    places rather than returning early with a partial pair.
    """
    ti: Any = None
    to: Any = None
    try:
        gens = getattr(response, "generations", None) or []

        # 1. usage_metadata on generation messages (canonical in modern langchain).
        for batch in gens:
            for gen in (batch or []):
                msg = getattr(gen, "message", None)
                um = getattr(msg, "usage_metadata", None) if msg is not None else None
                if um:
                    if ti is None:
                        ti = um.get("input_tokens")
                    if to is None:
                        to = um.get("output_tokens")

        # 2. Legacy aggregate location.
        if ti is None or to is None:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            if ti is None:
                ti = _first_present(usage, "prompt_tokens", "input_tokens")
            if to is None:
                to = _first_present(usage, "completion_tokens", "output_tokens")

        # 3. response_metadata token_usage on a generation message.
        if ti is None or to is None:
            for batch in gens:
                for gen in (batch or []):
                    msg = getattr(gen, "message", None)
                    rm = getattr(msg, "response_metadata", None) if msg is not None else None
                    if rm:
                        u = rm.get("token_usage") or rm.get("usage") or {}
                        if ti is None:
                            ti = _first_present(u, "prompt_tokens", "input_tokens")
                        if to is None:
                            to = _first_present(u, "completion_tokens", "output_tokens")

        return (_coerce_int(ti), _coerce_int(to))
    except Exception:
        return (None, None)


def _llm_response_text(response: Any) -> str:
    """Best-effort first-generation text for summary."""
    with contextlib.suppress(Exception):
        gens = getattr(response, "generations", None)
        if gens and gens[0]:
            first = gens[0][0]
            text = getattr(first, "text", None)
            if text:
                return str(text)
            msg = getattr(first, "message", None)
            if msg is not None:
                return str(getattr(msg, "content", msg))
    return ""


class TraceSageCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler. Every method is wrapped in try/except and never raises."""

    # LangChain checks these flags to enable token streaming etc; defaults are fine.
    raise_error: bool = False

    def __init__(self, tracer: TraceSage) -> None:
        super().__init__()
        self._tracer = tracer
        # Callbacks fire on arbitrary executor threads, so the per-run caches
        # below are guarded by a lock around every read-modify-write (trim,
        # insert, pop) — mirroring tracer._state_lock. Without it a trim racing
        # an insert can evict a still-active run's state.
        self._cache_lock = threading.Lock()
        # run_id → name cache so *_end events can recover the name set at *_start.
        # Capped to avoid unbounded growth in long-lived processes.
        self._names: dict[str, str] = {}
        self._names_cap = 50_000
        # run_id → {"first_ts": datetime, "count": int} for streaming token state.
        # We capture only the first token (for TTFT) and accumulate a count,
        # then surface both on on_llm_end. Per-token events are NOT emitted —
        # that would flood the queue at production scale.
        self._token_state: dict[str, dict[str, Any]] = {}
        self._token_state_cap = 50_000
        # run_id → llm/chat start timestamp, for true TTFT (first-token - start).
        # Bounded like the other caches; always popped on on_llm_end/on_llm_error.
        self._llm_start_ts: dict[str, datetime] = {}
        self._llm_start_ts_cap = 50_000

    def _remember_name(self, run_id: str, name: str | None) -> None:
        if name is None:
            return
        with self._cache_lock:
            if len(self._names) >= self._names_cap:
                # Trim oldest half (set is unordered but bounded — acceptable for v0.1).
                keys = list(self._names.keys())
                for k in keys[: self._names_cap // 2]:
                    self._names.pop(k, None)
            self._names[run_id] = name

    def _recall_name(self, run_id: str) -> str | None:
        with self._cache_lock:
            return self._names.pop(run_id, None)

    def _record_token(self, run_id: str) -> None:
        with self._cache_lock:
            st = self._token_state.get(run_id)
            if st is None:
                if len(self._token_state) >= self._token_state_cap:
                    # Trim oldest half — bounded for long-running processes.
                    keys = list(self._token_state.keys())
                    for k in keys[: self._token_state_cap // 2]:
                        self._token_state.pop(k, None)
                self._token_state[run_id] = {"first_ts": _utcnow(), "count": 1}
            else:
                st["count"] += 1

    def _consume_token_state(self, run_id: str) -> dict[str, Any] | None:
        with self._cache_lock:
            return self._token_state.pop(run_id, None)

    def _record_llm_start(self, run_id: str, ts: datetime) -> None:
        with self._cache_lock:
            if len(self._llm_start_ts) >= self._llm_start_ts_cap:
                # Trim oldest half — bounded for long-running processes.
                keys = list(self._llm_start_ts.keys())
                for k in keys[: self._llm_start_ts_cap // 2]:
                    self._llm_start_ts.pop(k, None)
            self._llm_start_ts[run_id] = ts

    def _consume_llm_start(self, run_id: str) -> datetime | None:
        with self._cache_lock:
            return self._llm_start_ts.pop(run_id, None)

    # ------------------------------------------------------------------ chain

    def on_chain_start(
        self,
        serialized: dict | None,
        inputs: dict | Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            agent_name = _extract_name(serialized, kwargs)
            self._remember_name(run_id_s, agent_name)
            max_chars = self._tracer._config.summary_max_chars
            ts = _utcnow()
            tag_list = list(tags) if tags else []

            # Synthetic RUN_START for root chain_start.
            if parent_s is None:
                run_start = RawEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=EventType.RUN_START,
                    run_id=run_id_s,
                    parent_run_id=None,
                    root_run_id=root,
                    timestamp=ts,
                    agent_name=agent_name,
                    summary=_safe_str(f"RUN_START {agent_name or run_id_s}", max_chars),
                    full_blob_eligible=False,
                    raw_payload={"inputs": _safe_dict(inputs), "tags": tag_list},
                    tags=tag_list,
                )
                self._tracer.emit(run_start)

            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.CHAIN_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=ts,
                agent_name=agent_name,
                summary=_safe_str(
                    f"{agent_name or 'chain'}: input={_safe_str(inputs, 200)}",
                    max_chars,
                ),
                full_blob_eligible=EventType.CHAIN_START in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"inputs": _safe_dict(inputs), "serialized": _safe_dict(serialized)},
                tags=tag_list,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_chain_start error: %s", e, exc_info=True)
            return None

    def on_chain_end(
        self,
        outputs: dict | Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            agent_name = self._recall_name(run_id_s)
            max_chars = self._tracer._config.summary_max_chars
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.CHAIN_END,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                agent_name=agent_name,
                summary=_safe_str(
                    f"{agent_name or 'chain'} end: output={_safe_str(outputs, 200)}",
                    max_chars,
                ),
                full_blob_eligible=EventType.CHAIN_END in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"outputs": _safe_dict(outputs)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_chain_end error: %s", e, exc_info=True)
            return None

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            agent_name = self._recall_name(run_id_s)
            max_chars = self._tracer._config.summary_max_chars
            err_text = _safe_str(error, 400)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.CHAIN_ERROR,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                agent_name=agent_name,
                summary=_safe_str(
                    f"{agent_name or 'chain'} ERROR: {err_text}", max_chars
                ),
                # Error events are blob-eligible so the full traceback (below) is
                # retrievable in the UI drawer / /full endpoint for debugging.
                full_blob_eligible=True,
                raw_payload={
                    "error": err_text,
                    "type": type(error).__name__,
                    "traceback": _traceback_str(error),
                },
                error_message=err_text,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_chain_error error: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ agent

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            tool_name = getattr(action, "tool", None)
            tool_input = getattr(action, "tool_input", None)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.AGENT_ACTION,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                tool_name=tool_name,
                summary=_safe_str(
                    f"-> {tool_name}: {_safe_str(tool_input, 200)}", max_chars
                ),
                full_blob_eligible=False,
                raw_payload={"tool": tool_name, "tool_input": _stringify(tool_input)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_agent_action error: %s", e, exc_info=True)
            return None

    def on_agent_finish(
        self,
        finish: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            return_values = getattr(finish, "return_values", None) or {}
            log_field = getattr(finish, "log", "")
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.AGENT_FINISH,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                summary=_safe_str(f"done: {_safe_str(return_values, 300)}", max_chars),
                full_blob_eligible=EventType.AGENT_FINISH in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"return_values": _safe_dict(return_values), "log": _safe_str(log_field, 1000)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_agent_finish error: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ tool

    def _resolve_tool_source(
        self, tool_name: str | None, serialized: dict | None, kwargs: dict
    ) -> str | None:
        """Best-effort MCP-server attribution for a tool. Registry (explicit, reliable)
        first, then tool metadata (``mcp_server_name``). Never raises."""
        try:
            registered = self._tracer.tool_source(tool_name)
            if registered:
                return registered
            metas = [kwargs.get("metadata")]
            if isinstance(serialized, dict):
                metas.append(serialized.get("metadata"))
            for meta in metas:
                if isinstance(meta, dict):
                    server = meta.get("mcp_server_name") or meta.get("mcp_server")
                    if server:
                        return str(server)
        except Exception:
            return None
        return None

    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            tool_name = _extract_name(serialized, kwargs)
            # Cache for on_tool_end / on_tool_error which may not get the name
            # via kwargs from every LangChain version.
            self._remember_name(run_id_s, tool_name)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.TOOL_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                tool_name=tool_name,
                mcp_server=self._resolve_tool_source(tool_name, serialized, kwargs),
                summary=_safe_str(
                    f"{tool_name}({_safe_str(input_str, 300)})", max_chars
                ),
                full_blob_eligible=EventType.TOOL_START in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"input": _safe_str(input_str, 10000), "serialized": _safe_dict(serialized)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_tool_start error: %s", e, exc_info=True)
            return None

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            # Always pop the cached name so the entry is freed even if kwargs
            # already has it (some LangChain versions populate kwargs["name"],
            # others don't — we fall back to the cache).
            cached = self._recall_name(run_id_s)
            tool_name = kwargs.get("name") or cached
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.TOOL_END,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                tool_name=tool_name,
                mcp_server=self._resolve_tool_source(tool_name, None, kwargs),
                summary=_safe_str(
                    f"{tool_name or 'tool'} -> {_safe_str(output, 300)}", max_chars
                ),
                full_blob_eligible=EventType.TOOL_END in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"output": _stringify(output)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_tool_end error: %s", e, exc_info=True)
            return None

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            err_text = _safe_str(error, 400)
            cached = self._recall_name(run_id_s)
            tool_name = kwargs.get("name") or cached
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.TOOL_ERROR,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                tool_name=tool_name,
                mcp_server=self._resolve_tool_source(tool_name, None, kwargs),
                summary=_safe_str(
                    f"{tool_name or 'tool'} ERROR: {err_text}", max_chars
                ),
                # Error events are blob-eligible so the full traceback (below) is
                # retrievable in the UI drawer / /full endpoint for debugging.
                full_blob_eligible=True,
                raw_payload={
                    "error": err_text,
                    "type": type(error).__name__,
                    "traceback": _traceback_str(error),
                },
                error_message=err_text,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_tool_error error: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ llm

    def on_llm_start(
        self,
        serialized: dict | None,
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            agent_name = _extract_name(serialized)
            num_prompts = len(prompts) if prompts else 0
            last = _safe_str(prompts[-1], 100) if prompts else ""
            ts = _utcnow()
            self._record_llm_start(run_id_s, ts)
            # Carry the model name to the matching on_llm_end (which has no
            # serialized info) so the end event — and its token usage — is
            # attributed to the same node.
            self._remember_name(run_id_s, agent_name)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.LLM_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=ts,
                agent_name=agent_name,
                summary=_safe_str(
                    f"LLM prompt ({num_prompts} prompts, last: {last})", max_chars
                ),
                full_blob_eligible=EventType.LLM_START in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"prompts": list(prompts) if prompts else [], "serialized": _safe_dict(serialized)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_llm_start error: %s", e, exc_info=True)
            return None

    def on_chat_model_start(
        self,
        serialized: dict | None,
        messages: list[Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Chat models hit this method, NOT on_llm_start."""
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            agent_name = _extract_name(serialized)
            # messages is a list[list[BaseMessage]] (one inner list per prompt).
            num = 0
            last_content = ""
            with contextlib.suppress(Exception):
                if messages:
                    flat = messages[0] if isinstance(messages[0], list) else messages
                    num = len(flat)
                    if flat:
                        last = flat[-1]
                        last_content = _safe_str(getattr(last, "content", last), 100)
            ts = _utcnow()
            self._record_llm_start(run_id_s, ts)
            # Carry the model name to the matching on_llm_end (see on_llm_start).
            self._remember_name(run_id_s, agent_name)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.CHAT_MODEL_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=ts,
                agent_name=agent_name,
                summary=_safe_str(
                    f"Chat ({num} messages, last: {last_content})", max_chars
                ),
                full_blob_eligible=EventType.CHAT_MODEL_START in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"messages": _safe_dict({"messages": _stringify(messages)})},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_chat_model_start error: %s", e, exc_info=True)
            return None

    def on_llm_new_token(
        self,
        token: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Per-token callback — used to compute TTFT and stream length.

        We deliberately do NOT emit an event per token (that would flood the
        queue at production scale). Instead, we record the first token's
        timestamp and accumulate a count in a bounded in-memory map, then
        surface both on the existing on_llm_end event.
        """
        try:
            del token, parent_run_id, kwargs
            self._record_token(str(run_id))
        except Exception as e:  # pragma: no cover
            log.error("on_llm_new_token error: %s", e, exc_info=True)
            return None

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            # Recall the model name stashed by on_llm_start/on_chat_model_start so
            # this event (which carries token usage) is attributed to the LLM node.
            agent_name = self._recall_name(run_id_s)
            ti, to = _extract_token_usage(response)
            text = _safe_str(_llm_response_text(response), 200)
            tokens_label = (ti or 0) + (to or 0)

            # Streaming telemetry: pop any first-token state and compute
            # time-to-first-token (TTFT) plus streamed token count.
            tok_state = self._consume_token_state(run_id_s)
            # Always pop the start-ts so this cache cannot grow unboundedly.
            start_ts = self._consume_llm_start(run_id_s)
            ttft_ms: int | None = None
            streamed_tokens: int | None = None
            ts_now = _utcnow()
            stream_duration_ms: int | None = None
            if tok_state:
                streamed_tokens = int(tok_state.get("count") or 0)
                first_ts = tok_state.get("first_ts")
                if first_ts is not None:
                    if start_ts is not None:
                        # True TTFT: llm_start timestamp → first streamed token.
                        ttft_ms = max(0, int((first_ts - start_ts).total_seconds() * 1000))
                    # Stream duration (first-token to end).
                    stream_duration_ms = max(0, int((ts_now - first_ts).total_seconds() * 1000))

            summary_parts = [f"LLM response ({tokens_label} tokens): {text}"]
            if streamed_tokens is not None:
                extras = [f"streamed={streamed_tokens}"]
                if stream_duration_ms is not None:
                    extras.append(f"stream_dur={stream_duration_ms}ms")
                    if streamed_tokens > 0:
                        tps = streamed_tokens * 1000.0 / max(stream_duration_ms, 1)
                        extras.append(f"tps={tps:.1f}")
                summary_parts.append(" [" + " ".join(extras) + "]")
            summary_text = "".join(summary_parts)

            payload = _safe_dict(response)
            if streamed_tokens is not None:
                payload["_stream"] = {
                    "streamed_token_count": streamed_tokens,
                    "first_token_ts": (
                        tok_state["first_ts"].isoformat()
                        if tok_state and tok_state.get("first_ts") else None
                    ),
                    "stream_duration_ms": stream_duration_ms,
                    "ttft_ms": ttft_ms,
                }

            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.LLM_END,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=ts_now,
                agent_name=agent_name,
                summary=_safe_str(summary_text, max_chars),
                full_blob_eligible=EventType.LLM_END in BLOB_ELIGIBLE_EVENTS,
                raw_payload=payload,
                token_input=ti,
                # If the model didn't report token usage but we counted streamed
                # tokens, surface the stream count as the output-token count.
                token_output=to if to is not None else streamed_tokens,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_llm_end error: %s", e, exc_info=True)
            return None

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            err_text = _safe_str(error, 400)
            # Free any pending start-ts / token state so the caches cannot leak
            # on a failed LLM call (no on_llm_end fires in that case). Recall the
            # name too — both to attribute the error to its LLM node and to free
            # the name cache.
            agent_name = self._recall_name(run_id_s)
            self._consume_llm_start(run_id_s)
            self._consume_token_state(run_id_s)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.LLM_ERROR,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                agent_name=agent_name,
                summary=_safe_str(f"ERROR: {err_text}", max_chars),
                # Error events are blob-eligible so the full traceback (below) is
                # retrievable in the UI drawer / /full endpoint for debugging.
                full_blob_eligible=True,
                raw_payload={
                    "error": err_text,
                    "type": type(error).__name__,
                    "traceback": _traceback_str(error),
                },
                error_message=err_text,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_llm_error error: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ retry

    def on_retry(
        self,
        retry_state: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """LangChain fires this when a runnable retries (backoff / transient error).

        Informational only: no agent_name / tool_name, so it produces no topology
        node. Best-effort attempt info is pulled from the retry_state.
        """
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            # Defensive: a hostile retry_state (e.g. an `outcome`/`attempt_number`
            # property that raises) must NOT drop the RETRY event. getattr's default
            # only catches AttributeError, so guard the access itself.
            attempt = None
            with contextlib.suppress(Exception):
                attempt = getattr(retry_state, "attempt_number", None)
            outcome = None
            with contextlib.suppress(Exception):
                outcome = getattr(retry_state, "outcome", None)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RETRY,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                summary=_safe_str(f"retry attempt {attempt}", max_chars),
                full_blob_eligible=False,
                raw_payload={
                    "attempt": attempt,
                    "outcome": _safe_str(outcome, 300) if outcome is not None else None,
                    "info": _safe_str(retry_state, 300),
                },
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_retry error: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ retriever

    def on_retriever_start(
        self,
        serialized: dict | None,
        query: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            name = _extract_name(serialized, kwargs, default="Retriever")
            self._remember_name(run_id_s, name)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RETRIEVER_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                agent_name=name,
                summary=_safe_str(f"{name}({_safe_str(query, 200)})", max_chars),
                full_blob_eligible=EventType.RETRIEVER_START in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"query": _safe_str(query, 10000), "serialized": _safe_dict(serialized)},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_retriever_start error: %s", e, exc_info=True)
            return None

    def on_retriever_end(
        self,
        documents: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            agent_name = self._recall_name(run_id_s)
            max_chars = self._tracer._config.summary_max_chars
            try:
                count = len(documents) if documents is not None else 0
            except Exception:
                count = 0
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RETRIEVER_END,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                agent_name=agent_name,
                timestamp=_utcnow(),
                summary=_safe_str(
                    f"{agent_name or 'retriever'} returned {count} docs", max_chars
                ),
                full_blob_eligible=EventType.RETRIEVER_END in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"documents": _stringify(documents), "count": count},
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_retriever_end error: %s", e, exc_info=True)
            return None

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            max_chars = self._tracer._config.summary_max_chars
            err_text = _safe_str(error, 400)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RETRIEVER_ERROR,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=_utcnow(),
                summary=_safe_str(f"ERROR: {err_text}", max_chars),
                # Error events are blob-eligible so the full traceback (below) is
                # retrievable in the UI drawer / /full endpoint for debugging.
                full_blob_eligible=True,
                raw_payload={
                    "error": err_text,
                    "type": type(error).__name__,
                    "traceback": _traceback_str(error),
                },
                error_message=err_text,
            )
            self._tracer.emit(event)
        except Exception as e:
            log.error("on_retriever_error error: %s", e, exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# Global handler registration
#
# Lets users opt every LangChain call into tracing without threading
# `callbacks=[handler]` through each invocation. Backed by langchain-core's
# configure-hook mechanism: a hook reads a ContextVar each time a callback
# manager is configured and adds the handler (inheritably, so nested runs are
# captured too). The hook is registered at most once; the active handler lives
# in the ContextVar and is cleared by uninstall.
# --------------------------------------------------------------------------- #

_GLOBAL_HANDLER_VAR: ContextVar[BaseCallbackHandler | None] = ContextVar(
    "tracesage_global_handler", default=None
)
_global_hook_registered = False
_global_hook_lock = threading.Lock()


def install_global_handler(handler: BaseCallbackHandler) -> None:
    """Register `handler` as a global LangChain callback (idempotent).

    After this, every chain/agent/LLM/tool invocation is captured even without an
    explicit ``callbacks=`` argument. Call :func:`uninstall_global_handler` to stop.
    """
    global _global_hook_registered
    with _global_hook_lock:
        if not _global_hook_registered:
            # register_configure_hook lives in langchain_core.tracers.context.
            from langchain_core.tracers.context import register_configure_hook

            register_configure_hook(_GLOBAL_HANDLER_VAR, True)  # inheritable
            _global_hook_registered = True
    _GLOBAL_HANDLER_VAR.set(handler)


def uninstall_global_handler() -> None:
    """Clear the globally-registered tracesage handler (the hook itself stays
    registered but becomes a no-op once the ContextVar holds None)."""
    _GLOBAL_HANDLER_VAR.set(None)
