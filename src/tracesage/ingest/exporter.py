"""In-process OpenTelemetry SDK bridge: ``ReadableSpan`` -> tracesage events.

``TraceSageSpanExporter`` is a regular OTel ``SpanExporter``. Plugged into a
``TracerProvider`` (``install_otel`` does that for you) it turns every span the
process produces — Pydantic AI, OpenAI Agents SDK, CrewAI, the OpenAI/Anthropic
instrumentations, or your own ``tracer.start_as_current_span`` — into tracesage
events through :class:`tracesage.ingest.SpanMapper` and ``TraceSage.emit``.

Requires the OTel SDK (``pip install "tracesage[otel]"``); ``tracesage.ingest``
imports this module lazily so the base install stays dependency-free. The
exporter never raises: a bad span is logged and skipped, exactly like the
LangChain callback handler.
"""
from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from tracesage.ingest.mapper import SpanMapper, SpanRecord

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )
except ImportError as _ie:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "The in-process OpenTelemetry bridge needs the OTel SDK: "
        'pip install "tracesage[otel]"'
    ) from _ie

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger("tracesage.ingest")


def span_to_record(span: Any) -> SpanRecord:
    """Convert an OTel SDK ``ReadableSpan`` into a transport-neutral SpanRecord."""
    ctx = span.get_span_context() if hasattr(span, "get_span_context") else span.context
    parent = getattr(span, "parent", None)
    kind = getattr(span, "kind", None)
    status = getattr(span, "status", None)
    status_code = getattr(getattr(status, "status_code", None), "name", "UNSET") or "UNSET"
    resource = getattr(span, "resource", None)
    scope = getattr(span, "instrumentation_scope", None)
    events: list[dict[str, Any]] = []
    for ev in getattr(span, "events", None) or []:
        events.append(
            {
                "name": getattr(ev, "name", None),
                "time_ns": int(getattr(ev, "timestamp", 0) or 0),
                "attributes": dict(getattr(ev, "attributes", None) or {}),
            }
        )
    start_ns = int(getattr(span, "start_time", 0) or 0)
    return SpanRecord(
        trace_id=format(ctx.trace_id, "032x"),
        span_id=format(ctx.span_id, "016x"),
        parent_span_id=format(parent.span_id, "016x") if parent is not None else None,
        name=str(getattr(span, "name", "") or ""),
        start_ns=start_ns,
        end_ns=int(getattr(span, "end_time", None) or start_ns),
        attributes=dict(getattr(span, "attributes", None) or {}),
        kind=getattr(kind, "name", None) or "INTERNAL",
        status_code=status_code,
        status_message=getattr(status, "description", None) or None,
        events=events,
        resource=dict(getattr(resource, "attributes", None) or {}),
        scope_name=getattr(scope, "name", None),
    )


class TraceSageSpanExporter(SpanExporter):
    """OTel ``SpanExporter`` that feeds spans into a tracesage tracer.

    ``tracer`` is anything with a thread-safe ``emit(RawEvent)`` (``TraceSage``,
    ``BackgroundTracer``, the disabled tracer). ``export`` never raises.
    """

    def __init__(self, tracer: Any, *, summary_max_chars: int | None = None) -> None:
        self._tracer = _unwrap(tracer)
        cfg = getattr(self._tracer, "_config", None)
        max_chars = int(summary_max_chars or getattr(cfg, "summary_max_chars", 500) or 500)
        self._mapper = SpanMapper(summary_max_chars=max_chars)

    @property
    def mapper(self) -> SpanMapper:
        return self._mapper

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        for span in spans:
            try:
                for event in self._mapper.map(span_to_record(span)):
                    self._tracer.emit(event)
            except Exception as e:  # one bad span must not break the host app
                log.debug("tracesage otel bridge: skipped span: %s", e)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _unwrap(tracer: Any) -> Any:
    """BackgroundTracer -> its TraceSage (emit hops onto the background loop)."""
    inner = getattr(tracer, "tracer", None)
    return inner if inner is not None and hasattr(inner, "emit") else tracer


def install_otel(tracer: Any, *, set_global: bool = True) -> TracerProvider | None:
    """Wire the OTel SDK in this process to ``tracer``.

    Builds a ``TracerProvider`` with a synchronous processor around
    :class:`TraceSageSpanExporter` and, when ``set_global`` is true, makes it the
    global provider if none has been set yet. If the application already installed
    an SDK provider, the tracesage processor is added to that one instead (both
    destinations then receive spans). Returns the provider carrying the processor,
    or None for a disabled tracer (``TRACESAGE_ENABLED=false``). Idempotent per tracer.
    """
    target = _unwrap(tracer)
    if getattr(getattr(target, "_config", None), "enabled", True) is False:
        return None  # kill switch: wire nothing, touch no global state
    existing = getattr(target, "_otel_ingest_provider", None)
    if existing is not None:
        return existing

    processor = SimpleSpanProcessor(TraceSageSpanExporter(target))
    provider: TracerProvider | None = None
    if set_global:
        current = otel_trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current  # the app already configured the SDK: piggy-back
            provider.add_span_processor(processor)
    if provider is None:
        provider = TracerProvider()
        provider.add_span_processor(processor)
        if set_global:
            # No-op (with an OTel warning) if a non-SDK provider was set first; we
            # still return ours so callers can hand it to instrumentors explicitly.
            otel_trace.set_tracer_provider(provider)
    with contextlib.suppress(Exception):  # exotic tracer objects without __dict__
        target._otel_ingest_provider = provider
    return provider


__all__ = ["TraceSageSpanExporter", "install_otel", "span_to_record"]
