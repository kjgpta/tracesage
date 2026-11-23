"""Tests for StorageWorker.

Mocks the StorageBackend, BlobStore, and ws_manager, so the real
implementations are not exercised here.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tracesage.config import TraceSageConfig
from tracesage.models import EventType, RawEvent, RunStatus, Stats, StoredEvent
from tracesage.worker import StorageWorker


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _FakeDB:
    def __init__(self) -> None:
        self.upserted_runs: list = []
        self.upserted_events_batches: list[list[StoredEvent]] = []
        self.status_updates: list[tuple] = []
        self.counter_updates: list[tuple] = []
        self.fail_next_batch = False

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def upsert_run(self, run: Any) -> None:
        self.upserted_runs.append(run)

    async def upsert_events_batch(self, events: list[StoredEvent]) -> None:
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise RuntimeError("simulated db failure")
        self.upserted_events_batches.append(list(events))

    async def update_run_status(self, run_id: str, status: Any, completed_at=None, error=None) -> None:
        self.status_updates.append((run_id, status, completed_at, error))

    async def increment_run_counters(self, run_id: str, steps: int = 0, tokens_in: int = 0, tokens_out: int = 0, cost: float | None = None) -> None:
        self.counter_updates.append((run_id, steps, tokens_in, tokens_out))

    async def recompute_run_counters(self, run_id: str) -> None:
        self.counter_updates.append((run_id, "recompute"))


class _FakeBlobStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict]] = []

    async def write(self, run_id: str, event_id: str, payload: dict) -> str:
        self.writes.append((run_id, event_id, payload))
        return f"{run_id}/{event_id}.json.gz"


class _FakeWS:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, Any]] = []

    async def broadcast(self, run_id: str, message: Any) -> None:
        self.broadcasts.append((run_id, message))

    async def broadcast_all(self, message: Any) -> None:
        self.broadcasts.append(("__all__", message))


def _make_event(
    event_type: EventType,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    full_blob_eligible: bool = False,
    raw_payload: dict | None = None,
    timestamp: datetime | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
    tool_name: str | None = None,
    mcp_server: str | None = None,
) -> RawEvent:
    rid = run_id or str(uuid.uuid4())
    return RawEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        run_id=rid,
        parent_run_id=parent_run_id,
        root_run_id=rid if parent_run_id is None else parent_run_id,
        timestamp=timestamp or _utcnow(),
        tool_name=tool_name,
        mcp_server=mcp_server,
        summary=f"{event_type.value}",
        full_blob_eligible=full_blob_eligible,
        raw_payload=raw_payload or {},
        token_input=token_input,
        token_output=token_output,
    )


def _make_worker(
    *,
    batch_size: int = 50,
    batch_timeout: float = 0.05,
) -> tuple[StorageWorker, asyncio.Queue, _FakeDB, _FakeBlobStore, _FakeWS, Stats]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    db = _FakeDB()
    blob = _FakeBlobStore()
    ws = _FakeWS()
    stats = Stats()
    cfg = TraceSageConfig(
        worker_batch_size=batch_size,
        worker_batch_timeout=batch_timeout,
    )
    worker = StorageWorker(queue, db, blob, ws, cfg, stats)
    return worker, queue, db, blob, ws, stats


# ---------------------------------------------------------------------- 1
async def test_worker_processes_batch() -> None:
    worker, queue, db, _blob, _ws, _stats = _make_worker()
    run_id = str(uuid.uuid4())
    for _ in range(10):
        await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))

    task = asyncio.create_task(worker.run())
    # Give the worker time to drain.
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 10:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    total = sum(len(b) for b in db.upserted_events_batches)
    assert total == 10


# ---------------------------------------------------------------------- 2
async def test_worker_db_error_continues() -> None:
    """A db failure on one batch must not kill the worker."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=2, batch_timeout=0.02)
    run_id = str(uuid.uuid4())

    db.fail_next_batch = True
    # First batch (will fail).
    await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))
    await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))

    task = asyncio.create_task(worker.run())
    # Wait until the failure is consumed.
    for _ in range(50):
        if not db.fail_next_batch:
            break
        await asyncio.sleep(0.02)

    # Push more events; these should succeed.
    for _ in range(3):
        await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))

    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 3:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    total = sum(len(b) for b in db.upserted_events_batches)
    assert total >= 3, f"expected at least 3 events processed, got {total}"


