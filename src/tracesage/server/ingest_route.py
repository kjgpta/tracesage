"""``POST /v1/traces`` — OTLP/HTTP trace ingest.

Point any OpenTelemetry exporter at tracesage (``OTEL_EXPORTER_OTLP_ENDPOINT=
http://127.0.0.1:7842``) and its spans become tracesage steps through
:class:`tracesage.ingest.SpanMapper`. Accepts the two OTLP/HTTP encodings:

- ``application/x-protobuf`` (the exporters' default) — needs ``opentelemetry-proto``
  (``tracesage[otel]``); without it the request gets ``501`` with the install hint;
- ``application/json`` — handled with no extra dependencies.

``Content-Encoding: gzip`` is honoured. Bodies above ``config.ingest_max_body_bytes``
are refused with ``413``. The route is registered only when the app was created
with an ``emit`` callable (``create_app(..., emit=tracer.emit)``), so a read-only
``tracesage serve`` answers ``404`` here. Like every ``/api/*`` path it sits behind
the bearer-token middleware.
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from tracesage.ingest.mapper import SpanMapper, SpanRecord
from tracesage.ingest.otlp import (
    OTLPDecodeError,
    ProtobufUnavailableError,
    decode_json,
    decode_protobuf,
    empty_response_protobuf,
)

log = logging.getLogger("tracesage.server.ingest")

router = APIRouter()

INGEST_PATH = "/v1/traces"
_PROTOBUF_TYPES = frozenset({"application/x-protobuf", "application/protobuf"})
_JSON_TYPES = frozenset({"application/json"})
# Bodies larger than this are decoded off the event loop.
_OFFLOAD_DECODE_BYTES = 64 * 1024


def _content_type(request: Request) -> str:
    raw = request.headers.get("content-type", "")
    return raw.split(";", 1)[0].strip().lower()


async def _read_body(request: Request, max_bytes: int) -> bytes | None:
    """Read the request body enforcing ``max_bytes``; None when exceeded."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _inflate(body: bytes, max_bytes: int, *, gzip_format: bool) -> bytes | None:
    """Decompress gzip/deflate, refusing output larger than ``max_bytes`` (zip-bomb
    guard). Returns None on oversize, raises ``zlib.error`` on malformed data."""
    wbits = 16 + zlib.MAX_WBITS if gzip_format else zlib.MAX_WBITS
    decompressor = zlib.decompressobj(wbits)
    out = decompressor.decompress(body, max_bytes + 1)
    if len(out) > max_bytes or decompressor.unconsumed_tail:
        return None
    out += decompressor.flush()
    return out if len(out) <= max_bytes else None


def _decode(fmt: str, body: bytes) -> list[SpanRecord]:
    return decode_protobuf(body) if fmt == "protobuf" else decode_json(body)


def _success(fmt: str, rejected: int, message: str | None) -> Response:
    if fmt == "protobuf":
        if rejected:
            from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

            resp = trace_service_pb2.ExportTraceServiceResponse()
            resp.partial_success.rejected_spans = rejected
            resp.partial_success.error_message = message or ""
            payload = resp.SerializeToString()
        else:
            payload = empty_response_protobuf()
        return Response(content=payload, media_type="application/x-protobuf")
    body: dict[str, Any] = {}
    if rejected:
        body["partialSuccess"] = {"rejectedSpans": rejected, "errorMessage": message or ""}
    return JSONResponse(content=body)


def _error(fmt: str, status: int, detail: str) -> Response:
    """Error body in the request's encoding is not required by OTLP; JSON is
    universally readable and what the exporters log."""
    del fmt
    return JSONResponse(status_code=status, content={"detail": detail})


