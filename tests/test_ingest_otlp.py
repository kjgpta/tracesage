"""Tests for tracesage.ingest.otlp: OTLP/JSON and protobuf decoders."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tracesage.ingest import OTLPDecodeError, decode_json, decode_protobuf
from tracesage.ingest.otlp import empty_response_protobuf, protobuf_available

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"
pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")


@pytest.mark.parametrize(
    ("name", "count"),
    [
        ("pydantic_ai.json", 4),
        ("openai_agents.json", 5),
        ("crewai_openinference.json", 4),
        ("openai_client.json", 1),
        ("anthropic_client.json", 2),
    ],
)
def test_decode_fixtures(name: str, count: int) -> None:
    spans = decode_json((FIXTURES / name).read_bytes())
    assert len(spans) == count
    for s in spans:
        assert len(s.trace_id) == 32
        assert len(s.span_id) == 16
        assert s.trace_id == s.trace_id.lower()
        assert s.start_ns > 0
        assert s.end_ns >= s.start_ns
        assert isinstance(s.attributes, dict)
        assert s.resource.get("service.name")


def test_json_value_types_and_scope() -> None:
    spans = decode_json((FIXTURES / "openai_agents.json").read_bytes())
    llm = next(s for s in spans if s.span_id == "3333333333333333")
    a = llm.attributes
    assert a["gen_ai.usage.input_tokens"] == 120  # intValue string -> int
    assert a["gen_ai.request.temperature"] == 0.2
    assert a["gen_ai.response.finish_reasons"] == ["tool_calls"]
    assert llm.kind == "CLIENT"
    assert llm.status_code == "UNSET"
    assert llm.scope_name == "opentelemetry.instrumentation.genai.openai_agents"
    tool = next(s for s in spans if s.span_id == "4444444444444444")
    assert tool.status_code == "ERROR"
    assert tool.status_message == "tz is required"
    assert tool.events[0]["name"] == "exception"
    assert tool.events[0]["attributes"]["exception.type"] == "ValueError"
    assert tool.events[0]["time_ns"] == 1763712100899000000
    root = next(s for s in spans if s.span_id == "1111111111111111")
    assert root.parent_span_id is None
    assert root.is_root


def test_json_accepts_plain_values_bool_kvlist_and_snake_case() -> None:
    doc = {
        "resource_spans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "x"}}]},
                "scope_spans": [
                    {
                        "scope": {"name": "s"},
                        "spans": [
                            {
                                "trace_id": "ab" * 16,
                                "span_id": "cd" * 8,
                                "name": "n",
                                "kind": "SPAN_KIND_SERVER",
                                "start_time_unix_nano": 5,
                                "end_time_unix_nano": 9,
                                "attributes": [
                                    {"key": "flag", "value": {"boolValue": True}},
                                    {"key": "nested", "value": {"kvlistValue": {"values": [
                                        {"key": "a", "value": {"intValue": 1}}]}}},
                                    {"key": "plain", "value": "literal"},
                                    {"key": "empty", "value": {}},
                                ],
                                "status": {"code": "STATUS_CODE_OK"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (span,) = decode_json(json.dumps(doc).encode())
    assert span.kind == "SERVER"
    assert span.status_code == "OK"
    assert span.attributes == {"flag": True, "nested": {"a": 1}, "plain": "literal", "empty": None}
    assert span.start_ns == 5
    assert span.end_ns == 9


def test_json_base64_ids_are_normalised_to_hex() -> None:
    trace = bytes(range(16))
    span = bytes(range(8))
    doc = {
        "resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": base64.b64encode(trace).decode(),
            "spanId": base64.b64encode(span).decode(),
            "parentSpanId": "",
            "name": "n",
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
        }]}]}]
    }
    (rec,) = decode_json(doc)
    assert rec.trace_id == trace.hex()
    assert rec.span_id == span.hex()
    assert rec.parent_span_id is None


def test_json_skips_spans_without_ids_and_empty_body() -> None:
    assert decode_json(b"{}") == []
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "no ids"}]}]}]}
    assert decode_json(doc) == []


@pytest.mark.parametrize("body", [b"not json", b"[1,2,3]", b"\xff\xfe", b"42"])
def test_json_errors(body: bytes) -> None:
    with pytest.raises(OTLPDecodeError):
        decode_json(body)


# ------------------------------------------------------------------ protobuf


def _pb_request_from_fixture(name: str):
    """Build an ExportTraceServiceRequest matching a JSON fixture (so the two decoders
    can be compared field for field)."""
    from google.protobuf.json_format import ParseDict

    doc = json.loads((FIXTURES / name).read_text())
    # protobuf's JSON mapping wants base64 ids; the OTLP/JSON fixtures use hex.
    for rs in doc["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for sp in ss["spans"]:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if sp.get(key):
                        sp[key] = base64.b64encode(bytes.fromhex(sp[key])).decode()
                    elif key in sp:
                        del sp[key]
    return ParseDict(doc, pb.ExportTraceServiceRequest())


@pytest.mark.parametrize(
    "name",
    ["pydantic_ai.json", "openai_agents.json", "crewai_openinference.json",
     "openai_client.json", "anthropic_client.json"],
)
def test_protobuf_roundtrip_matches_json(name: str) -> None:
    req = _pb_request_from_fixture(name)
    from_pb = decode_protobuf(req.SerializeToString())
    from_json = decode_json((FIXTURES / name).read_bytes())
    assert len(from_pb) == len(from_json)
    for a, b in zip(from_pb, from_json, strict=True):
        assert a.trace_id == b.trace_id
        assert a.span_id == b.span_id
        assert a.parent_span_id == b.parent_span_id
        assert a.name == b.name
        assert a.start_ns == b.start_ns
        assert a.end_ns == b.end_ns
        assert a.kind == b.kind
        assert a.status_code == b.status_code
        assert a.status_message == b.status_message
        assert a.resource == b.resource
        assert a.scope_name == b.scope_name
        assert a.attributes == b.attributes
        assert [e["name"] for e in a.events] == [e["name"] for e in b.events]
        for ea, eb in zip(a.events, b.events, strict=True):
            assert ea["attributes"] == eb["attributes"]
            assert ea["time_ns"] == eb["time_ns"]


def test_protobuf_bad_bytes() -> None:
    with pytest.raises(OTLPDecodeError):
        decode_protobuf(b"\x00\xff\x12garbage that is not a message\x99" * 3)


def test_protobuf_available_and_empty_response() -> None:
    assert protobuf_available()
    body = empty_response_protobuf()
    assert isinstance(body, bytes)
    resp = pb.ExportTraceServiceResponse()
    resp.ParseFromString(body)
    assert not resp.HasField("partial_success")
