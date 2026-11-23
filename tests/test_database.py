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

from tracesage.models import EventType, Run, RunStatus, StoredEvent
from tracesage.storage import SQLiteBackend


def _now() -> datetime:
    # The project uses the timezone.utc spelling, not the datetime.UTC alias.
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
    mcp_server: str | None = None,
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
        mcp_server=mcp_server,
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

    # A late auto-create upsert (RUNNING, zero counters, later start) must not
    # resurrect the finished run or wipe its counters.
    late = _make_run("r1", status=RunStatus.RUNNING, started_at=_now())
    await backend.upsert_run(late)
    got = await backend.get_run("r1")
    assert got is not None
    assert got.status == RunStatus.COMPLETED
    assert got.completed_at is not None
    assert got.total_steps == 7
    assert got.started_at == started

    # Still only one row.
    runs, total = await backend.list_runs()
    assert total == 1
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_list_runs_tag_filter_applies_before_pagination(
    backend: SQLiteBackend,
) -> None:
    # 3 "prod" runs, 2 "dev" runs. The tag filter must run in SQL so that
    # limit/offset and the total count reflect the *filtered* set — not be
    # applied after a page has already been truncated.
    for i in range(5):
        await backend.upsert_run(
            _make_run(f"r{i}", tags=["prod"] if i % 2 == 0 else ["dev"])
        )

    rows, total = await backend.list_runs(tag="prod", limit=2, offset=0)
    assert total == 3
    assert len(rows) == 2
    assert all("prod" in r.tags for r in rows)

    rows, total = await backend.list_runs(tag="prod", limit=2, offset=2)
    assert total == 3
    assert len(rows) == 1

    # Substring match, and a non-matching tag yields nothing.
    rows, total = await backend.list_runs(tag="pro")
    assert total == 3
    rows, total = await backend.list_runs(tag="nope")
    assert total == 0
    assert rows == []


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
async def test_iter_journey_matches_get_journey(backend: SQLiteBackend) -> None:
    """iter_journey streams the same events, in the same order, as get_journey."""
    parent = _make_run("parent")
    child = _make_run("child", root_run_id="parent")
    await backend.upsert_run(parent)
    await backend.upsert_run(child)

    base = _now()
    events = [
        _make_event(
            f"e{i}",
            "child" if i % 2 else "parent",
            root_run_id="parent",
            parent_run_id="parent" if i % 2 else None,
            timestamp=base + timedelta(milliseconds=i),
        )
        for i in range(7)
    ]
    for e in events:
        await backend.upsert_event(e)

    expected = await backend.get_journey("parent")
    # Use a small batch_size to exercise the fetchmany loop across batches.
    streamed = [e async for e in backend.iter_journey("parent", batch_size=2)]

    assert [e.event_id for e in streamed] == [e.event_id for e in expected]
    assert [e.event_id for e in streamed] == [f"e{i}" for i in range(7)]


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
async def test_update_run_status_is_monotonic(backend: SQLiteBackend) -> None:
    """A terminal status must never be overwritten by a later terminal event.

    First-terminal-wins: once a run is COMPLETED, an out-of-order FAILED event
    (e.g. a recovered nested chain error) must not flip the root run.
    """
    await backend.upsert_run(_make_run("r1", status=RunStatus.RUNNING))

    completed_at = _now()
    await backend.update_run_status(
        "r1", RunStatus.COMPLETED, completed_at=completed_at
    )

    got = await backend.get_run("r1")
    assert got is not None
    assert got.status == RunStatus.COMPLETED

    # A later, out-of-order terminal event must NOT change the status.
    await backend.update_run_status(
        "r1", RunStatus.FAILED, completed_at=_now(), error="boom"
    )

    got = await backend.get_run("r1")
    assert got is not None
    assert got.status == RunStatus.COMPLETED
    assert got.error_message is None


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
async def test_get_topology_populates_p99(backend: SQLiteBackend) -> None:
    """Nodes with duration samples must report a non-None, non-negative p99."""
    await backend.upsert_run(_make_run("r1"))
    base = _now()

    # Ten tool invocations with increasing durations (10..100ms). Each is a
    # paired TOOL_START / TOOL_END; only the END carries duration_ms.
    events: list[StoredEvent] = []
    for i in range(10):
        events.append(
            _make_event(
                f"s{i}",
                "r1",
                parent_run_id="r1",
                tool_name="search",
                timestamp=base + timedelta(milliseconds=2 * i),
                event_type=EventType.TOOL_START,
            )
        )
        events.append(
            _make_event(
                f"e{i}",
                "r1",
                parent_run_id="r1",
                tool_name="search",
                timestamp=base + timedelta(milliseconds=2 * i + 1),
                event_type=EventType.TOOL_END,
                duration_ms=(i + 1) * 10,
            )
        )
    for e in events:
        await backend.upsert_event(e)

    topo = await backend.get_topology()
    search = next(n for n in topo.nodes if n.id == "tool:search")
    assert search.p99_duration_ms is not None
    assert search.p99_duration_ms >= 0
    # p99 must lie within the observed range [10, 100].
    assert 10 <= search.p99_duration_ms <= 100


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


