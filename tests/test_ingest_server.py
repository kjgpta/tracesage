"""Tests for POST /v1/traces (OTLP/HTTP ingest) on the embedded server."""
from __future__ import annotations

import base64
import gzip
import json
import zlib
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tracesage.config import TraceSageConfig
from tracesage.ingest.otlp import ProtobufUnavailableError
from tracesage.models import EventType, RawEvent, Stats
from tracesage.server import WebSocketManager, create_app
from tracesage.storage.blob_store import BlobStore
from tracesage.storage.sqlite_backend import SQLiteBackend
from tracesage.tracer import TraceSage

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"
PYDANTIC_TRACE = "0af7651916cd43dd8448eb211c80319c"
PYDANTIC_ROOT = "a1b2c3d4e5f60718"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _pb_body(name: str) -> bytes:
    pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from google.protobuf.json_format import ParseDict

    doc = json.loads(_fixture(name))
    for rs in doc["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for sp in ss["spans"]:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if sp.get(key):
                        sp[key] = base64.b64encode(bytes.fromhex(sp[key])).decode()
                    elif key in sp:
                        del sp[key]
    return ParseDict(doc, pb.ExportTraceServiceRequest()).SerializeToString()


# ------------------------------------------------------------------ fixtures


@pytest_asyncio.fixture
async def db(tmp_data_dir):
    backend = SQLiteBackend(tmp_data_dir / "traces.db")
    await backend.init()
    try:
        yield backend
    finally:
        await backend.close()


@pytest.fixture
def sink() -> list[RawEvent]:
    return []


def _app(db, tmp_data_dir, sink, **cfg_kwargs: Any):
    cfg = TraceSageConfig(data_dir=tmp_data_dir, **cfg_kwargs)
    return create_app(
        db=db,
        blob_store=BlobStore(tmp_data_dir / "blobs"),
        ws_manager=WebSocketManager(),
        config=cfg,
        stats=Stats(),
        emit=sink.append,
    )


@pytest_asyncio.fixture
async def client(db, tmp_data_dir, sink):
    app = _app(db, tmp_data_dir, sink)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------- tests


async def test_json_post_maps_spans(client: AsyncClient, sink: list[RawEvent]) -> None:
    resp = await client.post(
        "/v1/traces", content=_fixture("pydantic_ai.json"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {}
    types = [e.event_type for e in sink]
    assert len(types) == 10
    assert types.count(EventType.RUN_START) == 1
    assert types.count(EventType.RUN_END) == 1
    assert all(e.root_run_id == PYDANTIC_TRACE for e in sink)
    assert all(e.raw_payload.get("_source") == "otlp" for e in sink)


async def test_protobuf_post_default_content_type(client: AsyncClient, sink: list[RawEvent]) -> None:
    body = _pb_body("openai_agents.json")
    resp = await client.post("/v1/traces", content=body)  # no Content-Type: protobuf default
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-protobuf"
    pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    parsed = pb.ExportTraceServiceResponse()
    parsed.ParseFromString(resp.content)
    assert not parsed.HasField("partial_success")
    assert len(sink) == 12  # 5 spans * 2 + RUN_START + RUN_END

    sink.clear()
    resp = await client.post(
        "/v1/traces", content=body, headers={"content-type": "application/x-protobuf"}
    )
    assert resp.status_code == 200
    assert len(sink) == 11  # same trace, same app: RUN_START is not re-emitted


async def test_gzip_and_deflate_bodies(client: AsyncClient, sink: list[RawEvent]) -> None:
    raw = _fixture("openai_client.json")
    resp = await client.post(
        "/v1/traces", content=gzip.compress(raw),
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert len(sink) == 4
    sink.clear()
    resp = await client.post(
        "/v1/traces", content=zlib.compress(raw),
        headers={"content-type": "application/json", "content-encoding": "deflate"},
    )
    assert resp.status_code == 200
    assert len(sink) == 3  # RUN_START already emitted for this trace above


async def test_bad_gzip_is_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/traces", content=b"\x1f\x8b not really gzip",
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert resp.status_code == 400


async def test_unsupported_content_type_is_415(client: AsyncClient) -> None:
    resp = await client.post("/v1/traces", content=b"<xml/>", headers={"content-type": "text/xml"})
    assert resp.status_code == 415
    resp = await client.post(
        "/v1/traces", content=b"{}", headers={"content-type": "application/json",
                                             "content-encoding": "br"},
    )
    assert resp.status_code == 415


async def test_bad_payloads_are_400(client: AsyncClient) -> None:
    resp = await client.post("/v1/traces", content=b"{not json",
                             headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert "invalid JSON" in resp.json()["detail"]
    resp = await client.post("/v1/traces", content=b"\xff\xfe\x00garbage\x99" * 5)
    assert resp.status_code == 400


async def test_empty_batch_is_200(client: AsyncClient, sink: list[RawEvent]) -> None:
    resp = await client.post("/v1/traces", content=b"{}", headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert sink == []


async def test_body_cap_413(db, tmp_data_dir, sink) -> None:
    app = _app(db, tmp_data_dir, sink, ingest_max_body_bytes=200)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        big = _fixture("pydantic_ai.json")
        assert len(big) > 200
        resp = await ac.post("/v1/traces", content=big, headers={"content-type": "application/json"})
        assert resp.status_code == 413
        # A small compressed body that inflates past the cap is refused too.
        bomb = gzip.compress(b'{"resourceSpans": [' + b" " * 5000 + b"]}")
        assert len(bomb) < 200
        resp = await ac.post(
            "/v1/traces", content=bomb,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
        assert resp.status_code == 413
    assert sink == []


async def test_protobuf_unavailable_is_501(client: AsyncClient, monkeypatch) -> None:
    import tracesage.server.ingest_route as route

    def _raise(_body: bytes):
        raise ProtobufUnavailableError('pip install "tracesage[otel]"')

    monkeypatch.setattr(route, "decode_protobuf", _raise)
    resp = await client.post("/v1/traces", content=b"\x0a\x00")
    assert resp.status_code == 501
    assert "tracesage[otel]" in resp.json()["detail"]


async def test_ingest_disabled_is_404(db, tmp_data_dir) -> None:
    cfg = TraceSageConfig(data_dir=tmp_data_dir)
    app = create_app(db=db, blob_store=BlobStore(tmp_data_dir / "blobs"),
                     ws_manager=WebSocketManager(), config=cfg, stats=Stats())  # no emit
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/v1/traces", content=_fixture("openai_client.json"),
                             headers={"content-type": "application/json"})
    assert resp.status_code == 404


async def test_auth_gates_ingest(db, tmp_data_dir, sink) -> None:
    app = _app(db, tmp_data_dir, sink, auth_token="s3cret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = _fixture("openai_client.json")
        headers = {"content-type": "application/json"}
        assert (await ac.post("/v1/traces", content=body, headers=headers)).status_code == 401
        assert sink == []
        bad = {**headers, "authorization": "Bearer nope"}
        assert (await ac.post("/v1/traces", content=body, headers=bad)).status_code == 401
        good = {**headers, "authorization": "Bearer s3cret"}
        assert (await ac.post("/v1/traces", content=body, headers=good)).status_code == 200
        assert len(sink) == 4


async def test_partial_success_reports_rejected_spans(client: AsyncClient, sink, monkeypatch) -> None:
    from tracesage.ingest.mapper import SpanMapper

    real = SpanMapper.map

    def _flaky(self, span):
        if span.span_id == "c8be7c8270314442":  # the tool span
            raise RuntimeError("mapper hiccup")
        return real(self, span)

    monkeypatch.setattr(SpanMapper, "map", _flaky)
    resp = await client.post("/v1/traces", content=_fixture("pydantic_ai.json"),
                             headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {"partialSuccess": {"rejectedSpans": 1,
                                              "errorMessage": "RuntimeError: mapper hiccup"}}
    assert len(sink) == 8  # 3 spans accepted (+ RUN_START/RUN_END)


async def test_end_to_end_through_tracer_and_api(tmp_path: Path) -> None:
    """Real pipeline: /v1/traces -> TraceSage.emit -> worker -> SQLite -> /api/*."""
    cfg = TraceSageConfig(data_dir=tmp_path, worker_batch_timeout=0.02, print_run_url=False)
    tl = await TraceSage.create(cfg, start_server=False)
    try:
        app = create_app(db=tl.db, blob_store=tl.blob_store, ws_manager=tl._ws_manager,
                         config=cfg, stats=tl.stats, emit=tl.emit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/traces", content=_pb_body("pydantic_ai.json"))
            assert resp.status_code == 200
            await tl.flush()

            run = (await ac.get(f"/api/runs/{PYDANTIC_TRACE}")).json()
            assert run["status"] == "completed"
            assert run["total_tokens_input"] == 70
            assert run["tags"] == ["otlp", "weather-app"]

            journey = (await ac.get(f"/api/runs/{PYDANTIC_TRACE}/journey")).json()["steps"]
            by_id = {e["event_id"]: e for e in journey}
            assert by_id[f"{PYDANTIC_ROOT}-end"]["duration_ms"] == 650
            assert by_id["c8be7c8270314442-end"]["duration_ms"] == 50
            assert by_id[f"{PYDANTIC_ROOT}-start"]["agent_name"] == "weather_agent"
            assert by_id[f"{PYDANTIC_ROOT}-start"]["parent_run_id"] == PYDANTIC_TRACE

            full = (await ac.get(
                f"/api/runs/{PYDANTIC_TRACE}/steps/c8be7c8270314442-start/full"
            )).json()
            payload = full["full_payload"]
            assert payload["_source"] == "otlp"
            assert payload["attributes"]["gen_ai.tool.name"] == "get_weather"

            topo = (await ac.get(f"/api/topology?scope=run:{PYDANTIC_TRACE}")).json()
            ids = {n["id"] for n in topo["nodes"]}
            assert {"agent:weather_agent", "tool:get_weather", "llm:test"} <= ids
            edges = {(e["source"], e["target"]) for e in topo["edges"]}
            assert ("agent:weather_agent", "tool:get_weather") in edges

            runs = (await ac.get("/api/runs")).json()
            assert runs["total"] == 1

            # Re-sending the same batch (exporter retry) does not duplicate steps.
            resp = await ac.post("/v1/traces", content=_pb_body("pydantic_ai.json"))
            assert resp.status_code == 200
            await tl.flush()
            journey2 = (await ac.get(f"/api/runs/{PYDANTIC_TRACE}/journey")).json()["steps"]
            assert len(journey2) == len(journey)
    finally:
        await tl.stop()


async def test_embedded_server_exposes_ingest(tmp_path: Path) -> None:
    """TraceSage with start_server=True registers /v1/traces (config.ingest=True)
    and not when ingest=False."""
    import httpx

    cfg = TraceSageConfig(data_dir=tmp_path / "a", port=0, print_run_url=False)
    tl = await TraceSage.create(cfg, start_server=True)
    try:
        assert tl.bound_port
        async with httpx.AsyncClient() as ac:
            resp = await ac.post(
                f"http://127.0.0.1:{tl.bound_port}/v1/traces",
                content=_fixture("openai_client.json"),
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 200
        await tl.flush()
        run = await tl.db.get_run("3df7651916cd43dd8448eb211c80319f")
        assert run is not None
        assert run.status.value == "completed"
    finally:
        await tl.stop()

    cfg_off = TraceSageConfig(data_dir=tmp_path / "b", port=0, ingest=False, print_run_url=False)
    tl2 = await TraceSage.create(cfg_off, start_server=True)
    try:
        async with httpx.AsyncClient() as ac:
            resp = await ac.post(
                f"http://127.0.0.1:{tl2.bound_port}/v1/traces",
                content=_fixture("openai_client.json"),
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 404
    finally:
        await tl2.stop()
