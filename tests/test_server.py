"""Server tests: REST + WebSocket + auth.

Uses the real `SQLiteBackend` and `BlobStore` with `tmp_data_dir` so the integration
between server, storage, and blob layers is exercised.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from tracelens import __version__
from tracelens.config import TraceLensConfig
from tracelens.models import (
    EventType,
    Run,
    RunStatus,
    Stats,
    StoredEvent,
    WSMessage,
)
from tracelens.server import WebSocketManager, create_app
from tracelens.storage.blob_store import BlobStore
from tracelens.storage.sqlite_backend import SQLiteBackend

# ---------- Fixtures ----------


@pytest_asyncio.fixture
async def db(tmp_data_dir):
    backend = SQLiteBackend(tmp_data_dir / "traces.db")
    await backend.init()
    try:
        yield backend
    finally:
        await backend.close()


@pytest.fixture
def blob_store(tmp_data_dir):
    return BlobStore(tmp_data_dir / "blobs")


@pytest.fixture
def ws_manager():
    return WebSocketManager()


@pytest.fixture
def config(tmp_data_dir):
    return TraceLensConfig(data_dir=tmp_data_dir, auth_token=None)


@pytest.fixture
def config_with_token(tmp_data_dir):
    return TraceLensConfig(data_dir=tmp_data_dir, auth_token="secret-test-token")


@pytest.fixture
def app(db, blob_store, ws_manager, config):
    return create_app(db=db, blob_store=blob_store, ws_manager=ws_manager, config=config, stats=Stats())


@pytest.fixture
def app_with_auth(db, blob_store, ws_manager, config_with_token):
    return create_app(
        db=db,
        blob_store=blob_store,
        ws_manager=ws_manager,
        config=config_with_token,
        stats=Stats(),
    )


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(app_with_auth):
    transport = ASGITransport(app=app_with_auth)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------- Helpers ----------


def _make_run(run_id: str = "run-1", status: RunStatus = RunStatus.RUNNING, started_offset_s: int = 0) -> Run:
    return Run(
        run_id=run_id,
        root_run_id=run_id,
        tags=["test"],
        status=status,
        started_at=datetime.now(UTC) - timedelta(seconds=started_offset_s),
    )


def _make_event(
    event_id: str,
    run_id: str = "run-1",
    event_type: EventType = EventType.CHAIN_START,
    blob_path: str | None = None,
    agent_name: str | None = "AgentA",
    tool_name: str | None = None,
    parent_run_id: str | None = None,
    seconds_offset: int = 0,
) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        root_run_id=run_id,
        event_type=event_type,
        timestamp=datetime.now(UTC) + timedelta(seconds=seconds_offset),
        agent_name=agent_name,
        tool_name=tool_name,
        summary=f"{event_id} summary",
        blob_path=blob_path,
        duration_ms=10,
    )


# ---------- Health + basic ----------


@pytest.mark.asyncio
async def test_health_no_auth_required_no_token(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "version": __version__}


@pytest.mark.asyncio
async def test_health_no_auth_required_with_token(auth_client):
    # Token configured but health must still be reachable without Authorization.
    r = await auth_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------- /api/runs list + pagination ----------


@pytest.mark.asyncio
async def test_list_runs_empty(client):
    r = await client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert body == {"runs": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_list_runs_pagination(client, db):
    # Insert 100 runs with monotonically increasing started_at — older offsets first.
    for i in range(100):
        await db.upsert_run(
            Run(
                run_id=f"run-{i:03d}",
                root_run_id=f"run-{i:03d}",
                tags=[],
                status=RunStatus.COMPLETED,
                started_at=datetime.now(UTC) - timedelta(seconds=100 - i),
            )
        )

    r = await client.get("/api/runs", params={"limit": 10, "offset": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 100
    assert body["limit"] == 10
    assert body["offset"] == 10
    assert len(body["runs"]) == 10


# ---------- 404 paths ----------


@pytest.mark.asyncio
async def test_get_journey_404_when_run_missing(client):
    r = await client.get("/api/runs/does-not-exist/journey")
    assert r.status_code == 404
    assert r.json() == {"detail": "Run not found"}


@pytest.mark.asyncio
async def test_get_full_step_404_when_event_missing(client, db):
    await db.upsert_run(_make_run("r1"))
    r = await client.get("/api/runs/r1/steps/nope/full")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_full_step_404_when_event_has_no_blob(client, db):
    await db.upsert_run(_make_run("r2"))
    ev = _make_event("e-no-blob", run_id="r2", blob_path=None)
    await db.upsert_event(ev)
    r = await client.get("/api/runs/r2/steps/e-no-blob/full")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "no full blob" in detail.lower() or "blob" in detail.lower()


# ---------- WebSocket ----------


@pytest.mark.asyncio
async def test_websocket_receives_events(app, ws_manager, db):
    await db.upsert_run(_make_run("ws-run-1"))

    with TestClient(app) as client, client.websocket_connect("/ws/trace/ws-run-1") as ws:
        # Catchup arrives first (empty journey).
        first = ws.receive_json()
        assert first["msg_type"] == "catchup"
        assert first["run_id"] == "ws-run-1"

        # Now broadcast something via the manager and assert delivery.
        msg = WSMessage(
            msg_type="event",
            run_id="ws-run-1",
            payload={"event_id": "e1", "summary": "broadcast"},
        )
        await ws_manager.broadcast("ws-run-1", msg)
        received = ws.receive_json()
        assert received["msg_type"] == "event"
        assert received["payload"]["event_id"] == "e1"


@pytest.mark.asyncio
async def test_websocket_catchup_on_connect(app, db):
    await db.upsert_run(_make_run("ws-run-2"))
    for i in range(5):
        await db.upsert_event(
            _make_event(
                f"evt-{i}",
                run_id="ws-run-2",
                seconds_offset=i,
            )
        )

    with TestClient(app) as client, client.websocket_connect("/ws/trace/ws-run-2") as ws:
        first = ws.receive_json()
        assert first["msg_type"] == "catchup"
        assert first["run_id"] == "ws-run-2"
        assert len(first["payload"]["steps"]) == 5
        ids = [s["event_id"] for s in first["payload"]["steps"]]
        assert ids == [f"evt-{i}" for i in range(5)]


# ---------- Delete ----------


@pytest.mark.asyncio
async def test_delete_run_removes_data(client, db, blob_store, tmp_data_dir):
    await db.upsert_run(_make_run("r-del"))
    # Persist a blob so we can also check filesystem cleanup.
    blob_path = await blob_store.write("r-del", "evt-blob", {"hello": "world"})
    await db.upsert_event(_make_event("evt-blob", run_id="r-del", blob_path=blob_path))
    await db.upsert_event(_make_event("evt-no-blob", run_id="r-del"))

    r = await client.delete("/api/runs/r-del")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "run_id": "r-del"}

    # DB row gone.
    assert await db.get_run("r-del") is None
    # Events gone.
    assert await db.get_journey("r-del") == []
    # Blob directory gone.
    blob_dir = tmp_data_dir / "blobs" / "r-del"
    assert not blob_dir.exists()


# ---------- Auth ----------


@pytest.mark.asyncio
async def test_auth_required_when_token_set(auth_client):
    r = await auth_client.get("/api/runs")
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}


@pytest.mark.asyncio
async def test_auth_correct_token_passes(auth_client):
    r = await auth_client.get(
        "/api/runs",
        headers={"Authorization": "Bearer secret-test-token"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_wrong_token_fails(auth_client):
    r = await auth_client.get(
        "/api/runs",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ui_static_paths_skip_auth(auth_client):
    """B11: UI shell must load without auth so the user can enter the token."""
    # The UI mount may or may not exist depending on test fixture setup; what
    # matters here is that /ui/* paths are NOT 401. They should be 200 if the
    # mount is present, or 404 if not — but never 401.
    for path in ("/ui/", "/ui/index.html", "/ui/app.js", "/ui/styles.css"):
        r = await auth_client.get(path)
        assert r.status_code != 401, (
            f"{path} returned 401 — auth must be skipped for UI shell"
        )


@pytest.mark.asyncio
async def test_root_path_skips_auth(auth_client):
    """The root path / is treated as public (typically a redirect to /ui/)."""
    r = await auth_client.get("/")
    assert r.status_code != 401


# ---------- Topology ----------


@pytest.mark.asyncio
async def test_topology_endpoint(client, db):
    # Both run rows must exist because events.run_id has a FK on runs.run_id.
    await db.upsert_run(_make_run("topo"))
    await db.upsert_run(
        Run(
            run_id="topo-child",
            root_run_id="topo",
            tags=[],
            status=RunStatus.COMPLETED,
            started_at=datetime.now(UTC),
        )
    )
    # Parent agent event
    parent = _make_event(
        "parent-evt",
        run_id="topo",
        event_type=EventType.CHAIN_START,
        agent_name="OrderAgent",
    )
    await db.upsert_event(parent)
    # Child tool whose parent_run_id matches the parent event's run_id.
    child = StoredEvent(
        event_id="child-evt",
        run_id="topo-child",
        parent_run_id="topo",
        root_run_id="topo",
        event_type=EventType.TOOL_START,
        timestamp=datetime.now(UTC),
        agent_name=None,
        tool_name="search_web",
        summary="search",
        duration_ms=15,
    )
    await db.upsert_event(child)

    r = await client.get("/api/topology")
    assert r.status_code == 200
    body = r.json()
    node_ids = {n["id"] for n in body["nodes"]}
    assert "agent:OrderAgent" in node_ids
    assert "tool:search_web" in node_ids
    edges = body["edges"]
    assert any(
        e["source"] == "agent:OrderAgent" and e["target"] == "tool:search_web"
        for e in edges
    )


# ---------- Export ----------


@pytest.mark.asyncio
async def test_export_jsonl_streams(client, db):
    await db.upsert_run(_make_run("exp"))
    for i in range(3):
        await db.upsert_event(
            _make_event(f"e{i}", run_id="exp", seconds_offset=i)
        )

    r = await client.get("/api/runs/exp/export", params={"format": "jsonl"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    body_text = r.text
    lines = [ln for ln in body_text.splitlines() if ln.strip()]
    # 1 run + 3 events
    assert len(lines) == 4
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["run_id"] == "exp"
    assert parsed[0]["status"] in {"running", "completed", "failed"}
    event_ids = [p["event_id"] for p in parsed[1:]]
    assert event_ids == ["e0", "e1", "e2"]


# ---------- Stats ----------


@pytest.mark.asyncio
async def test_stats_endpoint(client, db):
    await db.upsert_run(_make_run("s1", status=RunStatus.COMPLETED))
    r = await client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    # Includes DB-level keys
    assert "total_runs" in body
    assert body["total_runs"] >= 1
    # Includes runtime stats fields
    assert "queue_depth" in body
    assert "events_dropped" in body


# ---------- Single-run fetch 404 ----------


@pytest.mark.asyncio
async def test_get_run_404(client):
    r = await client.get("/api/runs/no-such-run")
    assert r.status_code == 404


# ---------- Invalid query param ----------


@pytest.mark.asyncio
async def test_list_runs_rejects_oversized_limit(client):
    r = await client.get("/api/runs", params={"limit": 500})
    assert r.status_code == 422


# ---------- WebSocket disconnect cleanup ----------


@pytest.mark.asyncio
async def test_websocket_disconnect_removes_subscriber(app, ws_manager, db):
    await db.upsert_run(_make_run("ws-disc"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/trace/ws-disc") as ws:
            ws.receive_json()  # catchup
            assert await ws_manager.subscriber_count("ws-disc") == 1
        # After context exit, give the server a tick to process disconnect.
        await asyncio.sleep(0.05)
    assert await ws_manager.subscriber_count("ws-disc") == 0
