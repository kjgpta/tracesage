"""Full-text search over summaries and redacted payload text."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from tracesage.config import TraceSageConfig
from tracesage.models import EventType, Run, RunStatus, Stats, StoredEvent
from tracesage.search import extract_search_text, fts_match_query
from tracesage.server import WebSocketManager, create_app
from tracesage.storage import SQLiteBackend
from tracesage.storage.blob_store import BlobStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_query_builder_quotes_hostile_input() -> None:
    assert fts_match_query('hello "world') == '"hello" "world"'
    assert fts_match_query("AND(") == '"AND"'
    assert fts_match_query("*") is None
    assert fts_match_query("NEAR") == '"NEAR"'
    assert fts_match_query("pref*") == '"pref"*'


def test_extract_walks_strings_and_caps() -> None:
    text = extract_search_text({"a": "alpha", "n": 3, "b": ["beta"]}, limit=8)
    assert "alpha" in text
    assert len(text) <= 8


async def _seed(db: SQLiteBackend) -> None:
    now = _now()
    await db.upsert_run(Run(run_id="r1", root_run_id="r1", status=RunStatus.COMPLETED, started_at=now))
    secret = StoredEvent(
        event_id="e1", run_id="r1", root_run_id="r1", event_type=EventType.TOOL_END,
        timestamp=now, tool_name="search", summary="looked up hotels",
        search_text="the guest asked for a seaside hotel",
    )
    redacted = StoredEvent(
        event_id="e2", run_id="r1", root_run_id="r1", event_type=EventType.TOOL_END,
        timestamp=now, tool_name="pay", summary="paid",
        search_text="card [REDACTED] ok",
    )
    await db.upsert_events_batch([secret, redacted])


async def test_payload_indexed_and_secret_redacted_text_is_what_was_stored(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    try:
        await _seed(db)
        hits = await db.search_events("seaside")
        assert [h.event_id for h in hits] == ["e1"]
        secret_hits = await db.search_events("4111111111111111")
        assert secret_hits == []
        redacted = await db.search_events("REDACTED")
        assert [h.event_id for h in redacted] == ["e2"]
    finally:
        await db.close()


@pytest.mark.parametrize("q", ['"', "AND(", "*", "NEAR"])
async def test_hostile_queries_do_not_raise(tmp_path, q: str) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    try:
        await _seed(db)
        await db.search_events(q)
    finally:
        await db.close()


async def test_delete_removes_fts_rows(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    try:
        await _seed(db)
        await db.delete_run("r1")
        assert await db.search_events("seaside") == []
    finally:
        await db.close()


async def test_list_runs_filters(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    try:
        now = _now()
        await db.upsert_run(Run(
            run_id="ok", root_run_id="ok", status=RunStatus.COMPLETED, started_at=now, thread_id="thr"
        ))
        await db.upsert_run(Run(
            run_id="bad", root_run_id="bad", status=RunStatus.FAILED, started_at=now
        ))
        await db.upsert_event(StoredEvent(
            event_id="m", run_id="ok", root_run_id="ok", event_type=EventType.LLM_END,
            timestamp=now, agent_name="gpt-4o-mini", model="gpt-4o-mini", summary="answer",
            search_text="unique-needle",
        ))
        found, _ = await db.list_runs(q="unique-needle")
        assert [r.run_id for r in found] == ["ok"]
        by_model, _ = await db.list_runs(model="gpt-4o-mini")
        assert [r.run_id for r in by_model] == ["ok"]
        failed, _ = await db.list_runs(errors_only=True)
        assert [r.run_id for r in failed] == ["bad"]
    finally:
        await db.close()


async def test_search_rest(tmp_path) -> None:
    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    cfg = TraceSageConfig(data_dir=tmp_path)
    app = create_app(
        db=db, blob_store=BlobStore(tmp_path / "blobs"), ws_manager=WebSocketManager(),
        config=cfg, stats=Stats(),
    )
    try:
        await _seed(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/search", params={"q": "seaside"})
        assert resp.status_code == 200
        assert resp.json()["hits"][0]["event_id"] == "e1"
    finally:
        await db.close()