async def test_iter_journey_releases_pool_between_batches(tmp_data_dir: Path) -> None:
    """iter_journey must NOT hold a pool slot across yields.

    With pool_size=1, if iter_journey held the single connection open for the
    whole stream (the old behaviour), a concurrent query issued mid-iteration
    would deadlock on the semaphore. The keyset-paginated impl releases the slot
    between batches, so the concurrent get_run completes.
    """
    be = SQLiteBackend(tmp_data_dir / "pool1.db", pool_size=1)
    await be.init()
    try:
        await be.upsert_run(_make_run("r1"))
        base = _now()
        for i in range(3):
            await be.upsert_event(
                _make_event(f"e{i}", "r1", timestamp=base + timedelta(milliseconds=i))
            )
        seen: list[str] = []
        async for ev in be.iter_journey("r1", batch_size=1):
            seen.append(ev.event_id)
            # Mid-iteration concurrent query; would hang forever with pool_size=1
            # if iter_journey still held the only connection across the yield.
            got = await asyncio.wait_for(be.get_run("r1"), timeout=5.0)
            assert got is not None
        assert seen == ["e0", "e1", "e2"]
    finally:
        await be.close()


async def test_delete_run_removes_whole_tree(backend: SQLiteBackend) -> None:
    """delete_run must remove the root run, its auto-created sub-run rows, and ALL
    descendant events (matched by root_run_id), not just the single root row."""
    await backend.upsert_run(_make_run("r1"))
    await backend.upsert_run(_make_run("s1", root_run_id="r1"))  # auto-created sub-run
    await backend.upsert_event(_make_event("e_root", "r1", root_run_id="r1"))
    await backend.upsert_event(
        _make_event("e_sub", "s1", root_run_id="r1", parent_run_id="r1")
    )
    # Sanity: journey (root_run_id-scoped) sees both events before delete.
    assert {e.event_id for e in await backend.get_journey("r1")} == {"e_root", "e_sub"}

    await backend.delete_run("r1")

    assert await backend.get_run("r1") is None
    assert await backend.get_run("s1") is None  # sub-run row gone (was orphaned before)
    assert await backend.get_journey("r1") == []  # no descendant events linger


# ---------- MCP tool-source attribution ----------


