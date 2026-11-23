"""OpenTelemetry span ingest: turn spans from any OTel-instrumented framework
(Pydantic AI, OpenAI Agents SDK, CrewAI, the OpenAI/Anthropic clients, …) into
tracesage events.

Two entry points feed the same mapper:

- in-process: :func:`install_otel` / :class:`TraceSageSpanExporter` plug into the
  OTel SDK running in the traced process (``tracesage.instrument("pydantic_ai")``);
- cross-process: ``POST /v1/traces`` on the embedded server (OTLP/HTTP).

Only the mapper and the JSON decoder are imported eagerly; the exporter needs the
OTel SDK (``tracesage[otel]``) and is resolved lazily.
"""
from __future__ import annotations

from typing import Any

from tracesage.ingest.mapper import SOURCE_MARKER, SpanMapper, SpanRecord
from tracesage.ingest.otlp import (
    OTLPDecodeError,
    ProtobufUnavailableError,
    decode_json,
    decode_protobuf,
)

__all__ = [
    "SOURCE_MARKER",
    "OTLPDecodeError",
    "ProtobufUnavailableError",
    "SpanMapper",
    "SpanRecord",
    "TraceSageSpanExporter",
    "decode_json",
    "decode_protobuf",
    "install_otel",
    "instrument",
]


def __getattr__(name: str) -> Any:
    if name in ("TraceSageSpanExporter", "install_otel"):
        from tracesage.ingest import exporter

        return getattr(exporter, name)
    if name == "instrument":
        from tracesage.ingest.frameworks import instrument

        return instrument
    raise AttributeError(f"module 'tracesage.ingest' has no attribute {name!r}")