# ---------------------------------------------------------------------- 3
async def test_worker_shutdown_drains_queue() -> None:
    """All queued events must be processed before the worker exits."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=20, batch_timeout=0.05)
    run_id = str(uuid.uuid4())
    for _ in range(100):
        await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))

    task = asyncio.create_task(worker.run())
    # Let it start; cancel triggers cancel-path drain.
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    total = sum(len(b) for b in db.upserted_events_batches)
    assert total == 100


# ---------------------------------------------------------------------- 4
async def test_duration_calculation() -> None:
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    run_id = str(uuid.uuid4())
    start_ts = _utcnow()
    end_ts = datetime.fromtimestamp(start_ts.timestamp() + 0.250, tz=UTC)

    await queue.put(
        _make_event(EventType.CHAIN_START, run_id=run_id, timestamp=start_ts)
    )
    await queue.put(
        _make_event(EventType.CHAIN_END, run_id=run_id, timestamp=end_ts)
    )

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 2:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    all_events: list[StoredEvent] = [e for batch in db.upserted_events_batches for e in batch]
    end_event = next(e for e in all_events if e.event_type == EventType.CHAIN_END)
    assert end_event.duration_ms is not None
    assert end_event.duration_ms >= 200  # ~250ms with rounding tolerance


# ---------------------------------------------------------------------- 5
async def test_blob_written_for_eligible_events() -> None:
    worker, queue, db, blob, _ws, _stats = _make_worker(batch_size=5, batch_timeout=0.02)
    run_id = str(uuid.uuid4())

    await queue.put(
        _make_event(
            EventType.LLM_END,
            run_id=run_id,
            full_blob_eligible=True,
            raw_payload={"text": "hello world"},
        )
    )

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if blob.writes:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(blob.writes) == 1
    flat = [e for batch in db.upserted_events_batches for e in batch]
    llm_end = next(e for e in flat if e.event_type == EventType.LLM_END)
    assert llm_end.blob_path is not None
    assert llm_end.blob_path.endswith(".json.gz")


# ---------------------------------------------------------------------- 6
async def test_nested_chain_error_does_not_fail_root() -> None:
    """A nested (parent_run_id set) CHAIN_ERROR that is recovered must NOT mark
    the root run FAILED. The subsequent root CHAIN_END (parent None) wins and the
    only terminal status update is COMPLETED on the root run_id."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    root_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())

    # Root chain starts.
    await queue.put(_make_event(EventType.CHAIN_START, run_id=root_id))
    # Nested sub-chain raises an error (parent_run_id points at the root).
    await queue.put(
        _make_event(
            EventType.CHAIN_ERROR,
            run_id=child_id,
            parent_run_id=root_id,
        )
    )
    # Root chain recovers and completes normally (parent_run_id is None).
    await queue.put(_make_event(EventType.CHAIN_END, run_id=root_id))

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 3:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Exactly one terminal update, on the ROOT run, and it is COMPLETED.
    assert len(db.status_updates) == 1, db.status_updates
    run_id, status, _completed_at, _error = db.status_updates[0]
    assert run_id == root_id
    assert status == RunStatus.COMPLETED
    # The nested error must NOT have produced a FAILED status update.
    assert all(s is not RunStatus.FAILED for _, s, _, _ in db.status_updates)


# ---------------------------------------------------------------------- 7
async def test_root_chain_error_marks_root_failed() -> None:
    """A ROOT-level CHAIN_ERROR (parent_run_id is None) still marks the run FAILED."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    root_id = str(uuid.uuid4())

    await queue.put(_make_event(EventType.CHAIN_START, run_id=root_id))
    await queue.put(_make_event(EventType.CHAIN_ERROR, run_id=root_id))

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 2:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(db.status_updates) == 1, db.status_updates
    run_id, status, _completed_at, _error = db.status_updates[0]
    assert run_id == root_id
    assert status == RunStatus.FAILED


# ---------------------------------------------------------------------- 8b
async def test_mcp_server_passthrough_to_stored_event() -> None:
    """RawEvent.mcp_server must survive into the persisted StoredEvent."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    root_id = str(uuid.uuid4())
    await queue.put(_make_event(EventType.CHAIN_START, run_id=root_id))
    await queue.put(
        _make_event(
            EventType.TOOL_START, run_id=root_id, tool_name="get_weather", mcp_server="weather"
        )
    )

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 2:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    flat = [e for batch in db.upserted_events_batches for e in batch]
    tool_events = [e for e in flat if e.tool_name == "get_weather"]
    assert tool_events
    assert tool_events[0].mcp_server == "weather"