@pytest.mark.parametrize("legacy_version", [0, 1])
async def test_schema_v1_to_v3_migration(tmp_data_dir: Path, legacy_version: int) -> None:
    """A pre-v2 DB (events table without mcp_server) must be upgraded in place by
    init() straight to the current schema: the mcp_server column is added, the v3
    mcp_tools table is created and usable, and existing rows survive. Covers both
    user_version=1 and unversioned (0) legacy DBs created before stamping existed."""
    db_path = tmp_data_dir / "v1.db"
    # Hand-build a v1 database: old events schema (no mcp_server), user_version=1.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, root_run_id TEXT NOT NULL, "
            "tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'running', "
            "started_at TEXT NOT NULL, completed_at TEXT, total_steps INTEGER NOT NULL "
            "DEFAULT 0, total_tokens_input INTEGER NOT NULL DEFAULT 0, "
            "total_tokens_output INTEGER NOT NULL DEFAULT 0, graph_definition TEXT, "
            "error_message TEXT)"
        )
        await conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "parent_run_id TEXT, root_run_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, agent_name TEXT, tool_name TEXT, summary TEXT "
            "NOT NULL, blob_path TEXT, duration_ms INTEGER, token_input INTEGER, "
            "token_output INTEGER, error_message TEXT)"
        )
        await conn.execute(
            "INSERT INTO runs (run_id, root_run_id, started_at) VALUES (?, ?, ?)",
            ("old", "old", _now().isoformat()),
        )
        await conn.execute(
            "INSERT INTO events (event_id, run_id, root_run_id, event_type, timestamp, "
            "tool_name, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("e0", "old", "old", "tool_start", _now().isoformat(), "legacy_tool", "s"),
        )
        if legacy_version:
            await conn.execute("PRAGMA user_version = 1")
        await conn.commit()

    be = SQLiteBackend(db_path)
    await be.init()  # should ALTER TABLE events ADD COLUMN mcp_server
    try:
        # Old row preserved, new column readable as None.
        journey = await be.get_journey("old")
        assert len(journey) == 1
        assert journey[0].tool_name == "legacy_tool"
        assert journey[0].mcp_server is None
        # New events with mcp_server now insert fine (column exists).
        await be.upsert_event(
            _make_event("e1", "old", event_type=EventType.TOOL_START,
                        tool_name="new_tool", mcp_server="weather")
        )
        async with be._conn() as conn:
            cur = await conn.execute("PRAGMA user_version")
            ver = (await cur.fetchone())[0]
            await cur.close()
        assert ver == 6  # v1 DB upgrades straight to the current schema version
        old = await be.get_run("old")
        assert old is not None
        assert old.thread_id is None  # v4 column added to the legacy runs table
        # The v3 mcp_tools table must exist AND be usable post-migration — a
        # regression that bumped the version without creating it would fail here.
        await be.upsert_mcp_tools("weather", ["get_weather", "air_quality"])
        assert await be.get_mcp_tools() == {"weather": ["air_quality", "get_weather"]}
    finally:
        await be.close()


async def test_schema_v2_to_v3_migration_adds_mcp_tools(tmp_data_dir: Path) -> None:
    """A v2 DB (events HAS mcp_server, user_version=2, but NO mcp_tools table) must
    upgrade to v3 by creating mcp_tools — without re-running the v1->v2 ALTER (which
    would fail because the column already exists)."""
    db_path = tmp_data_dir / "v2.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, root_run_id TEXT NOT NULL, "
            "tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'running', "
            "started_at TEXT NOT NULL, completed_at TEXT, total_steps INTEGER NOT NULL "
            "DEFAULT 0, total_tokens_input INTEGER NOT NULL DEFAULT 0, "
            "total_tokens_output INTEGER NOT NULL DEFAULT 0, graph_definition TEXT, "
            "error_message TEXT)"
        )
        # v2 events schema: mcp_server column present, but no mcp_tools table.
        await conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "parent_run_id TEXT, root_run_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, agent_name TEXT, tool_name TEXT, summary TEXT "
            "NOT NULL, blob_path TEXT, duration_ms INTEGER, token_input INTEGER, "
            "token_output INTEGER, error_message TEXT, mcp_server TEXT)"
        )
        await conn.execute(
            "INSERT INTO runs (run_id, root_run_id, started_at) VALUES (?, ?, ?)",
            ("r2", "r2", _now().isoformat()),
        )
        await conn.execute(
            "INSERT INTO events (event_id, run_id, root_run_id, event_type, timestamp, "
            "tool_name, summary, mcp_server) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("e0", "r2", "r2", "tool_start", _now().isoformat(), "get_weather", "s", "weather"),
        )
        await conn.execute("PRAGMA user_version = 2")
        await conn.commit()

    be = SQLiteBackend(db_path)
    await be.init()  # should CREATE TABLE mcp_tools and bump to v3, NOT re-ALTER events
    try:
        # Existing row + its mcp_server survive.
        journey = await be.get_journey("r2")
        assert len(journey) == 1
        assert journey[0].tool_name == "get_weather"
        assert journey[0].mcp_server == "weather"
        # The newly-created mcp_tools table is queryable.
        await be.upsert_mcp_tools("math", ["add", "multiply"])
        assert await be.get_mcp_tools() == {"math": ["add", "multiply"]}
        async with be._conn() as conn:
            cur = await conn.execute("PRAGMA user_version")
            ver = (await cur.fetchone())[0]
            await cur.close()
        assert ver == 6
    finally:
        await be.close()


