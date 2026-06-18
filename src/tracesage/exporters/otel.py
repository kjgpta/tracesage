"""Optional OpenTelemetry (OTLP) span export.

Translates tracesage's `StoredEvent` stream into OpenTelemetry spans and ships them
to an OTLP endpoint (an OTel Collector, Grafana Tempo, Jaeger, Datadog, Honeycomb,
Arize/Phoenix, …). This is the bridge from tracesage's local dev view to a
production observability backend — the same trace data, in the vendor-neutral
standard format.

Span model (tracesage is already span-shaped):
    root_run_id  -> trace (spans created under the root's span share its trace)
    run_id       -> span
    parent_run_id-> parent span
    *_start event-> span opened (start_time = event timestamp)
    *_end/_error -> span closed (end_time = event timestamp; status set)

Safety: this is BEST-EFFORT and must NEVER break the host application. The OTel
libraries are an optional extra (`pip install "tracesage[otel]"`); if they are
missing, or the collector is unreachable, ingestion continues and the caller's
program is unaffected. Every public method swallows its own exceptions.
"""
from __future__ import annotations

import contextlib
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracesage.models import StoredEvent

log = logging.getLogger("tracesage.otel")

# Event types that OPEN a span (request side) and those that CLOSE one (response).
_START_TYPES = frozenset(
    {
        "run_start", "chain_start", "tool_start", "llm_start",
        "chat_model_start", "retriever_start", "agent_action",
    }
)
_END_OK_TYPES = frozenset(
    {"run_end", "chain_end", "tool_end", "llm_end", "retriever_end", "agent_finish"}
)
_END_ERR_TYPES = frozenset(
    {"chain_error", "tool_error", "llm_error", "retriever_error"}
)


def _kind_of(event_type: str) -> str:
    if event_type.startswith(("llm", "chat_model")):
        return "llm"
    if event_type.startswith("retriever"):
        return "retriever"
    if event_type.startswith("tool"):
        return "tool"
    if event_type.startswith("agent"):
        return "agent"
    if event_type.startswith("run"):
        return "run"
    return "chain"


def _ts_ns(ev: StoredEvent) -> int:
    return int(ev.timestamp.timestamp() * 1_000_000_000)


class OTelSpanExporter:
    """Forwards StoredEvents to OTLP as spans. Construct via `from_config` / the
    tracer; `span_exporter` is an injection point for tests."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        service_name: str = "tracesage",
        headers: dict[str, str] | None = None,
        span_exporter: Any = None,
        max_active: int = 50_000,
    ) -> None:
        # Imported lazily so `import tracesage` never requires the OTel libraries.
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        from opentelemetry.trace import Status, StatusCode, set_span_in_context

        self._trace = trace
        self._Status = Status
        self._StatusCode = StatusCode
        self._set_span_in_context = set_span_in_context
        self._max_active = max_active
        # run_id -> live (un-ended) span. Bounded; evicting ends the span so it is
        # still exported rather than leaked.
        self._spans: OrderedDict[str, Any] = OrderedDict()

        self._provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        if span_exporter is not None:
            # Test / custom path: synchronous processor for deterministic flushing.
            self._provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            self._provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=self._normalize(endpoint), headers=headers or {})
                )
            )
        self._tracer = self._provider.get_tracer("tracesage")

    @staticmethod
    def _normalize(endpoint: str | None) -> str | None:
        # The HTTP exporter wants the full traces URL; accept a base URL too.
        if endpoint and not endpoint.rstrip("/").endswith("/v1/traces"):
            return endpoint.rstrip("/") + "/v1/traces"
        return endpoint

    def handle(self, ev: StoredEvent) -> None:
        """Translate one stored event into a span open/close. Never raises."""
        try:
            et = ev.event_type.value
            if et in _START_TYPES:
                self._open(ev, et)
            elif et in _END_OK_TYPES:
                self._close(ev, error=False)
            elif et in _END_ERR_TYPES:
                self._close(ev, error=True)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("otel handle failed for %s: %s", getattr(ev, "event_id", "?"), e)

    def _open(self, ev: StoredEvent, et: str) -> None:
        run_id = ev.run_id
        if run_id in self._spans:
            return  # first start for a run_id wins (e.g. run_start before chain_start)
        kind = _kind_of(et)
        parent = self._spans.get(ev.parent_run_id) if ev.parent_run_id else None
        ctx = self._set_span_in_context(parent) if parent is not None else None
        name = f"{kind} {ev.agent_name or ev.tool_name or kind}"
        span = self._tracer.start_span(name, context=ctx, start_time=_ts_ns(ev))
        span.set_attribute("tracesage.kind", kind)
        span.set_attribute("tracesage.run_id", run_id)
        if ev.agent_name:
            span.set_attribute("tracesage.agent", ev.agent_name)
        if ev.tool_name:
            span.set_attribute("tracesage.tool", ev.tool_name)
        if ev.mcp_server:
            span.set_attribute("tracesage.mcp_server", ev.mcp_server)
        self._spans[run_id] = span
        self._evict()

    def _close(self, ev: StoredEvent, *, error: bool) -> None:
        span = self._spans.pop(ev.run_id, None)
        if span is None:
            return
        if ev.token_input is not None:
            span.set_attribute("gen_ai.usage.input_tokens", ev.token_input)
        if ev.token_output is not None:
            span.set_attribute("gen_ai.usage.output_tokens", ev.token_output)
        if error:
            span.set_attribute("tracesage.error", ev.error_message or "error")
            span.set_status(self._Status(self._StatusCode.ERROR, ev.error_message or "error"))
        else:
            span.set_status(self._Status(self._StatusCode.OK))
        span.end(end_time=_ts_ns(ev))

    def _evict(self) -> None:
        # Bound memory: if a run started but never ended, end+export the oldest so
        # the map can't grow without limit.
        while len(self._spans) > self._max_active:
            _, span = self._spans.popitem(last=False)
            with contextlib.suppress(Exception):  # pragma: no cover
                span.end()

    def shutdown(self) -> None:
        """End any dangling spans and flush the exporter. Never raises."""
        try:
            for span in self._spans.values():
                with contextlib.suppress(Exception):  # pragma: no cover
                    span.end()
            self._spans.clear()
            self._provider.shutdown()
        except Exception as e:  # pragma: no cover
            log.debug("otel shutdown failed: %s", e)
