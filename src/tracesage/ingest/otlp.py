"""OTLP ``ExportTraceServiceRequest`` decoders -> ``list[SpanRecord]``.

``decode_json`` understands OTLP/JSON (the protobuf JSON mapping: camelCase field
names, hex trace/span ids, ``{"key": ..., "value": {"stringValue": ...}}`` attribute
lists, ``int64`` as strings) with no dependencies. ``decode_protobuf`` parses the
binary encoding via ``opentelemetry-proto`` (part of the ``tracesage[otel]`` extra),
imported lazily so the base install stays free of it.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from tracesage.ingest.mapper import SpanRecord

_SPAN_KINDS = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}
_STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}


class OTLPDecodeError(ValueError):
    """The body is not a valid OTLP traces payload."""


class ProtobufUnavailableError(RuntimeError):
    """``opentelemetry-proto`` is not installed."""


# ----------------------------------------------------------------------- JSON


def _any_value(node: Any) -> Any:
    """Decode an OTLP ``AnyValue`` (``{"stringValue": "x"}`` …) into a Python value.
    Tolerates already-plain values (some exporters emit those)."""
    if not isinstance(node, dict):
        return node
    if "stringValue" in node:
        return node["stringValue"]
    if "boolValue" in node:
        return bool(node["boolValue"])
    if "intValue" in node:
        try:
            return int(node["intValue"])
        except (TypeError, ValueError):
            return node["intValue"]
    if "doubleValue" in node:
        try:
            return float(node["doubleValue"])
        except (TypeError, ValueError):
            return node["doubleValue"]
    if "arrayValue" in node:
        values = (node["arrayValue"] or {}).get("values") or []
        return [_any_value(v) for v in values]
    if "kvlistValue" in node:
        return _attributes((node["kvlistValue"] or {}).get("values") or [])
    if "bytesValue" in node:
        return node["bytesValue"]  # base64 string; keep as-is
    return None


def _attributes(items: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(items, dict):  # non-standard but seen in the wild
        return {str(k): _any_value(v) for k, v in items.items()}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        out[key] = _any_value(item.get("value"))
    return out


def _hex_id(value: Any, nbytes: int) -> str | None:
    """Ids arrive as lowercase hex in OTLP/JSON (spec) but some exporters still
    send protobuf-JSON base64. Normalise both to lowercase hex."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if len(s) == nbytes * 2:
        try:
            int(s, 16)
            return s.lower()
        except ValueError:
            pass
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return s.lower()
    if len(raw) == nbytes:
        return raw.hex()
    return s.lower()


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pick(node: dict[str, Any], camel: str, snake: str, default: Any = None) -> Any:
    return node.get(camel, node.get(snake, default))


def _span_from_json(
    node: dict[str, Any], resource: dict[str, Any], scope_name: str | None
) -> SpanRecord | None:
    trace_id = _hex_id(_pick(node, "traceId", "trace_id"), 16)
    span_id = _hex_id(_pick(node, "spanId", "span_id"), 8)
    if not trace_id or not span_id:
        return None
    parent = _hex_id(_pick(node, "parentSpanId", "parent_span_id"), 8)
    kind_raw = node.get("kind", 1)
    kind = (
        _SPAN_KINDS.get(int(kind_raw), "INTERNAL")
        if isinstance(kind_raw, int) or (isinstance(kind_raw, str) and kind_raw.isdigit())
        else str(kind_raw).upper().removeprefix("SPAN_KIND_")
    )
    status = node.get("status") or {}
    code_raw = status.get("code", 0)
    status_code = (
        _STATUS_CODES.get(int(code_raw), "UNSET")
        if isinstance(code_raw, int) or (isinstance(code_raw, str) and code_raw.isdigit())
        else str(code_raw).upper().removeprefix("STATUS_CODE_")
    )
    events: list[dict[str, Any]] = []
    for ev in node.get("events") or []:
        if not isinstance(ev, dict):
            continue
        events.append(
            {
                "name": ev.get("name"),
                "time_ns": _int(_pick(ev, "timeUnixNano", "time_unix_nano")),
                "attributes": _attributes(ev.get("attributes")),
            }
        )
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=str(node.get("name") or ""),
        start_ns=_int(_pick(node, "startTimeUnixNano", "start_time_unix_nano")),
        end_ns=_int(_pick(node, "endTimeUnixNano", "end_time_unix_nano")),
        attributes=_attributes(node.get("attributes")),
        kind=kind,
        status_code=status_code,
        status_message=status.get("message") or None,
        events=events,
        resource=resource,
        scope_name=scope_name,
    )