async def test_thread_id_persisted_and_filterable(backend: SQLiteBackend) -> None:
    for rid, thread in (("a", "t1"), ("b", "t1"), ("c", "t2"), ("d", None)):
        await backend.upsert_run(
            Run(run_id=rid, root_run_id=rid, started_at=_now(), thread_id=thread)
        )
    runs, total = await backend.list_runs(thread_id="t1")
    assert total == 2
    assert {r.run_id for r in runs} == {"a", "b"}
    # A later upsert without a thread_id (e.g. a sub-event's run row) keeps it.
    await backend.upsert_run(Run(run_id="a", root_run_id="a", started_at=_now()))
    run_a = await backend.get_run("a")
    assert run_a is not None
    assert run_a.thread_id == "t1"


async def test_list_runs_sort(backend: SQLiteBackend) -> None:
    base = _now()
    for i, (rid, steps) in enumerate((("old", 9), ("mid", 1), ("new", 4))):
        run = Run(run_id=rid, root_run_id=rid, started_at=base + timedelta(seconds=i),
                  total_steps=steps)
        await backend.upsert_run(run)
    order = {}
    for sort in ("recent", "oldest", "steps"):
        runs, total = await backend.list_runs(sort=sort)
        assert total == 3
        order[sort] = [r.run_id for r in runs]
    assert order == {
        "recent": ["new", "mid", "old"],
        "oldest": ["old", "mid", "new"],
        "steps": ["old", "new", "mid"],
    }


async def test_list_threads_sort_min_turns_and_text_search(backend: SQLiteBackend) -> None:
    base = _now()
    turns = (("a1", "long", 0), ("a2", "long", 1), ("a3", "long", 2), ("b1", "short", 5))
    for rid, thread, offset in turns:
        await backend.upsert_run(Run(run_id=rid, root_run_id=rid, thread_id=thread,
                                     started_at=base + timedelta(seconds=offset)))
    await backend.upsert_event(StoredEvent(
        event_id="e", run_id="a2", root_run_id="a2", event_type=EventType.CHAIN_START,
        timestamp=base, summary="hello", search_text="walrus-needle",
    ))
    recent, _ = await backend.list_threads()
    assert [t.thread_id for t in recent] == ["short", "long"]
    by_turns, _ = await backend.list_threads(sort="turns")
    assert [t.thread_id for t in by_turns] == ["long", "short"]
    longer, total = await backend.list_threads(min_turns=2)
    assert ([t.thread_id for t in longer], total) == (["long"], 1)
    found, total = await backend.list_threads(q="walrus-needle")
    assert ([t.thread_id for t in found], total) == (["long"], 1)


async def _seed_filter_matrix(backend: SQLiteBackend) -> list[Run]:
    """Runs across sources, statuses and threads, with distinct sizes and times."""
    base = _now()
    spec = [
        # run_id, tags, status, thread, steps
        ("c1", ["source:cursor"], RunStatus.COMPLETED, "tc", 5),
        ("c2", ["source:cursor"], RunStatus.FAILED, "tc", 9),
        ("c3", ["source:cursor"], RunStatus.COMPLETED, "tc", 2),
        ("d1", ["source:claude-desktop"], RunStatus.COMPLETED, "td", 7),
        ("d2", ["source:claude-desktop"], RunStatus.INTERRUPTED, "td", 1),
        ("k1", ["source:claude-code"], RunStatus.COMPLETED, "tk", 3),
        ("y1", [], RunStatus.COMPLETED, "ty", 4),
        ("y2", ["mine"], RunStatus.FAILED, "ty", 8),
        ("y3", [], RunStatus.COMPLETED, None, 6),
    ]
    runs = []
    for i, (rid, tags, status, thread, steps) in enumerate(spec):
        run = Run(run_id=rid, root_run_id=rid, tags=tags, status=status, thread_id=thread,
                  started_at=base + timedelta(seconds=i), total_steps=steps)
        runs.append(run)
        await backend.upsert_run(run)
        await backend.update_run_status(rid, status, completed_at=base + timedelta(seconds=i))
    await backend.upsert_event(StoredEvent(
        event_id="ev", run_id="c2", root_run_id="c2", event_type=EventType.CHAIN_START,
        timestamp=base, summary="s", search_text="zebra-needle",
    ))
    await backend.upsert_event(StoredEvent(
        event_id="ev2", run_id="y2", root_run_id="y2", event_type=EventType.CHAIN_START,
        timestamp=base, summary="s", search_text="zebra-needle",
    ))
    return runs


