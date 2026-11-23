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

Naming follows the OpenTelemetry GenAI semantic conventions (still "Development"
status upstream): `invoke_agent {name}` for the root run, `chat {model}` /
`text_completion {model}`, `execute_tool {tool}` and `retrieval {name}`, with the
matching `gen_ai.*` attributes. Inner chain/graph-node spans have no GenAI
operation and are named after the node. tracesage-specific detail stays under
`tracesage.*`.

Safety: this is BEST-EFFORT and must NEVER break the host application. The OTel
libraries are an optional extra (`pip install "tracesage[otel]"`); if they are
missing, or the collector is unreachable, ingestion continues and the caller's
program is unaffected. Every public method swallows its own exceptions.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracesage.models import RawEvent, StoredEvent

log = logging.getLogger("tracesage.otel")

# Event types that OPEN a span (request side) and those that CLOSE one (response).
_START_TYPES = frozenset(
    {
        "run_start", "chain_start", "tool_start", "llm_start",
        "chat_model_start", "retriever_start", "agent_action",
    }
)
_END_OK_TYPES = frozenset(
    {
        "run_end", "chain_end", "tool_end", "llm_end", "retriever_end", "agent_finish",
        "chain_interrupt",
    }
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


def _metadata(raw: RawEvent | None) -> dict[str, Any]:
    meta = raw.raw_payload.get("metadata") if raw is not None else None
    return meta if isinstance(meta, dict) else {}


def _span_identity(
    ev: StoredEvent, et: str, kind: str, meta: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """GenAI-semconv span name and attributes for a span-opening event."""
    name = ev.agent_name or ev.tool_name or kind
    attrs: dict[str, Any] = {}
    if et == "chat_model_start" or et == "llm_start":
        op = "chat" if et == "chat_model_start" else "text_completion"
        model = meta.get("ls_model_name")
        attrs["gen_ai.operation.name"] = op
        if model:
            attrs["gen_ai.request.model"] = str(model)
        if meta.get("ls_provider"):
            attrs["gen_ai.provider.name"] = str(meta["ls_provider"])
        return f"{op} {model or name}", attrs
    if et == "tool_start":
        attrs["gen_ai.operation.name"] = "execute_tool"
        attrs["gen_ai.tool.name"] = name
        if ev.mcp_server:
            attrs["mcp.method.name"] = "tools/call"
        return f"execute_tool {name}", attrs
    if et == "retriever_start":
        attrs["gen_ai.operation.name"] = "retrieval"
        return f"retrieval {name}", attrs
    if ev.parent_run_id is None:
        attrs["gen_ai.operation.name"] = "invoke_agent"
        attrs["gen_ai.agent.name"] = name
        return f"invoke_agent {name}", attrs
    return name, attrs


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
        shutdown_timeout_ms: int = 2_000,
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
        self._shutdown_timeout_ms = shutdown_timeout_ms
        # Set only for the OTLP path, so shutdown() can bound the final flush.
        self._batch: Any = None
        self._otlp: Any = None
        if span_exporter is not None:
            # Test / custom path: synchronous processor for deterministic flushing.
            self._provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            self._otlp = OTLPSpanExporter(
                endpoint=self._normalize(endpoint), headers=headers or {}
            )
            self._batch = BatchSpanProcessor(self._otlp)
            self._provider.add_span_processor(self._batch)
        self._tracer = self._provider.get_tracer("tracesage")

    @staticmethod
    def _normalize(endpoint: str | None) -> str | None:
        # The HTTP exporter wants the full traces URL; accept a base URL too.
        if endpoint and not endpoint.rstrip("/").endswith("/v1/traces"):
            return endpoint.rstrip("/") + "/v1/traces"
        return endpoint

    def handle(self, ev: StoredEvent, raw: RawEvent | None = None) -> None:
        """Translate one stored event into a span open/close. `raw` (the in-flight
        event) supplies callback metadata such as the model and thread id. Never raises.

        Events that *arrived* as OpenTelemetry spans (OTLP ingest / the in-process
        bridge, marked ``raw_payload["_source"] == "otlp"``) are not re-exported:
        they already live in the producer's OTel pipeline, and an ``otlp_endpoint``
        pointing back at tracesage would otherwise loop forever."""
        try:
            if raw is not None and raw.raw_payload.get("_source") == "otlp":
                return
            et = ev.event_type.value
            if et in _START_TYPES:
                self._open(ev, et, raw)
            elif et in _END_OK_TYPES:
                self._close(ev, error=False)
            elif et in _END_ERR_TYPES:
                self._close(ev, error=True, raw=raw)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("otel handle failed for %s: %s", getattr(ev, "event_id", "?"), e)

    def _open(self, ev: StoredEvent, et: str, raw: RawEvent | None = None) -> None:
        run_id = ev.run_id
        if run_id in self._spans:
            return  # first start for a run_id wins (e.g. run_start before chain_start)
        kind = _kind_of(et)
        parent = self._spans.get(ev.parent_run_id) if ev.parent_run_id else None
        ctx = self._set_span_in_context(parent) if parent is not None else None
        meta = _metadata(raw)
        name, attrs = _span_identity(ev, et, kind, meta)
        span = self._tracer.start_span(name, context=ctx, start_time=_ts_ns(ev))
        for key, value in attrs.items():
            span.set_attribute(key, value)
        thread_id = (raw.thread_id if raw is not None else None) or meta.get("thread_id")
        if thread_id:
            span.set_attribute("gen_ai.conversation.id", str(thread_id))
        if meta.get("langgraph_node"):
            span.set_attribute("tracesage.langgraph.node", str(meta["langgraph_node"]))
        if meta.get("langgraph_step") is not None:
            span.set_attribute("tracesage.langgraph.step", str(meta["langgraph_step"]))
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

    def _close(self, ev: StoredEvent, *, error: bool, raw: RawEvent | None = None) -> None:
        span = self._spans.pop(ev.run_id, None)
        if span is None:
            return
        if ev.token_input is not None:
            span.set_attribute("gen_ai.usage.input_tokens", ev.token_input)
        if ev.token_output is not None:
            span.set_attribute("gen_ai.usage.output_tokens", ev.token_output)
        if ev.cost_usd is not None:
            span.set_attribute("tracesage.cost_usd", float(ev.cost_usd))
        if error:
            span.set_attribute("tracesage.error", ev.error_message or "error")
            err_type = raw.raw_payload.get("type") if raw is not None else None
            span.set_attribute("error.type", str(err_type or "_OTHER"))
            span.set_status(self._Status(self._StatusCode.ERROR, ev.error_message or "error"))
        else:
            if ev.event_type.value == "chain_interrupt":
                span.set_attribute("tracesage.interrupted", True)
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
        """End any dangling spans and flush the exporter, waiting at most
        `shutdown_timeout_ms` for an unreachable collector. Never raises."""
        try:
            for span in self._spans.values():
                with contextlib.suppress(Exception):  # pragma: no cover
                    span.end()
            self._spans.clear()
            if self._batch is not None:
                # provider.shutdown() alone waits out the exporter's full retry
                # backoff (~8s against a dead endpoint), and BatchSpanProcessor's
                # force_flush ignores its timeout. Flush on a side thread with our own
                # deadline, then shut the exporter down, which aborts the retry wait.
                flusher = threading.Thread(
                    target=self._batch.force_flush, name="tracesage.otel.flush", daemon=True
                )
                flusher.start()
                flusher.join(self._shutdown_timeout_ms / 1000)
                self._otlp.shutdown()
                flusher.join(1.0)
                otlp_log = logging.getLogger("opentelemetry.exporter.otlp.proto.http")
                previous = otlp_log.level
                otlp_log.setLevel(logging.ERROR)  # silence "already shutdown" on the 2nd call
                try:
                    self._provider.shutdown()
                finally:
                    otlp_log.setLevel(previous)
            else:
                self._provider.shutdown()
        except Exception as e:  # pragma: no cover
            log.debug("otel shutdown failed: %s", e)