def decode_json(body: bytes | str | dict[str, Any]) -> list[SpanRecord]:
    """Decode an OTLP/JSON ``ExportTraceServiceRequest``. Raises ``OTLPDecodeError``
    on malformed input."""
    if isinstance(body, dict):
        doc: Any = body
    else:
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError) as e:
            raise OTLPDecodeError(f"invalid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise OTLPDecodeError("OTLP/JSON body must be an object")
    spans: list[SpanRecord] = []
    for rs in _pick(doc, "resourceSpans", "resource_spans", []) or []:
        if not isinstance(rs, dict):
            continue
        resource = _attributes((rs.get("resource") or {}).get("attributes"))
        scope_spans = _pick(rs, "scopeSpans", "scope_spans") or _pick(
            rs, "instrumentationLibrarySpans", "instrumentation_library_spans", []
        )
        for ss in scope_spans or []:
            if not isinstance(ss, dict):
                continue
            scope = ss.get("scope") or _pick(ss, "instrumentationLibrary", "instrumentation_library") or {}
            scope_name = scope.get("name") if isinstance(scope, dict) else None
            for node in ss.get("spans") or []:
                if not isinstance(node, dict):
                    continue
                try:
                    rec = _span_from_json(node, resource, scope_name)
                except Exception as e:  # malformed span -> whole request rejected (400)
                    raise OTLPDecodeError(f"invalid span: {e}") from e
                if rec is not None:
                    spans.append(rec)
    return spans


# ------------------------------------------------------------------- protobuf

_INSTALL_HINT = (
    "Decoding OTLP protobuf requires `opentelemetry-proto`: "
    'pip install "tracesage[otel]" (or send OTLP/JSON with Content-Type: application/json).'
)


def _proto_module() -> Any:
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except ImportError as e:  # pragma: no cover - exercised via ProtobufUnavailableError path
        raise ProtobufUnavailableError(_INSTALL_HINT) from e
    return trace_service_pb2


def protobuf_available() -> bool:
    try:
        _proto_module()
    except ProtobufUnavailableError:
        return False
    return True


def _pb_any_value(v: Any) -> Any:
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_pb_any_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _pb_any_value(kv.value) for kv in v.kvlist_value.values}
    if which == "bytes_value":
        return base64.b64encode(v.bytes_value).decode("ascii")
    return getattr(v, which)


def _pb_attributes(items: Any) -> dict[str, Any]:
    return {kv.key: _pb_any_value(kv.value) for kv in items}


def decode_protobuf(body: bytes) -> list[SpanRecord]:
    """Decode a binary ``ExportTraceServiceRequest``. Raises ``ProtobufUnavailableError``
    when ``opentelemetry-proto`` is missing and ``OTLPDecodeError`` on bad bytes."""
    pb2 = _proto_module()
    req = pb2.ExportTraceServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as e:
        raise OTLPDecodeError(f"invalid protobuf: {e}") from e
    spans: list[SpanRecord] = []
    for rs in req.resource_spans:
        resource = _pb_attributes(rs.resource.attributes)
        for ss in rs.scope_spans:
            scope_name = ss.scope.name or None
            for sp in ss.spans:
                trace_id = sp.trace_id.hex()
                span_id = sp.span_id.hex()
                if not trace_id or not span_id:
                    continue
                parent = sp.parent_span_id.hex() or None
                events = [
                    {
                        "name": ev.name,
                        "time_ns": int(ev.time_unix_nano),
                        "attributes": _pb_attributes(ev.attributes),
                    }
                    for ev in sp.events
                ]
                spans.append(
                    SpanRecord(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent,
                        name=sp.name,
                        start_ns=int(sp.start_time_unix_nano),
                        end_ns=int(sp.end_time_unix_nano),
                        attributes=_pb_attributes(sp.attributes),
                        kind=_SPAN_KINDS.get(int(sp.kind), "INTERNAL"),
                        status_code=_STATUS_CODES.get(int(sp.status.code), "UNSET"),
                        status_message=sp.status.message or None,
                        events=events,
                        resource=resource,
                        scope_name=scope_name,
                    )
                )
    return spans


def empty_response_protobuf() -> bytes:
    """Serialized empty ``ExportTraceServiceResponse`` (what a collector returns)."""
    return _proto_module().ExportTraceServiceResponse().SerializeToString()


__all__ = [
    "OTLPDecodeError",
    "ProtobufUnavailableError",
    "decode_json",
    "decode_protobuf",
    "empty_response_protobuf",
    "protobuf_available",
]