def _has_source(run: Run) -> bool:
    return any(t.startswith("source:") for t in run.tags)


async def test_list_runs_filter_combinations(backend: SQLiteBackend) -> None:
    runs = await _seed_filter_matrix(backend)
    sources = {"all": None, "cursor": "source:cursor", "claude-desktop": "source:claude-desktop",
               "codex": "source:codex", "yours": "yours"}
    statuses = ["all", "completed", "failed", "interrupted", "running"]
    sorts = {
        "recent": lambda r: [x.run_id for x in sorted(r, key=lambda x: x.started_at, reverse=True)],
        "oldest": lambda r: [x.run_id for x in sorted(r, key=lambda x: x.started_at)],
        "steps": lambda r: [x.run_id for x in sorted(
            r, key=lambda x: (x.total_steps, x.started_at), reverse=True)],
    }
    for source, tag in sources.items():
        for status in statuses:
            for query in (None, "zebra-needle"):
                for sort, expected_order in sorts.items():
                    want = [
                        r for r in runs
                        if (tag is None
                            or (not _has_source(r) if tag == "yours" else tag in r.tags))
                        and (status == "all" or r.status.value == status)
                        and (query is None or r.run_id in {"c2", "y2"})
                    ]
                    got, total = await backend.list_runs(
                        status=status,
                        tag=None if tag in (None, "yours") else tag,
                        no_source=tag == "yours",
                        q=query,
                        sort=sort,
                    )
                    label = (source, status, query, sort)
                    assert total == len(want), label
                    assert [r.run_id for r in got] == expected_order(want), label


async def test_list_threads_filter_combinations(backend: SQLiteBackend) -> None:
    runs = await _seed_filter_matrix(backend)
    in_threads = [r for r in runs if r.thread_id]
    sources = {"all": None, "cursor": "source:cursor", "codex": "source:codex", "yours": "yours"}
    for source, tag in sources.items():
        for min_turns in (1, 2, 3):
            for query in (None, "zebra-needle", "tc"):
                for sort in ("recent", "oldest", "turns"):
                    pool = [
                        r for r in in_threads
                        if tag is None or (not _has_source(r) if tag == "yours" else tag in r.tags)
                    ]
                    by_thread: dict[str, list[Run]] = {}
                    for r in pool:
                        by_thread.setdefault(str(r.thread_id), []).append(r)
                    if query == "zebra-needle":
                        hit = {"tc", "ty"}
                    elif query == "tc":
                        hit = {"tc"}
                    else:
                        hit = None
                    want = {
                        t: rs for t, rs in by_thread.items()
                        if len(rs) >= min_turns and (hit is None or t in hit)
                    }
                    got, total = await backend.list_threads(
                        tag=None if tag in (None, "yours") else tag,
                        no_source=tag == "yours",
                        min_turns=min_turns,
                        q=query,
                        sort=sort,
                    )
                    label = (source, min_turns, query, sort)
                    assert total == len(want), label
                    assert {t.thread_id for t in got} == set(want), label
                    if sort == "turns":
                        counts = [t.run_count for t in got]
                        assert counts == sorted(counts, reverse=True), label


