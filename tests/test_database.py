"""Tests for SQLiteBackend."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from tracelens.models import EventType, Run, RunStatus, StoredEvent
from tracelens.storage import SQLiteBackend


def _now() -> datetime:
    # CLAUDE.md mandates timezone.utc spelling, not datetime.UTC alias.
    return datetime.now(timezone.utc)


def _make_run(
    run_id: str,
    *,
    root_run_id: str | None = None,
    status: RunStatus = RunStatus.RUNNING,
    started_at: datetime | None = None,
    tags: list[str] | None = None,
) -> Run:
    return Run(
        run_id=run_id,
        root_run_id=root_run_id or run_id,
        status=status,
        started_at=started_at or _now(),
        tags=tags or [],
    )


def _make_event(
    event_id: str,
    run_id: str,
    *,
    root_run_id: str | None = None,
    parent_run_id: str | None = None,
    event_type: EventType = EventType.CHAIN_START,
    timestamp: datetime | None = None,
    agent_name: str | None = None,
    tool_name: str | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        root_run_id=root_run_id or run_id,
        event_type=event_type,
        timestamp=timestamp or _now(),
        agent_name=agent_name,
        tool_name=tool_name,
        summary=f"summary-{event_id}",
        duration_ms=duration_ms,
        error_message=error_message,
    )


@pytest_asyncio.fixture
async def backend(tmp_data_dir: Path) -> AsyncIterator[SQLiteBackend]:
    be = SQLiteBackend(tmp_data_dir / "test.db")
    await be.init()
    try:
        yield be
    finally:
        await be.close()


@pytest.mark.asyncio
async def test_schema_created_on_init(backend: SQLiteBackend) -> None:
    async with backend._conn() as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
        await cur.close()
    names = {r["name"] for r in rows}
    assert "runs" in names
    assert "events" in names


@pytest.mark.asyncio
async def test_upsert_run_idempotent(backend: SQLiteBackend) -> None:
    started = _now()
    r1 = _make_run("r1", status=RunStatus.RUNNING, started_at=started)
    await backend.upsert_run(r1)

    r2 = _make_run("r1", status=RunStatus.COMPLETED, started_at=started)
    r2 = r2.model_copy(update={"completed_at": _now(), "total_steps": 7})
    await backend.upsert_run(r2)

    got = await backend.get_run("r1")
    assert got is not None
    assert got.status == RunStatus.COMPLETED
    assert got.total_steps == 7

    # Still only one row.
    runs, total = await backend.list_runs()
    assert total == 1
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_upsert_event_idempotent(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    e1 = _make_event("e1", "r1", agent_name="A1")
    await backend.upsert_event(e1)

    e2 = _make_event("e1", "r1", agent_name="A2", duration_ms=500)
    await backend.upsert_event(e2)

    got = await backend.get_event("e1")
    assert got is not None
    assert got.agent_name == "A2"
    assert got.duration_ms == 500

    journey = await backend.get_journey("r1")
    assert len(journey) == 1


@pytest.mark.asyncio
async def test_get_journey_uses_root_run_id(backend: SQLiteBackend) -> None:
    parent = _make_run("parent")
    child = _make_run("child", root_run_id="parent")
    await backend.upsert_run(parent)
    await backend.upsert_run(child)

    base = _now()
    e1 = _make_event(
        "e1", "parent", root_run_id="parent", timestamp=base
    )
    e2 = _make_event(
        "e2",
        "child",
        root_run_id="parent",
        parent_run_id="parent",
        timestamp=base + timedelta(milliseconds=10),
    )
    e3 = _make_event(
        "e3",
        "child",
        root_run_id="parent",
        parent_run_id="parent",
        timestamp=base + timedelta(milliseconds=20),
    )
    await backend.upsert_event(e1)
    await backend.upsert_event(e2)
    await backend.upsert_event(e3)

    journey = await backend.get_journey("parent")
    assert [e.event_id for e in journey] == ["e1", "e2", "e3"]


@pytest.mark.asyncio
async def test_list_runs_pagination(backend: SQLiteBackend) -> None:
    base = _now()
    for i in range(100):
        await backend.upsert_run(
            _make_run(
                f"r{i:03d}",
                started_at=base + timedelta(seconds=i),
            )
        )

    runs, total = await backend.list_runs(limit=10, offset=10)
    assert total == 100
    assert len(runs) == 10
    # Sorted by started_at DESC. r099 is newest, so offset=10 starts at r089.
    assert runs[0].run_id == "r089"
    assert runs[-1].run_id == "r080"


@pytest.mark.asyncio
async def test_list_runs_status_filter(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("a", status=RunStatus.RUNNING))
    await backend.upsert_run(_make_run("b", status=RunStatus.COMPLETED))
    await backend.upsert_run(_make_run("c", status=RunStatus.COMPLETED))
    await backend.upsert_run(_make_run("d", status=RunStatus.FAILED))

    runs, total = await backend.list_runs(status="completed")
    assert total == 2
    assert {r.run_id for r in runs} == {"b", "c"}

    runs, total = await backend.list_runs(status="failed")
    assert total == 1
    assert runs[0].run_id == "d"

    runs, total = await backend.list_runs(status="all")
    assert total == 4


@pytest.mark.asyncio
async def test_concurrent_writes(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    base = _now()

    async def write(i: int) -> None:
        await backend.upsert_event(
            _make_event(
                f"e{i:02d}",
                "r1",
                timestamp=base + timedelta(microseconds=i),
            )
        )

    await asyncio.gather(*(write(i) for i in range(20)))

    journey = await backend.get_journey("r1")
    assert len(journey) == 20


@pytest.mark.asyncio
async def test_increment_run_counters_atomic(
    backend: SQLiteBackend,
) -> None:
    await backend.upsert_run(_make_run("r1"))

    async def inc() -> None:
        await backend.increment_run_counters(
            "r1", steps=1, tokens_in=100, tokens_out=200
        )

    await asyncio.gather(*(inc() for _ in range(50)))

    got = await backend.get_run("r1")
    assert got is not None
    assert got.total_steps == 50
    assert got.total_tokens_input == 5000
    assert got.total_tokens_output == 10000


@pytest.mark.asyncio
async def test_delete_run_cascades_events(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    for i in range(5):
        await backend.upsert_event(_make_event(f"e{i}", "r1"))

    journey = await backend.get_journey("r1")
    assert len(journey) == 5

    await backend.delete_run("r1")

    assert await backend.get_run("r1") is None
    journey = await backend.get_journey("r1")
    assert journey == []


@pytest.mark.asyncio
async def test_get_stats_correct_counts(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("a", status=RunStatus.RUNNING))
    await backend.upsert_run(_make_run("b", status=RunStatus.RUNNING))
    await backend.upsert_run(_make_run("c", status=RunStatus.COMPLETED))
    await backend.upsert_run(_make_run("d", status=RunStatus.COMPLETED))
    await backend.upsert_run(_make_run("e", status=RunStatus.FAILED))

    # add some duration data
    await backend.upsert_event(
        _make_event(
            "evt1", "c", event_type=EventType.CHAIN_END, duration_ms=100
        )
    )
    await backend.upsert_event(
        _make_event(
            "evt2", "d", event_type=EventType.CHAIN_END, duration_ms=300
        )
    )

    stats = await backend.get_stats()
    assert stats["total_runs"] == 5
    assert stats["running"] == 2
    assert stats["completed"] == 2
    assert stats["failed"] == 1
    assert stats["avg_duration_ms"] == pytest.approx(200.0)
    assert stats["db_size_bytes"] > 0


@pytest.mark.asyncio
async def test_get_topology_aggregates(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    base = _now()

    # Planner agent: one start, one error (so one invocation, one error).
    parent_evt = _make_event(
        "p1",
        "r1",
        agent_name="Planner",
        timestamp=base,
        event_type=EventType.CHAIN_START,
    )
    # Two child tool calls: each with a paired start + end so the
    # invocation count = number of starts = 2.
    child_start1 = _make_event(
        "c1s",
        "r1",
        parent_run_id="r1",
        tool_name="search",
        timestamp=base + timedelta(milliseconds=2),
        event_type=EventType.TOOL_START,
    )
    child_end1 = _make_event(
        "c1",
        "r1",
        parent_run_id="r1",
        tool_name="search",
        timestamp=base + timedelta(milliseconds=5),
        event_type=EventType.TOOL_END,
        duration_ms=50,
    )
    child_start2 = _make_event(
        "c2s",
        "r1",
        parent_run_id="r1",
        tool_name="search",
        timestamp=base + timedelta(milliseconds=7),
        event_type=EventType.TOOL_START,
    )
    child_end2 = _make_event(
        "c2",
        "r1",
        parent_run_id="r1",
        tool_name="search",
        timestamp=base + timedelta(milliseconds=10),
        event_type=EventType.TOOL_END,
        duration_ms=70,
    )
    # Error event for the agent (no matching chain_end — the agent failed).
    err_evt = _make_event(
        "err",
        "r1",
        agent_name="Planner",
        timestamp=base + timedelta(milliseconds=15),
        event_type=EventType.CHAIN_ERROR,
        error_message="boom",
    )

    for e in (parent_evt, child_start1, child_end1, child_start2, child_end2, err_evt):
        await backend.upsert_event(e)

    topo = await backend.get_topology()

    node_ids = {n.id for n in topo.nodes}
    assert "agent:Planner" in node_ids
    assert "tool:search" in node_ids

    planner = next(n for n in topo.nodes if n.id == "agent:Planner")
    # invocation_count counts *_start events only: one CHAIN_START.
    assert planner.invocation_count == 1
    assert planner.error_count >= 1

    search = next(n for n in topo.nodes if n.id == "tool:search")
    # Two TOOL_START events.
    assert search.invocation_count == 2
    assert search.avg_duration_ms == pytest.approx(60.0)

    assert len(topo.edges) >= 1
    edge_pairs = {(e.source, e.target) for e in topo.edges}
    assert ("agent:Planner", "tool:search") in edge_pairs


@pytest.mark.asyncio
async def test_database_recovers_from_locked_db(
    tmp_data_dir: Path,
) -> None:
    """WAL mode should let two connections write without locking each other out."""
    db_path = tmp_data_dir / "wal.db"
    backend = SQLiteBackend(db_path)
    await backend.init()
    try:
        await backend.upsert_run(_make_run("r1"))

        # Open a second raw aiosqlite connection. With WAL, both should
        # successfully write.
        async with aiosqlite.connect(db_path) as conn2:
            for pragma in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA foreign_keys=ON",
            ):
                await conn2.execute(pragma)
            conn2.row_factory = aiosqlite.Row

            async def write_via_backend() -> None:
                await backend.upsert_run(_make_run("r2"))

            async def write_via_raw() -> None:
                await conn2.execute(
                    "INSERT INTO runs (run_id, root_run_id, tags, status, "
                    "started_at, total_steps, total_tokens_input, "
                    "total_tokens_output) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "r3",
                        "r3",
                        json.dumps([]),
                        "running",
                        _now().isoformat(),
                        0,
                        0,
                        0,
                    ),
                )
                await conn2.commit()

            await asyncio.gather(write_via_backend(), write_via_raw())

        runs, total = await backend.list_runs()
        ids = {r.run_id for r in runs}
        assert {"r1", "r2", "r3"} <= ids
        assert total >= 3
    finally:
        await backend.close()