@router.post(INGEST_PATH, summary="OTLP/HTTP trace ingest")
async def ingest_traces(request: Request) -> Response:
    emit = getattr(request.app.state, "emit", None)
    if emit is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "OTLP ingest is not enabled on this server (tracesage serve --ingest)"},
        )
    config = request.app.state.config
    max_bytes = int(getattr(config, "ingest_max_body_bytes", 16 * 1024 * 1024))

    ctype = _content_type(request)
    if ctype in _PROTOBUF_TYPES or ctype == "":
        fmt = "protobuf"  # OTLP/HTTP default encoding
    elif ctype in _JSON_TYPES:
        fmt = "json"
    else:
        return _error(
            "json", 415, f"unsupported Content-Type {ctype!r}; use application/x-protobuf or application/json"
        )

    body = await _read_body(request, max_bytes)
    if body is None:
        return _error(fmt, 413, f"request body exceeds ingest_max_body_bytes={max_bytes}")

    encoding = request.headers.get("content-encoding", "").lower()
    if "gzip" in encoding or "deflate" in encoding:
        try:
            body = _inflate(body, max_bytes, gzip_format="gzip" in encoding)
        except (OSError, EOFError, zlib.error) as e:
            return _error(fmt, 400, f"invalid {encoding} body: {e}")
        if body is None:
            return _error(fmt, 413, f"decompressed body exceeds ingest_max_body_bytes={max_bytes}")
    elif encoding and encoding != "identity":
        return _error(fmt, 415, f"unsupported Content-Encoding {encoding!r}")

    try:
        if len(body) > _OFFLOAD_DECODE_BYTES:
            loop = asyncio.get_running_loop()
            spans = await loop.run_in_executor(None, _decode, fmt, body)
        else:
            spans = _decode(fmt, body)
    except ProtobufUnavailableError as e:
        return _error(fmt, 501, str(e))
    except OTLPDecodeError as e:
        return _error(fmt, 400, str(e))
    except (ValueError, json.JSONDecodeError) as e:  # pragma: no cover - defensive
        return _error(fmt, 400, f"could not decode body: {e}")

    mapper: SpanMapper = request.app.state.ingest_mapper
    rejected = 0
    last_error: str | None = None
    for span in spans:
        try:
            for event in mapper.map(span):
                emit(event)
        except Exception as e:  # one bad span must not reject the batch
            rejected += 1
            last_error = f"{type(e).__name__}: {e}"
            log.debug("ingest: rejected span %s: %s", getattr(span, "span_id", "?"), e)
    return _success(fmt, rejected, last_error)


@router.post("/v1/hooks/{app}", summary="Coding-agent hook ingest")
async def ingest_hook(app: str, request: Request) -> JSONResponse:
    """Accept one hook payload. Always answers ``{}`` so the agent is never blocked."""
    emit = getattr(request.app.state, "emit", None)
    if emit is None:
        return JSONResponse(content={})
    config = request.app.state.config
    max_bytes = int(getattr(config, "ingest_max_body_bytes", 16 * 1024 * 1024))
    try:
        body = await _read_body(request, max_bytes)
        if not body:
            return JSONResponse(content={})
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return JSONResponse(content={})
        from tracesage.ingest.agent_hooks import normalize

        metadata_only = bool(getattr(config, "hook_metadata_only", False))
        for event in normalize(app, payload, metadata_only=metadata_only):
            emit(event)
    except Exception as exc:
        log.warning("hook ingest dropped a %s payload: %s", app, exc)
    return JSONResponse(content={})


@router.post("/v1/logs", summary="OTLP log ingest (accepted, not stored)")
async def ingest_logs(request: Request) -> JSONResponse:
    """Codex ``--otel`` posts logs here. tracesage stores traces, so the body is dropped."""
    config = getattr(request.app.state, "config", None)
    max_bytes = int(getattr(config, "ingest_max_body_bytes", 16 * 1024 * 1024))
    try:
        await _read_body(request, max_bytes)
    except Exception as exc:
        log.debug("log ingest dropped a body: %s", exc)
    return JSONResponse(content={})


__all__ = ["INGEST_PATH", "ingest_hook", "ingest_logs", "ingest_traces", "router"]