async def test_interrupted_status_counted_in_stats(backend: SQLiteBackend) -> None:
    await backend.upsert_run(Run(run_id="p", root_run_id="p", started_at=_now()))
    await backend.update_run_status("p", RunStatus.INTERRUPTED, completed_at=_now())
    runs, _ = await backend.list_runs(status="interrupted")
    assert [r.run_id for r in runs] == ["p"]
    assert (await backend.get_stats())["interrupted"] == 1


async def test_get_topology_sets_tool_source(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    await backend.upsert_event(
        _make_event("e1", "r1", event_type=EventType.TOOL_START,
                    tool_name="get_weather", mcp_server="weather")
    )
    topo = await backend.get_topology()
    tool_nodes = {n.id: n for n in topo.nodes if n.type == "tool"}
    assert "tool:get_weather" in tool_nodes
    assert tool_nodes["tool:get_weather"].source == "weather"


async def test_get_topology_adds_mcp_nodes_and_edges(backend: SQLiteBackend) -> None:
    """Each MCP server becomes an mcp:<server> node, with an edge to each of its tools."""
    await backend.upsert_run(_make_run("r1"))
    await backend.upsert_event(
        _make_event("e1", "r1", event_type=EventType.TOOL_START,
                    tool_name="get_weather", mcp_server="weather")
    )
    await backend.upsert_event(
        _make_event("e2", "r1", event_type=EventType.TOOL_START,
                    tool_name="add", mcp_server="math")
    )
    await backend.upsert_event(
        _make_event("e3", "r1", event_type=EventType.TOOL_START, tool_name="local_calc")
    )
    topo = await backend.get_topology()
    nodes = {n.id: n for n in topo.nodes}
    # MCP servers are first-class mcp nodes...
    assert nodes["mcp:weather"].type == "mcp"
    assert nodes["mcp:weather"].source == "weather"
    assert "mcp:math" in nodes
    # ...with a provides-edge to each of their tools, and none for the local tool.
    edge_pairs = {(e.source, e.target) for e in topo.edges}
    assert ("mcp:weather", "tool:get_weather") in edge_pairs
    assert ("mcp:math", "tool:add") in edge_pairs
    assert not any(src.startswith("mcp:") and tgt == "tool:local_calc"
                   for src, tgt in edge_pairs)


async def test_topology_scope_excludes_stale_runs(backend: SQLiteBackend) -> None:
    """Scoping keeps a removed component (here an MCP server) from lingering: an
    older run used 'weather'; the newer run uses 'news'. 'all' shows both; the newer
    run / last_n:1 show only 'news' — even though 'weather' is still registered."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)

    async def seed(rid: str, secs: int, server: str, tool: str) -> None:
        await backend.upsert_run(_make_run(rid, started_at=t0 + timedelta(seconds=secs)))
        await backend.upsert_run(_make_run(rid + "t", root_run_id=rid,
                                           started_at=t0 + timedelta(seconds=secs)))
        await backend.upsert_event(_make_event(rid + "a", rid, event_type=EventType.CHAIN_START,
                                               agent_name="agent", timestamp=t0 + timedelta(seconds=secs)))
        await backend.upsert_event(_make_event(rid + "b", rid + "t", root_run_id=rid,
                                               parent_run_id=rid, event_type=EventType.TOOL_START,
                                               tool_name=tool, mcp_server=server,
                                               timestamp=t0 + timedelta(seconds=secs)))

    await seed("run1", 0, "weather", "get_weather")
    await seed("run2", 100, "news", "fetch_news")
    # 'weather' stays in the registry even though the app no longer uses it.
    await backend.upsert_mcp_tools("weather", ["get_weather", "air_quality"])
    await backend.upsert_mcp_tools("news", ["fetch_news"])

    def servers(topo) -> set[str]:
        return {n.name for n in topo.nodes if n.type == "mcp"}

    assert servers(await backend.get_topology()) == {"weather", "news"}          # all-time
    assert servers(await backend.get_topology(scope="last_n:1")) == {"news"}     # latest run
    assert servers(await backend.get_topology(scope="run:run1")) == {"weather"}  # that run
    assert servers(await backend.get_topology(scope="run:run2")) == {"news"}     # stale gone
    await backend.upsert_run(Run(
        run_id="run1", root_run_id="run1", started_at=t0, thread_id="chat-a",
    ))
    await backend.upsert_run(Run(
        run_id="run2", root_run_id="run2", started_at=t0 + timedelta(seconds=100),
        thread_id="chat-a",
    ))
    assert servers(await backend.get_topology(scope="thread:chat-a")) == {"weather", "news"}
    assert (await backend.get_topology(scope="thread:missing")).nodes == []

    # Tools-by-source scopes the same way.
    inv = await backend.get_tool_inventory(scope="run:run2")
    assert {s["source"] for s in inv["sources"]} == {"news"}
    inv_all = await backend.get_tool_inventory()
    assert {s["source"] for s in inv_all["sources"]} == {"weather", "news"}

    # A scope that matches no run returns empty, not all-time.
    assert (await backend.get_topology(scope="run:nope")).nodes == []


async def test_topology_shows_registered_tools_and_agent_mcp_link(backend: SQLiteBackend) -> None:
    """Registered MCP tools appear (even uncalled), and an agent that calls an
    MCP tool is linked to that MCP server (agent -> mcp edge)."""
    await backend.upsert_run(_make_run("ag"))
    # Registry knows 3 weather tools; only get_weather is actually invoked.
    await backend.upsert_mcp_tools("weather", ["get_weather", "get_forecast", "rare_tool"])
    await backend.upsert_run(_make_run("t1", root_run_id="ag"))  # sub-run row for the tool (FK)
    await backend.upsert_event(
        _make_event("e_ag", "ag", event_type=EventType.CHAIN_START, agent_name="researcher")
    )
    await backend.upsert_event(
        _make_event("e_tool", "t1", root_run_id="ag", parent_run_id="ag",
                    event_type=EventType.TOOL_START, tool_name="get_weather", mcp_server="weather")
    )

    topo = await backend.get_topology()
    nodes = {n.id: n for n in topo.nodes}
    edge_pairs = {(e.source, e.target) for e in topo.edges}

    # Uncalled registered tools still show, connected to their server.
    assert nodes["tool:rare_tool"].invocation_count == 0
    assert nodes["tool:rare_tool"].source == "weather"
    assert ("mcp:weather", "tool:rare_tool") in edge_pairs
    assert ("mcp:weather", "tool:get_forecast") in edge_pairs
    assert ("mcp:weather", "tool:get_weather") in edge_pairs
    # The agent is linked to the MCP server it used.
    assert nodes["agent:researcher"].type == "agent"
    assert ("agent:researcher", "mcp:weather") in edge_pairs


async def test_mcp_tools_registry_roundtrip(backend: SQLiteBackend) -> None:
    await backend.upsert_mcp_tools("weather", ["get_weather", "get_forecast"])
    await backend.upsert_mcp_tools("weather", ["get_weather"])  # idempotent
    await backend.upsert_mcp_tools("math", ["add"])
    reg = await backend.get_mcp_tools()
    assert reg == {"weather": ["get_forecast", "get_weather"], "math": ["add"]}


async def test_tool_inventory_includes_uncalled_registered(backend: SQLiteBackend) -> None:
    """get_tool_inventory must list registered-but-never-called tools (invocations 0)."""
    await backend.upsert_run(_make_run("r1"))
    await backend.upsert_mcp_tools("weather", ["get_weather", "air_quality"])
    await backend.upsert_event(
        _make_event("e1", "r1", event_type=EventType.TOOL_START,
                    tool_name="get_weather", mcp_server="weather")
    )
    inv = await backend.get_tool_inventory()
    weather = next(s for s in inv["sources"] if s["source"] == "weather")
    by_name = {t["name"]: t for t in weather["tools"]}
    assert weather["tool_count"] == 2
    assert by_name["get_weather"]["invocations"] == 1
    assert by_name["air_quality"]["invocations"] == 0  # registered, never invoked


async def test_get_tool_inventory_groups_by_source(backend: SQLiteBackend) -> None:
    await backend.upsert_run(_make_run("r1"))
    events = [
        _make_event("a1", "r1", event_type=EventType.TOOL_START, tool_name="get_weather", mcp_server="weather"),
        _make_event("a2", "r1", event_type=EventType.TOOL_START, tool_name="get_forecast", mcp_server="weather"),
        _make_event("b1", "r1", event_type=EventType.TOOL_START, tool_name="add", mcp_server="math"),
        _make_event("c1", "r1", event_type=EventType.TOOL_START, tool_name="local_calc"),  # local
        _make_event("c2", "r1", event_type=EventType.TOOL_ERROR, tool_name="local_calc"),
    ]
    for e in events:
        await backend.upsert_event(e)

    inv = await backend.get_tool_inventory()
    by_source = {s["source"]: s for s in inv["sources"]}
    assert by_source["weather"]["kind"] == "mcp"
    assert by_source["weather"]["tool_count"] == 2
    assert by_source["math"]["tool_count"] == 1
    assert by_source["local"]["kind"] == "local"
    assert by_source["local"]["tool_count"] == 1
    assert by_source["local"]["error_count"] == 1
    # local bucket sorts last.
    assert inv["sources"][-1]["source"] == "local"


async def test_schema_v4_to_v5_flags_reindex_and_indexes_summaries(tmp_data_dir: Path) -> None:
    """A v4 database gains cost columns and an FTS index of existing summaries."""
    db_path = tmp_data_dir / "v4.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, root_run_id TEXT NOT NULL, "
            "tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'running', "
            "started_at TEXT NOT NULL, completed_at TEXT, total_steps INTEGER NOT NULL "
            "DEFAULT 0, total_tokens_input INTEGER NOT NULL DEFAULT 0, "
            "total_tokens_output INTEGER NOT NULL DEFAULT 0, graph_definition TEXT, "
            "error_message TEXT, thread_id TEXT)"
        )
        await conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "parent_run_id TEXT, root_run_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, agent_name TEXT, tool_name TEXT, summary TEXT "
            "NOT NULL, blob_path TEXT, duration_ms INTEGER, token_input INTEGER, "
            "token_output INTEGER, error_message TEXT, mcp_server TEXT)"
        )
        await conn.execute(
            "INSERT INTO events (event_id, run_id, root_run_id, event_type, timestamp, "
            "summary) VALUES ('e1', 'r1', 'r1', 'llm_end', '2026-01-01T00:00:00+00:00', "
            "'book the seaside hotel')"
        )
        await conn.execute("PRAGMA user_version = 4")
        await conn.commit()

    be = SQLiteBackend(db_path)
    await be.init()
    try:
        assert await be.needs_reindex()
        hits = await be.search_events("seaside")
        assert hits
        assert hits[0].event_id == "e1"
        async with be._conn() as conn:
            cur = await conn.execute("PRAGMA user_version")
            ver = (await cur.fetchone())[0]
            await cur.close()
        assert ver == 6
    finally:
        await be.close()


async def test_schema_v5_to_v6_keeps_indexed_payload_and_upserts_by_rowid(tmp_path) -> None:
    db_path = tmp_path / "v5.db"
    be = SQLiteBackend(db_path)
    await be.init()
    await be.upsert_run(_make_run("r1"))
    async with be._conn() as conn:
        await conn.execute(
            "INSERT INTO events (event_id, run_id, root_run_id, event_type, timestamp, summary) "
            "VALUES ('e1', 'r1', 'r1', 'llm_end', '2026-01-01T00:00:00+00:00', 'plain')"
        )
        await conn.execute("DELETE FROM events_fts")
        await conn.execute(
            "INSERT INTO events_fts(rowid, name, summary, error, payload, event_id, root_run_id) "
            "VALUES (999, '', 'plain', '', 'lighthouse keeper', 'e1', 'r1')"
        )
        await conn.execute("PRAGMA user_version = 5")
        await conn.commit()
    await be.close()

    be = SQLiteBackend(db_path)
    await be.init()
    try:
        hits = await be.search_events("lighthouse")
        assert [hit.event_id for hit in hits] == ["e1"]
        async with be._conn() as conn:
            cur = await conn.execute(
                "SELECT f.rowid = e.rowid FROM events_fts f JOIN events e USING(event_id)"
            )
            assert (await cur.fetchone())[0] == 1
            await cur.close()
    finally:
        await be.close()
