"""Manual tracing API: ``tracer.span(...)`` and ``@tracer.trace_fn(...)``.

For code that is neither LangChain nor OpenTelemetry-instrumented — a hand-rolled
agent loop, a raw HTTP call to a model, a retrieval step — open a span around it
and it shows up in the same timeline, graph and CLI as everything else::

    with tl.span("checkout", kind="agent", inputs={"cart": cart_id}) as root:
        with tl.span("lookup_prices", kind="tool") as sp:
            prices = lookup(cart_id)
            sp.set_output(prices)
        root.set_output("done")

Spans nest through a ``contextvars.ContextVar`` (so ``async with`` and threads
that copy the context behave), the outermost span is the run, and an exception
leaving the block records a ``*_error`` event and fails the run. LangChain
callbacks fired inside an active manual span nest under it.

Every event goes through ``TraceSage.emit`` (sampling, per-run cap, redaction),
and none of these methods can raise into user code.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
import traceback
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from tracesage.models import BLOB_ELIGIBLE_EVENTS, EventType, RawEvent

log = logging.getLogger("tracesage.manual")

SOURCE_MARKER = "manual"

_current: ContextVar[Span | None] = ContextVar("tracesage_current_span", default=None)

# kind -> (start, end, error, names the span by agent_name?)
_KINDS: dict[str, tuple[EventType, EventType, EventType]] = {
    "chain": (EventType.CHAIN_START, EventType.CHAIN_END, EventType.CHAIN_ERROR),
    "agent": (EventType.CHAIN_START, EventType.CHAIN_END, EventType.CHAIN_ERROR),
    "tool": (EventType.TOOL_START, EventType.TOOL_END, EventType.TOOL_ERROR),
    "llm": (EventType.CHAT_MODEL_START, EventType.LLM_END, EventType.LLM_ERROR),
    "retriever": (
        EventType.RETRIEVER_START,
        EventType.RETRIEVER_END,
        EventType.RETRIEVER_ERROR,
    ),
}
SPAN_KINDS: tuple[str, ...] = tuple(_KINDS)

_PREVIEW = 200


def current_span() -> Span | None:
    """The innermost active manual span in this context, if any."""
    return _current.get()


def current_run_id() -> str | None:
    """run_id of the innermost active manual span (used by the LangChain adapter
    to nest a root callback under it)."""
    span = _current.get()
    return span.run_id if span is not None and span._started and not span._ended else None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    """Coerce to JSON primitives (so redaction can walk it and the blob is clean)."""
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        try:
            return str(value)
        except Exception:
            return f"<unstringifiable {type(value).__name__}>"


def _preview(value: Any, limit: int = _PREVIEW) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        s = str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit]


class Span:
    """One manual step. Returned by ``tracer.span(...)``; use as ``with`` or
    ``async with``. Attribute setters are safe to call any time before the block
    exits; they only affect the end event."""

    __slots__ = (
        "_attributes",
        "_ended",
        "_outputs",
        "_parent",
        "_started",
        "_token",
        "_tracer",
        "inputs",
        "kind",
        "model",
        "name",
        "parent_run_id",
        "provider",
        "root_run_id",
        "run_id",
        "tags",
        "thread_id",
        "token_cache_read",
        "token_cache_write",
        "token_input",
        "token_output",
        "tool_name",
    )

    def __init__(
        self,
        tracer: Any,
        name: str,
        *,
        kind: str = "chain",
        tool_name: str | None = None,
        model: str | None = None,
        thread_id: str | None = None,
        inputs: Any = None,
        tags: list[str] | None = None,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown span kind {kind!r}; expected one of {', '.join(SPAN_KINDS)}")
        self._tracer = tracer  # None -> inert span (disabled tracer)
        self.name = str(name) if name else kind
        self.kind = kind
        self.tool_name = tool_name
        self.model = model
        self.thread_id = thread_id
        self.inputs = inputs
        self.tags = list(tags) if tags else []
        self.run_id = str(uuid.uuid4())
        self.parent_run_id: str | None = None
        self.root_run_id: str = self.run_id
        self.provider: str | None = None
        self.token_input: int | None = None
        self.token_output: int | None = None
        self.token_cache_read: int | None = None
        self.token_cache_write: int | None = None
        self._outputs: Any = None
        self._attributes: dict[str, Any] = {}
        self._parent: Span | None = None
        self._token: Token | None = None
        self._started = False
        self._ended = False

    # ------------------------------------------------------------ setters

    def set_output(self, output: Any) -> Span:
        """Record the step's result (persisted in the end event's payload)."""
        self._outputs = output
        return self

    def set_tokens(
        self,
        input: int | None = None,
        output: int | None = None,
        cache_read: int | None = None,
        cache_write: int | None = None,
    ) -> Span:
        """Record token usage (rolled up into the run's totals)."""
        if input is not None:
            self.token_input = int(input)
        if output is not None:
            self.token_output = int(output)
        if cache_read is not None:
            self.token_cache_read = int(cache_read)
        if cache_write is not None:
            self.token_cache_write = int(cache_write)
        return self

    def set_provider(self, provider: str | None) -> Span:
        """Record the model provider (for example ``openai`` or ``anthropic``)."""
        self.provider = provider or None
        return self

    def set_attributes(self, **attributes: Any) -> Span:
        """Attach arbitrary key/value metadata to the end event."""
        self._attributes.update(attributes)
        return self

    # ---------------------------------------------------------- lifecycle

    @property
    def is_root(self) -> bool:
        return self.parent_run_id is None

    def start(self) -> Span:
        """Open the span (``with`` does this for you). Idempotent."""
        if self._started:
            return self
        self._started = True
        try:
            parent = _current.get()
            if parent is not None and parent._started and not parent._ended:
                self._parent = parent
                self.parent_run_id = parent.run_id
                self.thread_id = self.thread_id or parent.thread_id
            self._token = _current.set(self)
            if self._tracer is None:
                return self
            self.root_run_id = self._tracer.get_or_set_root(self.run_id, self.parent_run_id)
            self._emit_start()
        except Exception as e:  # never raise into user code
            log.error("tracesage span start failed: %s", e, exc_info=True)
        return self

    def end(self, error: BaseException | None = None) -> None:
        """Close the span (``with`` does this for you). Idempotent."""
        if self._ended or not self._started:
            return
        self._ended = True
        try:
            if self._token is not None:
                with contextlib.suppress(ValueError, RuntimeError):
                    _current.reset(self._token)
                self._token = None
            if self._tracer is not None:
                self._emit_end(error)
        except Exception as e:
            log.error("tracesage span end failed: %s", e, exc_info=True)

    def __enter__(self) -> Span:
        return self.start()

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        self.end(exc)

    async def __aenter__(self) -> Span:
        return self.start()

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        self.end(exc)

    # ------------------------------------------------------------ events

    def _names(self) -> tuple[str | None, str | None]:
        if self.kind == "tool":
            return None, self.tool_name or self.name
        if self.kind == "llm":
            return self.model or self.name, None
        return self.name, None

    def _max_chars(self) -> int:
        cfg = getattr(self._tracer, "_config", None)
        return int(getattr(cfg, "summary_max_chars", 500) or 500)

    def _emit_start(self) -> None:
        start_type, _, _ = _KINDS[self.kind]
        agent_name, tool_name = self._names()
        ts = _utcnow()
        max_chars = self._max_chars()
        inputs = _jsonable(self.inputs)
        meta: dict[str, Any] = {"kind": self.kind}
        if self.thread_id:
            meta["thread_id"] = self.thread_id
        if self.model:
            meta["ls_model_name"] = self.model
        if self.is_root:
            self._tracer.emit(
                RawEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=EventType.RUN_START,
                    run_id=self.run_id,
                    parent_run_id=None,
                    root_run_id=self.root_run_id,
                    timestamp=ts,
                    agent_name=agent_name or tool_name,
                    summary=f"RUN_START {self.name}"[:max_chars],
                    raw_payload={"_source": SOURCE_MARKER, "inputs": inputs, "metadata": meta},
                    tags=self.tags,
                    thread_id=self.thread_id,
                )
            )
        preview = f": input={_preview(inputs)}" if inputs is not None else ""
        self._tracer.emit(
            RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=start_type,
                run_id=self.run_id,
                parent_run_id=self.parent_run_id,
                root_run_id=self.root_run_id,
                timestamp=ts,
                agent_name=agent_name,
                tool_name=tool_name,
                mcp_server=self._tracer.tool_source(tool_name) if tool_name else None,
                model=self.model if self.kind == "llm" else None,
                provider=self.provider,
                summary=f"{self.name}{preview}"[:max_chars],
                full_blob_eligible=start_type in BLOB_ELIGIBLE_EVENTS,
                raw_payload={"_source": SOURCE_MARKER, "inputs": inputs, "metadata": meta},
                tags=self.tags,
                thread_id=self.thread_id,
            )
        )

    def _emit_end(self, error: BaseException | None) -> None:
        _, end_type, error_type = _KINDS[self.kind]
        agent_name, tool_name = self._names()
        max_chars = self._max_chars()
        payload: dict[str, Any] = {"_source": SOURCE_MARKER}
        if self._attributes:
            payload["attributes"] = _jsonable(self._attributes)
        if error is not None:
            message = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
            payload["error"] = message
            payload["type"] = type(error).__name__
            tb = getattr(error, "__traceback__", None)
            if tb is not None:
                with contextlib.suppress(Exception):
                    payload["traceback"] = "".join(
                        traceback.format_exception(type(error), error, tb)
                    )[:4000]
            event_type = error_type
            summary = f"{self.name} error: {message}"
        else:
            outputs = _jsonable(self._outputs)
            payload["outputs"] = outputs
            event_type = end_type
            tail = f": output={_preview(outputs)}" if outputs is not None else ""
            summary = f"{self.name} end{tail}"
        self._tracer.emit(
            RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=event_type,
                run_id=self.run_id,
                parent_run_id=self.parent_run_id,
                root_run_id=self.root_run_id,
                timestamp=_utcnow(),
                agent_name=agent_name,
                tool_name=tool_name,
                mcp_server=self._tracer.tool_source(tool_name) if tool_name else None,
                summary=summary[:max_chars],
                full_blob_eligible=event_type in BLOB_ELIGIBLE_EVENTS,
                raw_payload=payload,
                token_input=self.token_input,
                token_output=self.token_output,
                token_cache_read=self.token_cache_read,
                token_cache_write=self.token_cache_write,
                model=self.model if self.kind == "llm" else None,
                provider=self.provider,
                error_message=payload.get("error"),
                tags=self.tags,
                thread_id=self.thread_id,
            )
        )

    def __repr__(self) -> str:
        return f"Span(name={self.name!r}, kind={self.kind!r}, run_id={self.run_id!r})"