# ---------------------------------------------------------------------- 8
async def test_bare_root_llm_marks_run_completed() -> None:
    """A root-level LLM call with no wrapping chain (parent_run_id is None) must be
    marked COMPLETED on LLM_END, not left RUNNING forever."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    root_id = str(uuid.uuid4())

    await queue.put(_make_event(EventType.LLM_START, run_id=root_id))
    await queue.put(_make_event(EventType.LLM_END, run_id=root_id))

    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if sum(len(b) for b in db.upserted_events_batches) >= 2:
            break
        await asyncio.sleep(0.02)

    worker.request_shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(db.status_updates) == 1, db.status_updates
    run_id, status, _completed_at, _error = db.status_updates[0]
    assert run_id == root_id
    assert status == RunStatus.COMPLETED


# ---------------------------------------------------------------------- 9
async def test_graceful_shutdown_drains_backlog_without_cancel() -> None:
    """request_shutdown() alone must persist the whole backlog before run() returns."""
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    run_id = str(uuid.uuid4())
    for _ in range(95):
        await queue.put(_make_event(EventType.AGENT_ACTION, run_id=run_id))

    worker.request_shutdown()
    await asyncio.wait_for(worker.run(), timeout=5.0)
    await asyncio.wait_for(queue.join(), timeout=1.0)

    assert sum(len(b) for b in db.upserted_events_batches) == 95


# ---------------------------------------------------------------------- 10
async def test_one_bad_event_does_not_abort_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, queue, db, _blob, _ws, _stats = _make_worker(batch_size=10, batch_timeout=0.02)
    run_id = str(uuid.uuid4())
    events = [_make_event(EventType.AGENT_ACTION, run_id=run_id) for _ in range(5)]
    bad_id = events[2].event_id

    real_build = worker._build_stored_event

    async def flaky_build(event: RawEvent) -> StoredEvent:
        if event.event_id == bad_id:
            raise RuntimeError("boom")
        return await real_build(event)

    monkeypatch.setattr(worker, "_build_stored_event", flaky_build)
    for e in events:
        await queue.put(e)

    worker.request_shutdown()
    await asyncio.wait_for(worker.run(), timeout=5.0)

    stored_ids = {e.event_id for b in db.upserted_events_batches for e in b}
    assert stored_ids == {e.event_id for e in events} - {bad_id}


# ---------------------------------------------------------------------- 11
async def test_cancel_mid_batch_replays_inflight_batch(tmp_path) -> None:
    """asyncio.run() teardown after an uncaught exception cancels the worker. A batch
    interrupted mid-write must still be persisted — previously its run row was left
    'running' with only the steps of earlier batches."""
    from tracesage.storage.blob_store import BlobStore
    from tracesage.storage.sqlite_backend import SQLiteBackend

    db = SQLiteBackend(tmp_path / "t.db")
    await db.init()
    queue: asyncio.Queue = asyncio.Queue()
    cfg = TraceSageConfig(worker_batch_size=50, worker_batch_timeout=0.02)
    worker = StorageWorker(queue, db, BlobStore(tmp_path / "blobs"), _FakeWS(), cfg, Stats())

    real_upsert = db.upsert_events_batch
    entered = asyncio.Event()
    calls = 0

    async def hang_once(events: list[StoredEvent]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.sleep(30)  # cancelled here
        await real_upsert(events)

    db.upsert_events_batch = hang_once  # type: ignore[method-assign]

    rid = str(uuid.uuid4())
    t0 = _utcnow()
    t1 = datetime.fromtimestamp(t0.timestamp() + 0.2, tz=UTC)
    for ev in (
        _make_event(EventType.RUN_START, run_id=rid, timestamp=t0),
        _make_event(EventType.CHAIN_START, run_id=rid, timestamp=t0),
        _make_event(EventType.CHAIN_END, run_id=rid, timestamp=t1, token_output=7),
    ):
        await queue.put(ev)

    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    run = await db.get_run(rid)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.total_steps == 3
    assert run.total_tokens_output == 7
    end = next(e for e in await db.get_journey(rid) if e.event_type == EventType.CHAIN_END)
    assert end.duration_ms is not None
    assert end.duration_ms >= 150
    assert queue.empty()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