def _bound_arguments(fn: Any, args: tuple, kwargs: dict) -> Any:
    """The call's arguments as a name->value dict (positional names resolved), or
    a plain {args, kwargs} when the signature cannot be bound."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": kwargs}


def trace_fn(
    tracer: Any,
    kind: str = "chain",
    name: str | None = None,
    *,
    tool_name: str | None = None,
    model: str | None = None,
    tags: list[str] | None = None,
) -> Any:
    """Decorator factory behind ``tracer.trace_fn``. Wraps sync and async functions
    in a span named after the function, with arguments as inputs and the return
    value as output. ``tracer=None`` returns functions untouched (disabled)."""
    if kind not in _KINDS:
        raise ValueError(f"unknown span kind {kind!r}; expected one of {', '.join(SPAN_KINDS)}")

    def decorate(fn: Any) -> Any:
        if tracer is None:
            return fn
        qualname = str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "fn"))
        # Keep "Class.method", drop "outer.<locals>." prefixes of nested functions.
        span_name = str(name or qualname.rsplit("<locals>.", 1)[-1])

        def _open(args: tuple, kwargs: dict) -> Span:
            return Span(
                tracer,
                span_name,
                kind=kind,
                tool_name=tool_name,
                model=model,
                tags=tags,
                inputs=_bound_arguments(fn, args, kwargs),
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _open(args, kwargs) as sp:
                    result = await fn(*args, **kwargs)
                    sp.set_output(result)
                    return result

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _open(args, kwargs) as sp:
                result = fn(*args, **kwargs)
                sp.set_output(result)
                return result

        return wrapper

    return decorate


__all__ = ["SPAN_KINDS", "Span", "current_run_id", "current_span", "trace_fn"]
